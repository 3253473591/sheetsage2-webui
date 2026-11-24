/**
 * 批量页纯逻辑的单测（`npm test`）。
 *
 * 这些规则都是"看起来显然、改起来很容易踩"的那种：
 * - 拖两个文件还是"混音 + 干声"，别被批量顺手改掉；
 * - 批次进度用完成数，不用加权百分比；
 * - 歌词源的四种口径（LRC / TXT / 日韩 / 无）要能一眼分开。
 */
import { describe, expect, it } from 'vitest'
import {
  batchStats, classifyDrop, formatSize, formatSpan, lrcBadge,
  PER_AUDIO_SECOND, PER_SONG_OVERHEAD,
} from '../src/batch'
import type { JobRow, PhaseKey, StageRow } from '../src/types'

function stage(state: StageRow['state'] = 'idle'): StageRow {
  return {
    state, progress: 0, step: '', label: '', error: null, error_code: null,
    error_action: null, started_at: null, finished_at: null, elapsed_seconds: 0,
  }
}

function row(overrides: Partial<JobRow> = {}): JobRow {
  const stages = {
    transcribe: stage(),
    lyrics: stage(),
  } as Record<PhaseKey, StageRow>
  return {
    id: 'j1',
    name: '歌',
    audio: '歌.flac',
    state: 'queued',
    stage: null,
    rev: 1,
    artifacts_rev: 0,
    created_at: 0,
    elapsed_seconds: 0,
    imported: false,
    batch: { id: 'b1', title: '批量' },
    duplicate_of: null,
    cancel_requested: false,
    audio_seconds: 240,
    stages,
    ...overrides,
  }
}

const file = (name: string, size = 1024) => ({ name, size })

describe('拖进来的是哪一种', () => {
  it('一个音频 = 单曲（现状不变）', () => {
    expect(classifyDrop([file('歌.flac')])).toBe('single')
  })

  it('两个音频 = 混音 + 干声（这条老规矩不能为了批量破坏掉）', () => {
    expect(classifyDrop([file('歌.flac'), file('歌-干声.wav')])).toBe('pair')
    expect(classifyDrop([file('a.flac'), file('b.wav')])).toBe('pair')
  })

  it('三个以上音频 = 批量', () => {
    expect(classifyDrop([file('a.flac'), file('b.flac'), file('c.flac')])).toBe('batch')
  })

  it('带了 .lrc 就是批量（哪怕只有一个音频）', () => {
    expect(classifyDrop([file('歌.flac'), file('歌.lrc')])).toBe('batch')
  })

  it('拖的是文件夹（size=0 的单条目）= 批量', () => {
    expect(classifyDrop([file('2026-11', 0)])).toBe('batch')
  })

  it('空 / 全是图片 = 不支持', () => {
    expect(classifyDrop([])).toBe('unsupported')
    expect(classifyDrop([file('封面.jpg')])).toBe('unsupported')
  })
})

describe('批次进度', () => {
  it('按完成首数算，不做加权平均百分比', () => {
    const rows = [
      row({ state: 'done', elapsed_seconds: 100 }),
      row({ state: 'done', elapsed_seconds: 120 }),
      row({ state: 'failed', elapsed_seconds: 60 }),
      row({ state: 'running' }),
      row({ state: 'queued' }),
    ]
    const stats = batchStats(rows)
    expect(stats.total).toBe(5)
    expect(stats.done).toBe(2)
    expect(stats.failed).toBe(1)
    expect(stats.running).toBe(1)
    expect(stats.pending).toBe(1)
    expect(stats.okRatio).toBeCloseTo(0.4)
    expect(stats.badRatio).toBeCloseTo(0.2)
    expect(stats.elapsedSeconds).toBe(280)
  })

  it('剩余时间 = 未完成的首数 × (固定开销 + 音频 × 系数)', () => {
    const rows = [
      row({ state: 'done', audio_seconds: 240 }),
      row({ state: 'queued', audio_seconds: 240 }),
    ]
    const expected = PER_SONG_OVERHEAD + 240 * PER_AUDIO_SECOND
    expect(batchStats(rows).remainingSeconds).toBe(Math.round(expected))
  })

  it('不知道音频多长时用已知均值兜底，全不知道就用 4 分钟', () => {
    const mixed = [row({ state: 'queued', audio_seconds: 200 }), row({ state: 'queued', audio_seconds: null })]
    const stats = batchStats(mixed)
    expect(stats.remainingSeconds).toBe(Math.round(2 * (PER_SONG_OVERHEAD + 200 * PER_AUDIO_SECOND)))

    const unknown = [row({ state: 'queued', audio_seconds: null })]
    expect(batchStats(unknown).remainingSeconds)
      .toBe(Math.round(PER_SONG_OVERHEAD + 240 * PER_AUDIO_SECOND))
  })

  it('全部结束（完成或失败）时剩余为 0，且能指出当前在跑的那一行', () => {
    expect(batchStats([row({ state: 'done' }), row({ state: 'failed' })]).remainingSeconds).toBe(0)
    const running = row({ id: 'x', state: 'running', name: '正在跑' })
    expect(batchStats([running, row({ state: 'done' })]).current?.name).toBe('正在跑')
    expect(batchStats([])).toMatchObject({ total: 0, remainingSeconds: 0, current: null })
  })
})

describe('歌词源与人话格式', () => {
  it('四种口径各自的短标签与语气', () => {
    expect(lrcBadge({ lrc_kind: 'lrc', lrc_lines: 48 })).toMatchObject({ short: 'LRC 48', tone: 'ok' })
    expect(lrcBadge({ lrc_kind: 'txt', lrc_lines: 42 }).tone).toBe('warn')
    expect(lrcBadge({ lrc_kind: 'lrc-ja', lrc_lines: 36 }).short).toBe('LRC·日')
    expect(lrcBadge({ lrc_kind: 'none', lrc_lines: 0 })).toMatchObject({ short: '无词', tone: 'idle' })
  })

  it('时长说人话：秒 / 分钟 / 小时', () => {
    expect(formatSpan(45)).toBe('45 秒')
    expect(formatSpan(180)).toBe('3 分钟')
    expect(formatSpan(3600 + 4 * 60)).toBe('1 小时 4 分')
    expect(formatSpan(null)).toBe('')
  })

  it('文件大小：B / KB / MB', () => {
    expect(formatSize(0)).toBe('—')
    expect(formatSize(512)).toBe('512 B')
    expect(formatSize(2048)).toBe('2 KB')
    expect(formatSize(28 * 1024 * 1024)).toBe('28.0 MB')
    expect(formatSize(300 * 1024 * 1024)).toBe('300 MB')
  })
})
