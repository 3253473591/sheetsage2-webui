/**
 * 阶段按钮的门禁单测（`npm test`）。
 *
 * 工作流是 扒谱 → 歌词，三条规则：
 * 1. 扒谱进行中 -> 「开始识别歌词」禁用；
 * 2. 没扒谱就点 -> 提示"请先完成扒谱"；
 * 3. 工程"有扒谱但未填词" -> 允许点（这就是要做歌词识别的正常状态）。
 */
import { describe, expect, it } from 'vitest'
import {
  NOT_READY_HINT,
  lyricsActions,
  lyricsBlockedMessage,
  transcribeActions,
} from '../src/stageActions'
import type { JobSnapshot, StageState } from '../src/types'

function job(transcribe: StageState, lyrics: StageState = 'idle'): JobSnapshot {
  const run = (key: 'transcribe' | 'lyrics', state: StageState) => ({
    key, label: key === 'lyrics' ? '歌词识别' : '扒谱', state, progress: 0, step: '',
    message: '', started_at: null, finished_at: null, elapsed_seconds: 0, error: null,
    detail: {},
  })
  return {
    id: 'x', name: 'x', audio: 'x.wav', rev: 1,
    state: 'done', stage: null,
    stages: { transcribe: run('transcribe', transcribe), lyrics: run('lyrics', lyrics) },
    stage_label: '', step: '', progress: 0, message: '', created_at: 0,
    elapsed_seconds: 0, error: null, warnings: [], files: [], stems: {},
    highlights: null, decisions: null, summary: null, events: 0,
  } as JobSnapshot
}

function manual(actions: { key: string; disabled?: boolean; title?: string }[]) {
  return actions.find((action) => action.key === 'lyrics')!
}
function auto(actions: { key: string }[]) {
  return actions.find((action) => action.key === 'auto')
}

describe('歌词按钮的门禁', () => {
  it('output/ 里载入的已有产物：整组按钮只读', () => {
    const imported = { ...job('done', 'done'), imported: true, audio: '' }
    const lyrics = lyricsActions(imported)
    expect(lyrics).toHaveLength(1)
    expect(lyrics[0]!.disabled).toBe(true)
    expect(lyrics[0]!.title).toContain('已有的产物')

    const transcribe = transcribeActions(imported)
    expect(transcribe[0]!.disabled).toBe(true)
    expect(transcribe[0]!.label).toContain('只读')
    expect(transcribe.some((action) => action.key === 'stop')).toBe(false)
  })

  it('扒谱进行中：歌词按钮禁用，提示去用自动提取', () => {
    const actions = lyricsActions(job('running'))
    expect(manual(actions).disabled).toBe(true)
    expect(manual(actions).title).toContain('扒谱进行中')
    // 「自动提取」必须还能点——它就是排队链路
    expect(auto(actions)).toBeDefined()
    expect(auto(actions)!.key).toBe('auto')
  })

  it('扒谱在排队：同样不让手动点歌词', () => {
    expect(manual(lyricsActions(job('queued'))).disabled).toBe(true)
  })

  it('没扒谱：可点，但提示"请先完成扒谱"', () => {
    const actions = lyricsActions(job('idle'))
    expect(manual(actions).disabled).toBeFalsy()
    expect(manual(actions).title).toBe(NOT_READY_HINT)
    expect(lyricsBlockedMessage(job('idle'))).toBe(NOT_READY_HINT)
  })

  it('扒谱失败/被取消：一样提示先扒谱', () => {
    for (const state of ['failed', 'cancelled'] as StageState[]) {
      expect(manual(lyricsActions(job(state))).disabled).toBeFalsy()
      expect(lyricsBlockedMessage(job(state))).toBe(NOT_READY_HINT)
    }
  })

  it('有扒谱但未填词：允许点（这是正常状态）', () => {
    const actions = lyricsActions(job('done', 'idle'))
    expect(manual(actions).disabled).toBeFalsy()
    expect(manual(actions).title).toBeUndefined()
    expect(manual(actions).label).toBe('开始识别歌词')
    expect(lyricsBlockedMessage(job('done', 'idle'))).toBeNull()
  })

  it('已经填过词：按钮变成"重新识别"', () => {
    expect(manual(lyricsActions(job('done', 'done'))).label).toBe('重新识别')
    // 扒谱也完成时，自动提取变成"重跑全流程"
    expect(auto(lyricsActions(job('done', 'done')))!.label).toContain('重跑全流程')
  })

  it('歌词正在跑：只给停止', () => {
    const actions = lyricsActions(job('done', 'running'))
    expect(actions).toHaveLength(1)
    expect(actions[0]!.key).toBe('stop')
  })

  it('歌词排队中也是只给停止', () => {
    expect(lyricsActions(job('done', 'queued'))[0]!.key).toBe('stop')
  })

  it('卡在扒谱进行中时，那句提示要说明是"进行中"而不是"没扒谱"', () => {
    expect(lyricsBlockedMessage(job('running'))).toContain('扒谱进行中')
  })
})

describe('扒谱按钮', () => {
  it('跑着的时候给停止', () => {
    expect(transcribeActions(job('running'))[0]!.key).toBe('stop')
  })

  it('没跑过是"开始扒谱"，跑过是"重新扒谱"', () => {
    expect(transcribeActions(job('idle'))[0]!.label).toBe('开始扒谱')
    expect(transcribeActions(job('done', 'done'))[0]!.label).toBe('重新扒谱')
  })
})
