/**
 * 前端请求超时的单测（G19）。
 *
 * 复现的是真实场景：模型卡死（G6）时后端不再回话，而 `fetch` 没有超时——页面就**永远
 * pending**，用户只能刷新。这里用"永远不 resolve、只响应 abort 的 fetch"把这条件造出来。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchWithTimeout, describeNetworkError } from '../src/api'

function hangingFetch(_input: unknown, init?: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => {
      const error = new Error('The operation was aborted')
      error.name = 'AbortError'
      reject(error)
    })
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('带超时的 fetch', () => {
  it('超时后抛人话，而不是永远 pending', async () => {
    vi.stubGlobal('fetch', hangingFetch)
    await expect(fetchWithTimeout('/env', {}, 30)).rejects.toThrow(/服务没响应/)
  })

  it('连不上时说明是"后台服务"的问题', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')))
    await expect(fetchWithTimeout('/env', {}, 1000)).rejects.toThrow(/连不上后台服务/)
  })

  it('正常响应原样返回', async () => {
    const response = new Response('{"ok":true}', { status: 200 })
    vi.stubGlobal('fetch', () => Promise.resolve(response))
    const result = await fetchWithTimeout('/healthz', {}, 1000)
    expect(result.status).toBe(200)
  })
})

describe('异常翻译', () => {
  it('AbortError -> 服务没响应（带上等了多少秒）', () => {
    const error = new Error('aborted')
    error.name = 'AbortError'
    expect(describeNetworkError(error, 30_000).message).toContain('30 秒')
  })

  it('别的错误 -> 连不上后台服务', () => {
    expect(describeNetworkError(new Error('boom')).message).toContain('连不上后台服务')
  })
})
