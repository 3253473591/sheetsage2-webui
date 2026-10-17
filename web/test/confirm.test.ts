import { describe, expect, it } from 'vitest'
import { CONFIRM_DELAY_MS, armProgress, armed, deleteWarning } from '@/confirm'

describe('删除确认的防手滑计时（G2）', () => {
  it('弹层刚出来时确认按钮不可点', () => {
    expect(armProgress(null)).toBe(0)
    expect(armed(null, Date.now())).toBe(false)
    expect(armed(1000, 1000)).toBe(false)
  })

  it('满 1 秒之后才可以点', () => {
    expect(armed(1000, 1000 + CONFIRM_DELAY_MS)).toBe(true)
    expect(armed(1000, 1000 + CONFIRM_DELAY_MS - 1)).toBe(false)
    expect(armProgress(1000, 1000 + CONFIRM_DELAY_MS / 2)).toBeCloseTo(0.5, 5)
  })

  it('进度永远落在 0~1 之间', () => {
    expect(armProgress(0, -5000)).toBe(0)
    expect(armProgress(0, 1e9)).toBe(1)
    expect(armProgress(0, 10, 0)).toBe(1)      // delay = 0 视为立即可点
  })

  it('弹层正文说清楚会丢什么、能不能恢复', () => {
    const text = deleteWarning('晚风', 12, true)
    expect(text).toContain('《晚风》')
    expect(text).toContain('12 个产物')
    expect(text).toContain('原音频')
    expect(text).toContain('回收站')
    expect(deleteWarning('晚风', 3)).not.toContain('原音频')
  })
})
