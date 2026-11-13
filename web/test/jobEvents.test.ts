/**
 * SSE 事件补丁（`useJobEvents`）的单测。
 *
 * 这些断言对应真实踩过的坑：
 * - 重连补发的**旧** `progress` 事件不能把进度打回去（与 `progress.ts` 单测互补，
 *   这里验的是"事件真的被写进了快照"）；
 * - `phase_done` 之后要重新拉一次产物（落盘后的真相以快照为准）；
 * - 未知事件类型不许把界面搞崩。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useJobEvents } from '../src/composables/useJobEvents'
import { useJobs } from '../src/composables/useJobs'
import type { JobEvent, PhaseKey } from '../src/types'
import { job as snapshot, stage } from './helpers'

vi.mock('../src/api', () => ({
  listJobs: vi.fn(),
  getJob: vi.fn(),
  fetchLanes: vi.fn(),
  fetchPeaks: vi.fn(),
  subscribeJob: vi.fn(),
}))

interface Harness {
  events: ReturnType<typeof useJobEvents>
  active: ReturnType<typeof useJobs>['active']
  logs: { text: string; tone: string }[]
  refresh: ReturnType<typeof vi.fn>
  send: (event: Partial<JobEvent>) => void
}

function harness(): Harness {
  const jobs = useJobs({ onError: () => undefined })
  jobs.upsert(snapshot({ id: 'a', state: 'running' }))
  jobs.activeId.value = 'a'
  const logs: { text: string; tone: string }[] = []
  const refresh = vi.fn().mockResolvedValue(undefined)
  const events = useJobEvents({
    active: jobs.active,
    refreshArtifacts: refresh,
    pushLog: (seq: number, phase: PhaseKey | null, text: string, tone) => {
      logs.push({ text: `${seq}:${phase ?? '-'}:${text}`, tone: tone ?? 'info' })
    },
    onPatched: () => undefined,
  })
  const send = (event: Partial<JobEvent>): void => {
    events.applyEvent({
      seq: 1, type: 'progress', time: 1001, data: {}, ...event,
    } as JobEvent)
  }
  return { events, active: jobs.active, logs, refresh, send }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('事件 -> 快照补丁', () => {
  it('progress 事件写进当前阶段的进度与消息', () => {
    const h = harness()
    h.send({ data: { phase: 'transcribe', progress: 0.42, message: '窗口 2/6' } })
    const run = h.active.value!.stages.transcribe
    expect(run.progress).toBeCloseTo(0.42)
    expect(run.message).toBe('窗口 2/6')
  })

  it('旧事件（早于这一趟开始）不修改进度', () => {
    const h = harness()
    h.active.value!.stages.transcribe = stage({
      key: 'transcribe', state: 'running', progress: 0.8, started_at: 2000,
    })
    h.send({ time: 1000, data: { phase: 'transcribe', progress: 0.1 } })
    expect(h.active.value!.stages.transcribe.progress).toBe(0.8)
  })

  it('进度只增不减', () => {
    const h = harness()
    h.active.value!.stages.transcribe = stage({ progress: 0.7, started_at: 0 })
    h.send({ time: 500, data: { phase: 'transcribe', progress: 0.3 } })
    expect(h.active.value!.stages.transcribe.progress).toBe(0.7)
  })

  it('step 事件换小步骤时记一行日志，没换就不记', () => {
    const h = harness()
    h.send({ type: 'step', data: { phase: 'transcribe', step: '识别音乐' } })
    h.send({ type: 'step', data: { phase: 'transcribe', step: '识别音乐' } })
    expect(h.logs.filter((line) => line.text.includes('识别音乐'))).toHaveLength(1)
  })

  it('phase_done 把阶段置成完成并重新拉产物', () => {
    const h = harness()
    h.send({ type: 'phase_done', data: { phase: 'transcribe', detail: { notes: 12 } } })
    const run = h.active.value!.stages.transcribe
    expect(run.state).toBe('done')
    expect(run.progress).toBe(1)
    expect(h.refresh).toHaveBeenCalled()
    expect(h.logs.at(-1)?.tone).toBe('ok')
  })

  it('phase_error 把失败原因显示出来', () => {
    const h = harness()
    h.send({ type: 'phase_error', data: { phase: 'lyrics', message: '没有拿到逐字时间戳' } })
    expect(h.active.value!.stages.lyrics.state).toBe('failed')
    expect(h.active.value!.stages.lyrics.error).toBe('没有拿到逐字时间戳')
    expect(h.logs.at(-1)?.tone).toBe('error')
  })

  it('未知事件类型不影响别的状态', () => {
    const h = harness()
    h.send({ type: 'wat' as never, data: { phase: 'transcribe' } })
    expect(h.active.value!.stages.transcribe.state).toBe('idle')
  })

  it('没有当前工程时事件被忽略（不炸）', () => {
    const jobs = useJobs({ onError: () => undefined })
    const events = useJobEvents({
      active: jobs.active,
      refreshArtifacts: vi.fn().mockResolvedValue(undefined),
      pushLog: () => undefined,
      onPatched: () => undefined,
    })
    expect(() => events.applyEvent({ seq: 1, type: 'progress', time: 1, data: {} } as JobEvent))
      .not.toThrow()
  })
})
