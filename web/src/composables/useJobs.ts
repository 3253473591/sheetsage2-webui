/**
 * 工程列表与"当前工程"：**作业快照的唯一所有者**。
 *
 * 组件不自己复制全局作业状态（那会立刻出现两份不同步的进度）；它们读这里的 `jobs` /
 * `active`，改动一律通过动作函数。产物数据（卷帘 / 波形）也在这里加载：它们都是
 * "当前这份快照"的派生物，跟着快照一起刷新才不会错位。
 */

import { computed, ref } from 'vue'
import * as api from '@/api'
import type { JobSnapshot, Lanes, Peaks } from '@/types'

export interface JobListDeps {
  /** 出错时交给上层显示：这里不抛，界面别炸。 */
  onError: (message: string) => void
}

export function useJobs(deps: JobListDeps) {
  const jobs = ref<JobSnapshot[]>([])
  const activeId = ref<string | null>(null)
  const lanes = ref<Lanes | null>(null)
  const peaks = ref<Peaks | null>(null)
  const stemPeaks = ref<Peaks | null>(null)
  const lanesLoading = ref(false)

  const active = computed<JobSnapshot | null>(
    () => jobs.value.find((job) => job.id === activeId.value) ?? null,
  )

  // ---------------------------------------------------------------- 列表
  /** 按 id 覆盖或插入（新的在最前）。快照与事件补丁都走它。 */
  function upsert(job: JobSnapshot): void {
    const index = jobs.value.findIndex((item) => item.id === job.id)
    if (index >= 0) jobs.value[index] = job
    else jobs.value.unshift(job)
  }

  function setJobs(list: JobSnapshot[]): void {
    jobs.value = list
  }

  function find(id: string): JobSnapshot | undefined {
    return jobs.value.find((job) => job.id === id)
  }

  /** 重新列一遍工程（导入备份包之后用）。拉不到就把错误交给上层，并返回 `false`。 */
  async function reload(): Promise<boolean> {
    try {
      jobs.value = await api.listJobs()
      return true
    } catch (exc) {
      deps.onError(exc instanceof Error ? exc.message : String(exc))
      return false
    }
  }

  // ---------------------------------------------------------------- 数据
  /** 阶段结束（或断线重连）后重新拉一次：落盘后的真相以快照为准。 */
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
    if (!find(id)?.stems?.vocal) {
      stemPeaks.value = null
      return
    }
    try {
      stemPeaks.value = await api.fetchPeaks(id, 1200, 'stem')
    } catch {
      stemPeaks.value = null
    }
  }

  /** 清掉"当前工程"的媒体数据（切工程/删工程时别让上一份糊在新工程上）。 */
  function clearMedia(): void {
    lanes.value = null
    peaks.value = null
    stemPeaks.value = null
  }

  function clearStemPeaks(): void {
    stemPeaks.value = null
  }

  return {
    jobs,
    activeId,
    active,
    lanes,
    peaks,
    stemPeaks,
    lanesLoading,
    upsert,
    setJobs,
    find,
    reload,
    refreshArtifacts,
    loadLanes,
    loadPeaks,
    clearMedia,
    clearStemPeaks,
  }
}
