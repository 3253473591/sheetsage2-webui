/**
 * 参数面板的纯逻辑（单独放一个文件是为了能单测）。
 *
 * 取值的**唯一真源在后端**：`/api/env` 的 `params` 就是 `jobs.PARAM_SPECS` 发过来的，
 * 这里只负责把它和工程当前的 `options` 折成"要渲染的几行"，以及算下一次的补丁。
 * 组件（`StageCard.vue`）纯展示 + 抛事件，`useStudio` 负责 PATCH。
 */

import type { JobOptions, ParamChoice, ParamGroup, ParamSpecs } from '@/types'

/** 面板上的一行：选项 + 当前值。 */
export interface ParamRow {
  key: string
  label: string
  widget: 'buttons' | 'select'
  value: string
  options: ParamChoice[]
}

/** `/api/env` 还没回来时的兜底：版式照原型留着，值用服务端的默认。 */
export const FALLBACK_PARAMS: ParamSpecs = {
  transcribe: [
    { key: 'dtype', label: '精度', default: 'bf16',
      options: [{ value: 'bf16', label: 'BF16 快速' }, { value: 'fp32', label: 'FP32 精确' }] },
    { key: 'device', label: '设备', default: 'auto',
      options: [{ value: 'auto', label: '自动 · GPU' }, { value: 'cpu', label: 'CPU' }] },
    { key: 'range', label: '范围', default: 'full',
      options: [{ value: 'full', label: '整首' }, { value: '30', label: '前 30 秒' },
                { value: '60', label: '前 60 秒' }] },
    { key: 'quantize', label: '量化', default: 'grid',
      options: [{ value: 'grid', label: '128 分三连音网格' }, { value: 'off', label: '不量化' }] },
    // 旋律的音高修正（一律降一个八度）**故意不做成选项**：虚拟歌姬不会按原调调音，
    // 用户不需要选。见 docs/WebUI问题清单.md §1。
  ],
  lyrics: [
    { key: 'asr_model', label: '模型', default: '1.7B',
      options: [{ value: '1.7B', label: '1.7B' }, { value: '0.6B', label: '0.6B' }] },
    { key: 'source', label: '音源', default: 'stem',
      options: [{ value: 'stem', label: '干声（推荐）' },
                { value: 'mix', label: '混音原曲' }] },
    { key: 'skip_instrumental', label: '段落', default: 'vocal',
      options: [{ value: 'vocal', label: '只跑唱段',
                 hint: '跳过前奏 / 间奏 / 尾奏（一般不唱），省时间也省显存' },
                { value: 'all', label: '全部段落',
                 hint: '每个段落都跑，包括前奏 / 间奏 / 尾奏' }] },
    { key: 'language', label: '语言', default: '', widget: 'select',
      options: [{ value: '', label: '自动' }, { value: 'Chinese', label: '中文' },
                { value: 'English', label: '英语' }, { value: 'Japanese', label: '日语' },
                { value: 'Cantonese', label: '粤语' }, { value: 'Korean', label: '韩语' }] },
  ],
}

/** 一组参数行 -> 要渲染的行（工程没设过的用服务端默认值）。 */
export function paramRows(groups: ParamGroup[] | undefined,
                          options: JobOptions | undefined): ParamRow[] {
  return (groups ?? []).map((group) => {
    const current = options?.[group.key as keyof JobOptions]
    return {
      key: group.key,
      label: group.label,
      widget: group.widget ?? 'buttons',
      value: current === undefined || current === null ? group.default : String(current),
      options: group.options ?? [],
    }
  })
}

/** 某个参数当前值的显示名（日志与"本次用了什么"用；找不到就原样返回）。 */
export function paramLabel(groups: ParamGroup[] | undefined, key: string,
                           value: string): string {
  const group = groups?.find((item) => item.key === key)
  const hit = group?.options.find((item) => item.value === value)
  if (hit) return hit.label
  return value || '自动'
}

/** 点了一个选项之后的补丁——**不改原对象**（快照要能被 Vue 追踪到变化）。 */
export function optionPatch(options: JobOptions | undefined,
                            key: string, value: string): JobOptions {
  return { ...(options ?? {}), [key]: value }
}

/** 参数面板只读的两种情况：服务端锁定（4G 特供），或对应阶段正在跑。 */
export function paramsLocked(envLocked: boolean, running: boolean): boolean {
  return Boolean(envLocked) || running
}
