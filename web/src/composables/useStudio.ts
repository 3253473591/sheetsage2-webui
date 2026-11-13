/**
 * 工作台状态机的**聚合入口**：`App.vue` 只认这里返回的这一个对象。
 *
 * 职责已经拆开，这里只做接线与跨模块的编排（切工程、删工程、初始化、存活探测）：
 *
 * | 模块 | 管什么 |
 * |---|---|
 * | `useJobs` | 工程列表、当前工程、快照刷新、卷帘/波形数据 |
 * | `useJobEvents` | SSE 订阅/续传/重连、事件补丁、兜底轮询、取消后的收尾轮询 |
 * | `useUploads` | 拖文件建工程、干声、上传进度 |
 * | `useTrashBackup` | 回收站、整库备份导入 |
 * | `logs` | 日志抽屉里的行 |
 *
 * 两条数据流要分清（这条没变，是产品行为）：
 * - **进度**（阶段状态/小步骤/百分比）由 SSE 事件**原地打补丁**到快照上；
 * - **产物**（highlights / 文件清单 / 卷帘 / 歌词）在阶段结束时**重新拉一次快照**。
 */

import { computed, onUnmounted, ref } from 'vue'
import * as api from '@/api'
import type { EnvInfo, JobOptions, PhaseKey } from '@/types'
import { lyricsBlockedMessage } from '@/stageActions'
import { useJobs } from './useJobs'
import { useJobEvents } from './useJobEvents'
import { useUploads } from './useUploads'
import { useTrashBackup } from './useTrashBackup'
import { useLogs } from './logs'

export type { LogLine, LogTone } from './logs'

export function useStudio() {
  const env = ref<EnvInfo | null>(null)
  const error = ref<string | null>(null)
  /** 后台服务是否已经停了（G16：红色横幅 + 阶段卡置灰）。 */
  const serviceDown = ref(false)

  const onError = (message: string): void => {
    error.value = message || null
  }

  const { logs, pushLog, clearLogs } = useLogs()
  const jobsApi = useJobs({ onError })
  const {
    jobs, active, activeId, lanes, peaks, stemPeaks, lanesLoading,
    upsert, find, reload, refreshArtifacts, loadPeaks, clearMedia, clearStemPeaks,
  } = jobsApi

  const events = useJobEvents({
    active,
    refreshArtifacts,
    pushLog,
    onPatched: () => {
      /* 补丁只改快照，不需要额外动作 */
    },
  })
  const { streamedLyrics } = events

  const uploads = useUploads({
    active,
    upsert,
    select: (id) => select(id),
    pushLog,
    onError,
    reloadPeaks: (id) => loadPeaks(id),
    beginRun: (job) => events.beginRun(job, upsert),
    clearStemPeaks,
  })
  const { uploadProgress, create, attachStem, detachStem } = uploads

  const trashApi = useTrashBackup({
    upsert,
    select: (id) => select(id),
    reload,
    find,
    pushLog,
    onError,
  })
  const { trash, loadTrash, restoreTrash, purgeTrash, backupUrl, restoreBackup } = trashApi

  /** `/api/env` 回来了才算"服务端和参数表都在"（顶栏据此从"连接中"切到正常）。 */
  const pluginsReady = computed(() => Boolean(env.value))

  // ---------------------------------------------------------------- 存活探测（G16）
  let healthHandle: number | null = null
  let healthFails = 0
  let disposed = false

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

  // ---------------------------------------------------------------- 编排
  /** 切到某个工程：先把上一份的通道与数据全清掉，再按需补通道。 */
  async function select(id: string): Promise<void> {
    events.detach()
    activeId.value = id
    events.reset()
    clearLogs()
    clearMedia()
    const known = find(id)
    if (known) {
      // 立刻用已有数据渲染，再拉一次最新快照（可能已经跑完了）
      void refreshArtifacts()
      events.beginRun(known, upsert)
      return
    }
    await refreshArtifacts()
  }

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
      if (!events.subscribed.value) events.openStream(job.id)
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
      if (!events.subscribed.value) events.openStream(job.id)
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
    }
  }

  async function cancel(): Promise<void> {
    const job = active.value
    if (!job) return
    try {
      upsert(await api.cancelJob(job.id))
      events.watchCancellation(job.id, upsert, find)   // 等它真停下，别让界面停在 58%
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
      events.detach()
      activeId.value = null
      clearMedia()
      clearLogs()
    }
  }

  /** 回到"没有选中工程"的状态：主区显示拖拽提示，后台任务照跑。 */
  function reset(): void {
    events.reset()
    activeId.value = null
    clearMedia()
    clearLogs()
  }

  // 合盖 / 换网 / 代理重启之后 SSE 会静默死掉：一回前台就补拉快照、补开事件流。
  const onVisible = (): void => {
    if (!document.hidden) events.revive(upsert)
  }
  const onOnline = (): void => events.revive(upsert)
  if (typeof window !== 'undefined') {
    window.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', onOnline)
  }

  onUnmounted(() => {
    disposed = true
    events.dispose()
    stopHealthWatch()
    window.removeEventListener('visibilitychange', onVisible)
    window.removeEventListener('online', onOnline)
  })

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
