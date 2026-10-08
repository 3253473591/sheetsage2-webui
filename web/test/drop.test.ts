/**
 * 一次拖两个文件时的归属判定（`npm test`）。
 *
 * 规则：认名字（干声/vocal/…）优先，否则按拖入顺序（第一个混音原曲、第二个干声）。
 */
import { describe, expect, it } from 'vitest'
import { splitDroppedFiles } from '../src/drop'

function file(name: string): File {
  return new File([new Uint8Array(1)], name, { type: 'audio/wav' })
}

describe('拖入文件的归属', () => {
  it('只拖一个就是混音原曲，干声留空', () => {
    const result = splitDroppedFiles([file('歌.flac')])
    expect(result.mix?.name).toBe('歌.flac')
    expect(result.stem).toBeNull()
    expect(result.note).toBe('')
  })

  it('拖两个：按顺序，第一个混音原曲、第二个干声', () => {
    const result = splitDroppedFiles([file('歌.flac'), file('a.wav')])
    expect(result.mix?.name).toBe('歌.flac')
    expect(result.stem?.name).toBe('a.wav')
    expect(result.note).toContain('按拖入顺序')
  })

  it('名字里带"干声"的优先当干声，哪怕是第一个', () => {
    const result = splitDroppedFiles([file('歌-干声.wav'), file('歌.flac')])
    expect(result.stem?.name).toBe('歌-干声.wav')
    expect(result.mix?.name).toBe('歌.flac')
    expect(result.note).toContain('按文件名认出来')
  })

  it('vocal / acapella 这些英文写法也认', () => {
    for (const name of ['song_vocal.wav', 'acapella.mp3', 'Voice.m4a', '人声.wav']) {
      const result = splitDroppedFiles([file('mix.flac'), file(name)])
      expect(result.stem?.name, name).toBe(name)
      expect(result.mix?.name).toBe('mix.flac')
    }
  })

  it('三个以上也只认一条干声（第一条不是干声的那个之外）', () => {
    const result = splitDroppedFiles([file('a.flac'), file('b.wav'), file('c.wav')])
    expect(result.mix?.name).toBe('a.flac')
    expect(result.stem?.name).toBe('b.wav')
  })

  it('空列表不炸', () => {
    expect(splitDroppedFiles([])).toEqual({ mix: null, stem: null, note: '' })
  })
})
