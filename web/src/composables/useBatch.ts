/**
 * 批量页的状态与动作（`useStudio` 只做接线）。
 *
 * 两条规矩（和 `docs/模块结构.md` §3 对齐）：
 * 1. **不复制工程快照**：这里只持有批次的轻量行（`BatchRow`），点某一首还是走
 *    `useJobs.select(id)` 拿全量快照与 SSE——同一份状态只有一个所有者。
 * 2. **只有一条轮询**：批次视图不订阅 40 条 SSE；有行在跑时 1.5s 拉一次批次明细，
 *    点进某个工程才开那条工程的 SSE。
 *
 * 批次没有"后端实体"：列表从工程派生，所以这里也不需要建/删批次，只需要
 * 扫目录 → 建工程 → 看明细 → 暂停/重试。
 */

import { computed, ref, type Ref } from 'vue'
import * as api from '@/api'
import { groupForUpload } from '@/drop'
import type { BatchDetail, BatchInfo, BatchScan, JobOptions, JobRow, PhaseKey } from '@/types'

/** 有行在跑时的轮询间隔。批量视图只是"看进度"，1.5s 足够。 */
export const POLL_MS = 1500

export interface BatchDeps {
  onError: (message: string) => void
  /** 建完/停止/重试之后让工程列表跟上（`useJobs.reload`）。 */
  onJobsChanged: () => void | Promise<void>
  /** 每拍轮询拿到的轻量行 → 让侧栏/列表原地更新（`useJobs.applyRows`）。 */
  onRows: (rows: JobRow[]) => void
  /** 打开某个工程的详情页（`useStudio.select`）。 */
  onOpenJob: (jobId: string) => void
}

function message(exc: unknown): string {
  return exc instanceof Error ? exc.message : String(exc)
}

