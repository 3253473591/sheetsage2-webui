/**
 * 上传（`useUploads`）的单测：一次拖两个文件的拆法、上传进度、以及"上传不触发阶段"。
 *
 * 门禁是产品行为：建完工程必须停在 `draft`，用户点「开始扒谱」才排队。这里盯的是
 * 前端这一侧——`create()` 不许自己去调 `runStage`。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../src/api', () => ({
  uploadJob: vi.fn(),
  uploadStem: vi.fn(),
  deleteStem: vi.fn(),
  getJob: vi.fn(),
  runStage: vi.fn(),
}))

import * as api from '../src/api'
import { useUploads } from '../src/composables/useUploads'
import { computed } from 'vue'
import { job as snapshot } from './helpers'

function harness() {
  const jobs: ReturnType<typeof snapshot>[] = []
  const errors: string[] = []
  const notes: string[] = []
  const selected: string[] = []
  const uploads = useUploads({
    active: computed(() => jobs[0] ?? null),
    upsert: (job) => {
      const index = jobs.findIndex((item) => item.id === job.id)
      if (index >= 0) jobs[index] = job
      else jobs.unshift(job)
    },
    select: async (id: string) => {
      selected.push(id)
    },
    pushLog: (_seq, _phase, text) => notes.push(text),
    onError: (message) => errors.push(message),
    reloadPeaks: () => undefined,
    beginRun: () => undefined,
    clearStemPeaks: () => undefined,
  })
  return { uploads, jobs, errors, notes, selected }
}

function file(name: string): File {
  return new File([new Uint8Array([1, 2, 3])], name)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('建工程', () => {
  it('只拖一个音频：建工程、选中、**不触发任何阶段**', async () => {
    const h = harness()
    vi.mocked(api.uploadJob).mockResolvedValue(snapshot({ id: 'a' }))
    await h.uploads.create(file('歌.wav'))
    expect(api.uploadJob).toHaveBeenCalledTimes(1)
    expect(h.selected).toEqual(['a'])
    expect(api.runStage).not.toHaveBeenCalled()
    expect(h.uploads.uploadProgress.value).toBeNull()      // 传完就把进度收起来
  })

  it('一次拖两个：第二个当干声挂上去', async () => {
    const h = harness()
    vi.mocked(api.uploadJob).mockResolvedValue(snapshot({ id: 'a' }))
    vi.mocked(api.uploadStem).mockResolvedValue(snapshot({ id: 'a', stems: { vocal: { name: 'v.wav' } } as never }))
    await h.uploads.create([file('歌.wav'), file('歌_干声.wav')])
    expect(api.uploadJob).toHaveBeenCalledTimes(1)
    expect(api.uploadStem).toHaveBeenCalledTimes(1)
    expect(h.selected).toEqual(['a'])
  })

  it('上传失败显示人话，不抛', async () => {
    const h = harness()
    vi.mocked(api.uploadJob).mockRejectedValue(new Error('文件超过 512 MB 上限'))
    await h.uploads.create(file('大.wav'))
    expect(h.errors.at(-1)).toBe('文件超过 512 MB 上限')
    expect(h.uploads.uploadProgress.value).toBeNull()
  })
})

describe('换音源', () => {
  it('挂干声会把新快照写回列表', async () => {
    const h = harness()
    h.jobs.push(snapshot({ id: 'a' }))
    vi.mocked(api.uploadStem).mockResolvedValue(snapshot({ id: 'a', rev: 2 }))
    await h.uploads.attachStem(file('v.wav'))
    expect(h.jobs[0]!.rev).toBe(2)
  })

  it('移除干声同样写回快照', async () => {
    const h = harness()
    h.jobs.push(snapshot({ id: 'a', stems: { vocal: { name: 'v.wav' } as never } }))
    vi.mocked(api.deleteStem).mockResolvedValue(snapshot({ id: 'a', rev: 3 }))
    await h.uploads.detachStem()
    expect(h.jobs[0]!.rev).toBe(3)
  })

  it('没有当前工程时什么都不做', async () => {
    const h = harness()
    await h.uploads.attachStem(file('v.wav'))
    expect(api.uploadStem).not.toHaveBeenCalled()
  })
})
