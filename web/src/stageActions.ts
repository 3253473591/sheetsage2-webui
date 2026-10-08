/**
 * 阶段按钮的可用性规则（纯函数，单独放一个文件是为了能单测）。
 *
 * 工作流是 **扒谱 → 歌词**（原型 `静态页面.html` 也是这么写的），所以：
 *
 * | 扒谱状态 | 「开始识别歌词」 | 「⏵ 自动提取（扒谱 → 歌词）」 |
 * |---|---|---|
 * | 进行中 / 排队 | **禁用**（点了也没用，扒谱跑完才放开） | 可用（它就是排队链路） |
 * | 没跑过 / 失败 / 取消 | **可点，但提示"请先完成扒谱"** | 可用 |
 * | 已完成（有谱，可能还没填词） | 可用（"开始识别歌词" / "重新识别"） | 可用（重跑全流程） |
 *
 * 后端也有同一道门（`POST /stages/lyrics` 会回 409"请先完成扒谱"），
 * 界面这道只是别让人白点。
 */

import type { JobSnapshot, PhaseKey, StageSnapshot } from '@/types'

export interface StageAction {
  key: 'transcribe' | 'lyrics' | 'auto' | 'stop'
  label: string
  kind: 'primary' | 'secondary'
  disabled?: boolean
  /** 鼠标悬停的说明：为什么不能点。 */
  title?: string
}

export const NOT_READY_HINT = '请先完成扒谱'
/** `output/` 里本来就有的产物：只读，不能在这里重跑。 */
export const IMPORTED_HINT = '这是 output/ 里已有的产物（没有原音频），只能看和下载；要重跑请拖入音频新建工程'

function stage(job: JobSnapshot | null, phase: PhaseKey): StageSnapshot | undefined {
  return job?.stages?.[phase]
}

function busy(run: StageSnapshot | undefined): boolean {
  return run?.state === 'running' || run?.state === 'queued'
}

export function transcribed(job: JobSnapshot | null): boolean {
  return stage(job, 'transcribe')?.state === 'done'
}

/** 扒谱目前能不能点（跑着的时候给出"停止"）。 */
export function transcribeActions(job: JobSnapshot | null): StageAction[] {
  const run = stage(job, 'transcribe')
  if (job?.imported) {
    return [{ key: 'transcribe', label: '已有产物（只读）', kind: 'primary',
              disabled: true, title: IMPORTED_HINT }]
  }
  if (busy(run)) {
    return [{ key: 'stop', label: '停止', kind: 'secondary' }]
  }
  return [{
    key: 'transcribe',
    label: run?.state === 'done' ? '重新扒谱' : '开始扒谱',
    kind: 'primary',
  }]
}

export function lyricsActions(job: JobSnapshot | null): StageAction[] {
  const run = stage(job, 'lyrics')
  if (job?.imported) {
    return [{ key: 'lyrics',
              label: run?.state === 'done' ? '已有歌词（只读）' : '没有歌词',
              kind: 'primary', disabled: true, title: IMPORTED_HINT }]
  }
  if (busy(run)) {
    return [{ key: 'stop', label: '停止', kind: 'secondary' }]
  }

  const ready = transcribed(job)
  const transcribing = busy(stage(job, 'transcribe'))

  // 「自动提取」永远在：它就是"扒谱 → 歌词"的排队链路，扒谱在跑时更要能点
  const auto: StageAction = {
    key: 'auto',
    label: ready ? '自动提取 · 重跑全流程' : '⏵ 自动提取（扒谱 → 歌词）',
    kind: ready ? 'secondary' : 'primary',
  }

  const manual: StageAction = {
    key: 'lyrics',
    label: run?.state === 'done' ? '重新识别' : '开始识别歌词',
    kind: 'primary',
  }
  if (transcribing) {
    // 扒谱正在跑：按钮置灰，别让人白点
    manual.disabled = true
    manual.title = '扒谱进行中，完成后再点（或用「自动提取」排队）'
  } else if (!ready) {
    // 没扒谱/扒谱失败：可点，点了给一句人话
    manual.title = NOT_READY_HINT
  }

  return [auto, manual]
}

/** 点了「开始识别歌词」但还没扒谱时，界面该显示什么。 */
export function lyricsBlockedMessage(job: JobSnapshot | null): string | null {
  if (transcribed(job)) return null
  if (busy(stage(job, 'transcribe'))) return '扒谱进行中，完成后再点「开始识别歌词」'
  return NOT_READY_HINT
}
