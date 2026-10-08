/**
 * 拖进来一堆文件时，谁是混音原曲、谁是干声？
 *
 * 规则（界面上也这么写）：
 * 1. 文件名里带"干声 / vocal / voice / 人声 / acapella"的，当**干声**；
 * 2. 其余按拖入顺序：第一个是混音原曲，第二个（如果还有）当干声；
 * 3. 只拖一个就是混音原曲（干声后面再传）。
 *
 * 不做音频内容判断——**内容本来就不校验**（用户可能故意把混音原曲传进干声栏）。
 */

export interface DroppedFiles {
  mix: File | null
  stem: File | null
  /** 给界面看的一句话说明。 */
  note: string
}

const STEM_PATTERN = /干声|人声|vocal|voice|acapella|acappella|dry/i

export function splitDroppedFiles(files: File[]): DroppedFiles {
  const list = files.filter(Boolean)
  if (!list.length) return { mix: null, stem: null, note: '' }

  const named = list.findIndex((file) => STEM_PATTERN.test(file.name))
  if (named >= 0 && list.length > 1) {
    const stem = list[named]!
    const mix = list.find((file) => file !== stem)!
    return { mix, stem, note: `按文件名认出来：${stem.name} 当干声` }
  }
  if (list.length === 1) return { mix: list[0]!, stem: null, note: '' }
  return {
    mix: list[0]!,
    stem: list[1]!,
    note: `按拖入顺序：${list[0]!.name} 当混音原曲，${list[1]!.name} 当干声`,
  }
}
