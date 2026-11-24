/**
 * 一次拖两个文件时的归属判定（`npm test`）。
 *
 * 规则：认名字（干声/vocal/…）优先，否则按拖入顺序（第一个混音原曲、第二个干声）。
 * 另外这里还测**拖文件夹**那条路：整批文件 → 按同名配成「音频 + 歌词」。
 */
import { describe, expect, it } from 'vitest'
import {
  collectFiles, groupForUpload, splitDroppedFiles, type DirEntryLike,
} from '../src/drop'

function file(name: string): File {
  return new File([new Uint8Array(1)], name, { type: 'audio/wav' })
}

function entry(name: string, children: DirEntryLike[] = []): DirEntryLike {
  if (!children.length) {
    return {
      isFile: true, isDirectory: false, name,
      file: (ok) => ok(file(name)),
    }
  }
  // 目录：`readEntries` 第一次给全部、第二次给空（和浏览器的分批语义一致）
  let sent = false
  return {
    isFile: false, isDirectory: true, name,
    createReader: () => ({
      readEntries: (ok) => {
        ok(sent ? [] : children)
        sent = true
      },
    }),
  }
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

describe('拖文件夹：整批文件 → 一首一首', () => {
  it('按同名配对，大小写不敏感，.lrc 优先于 .txt', () => {
    const pairs = groupForUpload([
      file('01 - 甲.flac'),
      file('01 - 甲.lrc'),
      file('02 - 乙.wav'),
      file('02 - 乙.TXT'),
      file('02 - 乙.lrc'),
      file('cover.jpg'),
      file('03 - 丙.mp3'),
    ])
    expect(pairs.map((pair) => pair.name)).toEqual(['01 - 甲', '02 - 乙', '03 - 丙'])
    expect(pairs[0]!.lyrics?.name).toBe('01 - 甲.lrc')
    expect(pairs[1]!.lyrics?.name).toBe('02 - 乙.lrc')     // 同时有 txt 时选 lrc
    expect(pairs[2]!.lyrics).toBeNull()                    // 没词
  })

  it('只有歌词没有音频 = 这一首不存在', () => {
    expect(groupForUpload([file('只有词.lrc')])).toEqual([])
  })

  it('目录递归展开（readEntries 分两批返回）', () => {
    return collectFiles([
      entry('Album', [
        entry('01.flac'),
        entry('Sub', [entry('02.flac'), entry('02.lrc')]),
      ]),
    ]).then((files) => {
      expect(files.map((item) => item.name).sort()).toEqual(['01.flac', '02.flac', '02.lrc'])
    })
  })
})
