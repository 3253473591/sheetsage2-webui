/**
 * 工作台状态机：工程列表 → 当前工程 → SSE 事件 → 卷帘/波形数据。
 *
 * 只有这一个地方持有可变状态，组件全是纯展示 + 事件。
 *
 * 两条数据流要分清：
 * - **进度**（阶段状态/小步骤/百分比）由 SSE 事件**原地打补丁**到快照上，
 *   所以进度条是连续动的，不需要轮询；
 * - **产物**（highlights / 文件清单 / 卷帘 / 歌词）在阶段结束时**重新拉一次快照**，
 *   因为那才是落盘后的真相。
 */

import { computed, onUnmounted, ref } from 'vue'
import * as api from '@/api'
import type {
  BackupSummary,
  EnvInfo,
  JobEvent,
  JobOptions,
  JobSnapshot,
  Lanes,
  LyricLine,
  Peaks,
  PhaseKey,
  TrashEntry,
} from '@/types'
import { TERMINAL_STATES } from '@/types'
import { splitDroppedFiles } from '@/drop'
import { lyricsBlockedMessage } from '@/stageActions'
import { patchFromEvent, startsNewRun } from '@/progress'

export interface LogLine {
  seq: number
  phase: PhaseKey | null
  time: number
  text: string
  tone: 'info' | 'ok' | 'warn' | 'error'
}

const MAX_LOGS = 400
/** 进度流断开后的最大重连次数（带退避）。 */
const MAX_RETRIES = 10

