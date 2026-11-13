/**
 * SSE 事件流：订阅、续传、断线重连、事件去重，以及"进度原地打补丁"。
 *
 * 这些规则是踩过坑的，改动前先读一遍：
 * - 每个事件带 `seq`，断线重连时带 `since` 续传（后端从下一条补发）；
 * - **旧事件不算数**、进度**只增不减**（规则在 `@/progress`）；
 * - 新一轮运行（`startsNewRun`）才允许把进度归零，否则重连补发的旧 `phase` 会把 80% 打回 0；
 * - SSE 会**静默死掉**（合盖 / 换网 / 代理重启），所以有阶段在跑时再开一条 3 秒轮询兜底；
 * - 取消是协作式的：点完停止要自己拉快照，别让界面停在 58%。
 */

import { ref, type ComputedRef } from 'vue'
import * as api from '@/api'
import type { JobEvent, JobSnapshot, LyricLine, PhaseKey } from '@/types'
import { TERMINAL_STATES } from '@/types'
import type { LogTone } from './logs'
import { patchFromEvent, startsNewRun } from '@/progress'

/** 进度流断开后的最大重连次数（带退避）。 */
const MAX_RETRIES = 10

export interface JobEventDeps {
  /** 当前工程（事件只往它上面打补丁）。 */
  active: ComputedRef<JobSnapshot | null>
  /** 阶段结束/断线后重新拉快照 + 产物。 */
  refreshArtifacts: () => Promise<void>
  pushLog: (seq: number, phase: PhaseKey | null, text: string,
            tone?: LogTone, time?: number) => void
  /** 快照被事件补丁改过之后叫一声（上层据此决定要不要开/关兜底轮询）。 */
  onPatched: () => void
}

