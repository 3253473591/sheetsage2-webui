// @vitest-environment jsdom
/**
 * 工作台的**编排路径**：`runStage` / `runChain` 成功后要重新把两条通道都打开。
 *
 * 为什么这条要单独测：现有事件测试只覆盖"事件打补丁"，没覆盖"点重新运行之后通道谁来开"。
 * 真实踩到的坑是——工程跑完一次后 SSE 与兜底轮询都已停（各自在终态时收尾），
 * 重跑如果只补开 SSE，遇到 SSE 静默死掉（合盖 / 换网）界面就停在旧进度上，
 * 直到用户手动刷新。所以重跑必须和"新建工程"走同一个入口：SSE + 3 秒轮询。
 *
 * 挂一个真 `createApp` 是因为 `useStudio` 用了 `onUnmounted`（要卸载时清通道），
 * 光调函数拿不到生命周期。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp } from 'vue'

import { useStudio } from '../src/composables/useStudio'
import * as api from '../src/api'
import { job } from './helpers'

vi.mock('../src/api', () => ({
  StaleOptionsError: class StaleOptionsError extends Error {},
  pingHealth: vi.fn(),
  fetchEnv: vi.fn(),
  listJobs: vi.fn(),
  getJob: vi.fn(),
  fetchLanes: vi.fn(),
  fetchPeaks: vi.fn(),
  fetchTrash: vi.fn(),
  runStage: vi.fn(),
  runChain: vi.fn(),
  cancelJob: vi.fn(),
  setJobOptions: vi.fn(),
  cleanupJob: vi.fn(),
  deleteJob: vi.fn(),
  deleteStem: vi.fn(),
  uploadJob: vi.fn(),
  uploadStem: vi.fn(),
  restoreTrash: vi.fn(),
  purgeTrash: vi.fn(),
  restoreBackup: vi.fn(),
  backupUrl: vi.fn(() => ''),
  subscribeJob: vi.fn(),
}))

/** 一个已经跑完的工程：SSE 与轮询都已收尾。 */
const DONE = job({ id: 'a', name: 'A', state: 'done', rev: 3 })

let studio: ReturnType<typeof useStudio>
let app: ReturnType<typeof createApp> | null = null

beforeEach(async () => {
  vi.useFakeTimers()
  vi.mocked(api.listJobs).mockResolvedValue([DONE])
  vi.mocked(api.fetchEnv).mockResolvedValue({} as never)
  vi.mocked(api.getJob).mockResolvedValue(DONE)
  vi.mocked(api.fetchLanes).mockResolvedValue(null as never)
  vi.mocked(api.fetchPeaks).mockResolvedValue(null as never)
  vi.mocked(api.pingHealth).mockResolvedValue(true)
  vi.mocked(api.subscribeJob).mockReturnValue(() => undefined)

  app = createApp({ setup() { studio = useStudio(); return () => null } })
  app.mount(document.createElement('div'))
  await studio.init()          // 选中唯一的工程（已完成 → 两条通道都不该开着）
})

afterEach(() => {
  app?.unmount()
  app = null
  vi.useRealTimers()
  vi.clearAllMocks()
})

/** 定时器推进一轮后，兜底轮询有没有真的去拉快照。 */
function pollingFired(): boolean {
  vi.mocked(api.getJob).mockClear()
  vi.advanceTimersByTime(3000)
  return vi.mocked(api.getJob).mock.calls.some(([id]) => id === 'a')
}

describe('完成过的工程重跑', () => {
  it('runStage 之后 SSE 与兜底轮询一起重新打开', async () => {
    expect(pollingFired()).toBe(false)     // 前提：完成工程的轮询确实已停

    vi.mocked(api.runStage).mockResolvedValue(job({ id: 'a', state: 'queued', rev: 4 }))
    await studio.runStage('transcribe')

    expect(api.subscribeJob).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.subscribeJob).mock.calls[0][0]).toBe('a')
    expect(pollingFired()).toBe(true)
  })

  it('runChain 之后 SSE 与兜底轮询一起重新打开', async () => {
    expect(pollingFired()).toBe(false)

    vi.mocked(api.runChain).mockResolvedValue(job({ id: 'a', state: 'queued', rev: 4 }))
    await studio.runChain()

    expect(api.subscribeJob).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.subscribeJob).mock.calls[0][0]).toBe('a')
    expect(pollingFired()).toBe(true)
  })
})
