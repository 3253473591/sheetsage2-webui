/**
 * 回收站（G2）与整库备份/迁移（G22）。
 *
 * 两者都"跨工程"（不针对当前这一个），所以单独一块：回收站要能恢复并选中恢复回来的工程，
 * 备份包导入完要重新列一遍列表——但都不去碰作业领域状态本身（那是 `useJobs` 的）。
 */

import { ref } from 'vue'
import * as api from '@/api'
import type { BackupSummary, JobSnapshot, TrashEntry } from '@/types'

export interface TrashBackupDeps {
  upsert: (job: JobSnapshot) => void
  select: (id: string) => Promise<void>
  /** 重新拉工程列表；失败返回 `false`（这时候别假装导入成功）。 */
  reload: () => Promise<boolean>
  find: (id: string) => JobSnapshot | undefined
  pushLog: (seq: number, phase: null, text: string, tone?: 'info' | 'ok' | 'warn' | 'error',
            time?: number) => void
  onError: (message: string) => void
}

export function useTrashBackup(deps: TrashBackupDeps) {
  /** 回收站（G2）：软删除的工程，能恢复。 */
  const trash = ref<TrashEntry[]>([])
  /** 备份包下载地址（顶栏那个「备份」按钮）。 */
  const backupUrl = api.backupUrl(false)

  function fail(exc: unknown): void {
    deps.onError(exc instanceof Error ? exc.message : String(exc))
  }

  // ---------------------------------------------------------------- 回收站
  async function loadTrash(): Promise<void> {
    try {
      trash.value = await api.fetchTrash()
    } catch (exc) {
      fail(exc)
    }
  }

  /** 从回收站恢复一个工程：恢复完直接选中它（用户下一步通常就是看结果）。 */
  async function restoreTrash(entryId: string): Promise<void> {
    deps.onError('')
    try {
      const job = await api.restoreTrash(entryId)
      deps.upsert(job)
      await deps.select(job.id)
      await loadTrash()
    } catch (exc) {
      fail(exc)
    }
  }

  /** 彻底删除回收站里的一项（不可恢复）。 */
  async function purgeTrash(entryId: string): Promise<void> {
    deps.onError('')
    try {
      await api.purgeTrash(entryId)
      await loadTrash()
    } catch (exc) {
      fail(exc)
    }
  }

  // ---------------------------------------------------------------- 备份 / 迁移
  /**
   * 导入一个备份包：把里面的工程收进来（**不覆盖**已有的，重名会落成 `-2`）。
   *
   * 导入完重新列一遍工程——用户下一步通常就是打开其中一个看看。
   */
  async function restoreBackup(file: File): Promise<BackupSummary | null> {
    deps.onError('')
    try {
      const summary = await api.restoreBackup(file)
      // 列表拉不回来就说导入失败（旧行为）：否则界面还在用导入前的列表，
      // 却弹一句"已导入 N 个工程"，下一步点哪个都找不到。
      if (!(await deps.reload())) return null
      const names = summary.restored.map((item) => item.name).slice(0, 5)
      deps.pushLog(0, null,
                   `已导入 ${summary.count} 个工程`
                   + (names.length
                     ? `：${names.join('、')}${summary.restored.length > 5 ? ' …' : ''}`
                     : ''),
                   'ok')
      if (summary.skipped.length) {
        deps.pushLog(0, null,
                     `有 ${summary.skipped.length} 个没导进来：${summary.skipped[0]!.reason}`,
                     'warn')
      }
      if (summary.restored.length) {
        const first = deps.find(summary.restored[0]!.id)
        if (first) await deps.select(first.id)
      }
      return summary
    } catch (exc) {
      fail(exc)
      return null
    }
  }

  return { trash, loadTrash, restoreTrash, purgeTrash, backupUrl, restoreBackup }
}
