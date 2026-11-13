/**
 * 测试用的最小快照：只给会被断言到的字段，其余用 `unknown` 兜过去。
 *
 * 真实快照有几十个字段，每个测试都抄一遍只会让测试更难读；这里集中一处，
 * 字段与 `types.ts` 的 `JobSnapshot` 对齐（新增字段时只改这里）。
 */
import type { JobSnapshot, StageSnapshot } from '../src/types'

export function stage(overrides: Partial<StageSnapshot> = {}): StageSnapshot {
  return {
    key: 'transcribe',
    label: '扒谱',
    state: 'idle',
    progress: 0,
    step: '',
    message: '',
    started_at: null,
    finished_at: null,
    elapsed_seconds: 0,
    error: null,
    detail: {},
    ...overrides,
  } as StageSnapshot
}

export function job(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  const base = {
    id: 'job-1',
    name: '测试',
    audio: '测试.wav',
    rev: 1,
    artifacts_rev: 0,
    state: 'draft',
    stage: null,
    stages: { transcribe: stage(), lyrics: stage({ key: 'lyrics', label: '歌词识别' }) },
    stage_label: '',
    step: '',
    progress: 0,
    message: '',
    created_at: 0,
    elapsed_seconds: 0,
    error: null,
    warnings: [],
    files: [],
    stems: {},
    options: {},
    highlights: null,
    decisions: null,
    summary: null,
    events: 0,
  } as unknown as JobSnapshot
  return { ...base, ...overrides }
}
