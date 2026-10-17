/**
 * 删除确认的"防手滑"规则（纯函数，有单测）。
 *
 * 删除按钮就在「下载 ▾」旁边，手指偏 20 像素就是几小时的活全没。所以：
 *
 * 1. **先说清楚要删什么**（几个产物、有没有原音频、能不能恢复）；
 * 2. **弹层出现 1 秒之内**确认按钮是灰的——连点两下（第一下点到删除、第二下点到
 *    弹层上的确认）不会瞬间生效。
 *
 * 真正兜底的是后端：删除是**软删除**（目录挪进 `.trash/`），7 天内都能恢复。
 */

/** 确认按钮要"等满"多久才能点（毫秒）。 */
export const CONFIRM_DELAY_MS = 1000

/**
 * 弹层打开后过了多久（0~1，1 = 可以点确认了）。
 * `startedAt` 为 null 表示还没开始计时。
 */
export function armProgress(startedAt: number | null, now: number = Date.now(),
                            delay: number = CONFIRM_DELAY_MS): number {
  if (startedAt === null) return 0
  if (delay <= 0) return 1
  const ratio = (now - startedAt) / delay
  return Math.min(1, Math.max(0, ratio))
}

export function armed(startedAt: number | null, now: number = Date.now(),
                      delay: number = CONFIRM_DELAY_MS): boolean {
  return armProgress(startedAt, now, delay) >= 1
}

/**
 * 删除弹层的正文：把"会丢什么、能不能恢复"说清楚，别只写一句"确定删除吗"。
 */
export function deleteWarning(name: string, files: number,
                              hasAudio = false): string {
  const parts = [`把《${name}》的 ${files} 个产物`]
  if (hasAudio) parts.push('和上传的原音频')
  return `这将删除${parts.join('')}。删掉之后会先进回收站，7 天内可以恢复。`
}
