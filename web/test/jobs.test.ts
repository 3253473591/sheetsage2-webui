/**
 * 作业列表（`useJobs`）的单测：列表顺序、当前工程、快照替换。
 *
 * 这一层是"作业状态的唯一所有者"：组件不许自己复制一份，所以"按 id 覆盖而不是追加"
 * 这件事值得盯着——它是进度条不乱跳的前提。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { JobSnapshot } from '../src/types'

vi.mock('../src/api', () => ({
  listJobs: vi.fn(),
  getJob: vi.fn(),
  fetchLanes: vi.fn(),
  fetchPeaks: vi.fn(),
}))

import * as api from '../src/api'
import { useJobs } from '../src/composables/useJobs'
import { job as snapshot } from './helpers'

const errors: string[] = []

function make() {
  return useJobs({ onError: (message) => errors.push(message) })
}

beforeEach(() => {
  errors.length = 0
  vi.clearAllMocks()
})

describe('工程列表', () => {
  it('同一个 id 再进来是原地替换，不会多出一行', () => {
    const jobs = make()
    jobs.upsert(snapshot({ id: 'a', progress: 0.1 }))
    jobs.upsert(snapshot({ id: 'b' }))
    jobs.upsert(snapshot({ id: 'a', progress: 0.6 }))
    expect(jobs.jobs.value.map((item) => item.id)).toEqual(['b', 'a'])
    expect(jobs.jobs.value.find((item) => item.id === 'a')!.progress).toBe(0.6)
  })

  it('新工程插在最前面（侧栏"新的在前"）', () => {
    const jobs = make()
    jobs.upsert(snapshot({ id: 'old' }))
    jobs.upsert(snapshot({ id: 'new' }))
    expect(jobs.jobs.value.map((item) => item.id)).toEqual(['new', 'old'])
  })

  it('当前工程跟着 activeId 走，找不到就是 null', () => {
    const jobs = make()
    jobs.upsert(snapshot({ id: 'a' }))
    jobs.activeId.value = 'a'
    expect(jobs.active.value?.id).toBe('a')
    jobs.activeId.value = 'nope'
    expect(jobs.active.value).toBeNull()
  })

  it('列不出来时把错误交给上层，不抛', async () => {
    const jobs = make()
    vi.mocked(api.listJobs).mockRejectedValueOnce(new Error('连不上后台服务'))
    await jobs.reload()
    expect(errors).toEqual(['连不上后台服务'])
    expect(jobs.jobs.value).toEqual([])
  })
})

describe('产物刷新', () => {
  it('refreshArtifacts 只拉当前工程：没有选中就什么都不做', async () => {
    const jobs = make()
    await jobs.refreshArtifacts()
    expect(api.getJob).not.toHaveBeenCalled()
    expect(api.fetchLanes).not.toHaveBeenCalled()
  })

  it('拉到的快照覆盖列表里的那一行，并带上卷帘数据', async () => {
    const jobs = make()
    jobs.upsert(snapshot({ id: 'a', progress: 0.2 }))
    jobs.activeId.value = 'a'
    vi.mocked(api.getJob).mockResolvedValue(snapshot({ id: 'a', progress: 0.9 }))
    vi.mocked(api.fetchLanes).mockResolvedValue({ bars: [] } as never)
    vi.mocked(api.fetchPeaks).mockRejectedValue(new Error('没有原音频'))
    await jobs.refreshArtifacts()
    expect(jobs.active.value?.progress).toBe(0.9)
    expect(jobs.lanes.value).toEqual({ bars: [] })
    expect(jobs.peaks.value).toBeNull()      // 只读工程没有原音频：波形是空的，不是错误
  })
})
