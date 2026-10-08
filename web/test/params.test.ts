/**
 * 参数面板的纯逻辑单测（`npm test`）。
 *
 * 面板版式对齐 `静态页面.html`，但取值由后端 `/api/env` 给（`jobs.PARAM_SPECS`）：
 * 1. 工程没设过的参数用服务端默认值；
 * 2. 「语言」是下拉框（本项目比原型多的那一处），其余是标签按钮；
 * 3. 生成的补丁不改原对象（快照要能被 Vue 追踪到）；
 * 4. 4G 特供 / 阶段正在跑时面板只读。
 */
import { describe, expect, it } from 'vitest'
import {
  FALLBACK_PARAMS,
  optionPatch,
  paramLabel,
  paramRows,
  paramsLocked,
} from '../src/params'

describe('参数行', () => {
  it('工程设过就用工程的值，没设过用服务端的默认值', () => {
    const rows = paramRows(FALLBACK_PARAMS.transcribe, { dtype: 'fp32' })
    expect(rows.map((row) => row.key)).toEqual(['dtype', 'device', 'range', 'quantize'])
    expect(rows[0]!.value).toBe('fp32')          // 工程选过 FP32 精确
    expect(rows[1]!.value).toBe('auto')          // 没选过 -> 默认
    expect(rows[2]!.value).toBe('full')
    expect(rows[3]!.value).toBe('grid')
  })

  it('没有 env（/api/env 没回来）时组件用 FALLBACK_PARAMS，函数本身不炸', () => {
    // 组件里是 `env?.params ?? FALLBACK_PARAMS`；函数拿到 undefined 就当"没有这一组"
    expect(paramRows(undefined, undefined)).toEqual([])
    expect(paramRows(FALLBACK_PARAMS.transcribe, undefined).length).toBe(4)
  })

  it('「语言」是下拉框，其余是标签按钮', () => {
    const lyrics = paramRows(FALLBACK_PARAMS.lyrics, undefined)
    const language = lyrics.find((row) => row.key === 'language')!
    expect(language.widget).toBe('select')
    expect(language.options.map((item) => item.label)).toEqual(
      ['自动', '中文', '英语', '日语', '粤语', '韩语'],
    )
    // 原型只有 4 个语言，这里是下拉框全量——这就是"本项目多的那一处"
    expect(language.options.length).toBeGreaterThan(4)
    for (const row of lyrics.filter((item) => item.key !== 'language')) {
      expect(row.widget).toBe('buttons')
    }
  })

  it('扒谱的「量化」是原型没有的那一项；音高修正不做成选项', () => {
    const rows = paramRows(FALLBACK_PARAMS.transcribe, undefined)
    const quantize = rows.find((row) => row.key === 'quantize')!
    expect(quantize.label).toBe('量化')
    expect(quantize.options.map((item) => item.value)).toEqual(['grid', 'off'])
    // 网格 = 一拍的 1/96（直线支 × 三连音支），命名跟 Synthesizer V 的 SNAP 菜单一致
    expect(quantize.options.map((item) => item.label))
      .toEqual(['128 分三连音网格', '不量化'])
    // 音高一律降一个八度（虚拟歌姬不会按原调调音），面板上不该有这一行
    expect(rows.some((row) => row.key === 'pitch')).toBe(false)
  })

  it('歌词的「段落」默认只跑唱段（跳过前奏/间奏/尾奏），每个取值都带说明', () => {
    const row = paramRows(FALLBACK_PARAMS.lyrics, undefined)
      .find((item) => item.key === 'skip_instrumental')!
    expect(row.label).toBe('段落')
    expect(row.value).toBe('vocal')                // 没设过 -> 用默认值
    expect(row.options.map((item) => item.value)).toEqual(['vocal', 'all'])
    expect(row.options.map((item) => item.label)).toEqual(['只跑唱段', '全部段落'])
    for (const item of row.options) expect(item.hint).toBeTruthy()
    // 工程设过就跟着工程走
    expect(paramRows(FALLBACK_PARAMS.lyrics, { skip_instrumental: 'all' })
      .find((item) => item.key === 'skip_instrumental')!.value).toBe('all')
  })

  it('三行的顺序与原型一致（精度 / 设备 / 范围），量化在最后', () => {
    expect(paramRows(FALLBACK_PARAMS.transcribe, undefined).map((row) => row.label))
      .toEqual(['精度', '设备', '范围', '量化'])
    expect(paramRows(FALLBACK_PARAMS.lyrics, undefined).map((row) => row.label))
      .toEqual(['模型', '音源', '段落', '语言'])
  })

  it('值 -> 显示名', () => {
    expect(paramLabel(FALLBACK_PARAMS.transcribe, 'range', '30')).toBe('前 30 秒')
    expect(paramLabel(FALLBACK_PARAMS.lyrics, 'language', '')).toBe('自动')
    expect(paramLabel(FALLBACK_PARAMS.transcribe, 'nope', 'x')).toBe('x')
  })
})

describe('参数补丁', () => {
  it('只改一项，不动原对象', () => {
    const before = { dtype: 'bf16', quantize: 'grid' }
    const after = optionPatch(before, 'quantize', 'off')
    expect(after).toEqual({ dtype: 'bf16', quantize: 'off' })
    expect(before.quantize).toBe('grid')         // 原对象没被改
    expect(after).not.toBe(before)
  })

  it('工程还没参数（老快照）时也能给出一份完整补丁', () => {
    expect(optionPatch(undefined, 'dtype', 'fp32')).toEqual({ dtype: 'fp32' })
  })
})

describe('面板只读的两种情况', () => {
  it('4G 特供：服务端锁定 -> 只读', () => {
    expect(paramsLocked(true, false)).toBe(true)
  })

  it('阶段正在跑 -> 只读（那一趟用的是改动前的值）', () => {
    expect(paramsLocked(false, true)).toBe(true)
  })

  it('默认模式且没在跑 -> 可改', () => {
    expect(paramsLocked(false, false)).toBe(false)
  })
})