export function useStudio() {
  const env = ref<EnvInfo | null>(null)
  const jobs = ref<JobSnapshot[]>([])
  const activeId = ref<string | null>(null)
  const lanes = ref<Lanes | null>(null)
  const peaks = ref<Peaks | null>(null)
  const stemPeaks = ref<Peaks | null>(null)
  const streamedLyrics = ref<LyricLine[]>([])
  const logs = ref<LogLine[]>([])
  const error = ref<string | null>(null)
  /** 回收站（G2）：软删除的工程，能恢复。 */
  const trash = ref<TrashEntry[]>([])
  const uploadProgress = ref<number | null>(null)
  const lanesLoading = ref(false)

  const active = computed<JobSnapshot | null>(
    () => jobs.value.find((job) => job.id === activeId.value) ?? null,
  )
  const pluginsReady = computed(() => Boolean(env.value))

  /** 后台服务是否已经停了（G16：红色横幅 + 阶段卡置灰）。 */
  const serviceDown = ref(false)

  let unsubscribe: (() => void) | null = null
  let retryHandle: number | null = null
  let lastSeq = 0
  let attempts = 0
  let disposed = false
  let healthHandle: number | null = null
  let healthFails = 0

  // ---------------------------------------------------------------- 通用
  function upsert(job: JobSnapshot): void {
    const index = jobs.value.findIndex((item) => item.id === job.id)
    if (index >= 0) jobs.value[index] = job
    else jobs.value.unshift(job)
  }

  function pushLog(seq: number, phase: PhaseKey | null, text: string,
                   tone: LogLine['tone'] = 'info', time = Date.now() / 1000): void {
    logs.value.push({ seq: seq || logs.value.length + 1, phase, time, text, tone })
    if (logs.value.length > MAX_LOGS) logs.value.splice(0, logs.value.length - MAX_LOGS)
  }

  function stage(job: JobSnapshot | null, phase: PhaseKey) {
    return job?.stages?.[phase]
  }

  // ---------------------------------------------------------------- 事件
  function applyEvent(event: JobEvent): void {
    const job = active.value
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
        pushLog(event.seq, phase, `开始${event.data.label ?? ''}`, 'info', time)
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
          pushLog(event.seq, phase, patch?.step ?? '', 'info', time)
        }
        break
      }
      case 'lyrics':
        if (event.data.lines) streamedLyrics.value = event.data.lines
        if (run && event.data.lines) {
          run.detail = { ...run.detail, lines: event.data.lines.length,
                         chars: event.data.chars?.length ?? run.detail.chars }
        }
        pushLog(event.seq, phase,
                `已识别 ${event.data.chars?.length ?? 0} 字 / ${event.data.lines?.length ?? 0} 行`
                + (event.data.slices_total ? `（${event.data.slices}/${event.data.slices_total} 段）` : ''),
                'info', time)
        break
      case 'log':
        pushLog(event.seq, phase, event.data.message ?? '', 'warn', time)
        break
      case 'phase_done':
        if (run) {
          run.state = 'done'
          run.progress = 1
          if (event.data.detail) run.detail = { ...run.detail, ...event.data.detail }
        }
        pushLog(event.seq, phase, `${event.data.phase === 'lyrics' ? '歌词识别' : '扒谱'}完成`,
                'ok', time)
        void refreshArtifacts()
        break
      case 'phase_error':
        if (run) {
          run.state = 'failed'
          run.error = event.data.message ?? '失败'
        }
        pushLog(event.seq, phase, event.data.message ?? '失败', 'error', time)
        break
      case 'phase_cancelled':
        if (run) run.state = 'cancelled'
        pushLog(event.seq, phase, '已取消', 'warn', time)
        break
      case 'done':
        pushLog(event.seq, null, '全部完成', 'ok', time)
        break
      case 'error':
        pushLog(event.seq, phase, event.data.message ?? '任务失败', 'error', time)
        break
      case 'cancelled':
        pushLog(event.seq, phase, '任务已取消', 'warn', time)
        break
      default:
        break
    }
  }

  // ---------------------------------------------------------------- 存活探测（G16）
  async function probeHealth(): Promise<void> {
    if (await api.pingHealth()) {
      healthFails = 0
      if (serviceDown.value) serviceDown.value = false
      return
    }
    healthFails += 1
    if (healthFails >= 3) serviceDown.value = true
  }

  /** 每 3 秒探一次存活：服务被关掉/崩溃后，30 秒内页面给出明确提示。 */
  function startHealthWatch(): void {
    if (healthHandle !== null) return
    const tick = (): void => {
      void probeHealth().finally(() => {
        if (!disposed) healthHandle = window.setTimeout(tick, 3000)
      })
    }
    healthHandle = window.setTimeout(tick, 0)
  }

  function stopHealthWatch(): void {
    if (healthHandle !== null) {
      window.clearTimeout(healthHandle)
      healthHandle = null
    }
  }

  // ---------------------------------------------------------------- 数据
  async function refreshArtifacts(): Promise<void> {
    const id = activeId.value
    if (!id) return
    try {
      upsert(await api.getJob(id))
    } catch {
      /* 快照拉不到不影响事件流 */
    }
    await loadLanes(id)
    void loadPeaks(id)
  }

  async function loadLanes(id: string): Promise<void> {
    lanesLoading.value = true
    try {
      lanes.value = await api.fetchLanes(id)
    } catch {
      lanes.value = null            // 还没扒谱：卷帘就是空的
    } finally {
      lanesLoading.value = false
    }
  }

  async function loadPeaks(id: string): Promise<void> {
    try {
      peaks.value = await api.fetchPeaks(id, 1200, 'mix')
    } catch {
      peaks.value = null
    }
    // 干声是可选音源：没有就别去请求（后端会 404）
    if (!jobs.value.find((job) => job.id === id)?.stems?.vocal) {
      stemPeaks.value = null
      return
    }
    try {
      stemPeaks.value = await api.fetchPeaks(id, 1200, 'stem')
    } catch {
      stemPeaks.value = null
    }
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
        void refreshArtifacts()
        if (reason === 'terminal') return
        attempts += 1
        if (attempts > MAX_RETRIES) {
          pushLog(0, null, '进度流断开，刷新页面可继续查看结果', 'warn')
          return
        }
        retryHandle = window.setTimeout(() => openStream(id), 500 * attempts)
      },
    })
  }

  async function select(id: string): Promise<void> {
    detach()
    activeId.value = id
    lastSeq = 0
    attempts = 0
    logs.value = []
    lanes.value = null
    peaks.value = null
    stemPeaks.value = null
    streamedLyrics.value = []
    const known = jobs.value.find((job) => job.id === id)
    if (known) {
      // 立刻用已有数据渲染，再拉一次最新快照（可能已经跑完了）
      void refreshArtifacts()
      if (!TERMINAL_STATES.has(known.state)) openStream(id)
      return
    }
    await refreshArtifacts()
  }

  // ---------------------------------------------------------------- 动作
  async function init(): Promise<void> {
    try {
      env.value = await api.fetchEnv()
    } catch {
      /* 顶栏信息拿不到不影响使用 */
    }
    try {
      jobs.value = await api.listJobs()
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
    const running = jobs.value.find((job) => job.state === 'running' || job.state === 'queued')
    const first = running ?? jobs.value[0]
    if (first) await select(first.id)
    startHealthWatch()
  }

  /**
   * 建工程。可以一次给多个文件（一次拖两个）：第一个当混音原曲，第二个当干声。
   *
   * **上传不触发任何阶段（门禁）**：后端把工程建出来就停住（状态 `draft`），
   * 界面上参数面板这时才露出来；用户点「开始扒谱」/「⏵ 自动提取」才真的开跑。
   */
  async function create(files: File[] | File): Promise<void> {
    const list = Array.isArray(files) ? files : [files]
    const { mix, stem, note } = splitDroppedFiles(list)
    if (!mix) return
    error.value = null
    uploadProgress.value = 0
    try {
      const job = await api.uploadJob(mix, undefined, (fraction) => {
        uploadProgress.value = fraction
      })
      upsert(job)
      await select(job.id)
      if (note) pushLog(0, null, note)
      // 一次拖两个：第二个当干声。先建工程再挂——没有工程就没地方挂。
      // 这时还没开始扒谱（门禁），挂干声更不冲突。
      if (stem) await attachStem(stem)
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    } finally {
      uploadProgress.value = null
    }
  }

  async function runStage(phase: PhaseKey): Promise<void> {
    const job = active.value
    if (!job) return
    // 界面这道门只是别让人白点：后端也有同一道（409"请先完成扒谱"）
    if (phase === 'lyrics') {
      const blocked = lyricsBlockedMessage(job)
      if (blocked) {
        error.value = blocked
        return
      }
    }
    error.value = null
    try {
      upsert(await api.runStage(job.id, phase))
      if (!unsubscribe) openStream(job.id)
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  /** 「自动提取」：扒谱 → 歌词 一条龙（走后端的 chain，不是两次 runStage）。 */
  async function runChain(): Promise<void> {
    const job = active.value
    if (!job) return
    error.value = null
    try {
      upsert(await api.runChain(job.id))
      if (!unsubscribe) openStream(job.id)
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  async function cancel(): Promise<void> {
    const job = active.value
    if (!job) return
    try {
      upsert(await api.cancelJob(job.id))
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  /** 上传干声（歌词音源）。**不做内容校验**：传混音原曲也允许。 */
  async function attachStem(file: File): Promise<void> {
    const job = active.value
    if (!job) return
    error.value = null
    uploadProgress.value = 0
    try {
      upsert(await api.uploadStem(job.id, file, 'vocal', (fraction) => {
        uploadProgress.value = fraction
      }))
      await loadPeaks(job.id)
      if (!unsubscribe && !TERMINAL_STATES.has(job.state)) openStream(job.id)
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    } finally {
      uploadProgress.value = null
    }
  }

  async function detachStem(): Promise<void> {
    const job = active.value
    if (!job) return
    try {
      upsert(await api.deleteStem(job.id, 'vocal'))
      stemPeaks.value = null
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  /**
   * 改一个参数（精度 / 设备 / 范围 / 量化 / 模型 / 音源 / 语言）。
   *
   * 后端会按参数所属的阶段**作废旧结果**（扒谱参数连歌词一起作废），所以这里只发补丁，
   * 快照回来之后界面自然就变成"待开始"。值不合法是 400、阶段正在跑是 409、4G 特供是 409，
   * 都当作错误显示出来——别假装改成功了。
   */
  async function setOptions(patch: JobOptions): Promise<void> {
    const job = active.value
    if (!job) return
    error.value = null
    try {
      // 乐观锁（G8）：基于当前 rev 改，服务端发现这页是旧的会回 409 + 新快照。
      upsert(await api.setJobOptions(job.id, patch, job.rev))
    } catch (exc) {
      if (exc instanceof api.StaleOptionsError) {
        // 用服务端给的最新快照原地刷新，别覆盖别人的改动
        if (exc.snapshot) upsert(exc.snapshot)
        error.value = exc.message
        return
      }
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  /** 清掉当前工程的可重算缓存（zip / SVP / 波形 / prev），释放磁盘（G11）。 */
  async function cleanup(): Promise<void> {
    const job = active.value
    if (!job) return
    error.value = null
    try {
      const result = await api.cleanupJob(job.id)
      pushLog(0, null, `已清理缓存，释放 ${Math.round(result.freed / 1024 / 1024)} MB`, 'ok')
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  /** 删除工程。默认软删除（进回收站，7 天内可恢复）；`permanent=true` 才是真删。 */
  async function remove(id: string, permanent = false): Promise<void> {
    try {
      await api.deleteJob(id, permanent)
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
      return
    }
    jobs.value = jobs.value.filter((job) => job.id !== id)
    if (activeId.value === id) {
      detach()
      activeId.value = null
      lanes.value = null
      peaks.value = null
      logs.value = []
    }
  }

  // ---------------------------------------------------------------- 回收站（G2）
  async function loadTrash(): Promise<void> {
    try {
      trash.value = await api.fetchTrash()
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  /** 从回收站恢复一个工程：恢复完直接选中它（用户下一步通常就是看结果）。 */
  async function restoreTrash(entryId: string): Promise<void> {
    error.value = null
    try {
      const job = await api.restoreTrash(entryId)
      upsert(job)
      await select(job.id)
      await loadTrash()
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  /** 彻底删除回收站里的一项（不可恢复）。 */
  async function purgeTrash(entryId: string): Promise<void> {
    error.value = null
    try {
      await api.purgeTrash(entryId)
      await loadTrash()
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  // ---------------------------------------------------------------- 备份 / 迁移（G22）
  /** 备份包下载地址（顶栏那个「备份」按钮）。 */
  const backupUrl = api.backupUrl(false)

  /**
   * 导入一个备份包：把里面的工程收进来（**不覆盖**已有的，重名会落成 `-2`）。
   *
   * 导入完重新列一遍工程——用户下一步通常就是打开其中一个看看。
   */
  async function restoreBackup(file: File): Promise<BackupSummary | null> {
    error.value = null
    try {
      const summary = await api.restoreBackup(file)
      jobs.value = await api.listJobs()
      const names = summary.restored.map((item) => item.name).slice(0, 5)
      pushLog(0, null,
              `已导入 ${summary.count} 个工程`
              + (names.length ? `：${names.join('、')}${summary.restored.length > 5 ? ' …' : ''}` : ''),
              'ok')
      if (summary.skipped.length) {
        pushLog(0, null,
                `有 ${summary.skipped.length} 个没导进来：${summary.skipped[0]!.reason}`,
                'warn')
      }
      if (summary.restored.length) {
        const first = jobs.value.find((item) => item.id === summary.restored[0]!.id)
        if (first) await select(first.id)
      }
      return summary
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
      return null
    }
  }

  onUnmounted(() => {
    disposed = true
    detach()
    stopHealthWatch()
  })

  /** 回到"没有选中工程"的状态：主区显示拖拽提示，后台任务照跑。 */
  function reset(): void {
    detach()
    activeId.value = null
    lanes.value = null
    peaks.value = null
    stemPeaks.value = null
    streamedLyrics.value = []
    logs.value = []
    lastSeq = 0
  }

  return {
    env,
    jobs,
    active,
    activeId,
    lanes,
    peaks,
    stemPeaks,
    streamedLyrics,
    logs,
    error,
    serviceDown,
    uploadProgress,
    lanesLoading,
    pluginsReady,
    init,
    select,
    create,
    runStage,
    runChain,
    cancel,
    attachStem,
    detachStem,
    setOptions,
    cleanup,
    remove,
    trash,
    loadTrash,
    restoreTrash,
    purgeTrash,
    backupUrl,
    restoreBackup,
    reset,
    dismissError: () => {
      error.value = null
    },
  }
}
