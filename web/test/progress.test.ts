/**
 * SSE 进度单调性的单测（G18）。
 *
 * 复现的是真实场景：重跑一个阶段后重连（带 `?since=`），后端先把**这一趟**的事件补发过来、
 * 又把上一趟残留的事件补发过来，于是进度条从 12% 跳回 80%。规则很简单——
 * **旧事件不许动进度，进度只许往上走，只有新一趟的 `phase` 事件才能归零**。
 */
import { describe, expect, it } from 'vitest'
import {
  isStaleEvent,
  monotonicProgress,
  patchFromEvent,
  startsNewRun,
} from '../src/progress'
import type { JobEvent, StageSnapshot } from '../src/types'

function stage(overrides: Partial<StageSnapshot> = {}): StageSnapshot {
  return {
    key: 'transcribe',
    label: '扒谱',
    state: 'running',
    progress: 0.8,
    step: '识别音乐',
    message: '',
    started_at: 1000,
    finished_at: null,
    elapsed_seconds: 10,
    error: null,
    detail: {},
    ...overrides,
  }
}

function event(overrides: Partial<JobEvent> = {}): JobEvent {
  return {
    seq: 1,
    type: 'progress',
    time: 1001,
    data: { phase: 'transcribe', progress: 0.5, message: '窗口 2/6' },
    ...overrides,
  }
}

describe('进度只增不减', () => {
  it('旧值更大时保持旧值', () => {
    expect(monotonicProgress(0.8, 0.3)).toBe(0.8)
    expect(monotonicProgress(0.2, 0.9)).toBe(0.9)
  })

  it('没有当前值时按事件走', () => {
    expect(monotonicProgress(undefined, 0.42)).toBe(0.42)
    expect(monotonicProgress(0.5, undefined)).toBe(0.5)
  })

  it('夹在 0~1 之间（后端偶尔给 1.0000001）', () => {
    expect(monotonicProgress(0, 1.0000001)).toBe(1)
    expect(monotonicProgress(0.5, -3)).toBe(0.5)
  })
})

describe('旧事件识别', () => {
  it('比这一趟开始还早的事件算旧事件', () => {
    expect(isStaleEvent(900, 1000)).toBe(true)
    expect(isStaleEvent(1001, 1000)).toBe(false)
  })

  it('容差内不算旧（同一毫秒的先后关系不可靠）', () => {
    expect(isStaleEvent(999, 1000)).toBe(false)
  })

  it('还没开始过就没有旧事件一说', () => {
    expect(isStaleEvent(500, null)).toBe(false)
  })
})

describe('新一轮的判定', () => {
  it('还没跑过：任何 phase 事件都是新一轮', () => {
    expect(startsNewRun(1005, null)).toBe(true)
  })

  it('比 started_at 更早的 phase 事件不是新一轮（重连补发的）', () => {
    expect(startsNewRun(900, 1000)).toBe(false)
    expect(startsNewRun(1000, 1000)).toBe(true)
  })
})

describe('patchFromEvent', () => {
  it('正常推进：进度与步骤都更新', () => {
    const patch = patchFromEvent(stage({ progress: 0.2, step: '识别音乐' }),
                                 event({ data: { phase: 'transcribe', step: '生成工程',
                                                 progress: 0.88, message: '窗口 2/6' } }))
    expect(patch?.progress).toBe(0.88)
  })

  it('上一趟的旧事件不动进度（这就是那个"从 12% 跳回 80%"的 bug）', () => {
    const run = stage({ progress: 0.12, started_at: 1000 })
    const stale = event({ time: 900, data: { phase: 'transcribe', progress: 0.8 } })
    expect(patchFromEvent(run, stale)).toBeNull()
  })

  it('进度倒退的事件不会把条拉回去', () => {
    const patch = patchFromEvent(stage({ progress: 0.8 }), event({ data: { progress: 0.3 } }))
    expect(patch?.progress).toBe(0.8)
  })

  it('只有 step/progress 两类事件参与', () => {
    expect(patchFromEvent(stage(), event({ type: 'log' }))).toBeNull()
    expect(patchFromEvent(stage(), event({ type: 'phase_done' }))).toBeNull()
  })

  it('step 事件带的消息不会覆盖已有 message', () => {
    const patch = patchFromEvent(stage({ message: '旧消息' }),
                                 event({ type: 'step', data: { step: '生成工程' } }))
    expect(patch?.step).toBe('生成工程')
    expect(patch?.message).toBeUndefined()
  })
})
