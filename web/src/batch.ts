/**
 * 批量页的纯逻辑（单独放一个文件是为了能单测；组件只负责画）。
 *
 * 三件事：
 * 1. `classifyDrop`：拖进来的东西该走「单曲 / 单曲+干声 / 批量」哪条路。
 *    规则必须显式写下来——以前"拖两个文件"一律当"混音+干声"，批量进来后这条会
 *    把第二首歌吃掉。
 * 2. `batchStats`：批次统计与剩余时间。**总进度 = 完成首数 / 总数**，不做加权平均
 *    百分比（两阶段耗时不可知，加权出来的 47% 什么都不说明）。
 * 3. `lrcBadge` / `formatSpan`：预览表里"歌词源"那一列的四种口径，和人话时长。
 */

import type { BatchScanItem, JobRow } from './types'

/** 拖入的东西该怎么处理。 */
export type DropKind = 'single' | 'pair' | 'batch' | 'unsupported'

const AUDIO_EXT = /\.(wav|mp3|flac|m4a|aac|ogg|opus|oga|aif|aiff|wma|mp4|wv|ape|alac|caf)$/i
const LYRICS_EXT = /\.(lrc|txt)$/i
/** 干声的命名习惯（和 `drop.ts` 的 `STEM_PATTERN` 同一条规则）。 */
const STEM_PATTERN = /干声|人声|vocal|voice|acapella|acappella|dry/i

interface DroppedLike {
  name: string
  size: number
}

/**
 * 拖进来的文件 → 走哪条路。
 *
 * | 判定 | 行为 |
 * |---|---|
 * | 1 个音频 | 单曲（现状） |
 * | 2 个音频、没带歌词 | 混音 + 干声（现状，别为了批量破坏它） |
 * | ≥3 个音频 / 带了 .lrc/.txt / 拖的是文件夹 | 批量 |
 */
export function classifyDrop(files: DroppedLike[]): DropKind {
  const list = files.filter(Boolean)
  if (!list.length) return 'unsupported'
  const lyrics = list.filter((file) => LYRICS_EXT.test(file.name))
  const audio = list.filter((file) => AUDIO_EXT.test(file.name))
  // 文件夹：Chrome 拖目录时给一个 size=0 的条目（拿不到里面的文件）
  const folder = list.length === 1 && list[0]!.size === 0 && !AUDIO_EXT.test(list[0]!.name)
  if (folder) return 'batch'
  if (lyrics.length) return 'batch'
  if (!audio.length) return 'unsupported'
  if (audio.length === 1) return 'single'
  if (audio.length === 2) {
    const stems = audio.filter((file) => STEM_PATTERN.test(file.name)).length
    return stems === 2 ? 'batch' : 'pair'
  }
  return 'batch'
}

/** 批次的统计与进度（纯计算，界面只读它）。 */
export interface BatchProgress {
  total: number
  done: number
  failed: number
  running: number
  pending: number
  /** 已完成 / 失败 在总数里的占比（分段进度条用，0~1） */
  okRatio: number
  badRatio: number
  /** 正在跑的那一行（没有就是 null） */
  current: JobRow | null
  /** 已用时间（秒）：各成员阶段耗时之和 */
  elapsedSeconds: number
  /** 剩余时间（秒）：没跑完的按音频长度估，未知长度用已知均值兜底 */
  remainingSeconds: number | null
}

/** 一首歌（扒谱 + 歌词）的粗估：固定开销 + 音频时长 × 系数。与 `eta.py` 同口径。 */
export const PER_SONG_OVERHEAD = 25
export const PER_AUDIO_SECOND = 0.55

