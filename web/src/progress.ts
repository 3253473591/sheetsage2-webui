/**
 * SSE 进度的单调性（G18）：**进度只会往前走，重连补发过来的旧事件不许把它拉回去。**
 *
 * 后端只保证事件 `seq` 单调，不保证 `progress` 单调——重跑一个阶段时进度会从 0 重新开始，
 * 而重连（带 `?since=`）或切换工程之后，前端可能先收到"这一趟"的事件、再收到上一趟补发的
 * 事件，于是进度条从 12% 跳回 80%，用户会以为任务在倒着跑。
 *
 * 三道规则（都在这个文件里，**纯函数**，有单测）：
 *
 * 1. **只增不减**（`monotonicProgress`）：`step` / `progress` 事件只能把进度往上推。
 * 2. **旧事件不算数**（`isStaleEvent`）：事件时间比这一趟的 `started_at` 还早，就是上一趟
 *    留下的——记日志可以，别动进度。
 * 3. **新一轮从 0 开始**（`startsNewRun`）：`phase` 事件带的时间不早于 `started_at`
 *    才当作新一轮，这时才允许把进度归零。
 *
 * 为什么不用"改参数后把 `lastSeq` 归零"：那样重连时会把**整段历史**重放一遍，
 * 反而更容易出现"旧快照打在新的状态上"。单调 + 时间戳过滤是不依赖重放语义的做法。
 */

import type { JobEvent, StageSnapshot } from './types'

/** 时间戳比较的容差（秒）：事件与 `started_at` 都是服务器时钟，但同一毫秒内的先后关系不可靠。 */
export const STALE_TOLERANCE_SECONDS = 1.5

/** 事件是不是上一趟运行留下的（比这一趟开始还早）。没有 `started_at` 就无从判断，一律当新事件。 */
export function isStaleEvent(
  eventTime: number,
  startedAt: number | null | undefined,
  tolerance = STALE_TOLERANCE_SECONDS,
): boolean {
  if (!startedAt || !eventTime) return false
  return eventTime + tolerance < startedAt
}

/** 进度只增不减，并夹在 0~1（后端偶尔会给出 1.0000001 这种值）。 */
export function monotonicProgress(current: number | undefined, next: number | undefined): number {
  const base = Number.isFinite(current) ? clamp01(current as number) : 0
  if (!Number.isFinite(next)) return base
  return Math.max(base, clamp01(next as number))
}

/** `phase` 事件开启的是不是**新一轮**：时间不早于当前的 `started_at`，或者当前还没开始过。 */
export function startsNewRun(
  eventTime: number,
  startedAt: number | null | undefined,
  tolerance = STALE_TOLERANCE_SECONDS,
): boolean {
  if (!startedAt) return true
  if (!eventTime) return false
  return eventTime + tolerance >= startedAt
}

/** 一条进展类事件对阶段的补丁；返回 `null` = 这条事件不该影响这个阶段（旧事件）。 */
export interface StagePatch {
  progress?: number
  step?: string
  message?: string
}

export function patchFromEvent(run: StageSnapshot, event: JobEvent): StagePatch | null {
  if (event.type !== 'step' && event.type !== 'progress') return null
  if (isStaleEvent(event.time || 0, run.started_at)) return null
  const patch: StagePatch = {}
  if (event.type === 'step') {
    if (event.data.step !== undefined) patch.step = event.data.step
  }
  if (typeof event.data.progress === 'number') {
    patch.progress = monotonicProgress(run.progress, event.data.progress)
  }
  if (event.type === 'progress' && event.data.message) {
    patch.message = event.data.message
  }
  return patch
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value))
}