export function useJobEvents(deps: JobEventDeps) {
  /** 边识别边出的歌词（模型逐段返回，落盘前先给界面看）。 */
  const streamedLyrics = ref<LyricLine[]>([])

  let unsubscribe: (() => void) | null = null
  let retryHandle: number | null = null
  let lastSeq = 0
  let attempts = 0
  let disposed = false

  // ---------------------------------------------------------------- 事件 -> 快照
  function stage(job: JobSnapshot | null, phase: PhaseKey) {
    return job?.stages?.[phase]
  }

  function applyEvent(event: JobEvent): void {
    const job = deps.active.value
    if (!job) return
    const phase = (event.data.phase ?? null) as PhaseKey | null
    const run = phase ? stage(job, phase) : undefined
    const time = event.time || Date.now() / 1000
    switch (event.type) {
      case 'phase':
        if (run) {
          // 新一轮才允许把进度归零（G18）：重连补发的旧 `phase` 事件不能把 80% 打回 0。
          if (startsNewRun(time, run.started_at)) {
            run.progress = 0
            run.started_at = time
          }
          run.state = (event.data.state as never) ?? 'running'
          run.error = null
          run.eta = event.data.eta ?? run.eta ?? null
        }
        deps.pushLog(event.seq, phase, `开始${event.data.label ?? ''}`, 'info', time)
        break
      case 'step':
      case 'progress': {
        // 只增不减 + 旧事件不算数（G18），规则都在 `@/progress`
        const patch = run ? patchFromEvent(run, event) : null
        const stepChanged = Boolean(run && patch?.step !== undefined && patch.step !== run.step)
        if (run && patch) {
          if (patch.step !== undefined) run.step = patch.step
          if (patch.progress !== undefined) run.progress = patch.progress
          if (patch.message !== undefined) run.message = patch.message
        }
        // 小步骤只在**真的换了**的时候记一行日志，否则逐窗口回调会把日志抽屉刷满
        if (event.type === 'step' && stepChanged) {
          deps.pushLog(event.seq, phase, patch?.step ?? '', 'info', time)
        }
        break
      }
      case 'lyrics':
        if (event.data.lines) streamedLyrics.value = event.data.lines
        if (run && event.data.lines) {
          run.detail = { ...run.detail, lines: event.data.lines.length,
                         chars: event.data.chars?.length ?? run.detail.chars }
        }
        deps.pushLog(event.seq, phase,
                     `已识别 ${event.data.chars?.length ?? 0} 字 / ${event.data.lines?.length ?? 0} 行`
                     + (event.data.slices_total ? `（${event.data.slices}/${event.data.slices_total} 段）` : ''),
                     'info', time)
        break
      case 'log':
        deps.pushLog(event.seq, phase, event.data.message ?? '', 'warn', time)
        break
      case 'phase_done':
        if (run) {
          run.state = 'done'
          run.progress = 1
          if (event.data.detail) run.detail = { ...run.detail, ...event.data.detail }
        }
        deps.pushLog(event.seq, phase,
                     `${event.data.phase === 'lyrics' ? '歌词识别' : '扒谱'}完成`, 'ok', time)
        void deps.refreshArtifacts()
        break
      case 'phase_error':
        if (run) {
          run.state = 'failed'
          run.error = event.data.message ?? '失败'
        }
        deps.pushLog(event.seq, phase, event.data.message ?? '失败', 'error', time)
        break
      case 'phase_cancelled':
        if (run) run.state = 'cancelled'
        deps.pushLog(event.seq, phase, '已取消', 'warn', time)
        break
      case 'done':
        deps.pushLog(event.seq, null, '全部完成', 'ok', time)
        break
      case 'error':
        deps.pushLog(event.seq, phase, event.data.message ?? '任务失败', 'error', time)
        break
      case 'cancelled':
        deps.pushLog(event.seq, phase, '任务已取消', 'warn', time)
        break
      default:
        break
    }
    deps.onPatched()
  }

  // ---------------------------------------------------------------- 订阅
  function detach(): void {
    unsubscribe?.()
    unsubscribe = null
    if (retryHandle !== null) {
      window.clearTimeout(retryHandle)
      retryHandle = null
    }
  }

  function openStream(id: string): void {
    if (disposed) return
    unsubscribe = api.subscribeJob(id, lastSeq, {
      onEvent: (event) => {
        attempts = 0
        lastSeq = Math.max(lastSeq, event.seq)
        applyEvent(event)
      },
      onClose: (reason) => {
        unsubscribe = null
        if (disposed) return
        void deps.refreshArtifacts()
        if (reason === 'terminal') return
        attempts += 1
        if (attempts > MAX_RETRIES) {
          deps.pushLog(0, null, '进度流断开，刷新页面可继续查看结果', 'warn')
          return
        }
        retryHandle = window.setTimeout(() => openStream(id), 500 * attempts)
      },
    })
  }

  // ------------------------------------------------------- 取消之后的收尾轮询
  // 取消是协作式的：点的瞬间后端只是置了个标记，得等当前这一段跑完才真停，
  // `phase_cancelled` 事件也要等那一刻才发。实测用户会以为"卡在 58% 了"。
  // 所以点完停止就自己拉快照几秒钟一次，直到阶段落终态——不依赖 SSE 时序。
  let cancelWatch: number | null = null
  let cancelWatchDeadline = 0

  function stopCancelWatch(): void {
    if (cancelWatch !== null) {
      window.clearInterval(cancelWatch)
      cancelWatch = null
    }
  }

  function watchCancellation(id: string, upsert: (job: JobSnapshot) => void,
                             find: (id: string) => JobSnapshot | undefined): void {
    stopCancelWatch()
    cancelWatchDeadline = Date.now() + 180_000
    cancelWatch = window.setInterval(async () => {
      const current = find(id)
      if (!current || Date.now() > cancelWatchDeadline
          || ['done', 'failed', 'cancelled'].includes(current.state)) {
        stopCancelWatch()
        return
      }
      try {
        upsert(await api.getJob(id))
      } catch {
        /* 拉不到就下一轮再试 */
      }
    }, 1500)
  }

  // ------------------------------------------------------- 运行中的兜底轮询
  let pollHandle: number | null = null

  function stopPolling(): void {
    if (pollHandle !== null) {
      window.clearInterval(pollHandle)
      pollHandle = null
    }
  }

  function startPolling(upsert: (job: JobSnapshot) => void): void {
    if (pollHandle !== null) return
    pollHandle = window.setInterval(async () => {
      const job = deps.active.value
      if (!job) {
        stopPolling()
        return
      }
      if (TERMINAL_STATES.has(job.state) && !job.cancel_requested) {
        stopPolling()
        return
      }
      try {
        upsert(await api.getJob(job.id))
      } catch {
        /* 拉不到就下一轮再试 */
      }
    }, 3000)
  }

  /** 有阶段在跑就把两条通道都补上（SSE + 轮询兜底）。 */
  function beginRun(job: JobSnapshot, upsert: (job: JobSnapshot) => void): void {
    if (!TERMINAL_STATES.has(job.state)) {
      if (!unsubscribe) openStream(job.id)
      startPolling(upsert)
    }
  }

  /** 页面回到前台 / 网络恢复：SSE 可能已经死了，拉一次快照并补开事件流。 */
  function revive(upsert: (job: JobSnapshot) => void): void {
    const job = deps.active.value
    if (!job) return
    void deps.refreshArtifacts()
    beginRun(job, upsert)
  }

  /** 切工程 / 删工程：通道全断，续传水位归零。 */
  function reset(): void {
    detach()
    stopPolling()
    stopCancelWatch()
    streamedLyrics.value = []
    lastSeq = 0
    attempts = 0
  }

  function dispose(): void {
    disposed = true
    reset()
  }

  return {
    streamedLyrics,
    applyEvent,
    openStream,
    detach,
    watchCancellation,
    startPolling,
    stopPolling,
    beginRun,
    revive,
    reset,
    dispose,
  }
}