export function batchStats(rows: JobRow[]): BatchProgress {
  const total = rows.length
  const done = rows.filter((row) => row.state === 'done').length
  const failed = rows.filter((row) => row.state === 'failed').length
  const runningRows = rows.filter((row) => row.state === 'running')
  const pending = Math.max(0, total - done - failed - runningRows.length)

  const elapsedSeconds = Math.round(
    rows.reduce((sum, row) => sum + (row.elapsed_seconds || 0), 0),
  )

  const known = rows
    .map((row) => row.audio_seconds)
    .filter((value): value is number => typeof value === 'number' && value > 0)
  const meanSeconds = known.length
    ? known.reduce((sum, value) => sum + value, 0) / known.length
    : 240

  const left = rows.filter((row) => row.state !== 'done' && row.state !== 'failed'
    && row.state !== 'cancelled')
  const remainingSeconds = left.length
    ? Math.round(left.reduce((sum, row) => {
      const audio = row.audio_seconds ?? meanSeconds
      return sum + PER_SONG_OVERHEAD + audio * PER_AUDIO_SECOND
    }, 0))
    : 0

  return {
    total,
    done,
    failed,
    running: runningRows.length,
    pending,
    okRatio: total ? done / total : 0,
    badRatio: total ? failed / total : 0,
    current: runningRows[0] ?? null,
    elapsedSeconds,
    remainingSeconds,
  }
}

/** 预览表「歌词源」列的四种口径（短标签给表格，长句给 title）。 */
export function lrcBadge(item: Pick<BatchScanItem, 'lrc_kind' | 'lrc_lines'>): {
  short: string
  text: string
  tone: 'ok' | 'warn' | 'idle'
} {
  switch (item.lrc_kind) {
    case 'lrc':
      return {
        short: `LRC ${item.lrc_lines}`,
        text: `同名 LRC ${item.lrc_lines} 行：逐行精确对齐，跳过语音识别`,
        tone: 'ok',
      }
    case 'lrc-ja':
    case 'lrc-ko':
      return {
        short: `LRC·${item.lrc_kind === 'lrc-ja' ? '日' : '韩'}`,
        text: '日/韩 LRC 走 Qwen 强制对齐器（随包 TIFA 没有日韩 G2P）',
        tone: 'warn',
      }
    case 'txt':
      return {
        short: 'TXT',
        text: '纯文本没有时间戳：仍要跑语音识别，字面用官方词',
        tone: 'warn',
      }
    default:
      return {
        short: '无词',
        text: '没有同名 LRC：走语音识别（慢一些，词也可能不对）',
        tone: 'idle',
      }
  }
}

/** 秒 → 人话（"1 小时 4 分" / "3 分钟" / "40 秒"）。批量里别用 mm:ss。 */
export function formatSpan(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return ''
  const value = Math.max(0, Math.round(seconds))
  if (value < 60) return `${value} 秒`
  const minutes = Math.round(value / 60)
  if (minutes < 60) return `${Math.max(1, minutes)} 分钟`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}

// ---------------------------------------------------------------- 拖拽上传的续传
/** 存在 localStorage 里的"上次没传完的批次"。 */
export interface PendingUpload {
  batchId: string
  folderName: string
  /** 这次拖进来一共几首（= 应传总数） */
  expected: number
  /** 已经传成功几首 */
  done: number
}

/** 这次上传该往哪个批次写。 */
export interface UploadTarget {
  /** 复用上次的批次 id；新批次是 undefined（让服务端铸） */
  batchId?: string
  /** 声明总首数（服务端只在缺的时候记） */
  expectedTotal: number
  /** 是不是接着上次传 */
  resume: boolean
}

/**
 * 同名的文件夹再拖一次，就接着上次的批次传（服务端已经支持 `batch_id` 复用）。
 *
 * 三条规则：
 * - 没有记录 / 换了文件夹 / 上次已经传够了 → 开新批次；
 * - 同名文件夹且上次没传够 → 复用批次 id，总首数沿用上次的（用户可能只挑了剩下的几首传）；
 * - 复用时不把 `expectedTotal` 改小，否则"还差几首"会算错。
 */
export function resolveUploadTarget(
  stored: PendingUpload | null,
  folderName: string,
  total: number,
): UploadTarget {
  if (stored && stored.folderName === folderName && stored.done > 0
    && stored.done < stored.expected) {
    return { batchId: stored.batchId, expectedTotal: stored.expected, resume: true }
  }
  return { expectedTotal: Math.max(total, stored?.expected ?? 0), resume: false }
}

/** 还差几首（用于批次页提示）。没有 `expected_total` 就是 0。 */
export function missingUploads(expected: number | undefined, total: number): number {
  if (!expected || expected <= total) return 0
  return expected - total
}

/** 预览表里"大小"那一列。 */
export function formatSize(bytes: number): string {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(bytes < 100 * 1024 * 1024 ? 1 : 0)} MB`
}