export function useBatch(deps: BatchDeps) {
  const batches = ref<BatchInfo[]>([])
  const activeId = ref<string | null>(null)
  const detail = ref<BatchDetail | null>(null)
  const paused = ref(false)
  const busy = ref(false)
  const scanResult: Ref<BatchScan | null> = ref(null)
  const scanning = ref(false)
  /** 拖文件夹上传时的进度（`null` = 没在传）。 */
  const uploadProgress = ref<{ done: number; total: number; name: string } | null>(null)

  let poll: number | null = null

  /** 当前批次（没选中就是 null）——`App.vue` 用它决定主区显示哪个视图。 */
  const active = computed(() => (activeId.value ? detail.value : null))

  const hasWork = computed(() => {
    const rows = detail.value?.rows ?? []
    return rows.some((row) => ['running', 'queued', 'draft'].includes(row.state))
  })

  // ---------------------------------------------------------------- 列表
  async function reload(): Promise<void> {
    try {
      batches.value = await api.listBatches()
    } catch (exc) {
      deps.onError(message(exc))
    }
  }

  async function refresh(): Promise<void> {
    if (!activeId.value) return
    try {
      const next = await api.getBatch(activeId.value)
      detail.value = next
      // 侧栏与列表用的是同一份工程状态：把轻量行合并进去（不复制第二份）
      deps.onRows(next.rows)
      // 组头的 "12/40" 也要跟着走（`reload` 只在建完/停止时拉，不必每拍都请求）
      batches.value = batches.value.map((batch) => (batch.id === next.batch.id
        ? {
            ...batch,
            done: next.stats.done,
            failed: next.stats.failed,
            running: next.stats.running,
            pending: next.stats.pending,
          }
        : batch))
      if (!hasWork.value) stopPolling()
    } catch {
      /* 轮询失败下一拍再试，别把红条刷屏 */
    }
  }

  function startPolling(): void {
    if (poll !== null) return
    poll = window.setInterval(() => void refresh(), POLL_MS)
  }

  function stopPolling(): void {
    if (poll !== null) {
      window.clearInterval(poll)
      poll = null
    }
  }

  // ---------------------------------------------------------------- 选择
  async function open(batchId: string): Promise<void> {
    activeId.value = batchId
    detail.value = null
    await refresh()
    if (hasWork.value) startPolling()
  }

  function close(): void {
    stopPolling()
    activeId.value = null
    detail.value = null
  }

  // ---------------------------------------------------------------- 扫描 / 建批次
  async function scan(dir: string): Promise<BatchScan | null> {
    scanning.value = true
    try {
      scanResult.value = await api.scanBatch(dir)
      return scanResult.value
    } catch (exc) {
      deps.onError(message(exc))
      scanResult.value = null
      return null
    } finally {
      scanning.value = false
    }
  }

  async function start(payload: {
    dir: string
    options?: JobOptions
    plan?: PhaseKey[]
  }): Promise<string | null> {
    busy.value = true
    try {
      const body = await api.createBatch(payload)
      await Promise.all([reload(), Promise.resolve(deps.onJobsChanged())])
      await open(body.batch.id)
      if (body.problems.length) {
        deps.onError(`有 ${body.problems.length} 首没建起来：${body.problems[0]}`)
      }
      return body.batch.id
    } catch (exc) {
      deps.onError(message(exc))
      return null
    } finally {
      busy.value = false
    }
  }

  /**
   * **拖文件夹**：整批文件走上传路线（浏览器拿不到绝对路径，只能一个个传）。
   *
   * 配对规则和扫描一致（`groupForUpload`：按 stem 同名、`.lrc` 优先）；第一个文件由后端
   * 铸一个批次 id，后面每个都带上它——上传顺序就是批次里的顺序。
   */
  async function uploadFiles(files: File[]): Promise<string | null> {
    const pairs = groupForUpload(files)
    if (!pairs.length) {
      deps.onError('这个文件夹里没有音频（wav / mp3 / flac / m4a…）')
      return null
    }
    busy.value = true
    uploadProgress.value = { done: 0, total: pairs.length, name: pairs[0]!.name }
    let batchId: string | null = null
    try {
      for (const [index, pair] of pairs.entries()) {
        uploadProgress.value = { done: index, total: pairs.length, name: pair.name }
        const lyrics = pair.lyrics ? await pair.lyrics.text() : undefined
        const body = await api.uploadBatchFile(pair.audio, {
          lyrics,
          batchId: batchId ?? undefined,
          title: `拖入的 ${pairs.length} 首`,
          plan: ['transcribe', 'lyrics'],
        })
        batchId = body.batch.id
      }
      uploadProgress.value = { done: pairs.length, total: pairs.length, name: '正在建批次…' }
      await deps.onJobsChanged()
      await reload()
      if (batchId) await open(batchId)
      return batchId
    } catch (exc) {
      deps.onError(message(exc))
      return null
    } finally {
      uploadProgress.value = null
      busy.value = false
    }
  }

  // ---------------------------------------------------------------- 运行控制
  async function pause(): Promise<void> {
    try {
      paused.value = await api.pauseQueue()
    } catch (exc) {
      deps.onError(message(exc))
    }
  }

  async function resume(): Promise<void> {
    try {
      paused.value = await api.resumeQueue()
      if (activeId.value) {
        await refresh()
        if (hasWork.value) startPolling()
      }
    } catch (exc) {
      deps.onError(message(exc))
    }
  }

  async function togglePause(): Promise<void> {
    await (paused.value ? resume() : pause())
  }

  /** 停止：把还没结束的成员逐个取消（正在跑的那一首会在下一个检查点停）。 */
  async function stop(): Promise<void> {
    const rows = detail.value?.rows ?? []
    const open_ = rows.filter((row) => ['running', 'queued', 'draft'].includes(row.state))
    if (!open_.length) return
    busy.value = true
    try {
      await Promise.allSettled(open_.map((row) => api.cancelJob(row.id)))
      await Promise.all([refresh(), Promise.resolve(deps.onJobsChanged())])
    } finally {
      busy.value = false
    }
  }

  /** 只重试失败的那几首（好的不动）。 */
  async function retryFailed(): Promise<void> {
    const rows = detail.value?.rows ?? []
    const failed = rows.filter((row) => row.state === 'failed')
    if (!failed.length) return
    busy.value = true
    try {
      await Promise.allSettled(failed.map((row) => api.runChain(row.id)))
      await Promise.all([refresh(), Promise.resolve(deps.onJobsChanged())])
      if (hasWork.value) startPolling()
    } finally {
      busy.value = false
    }
  }

  /** 打包下载：只打包跑完的那些（服务端还会再跳过不合格的，清单里写明）。 */
  function exportUrl(): string {
    const ids = (detail.value?.rows ?? [])
      .filter((row) => row.state === 'done')
      .map((row) => row.id)
    return api.batchExportUrl(ids)
  }

  function openJob(jobId: string): void {
    close()
    deps.onOpenJob(jobId)
  }

  function dispose(): void {
    stopPolling()
  }

  return {
    batches,
    activeId,
    active,
    detail,
    paused,
    busy,
    scanResult,
    scanning,
    uploadProgress,
    hasWork,
    reload,
    refresh,
    open,
    close,
    scan,
    start,
    uploadFiles,
    pause,
    resume,
    togglePause,
    stop,
    retryFailed,
    exportUrl,
    openJob,
    dispose,
  }
}
