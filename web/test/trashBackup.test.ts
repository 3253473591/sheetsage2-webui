/**
 * 回收站与备份（`useTrashBackup`）的单测。
 *
 * 盯两件事：**恢复之后要立刻选中它**（用户下一步就是看结果）、**导入备份不覆盖已有工程**
 * （重名落成 `-2` 是后端保证的，这里只验前端把结果如实反映出来）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../src/api', () => ({
  fetchTrash: vi.fn(),
  restoreTrash: vi.fn(),
  purgeTrash: vi.fn(),
  restoreBackup: vi.fn(),
  listJobs: vi.fn(),
  backupUrl: vi.fn(() => '/api/backup'),
}))

import * as api from '../src/api'
import { useTrashBackup } from '../src/composables/useTrashBackup'
import { job as snapshot } from './helpers'

function harness() {
  const jobs = [snapshot({ id: 'a', name: '甲' })]
  const selected: string[] = []
  const logs: string[] = []
  const errors: string[] = []
  let reloaded = 0
  const trash = useTrashBackup({
    upsert: (job) => {
      if (!jobs.some((item) => item.id === job.id)) jobs.unshift(job)
    },
    select: async (id: string) => {
      selected.push(id)
    },
    reload: async () => {
      reloaded += 1
      // 真实现里 reload 会把列表整个换掉，这里照做——否则 find 不到新导进来的工程
      const list = await api.listJobs()
      jobs.splice(0, jobs.length, ...list)
    },
    find: (id) => jobs.find((item) => item.id === id),
    pushLog: (_seq, _phase, text) => logs.push(text),
    onError: (message) => errors.push(message),
  })
  return { trash, jobs, selected, logs, errors, reloads: () => reloaded }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('回收站', () => {
  it('恢复之后选中它，并刷新回收站列表', async () => {
    const h = harness()
    vi.mocked(api.restoreTrash).mockResolvedValue(snapshot({ id: 'b', name: '乙' }))
    vi.mocked(api.fetchTrash).mockResolvedValue([])
    await h.trash.restoreTrash('甲-x-20260101')
    expect(h.selected).toEqual(['b'])
    expect(api.fetchTrash).toHaveBeenCalled()
    expect(h.trash.trash.value).toEqual([])
  })

  it('彻底删除后再拉一次列表', async () => {
    const h = harness()
    vi.mocked(api.purgeTrash).mockResolvedValue(undefined)
    vi.mocked(api.fetchTrash).mockResolvedValue([{ id: 'x', name: '丙' } as never])
    await h.trash.purgeTrash('x')
    expect(h.trash.trash.value).toHaveLength(1)
  })

  it('失败时显示人话', async () => {
    const h = harness()
    vi.mocked(api.restoreTrash).mockRejectedValue(new Error('回收站里没有这一项'))
    await h.trash.restoreTrash('nope')
    expect(h.errors.at(-1)).toBe('回收站里没有这一项')
  })
})

describe('备份导入', () => {
  it('导入完重新列工程、选中第一个、并把跳过项说出来', async () => {
    const h = harness()
    const summary = {
      restored: [{ id: 'b', name: '乙', files: 3 }],
      skipped: [{ name: '丙', reason: '目录被占用' }],
      count: 1,
      backup: { created_at: 1, app_version: '0.0.0', projects: 1 },
    }
    vi.mocked(api.restoreBackup).mockResolvedValue(summary as never)
    vi.mocked(api.listJobs).mockResolvedValue([snapshot({ id: 'b', name: '乙' })])
    const result = await h.trash.restoreBackup(new File([], 'b.zip'))
    expect(result?.count).toBe(1)
    expect(h.reloads()).toBe(1)
    expect(h.selected).toEqual(['b'])
    expect(h.logs.some((line) => line.includes('已导入 1 个工程'))).toBe(true)
    expect(h.logs.some((line) => line.includes('目录被占用'))).toBe(true)
  })

  it('备份包坏了：显示错误并返回 null', async () => {
    const h = harness()
    vi.mocked(api.restoreBackup).mockRejectedValue(new Error('不是备份包'))
    const result = await h.trash.restoreBackup(new File([], 'x.zip'))
    expect(result).toBeNull()
    expect(h.errors.at(-1)).toBe('不是备份包')
  })

  it('备份地址带 include_cache=false（缓存能重算，不该进包）', () => {
    const h = harness()
    expect(h.trash.backupUrl).toBe('/api/backup')
    expect(api.backupUrl).toHaveBeenCalledWith(false)
  })
})
