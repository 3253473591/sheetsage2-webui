/**
 * 上传：拖进来的文件怎么拆成"混音原曲 + 干声"、进度条、以及换音源。
 *
 * **上传不触发任何阶段（门禁）**：后端把工程建出来就停住（状态 `draft`），
 * 参数面板这时才露出来；用户点「开始扒谱」/「⏵ 自动提取」才真的开跑。
 */

import { ref, type ComputedRef } from 'vue'
import * as api from '@/api'
import type { JobSnapshot } from '@/types'
import { splitDroppedFiles } from '@/drop'

export interface UploadDeps {
  active: ComputedRef<JobSnapshot | null>
  upsert: (job: JobSnapshot) => void
  /** 建完/恢复完要选中它（用户下一步通常就是看结果）。 */
  select: (id: string) => Promise<void>
  pushLog: (seq: number, phase: null, text: string, tone?: 'info' | 'ok' | 'warn' | 'error',
            time?: number) => void
  onError: (message: string) => void
  /** 传完干声要重画波形。 */
  reloadPeaks: (id: string) => Promise<void> | void
  /** 有阶段在跑时补开两条通道。 */
  beginRun: (job: JobSnapshot) => void
  /** 干声换掉之后波形没了。 */
  clearStemPeaks: () => void
}

export function useUploads(deps: UploadDeps) {
  const uploadProgress = ref<number | null>(null)

  function fail(exc: unknown): void {
    deps.onError(exc instanceof Error ? exc.message : String(exc))
  }

  /**
   * 建工程。可以一次给多个文件（一次拖两个）：第一个当混音原曲，第二个当干声。
   */
  async function create(files: File[] | File): Promise<void> {
    const list = Array.isArray(files) ? files : [files]
    const { mix, stem, note } = splitDroppedFiles(list)
    if (!mix) return
    deps.onError('')
    uploadProgress.value = 0
    try {
      const job = await api.uploadJob(mix, undefined, (fraction) => {
        uploadProgress.value = fraction
      })
      deps.upsert(job)
      await deps.select(job.id)
      if (note) deps.pushLog(0, null, note)
      // 一次拖两个：第二个当干声。先建工程再挂——没有工程就没地方挂。
      // 这时还没开始扒谱（门禁），挂干声更不冲突。
      if (stem) await attachStem(stem)
    } catch (exc) {
      fail(exc)
    } finally {
      uploadProgress.value = null
    }
  }

  /** 上传干声（歌词音源）。**不做内容校验**：传混音原曲也允许。 */
  async function attachStem(file: File): Promise<void> {
    const job = deps.active.value
    if (!job) return
    deps.onError('')
    uploadProgress.value = 0
    try {
      deps.upsert(await api.uploadStem(job.id, file, 'vocal', (fraction) => {
        uploadProgress.value = fraction
      }))
      await deps.reloadPeaks(job.id)
      deps.beginRun(job)
    } catch (exc) {
      fail(exc)
    } finally {
      uploadProgress.value = null
    }
  }

  async function detachStem(): Promise<void> {
    const job = deps.active.value
    if (!job) return
    try {
      deps.upsert(await api.deleteStem(job.id, 'vocal'))
      deps.clearStemPeaks()
    } catch (exc) {
      fail(exc)
    }
  }

  return { uploadProgress, create, attachStem, detachStem }
}
