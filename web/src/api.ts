/**
 * 后端 API 客户端。
 *
 * 四个约定：
 * 1. 上传走 XHR——只有它给得出上传进度；其余请求走 fetch。
 * 2. 进度走 SSE（`text/event-stream`），事件带单调递增的 `id`；断线时我们主动关流、
 *    带着 `?since=` 重连，后端从下一个事件补发，所以刷新页面也不会丢进度。
 * 3. 任务终态一到就主动 `close()`，否则 EventSource 会对着已结束的流无限重连。
 * 4. **每个请求都有超时**（G19）：服务卡住（模型卡死、磁盘挂住）时页面不能永远 pending，
 *    只能靠用户手动刷新。超时之后给一句人话，让用户知道"服务还在，只是没回"。
 */

import type {
  BackupSummary,
  BatchDetail,
  BatchInfo,
  BatchScan,
  EnvInfo,
  ExportOptions,
  JobBatch,
  JobEvent,
  JobOptions,
  JobRow,
  JobSnapshot,
  Lanes,
  Peaks,
  PhaseKey,
  TrashEntry,
} from './types'

const BASE = '/api'

/** 普通请求的超时（毫秒）。30 秒还没回来说明服务真的卡住了。 */
export const REQUEST_TIMEOUT_MS = 30_000
/** 上传/导入的超时：几百 MB 的音频慢网也要几分钟，别在这里误杀。 */
export const UPLOAD_TIMEOUT_MS = 10 * 60_000

/** 把一个底层异常翻成人话。**超时**要说得像"服务没响应"而不是"网络错误"。 */
export function describeNetworkError(exc: unknown, timeoutMs = REQUEST_TIMEOUT_MS): Error {
  if (exc instanceof Error && exc.name === 'AbortError') {
    return new Error(
      `服务没响应（已等 ${Math.round(timeoutMs / 1000)} 秒），可能还在忙：`
      + '稍等一下重试；一直这样就把后台窗口关掉重启一次',
    )
  }
  const text = exc instanceof Error ? exc.message : String(exc)
  return new Error(`连不上后台服务（${text || '网络错误'}）：可能已经关闭或崩溃了`)
}

/**
 * 带超时的 fetch。超时用 `AbortController` 掐掉，**不会**留下一个永远 pending 的 Promise。
 */
export async function fetchWithTimeout(
  path: string,
  init: RequestInit = {},
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetch(BASE + path, { ...init, signal: controller.signal })
  } catch (exc) {
    throw describeNetworkError(exc, timeoutMs)
  } finally {
    clearTimeout(timer)
  }
}

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string' && body.detail) return body.detail
  } catch {
    /* 不是 JSON 就用兜底文案 */
  }
  return `${fallback}（HTTP ${response.status}）`
}

async function getJson<T>(path: string, timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  const response = await fetchWithTimeout(path, {}, timeoutMs)
  if (!response.ok) throw new Error(await readError(response, '请求失败'))
  return (await response.json()) as T
}

export function fetchEnv(): Promise<EnvInfo> {
  return getJson<EnvInfo>('/env')
}

/** 存活探测（G16/G19）：单独一个短超时——服务没响应时要**尽快**判定，别陪着等 30 秒。 */
export const HEALTH_TIMEOUT_MS = 5000

export async function pingHealth(): Promise<boolean> {
  try {
    const response = await fetchWithTimeout('/healthz', { method: 'GET' }, HEALTH_TIMEOUT_MS)
    return response.ok
  } catch {
    return false
  }
}

export async function listJobs(): Promise<JobSnapshot[]> {
  const body = await getJson<{ jobs: JobSnapshot[] }>('/jobs')
  return body.jobs
}

export function getJob(id: string): Promise<JobSnapshot> {
  return getJson<JobSnapshot>(`/jobs/${encodeURIComponent(id)}`)
}

export function fetchLanes(id: string): Promise<Lanes> {
  return getJson<Lanes>(`/jobs/${encodeURIComponent(id)}/lanes`)
}

export function fetchPeaks(id: string, buckets = 1200, source: 'mix' | 'stem' = 'mix'): Promise<Peaks> {
  return getJson<Peaks>(
    `/jobs/${encodeURIComponent(id)}/peaks?buckets=${buckets}&source=${source}`,
  )
}

/** 上传音频并建工程。`plan` 为空表示两个阶段都跑（扒谱 → 歌词）。 */
export function uploadJob(
  file: File,
  plan?: PhaseKey[],
  onProgress?: (fraction: number) => void,
): Promise<JobSnapshot> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('file', file, file.name)
    if (plan && plan.length) form.append('plan', plan.join(','))
    const request = new XMLHttpRequest()
    request.open('POST', `${BASE}/jobs`)
    request.responseType = 'text'
    request.timeout = UPLOAD_TIMEOUT_MS
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total)
    })
    request.addEventListener('load', () => {
      if (request.status >= 200 && request.status < 300) {
        try {
          resolve(JSON.parse(request.responseText) as JobSnapshot)
        } catch {
          reject(new Error('服务端返回的不是 JSON'))
        }
        return
      }
      let detail = `上传失败（HTTP ${request.status}）`
      try {
        const body = JSON.parse(request.responseText) as { detail?: unknown }
        if (typeof body.detail === 'string' && body.detail) detail = body.detail
      } catch {
        /* 保持兜底文案 */
      }
      reject(new Error(detail))
    })
    request.addEventListener('error', () => reject(new Error('网络错误，上传失败')))
    request.addEventListener('timeout', () => reject(new Error(
      '上传超时：文件太大或后台服务卡住了，稍后重试；一直这样就重启一次服务')))
    request.addEventListener('abort', () => reject(new Error('上传已取消')))
    request.send(form)
  })
}

async function post(path: string): Promise<JobSnapshot> {
  const response = await fetchWithTimeout(path, { method: 'POST' })
  if (!response.ok) throw new Error(await readError(response, '请求失败'))
  return (await response.json()) as JobSnapshot
}

export function cancelJob(id: string): Promise<JobSnapshot> {
  return post(`/jobs/${encodeURIComponent(id)}/cancel`)
}

export function runStage(id: string, phase: PhaseKey): Promise<JobSnapshot> {
  return post(`/jobs/${encodeURIComponent(id)}/stages/${phase}`)
}

/** 「自动提取」：扒谱 → 歌词 一条龙（故意在扒谱跑完之前就把歌词排进队列）。 */
export function runChain(id: string): Promise<JobSnapshot> {
  return post(`/jobs/${encodeURIComponent(id)}/chain`)
}

/**
 * 删除工程。**默认是软删除**（进回收站，7 天内可恢复）；`permanent=true` 才是真删。
 */
export async function deleteJob(id: string, permanent = false): Promise<void> {
  const query = permanent ? '?permanent=true' : ''
  // 注意：`fetchWithTimeout` 自己会拼 BASE，这里**不能**再写一遍
  // （写了就是 /api/api/jobs/…，会 404/405）
  const response = await fetchWithTimeout(`/jobs/${encodeURIComponent(id)}${query}`,
                                         { method: 'DELETE' })
  if (!response.ok) throw new Error(await readError(response, '删除失败'))
}

/** 回收站列表（被软删除的工程）。 */
export async function fetchTrash(): Promise<TrashEntry[]> {
  const body = await getJson<{ entries: TrashEntry[] }>('/trash')
  return body.entries
}

/** 从回收站恢复一个工程（目录挪回 output/），返回恢复后的快照。 */
export async function restoreTrash(entryId: string): Promise<JobSnapshot> {
  const response = await fetchWithTimeout(
    `/trash/${encodeURIComponent(entryId)}/restore`, { method: 'POST' })
  if (!response.ok) throw new Error(await readError(response, '恢复失败'))
  return (await response.json()) as JobSnapshot
}

/** 彻底删掉回收站里的一项（不可恢复）。 */
export async function purgeTrash(entryId: string): Promise<void> {
  const response = await fetchWithTimeout(`/trash/${encodeURIComponent(entryId)}`,
                                         { method: 'DELETE' })
  if (!response.ok) throw new Error(await readError(response, '彻底删除失败'))
}

export function fileUrl(id: string, path: string): string {
  const encoded = path.split('/').map(encodeURIComponent).join('/')
  return `${BASE}/jobs/${encodeURIComponent(id)}/files/${encoded}`
}

export function bundleUrl(id: string): string {
  return `${BASE}/jobs/${encodeURIComponent(id)}/download`
}

/**
 * 导出 SVP（Synthesizer V Studio 工程，带变速与歌词）。
 * `parts` 是下载弹窗里勾的内容，默认只有人声（`vocal`）。
 */
export function svpUrl(id: string, parts: string[] = []): string {
  const query = parts.length ? `&parts=${encodeURIComponent(parts.join(','))}` : ''
  return `${BASE}/jobs/${encodeURIComponent(id)}/download?kind=svp${query}`
}

/** 下载弹窗的数据源：哪几组能导、各多少个音、会占几轨。 */
export function fetchExports(id: string): Promise<ExportOptions> {
  return getJson<ExportOptions>(`/jobs/${encodeURIComponent(id)}/exports`)
}

export function audioUrl(id: string): string {
  return `${BASE}/jobs/${encodeURIComponent(id)}/audio`
}

export function stemAudioUrl(id: string, kind: 'vocal' = 'vocal'): string {
  return `${BASE}/jobs/${encodeURIComponent(id)}/stems/${kind}/audio`
}

/** 上传/更换一条音源（干声）。**内容不校验**：传混音原曲甚至别的歌也允许。 */
export function uploadStem(
  id: string,
  file: File,
  kind: 'vocal' = 'vocal',
  onProgress?: (fraction: number) => void,
): Promise<JobSnapshot> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('file', file, file.name)
    const request = new XMLHttpRequest()
    request.open('POST', `${BASE}/jobs/${encodeURIComponent(id)}/stems/${kind}`)
    request.responseType = 'text'
    request.timeout = UPLOAD_TIMEOUT_MS
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total)
    })
    request.addEventListener('load', () => {
      if (request.status >= 200 && request.status < 300) {
        try {
          resolve(JSON.parse(request.responseText) as JobSnapshot)
        } catch {
          reject(new Error('服务端返回的不是 JSON'))
        }
        return
      }
      let detail = `上传失败（HTTP ${request.status}）`
      try {
        const body = JSON.parse(request.responseText) as { detail?: unknown }
        if (typeof body.detail === 'string' && body.detail) detail = body.detail
      } catch {
        /* 保持兜底文案 */
      }
      reject(new Error(detail))
    })
    request.addEventListener('error', () => reject(new Error('网络错误，上传失败')))
    request.addEventListener('timeout', () => reject(new Error(
      '上传超时：文件太大或后台服务卡住了，稍后重试；一直这样就重启一次服务')))
    request.send(form)
  })
}

export async function deleteStem(id: string, kind: 'vocal' = 'vocal'): Promise<JobSnapshot> {
  const response = await fetchWithTimeout(`/jobs/${encodeURIComponent(id)}/stems/${kind}`,
                                         { method: 'DELETE' })
  if (!response.ok) throw new Error(await readError(response, '移除失败'))
  return (await response.json()) as JobSnapshot
}

/** 乐观锁冲突（G8）：改参数时工程已经被别人改过了，带着"新快照"抛出来。 */
export class StaleOptionsError extends Error {
  snapshot: JobSnapshot | null
  constructor(message: string, snapshot: JobSnapshot | null) {
    super(message)
    this.snapshot = snapshot
  }
}

/**
 * 改工程级参数（参数面板：精度 / 设备 / 范围 / 量化 / 模型 / 音源 / 语言）。
 *
 * `expectedRev` 是乐观锁（G8）：带上当前 `rev`，服务端发现版本不一致就回 409 + 当前快照，
 * 而不是把别人的改动静默盖掉。
 */
export async function setJobOptions(
  id: string,
  values: JobOptions,
  expectedRev?: number,
): Promise<JobSnapshot> {
  const body: Record<string, unknown> = { ...values }
  if (expectedRev !== undefined) body.expected_rev = expectedRev
  const response = await fetchWithTimeout(`/jobs/${encodeURIComponent(id)}/options`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    if (response.status === 409) {
      try {
        const payload = (await response.json()) as { detail?: unknown }
        const detail = payload.detail
        if (detail && typeof detail === 'object') {
          const d = detail as { message?: unknown; snapshot?: unknown }
          throw new StaleOptionsError(
            typeof d.message === 'string' ? d.message : '这页是旧的',
            (d.snapshot as JobSnapshot) ?? null,
          )
        }
      } catch (exc) {
        if (exc instanceof StaleOptionsError) throw exc
      }
    }
    throw new Error(await readError(response, '设置失败'))
  }
  return (await response.json()) as JobSnapshot
}

/** 清掉一个工程的可重算缓存（zip / SVP / 波形 / prev），释放磁盘（G11）。 */
export async function cleanupJob(id: string): Promise<{ freed: number }> {
  const response = await fetchWithTimeout(`/jobs/${encodeURIComponent(id)}/cleanup`,
                                         { method: 'POST' })
  if (!response.ok) throw new Error(await readError(response, '清理失败'))
  return (await response.json()) as { freed: number }
}

// ---------------------------------------------------------------- 批量导入
async function postJson<T>(path: string, body: unknown,
                           timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  const response = await fetchWithTimeout(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }, timeoutMs)
  if (!response.ok) throw new Error(await readError(response, '请求失败'))
  return (await response.json()) as T
}

/** 轻量列表（侧栏与批量表用：不带 LRC 全文、不带产物清单）。 */
export async function listJobRows(): Promise<JobRow[]> {
  const body = await getJson<{ jobs: JobRow[] }>('/jobs?view=rows')
  return body.jobs
}

/** 扫一个文件夹（**只读预览**，不建工程）。目录扫描可能要一两秒（探时长），超时放宽。 */
export function scanBatch(dir: string): Promise<BatchScan> {
  return postJson<BatchScan>('/batches/scan', { dir }, 60_000)
}

/**
 * 弹服务端的原生目录选择器（本机工具才有）。拿不到 tkinter / 没有桌面时后端回 501，
 * 这里把 detail 原样抛出去，界面据此提示"手动粘贴路径"。
 */
export async function browseFolder(): Promise<string | null> {
  const body = await postJson<{ dir: string | null }>('/batches/browse', {}, 120_000)
  return body.dir
}

/**
 * **拖文件夹**这条路的建工程接口：一次传一个文件，带上同一个 `batchId` 就归一批。
 *
 * 用 XHR 是为了拿单个文件的上传进度（几十 MB 的 flac 在慢盘上也要几秒）。
 */
export function uploadBatchFile(
  file: File,
  payload: {
    lyrics?: string
    batchId?: string
    title?: string
    plan?: PhaseKey[]
    expectedTotal?: number
  } = {},
  onProgress?: (fraction: number) => void,
): Promise<{ batch: JobBatch; jobs: JobRow[]; problems: string[] }> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('file', file, file.name)
    if (payload.lyrics) form.append('lyrics', payload.lyrics)
    if (payload.batchId) form.append('batch_id', payload.batchId)
    if (payload.title) form.append('title', payload.title)
    if (payload.plan?.length) form.append('plan', payload.plan.join(','))
    if (payload.expectedTotal) form.append('expected_total', String(payload.expectedTotal))
    const request = new XMLHttpRequest()
    request.open('POST', `${BASE}/batches/upload`)
    request.responseType = 'text'
    request.timeout = UPLOAD_TIMEOUT_MS
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total)
    })
    request.addEventListener('load', () => {
      if (request.status >= 200 && request.status < 300) {
        try {
          resolve(JSON.parse(request.responseText) as {
            batch: JobBatch
            jobs: JobRow[]
            problems: string[]
          })
        } catch {
          reject(new Error('服务端返回的不是 JSON'))
        }
        return
      }
      let detail = `上传失败（HTTP ${request.status}）`
      try {
        const body = JSON.parse(request.responseText) as { detail?: unknown }
        if (typeof body.detail === 'string' && body.detail) detail = body.detail
      } catch {
        /* 保持兜底文案 */
      }
      reject(new Error(detail))
    })
    request.addEventListener('error', () => reject(new Error('网络错误，上传失败')))
    request.addEventListener('timeout', () => reject(new Error(
      '上传超时：文件太大或后台服务卡住了，稍后重试；一直这样就重启一次服务')))
    request.send(form)
  })
}

/** 按文件夹建一批工程并排队（串行：一个跑完自动接下一个）。 */
export function createBatch(payload: {
  dir: string
  options?: JobOptions
  plan?: PhaseKey[]
  link?: boolean
}): Promise<{ batch: JobBatch; jobs: JobRow[]; problems: string[] }> {
  return postJson('/batches', payload, UPLOAD_TIMEOUT_MS)
}

export async function listBatches(): Promise<BatchInfo[]> {
  const body = await getJson<{ batches: BatchInfo[] }>('/batches')
  return body.batches
}

export function getBatch(id: string): Promise<BatchDetail> {
  return getJson<BatchDetail>(`/batches/${encodeURIComponent(id)}`)
}

/** 暂停派发（正在跑的那一首不打断）。 */
export async function pauseQueue(): Promise<boolean> {
  const body = await postJson<{ paused: boolean }>('/batches/pause', {})
  return body.paused
}

export async function resumeQueue(): Promise<boolean> {
  const body = await postJson<{ paused: boolean }>('/batches/resume', {})
  return body.paused
}

/** 批量下载地址：一次把多个工程打成 zip（`_manifest.json` 里记着跳过的那几首）。 */
export function batchExportUrl(ids: string[], kind: 'svp' | 'files' = 'svp',
                               parts: string[] = []): string {
  const query = new URLSearchParams({ ids: ids.join(','), kind })
  if (parts.length) query.set('parts', parts.join(','))
  return `${BASE}/exports/batch?${query.toString()}`
}

/** **继续剩余**：把这一批里没跑完的成员重新排队（已完成的绝不重跑）。 */
export async function resumeBatch(id: string): Promise<{
  resumed: number
  job_ids: string[]
  skipped: { id: string; name: string; reason: string }[]
}> {
  return postJson(`/batches/${encodeURIComponent(id)}/resume`, {})
}

/** 诊断包下载地址（G12）。不传 id = 全部工程 + env + 日志。 */
export function diagnoseUrl(id?: string): string {
  return id ? `${BASE}/jobs/${encodeURIComponent(id)}/diagnose` : `${BASE}/diagnose`
}

// ---------------------------------------------------------------- 备份 / 迁移（G22）
/** 备份包下载地址：所有工程 + 原音频 + 产物，换电脑/重装前点一下。 */
export function backupUrl(includeCache = false): string {
  return `${BASE}/backup${includeCache ? '?include_cache=true' : ''}`
}

/**
 * 导入备份包（zip）。**不覆盖已有工程**：重名会在数据目录里落成 `-2`、`-3`。
 */
export function restoreBackup(file: File): Promise<BackupSummary> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('file', file, file.name)
    const request = new XMLHttpRequest()
    request.open('POST', `${BASE}/backup/restore`)
    request.responseType = 'text'
    request.timeout = UPLOAD_TIMEOUT_MS
    request.addEventListener('load', () => {
      if (request.status >= 200 && request.status < 300) {
        try {
          resolve(JSON.parse(request.responseText) as BackupSummary)
        } catch {
          reject(new Error('服务端返回的不是 JSON'))
        }
        return
      }
      let detail = `导入失败（HTTP ${request.status}）`
      try {
        const body = JSON.parse(request.responseText) as { detail?: unknown }
        if (typeof body.detail === 'string' && body.detail) detail = body.detail
      } catch {
        /* 保持兜底文案 */
      }
      reject(new Error(detail))
    })
    request.addEventListener('error', () => reject(new Error('网络错误，导入失败')))
    request.addEventListener('timeout', () => reject(new Error(
      '导入超时：备份包太大或后台服务卡住了，稍后重试；一直这样就重启一次服务')))
    request.addEventListener('abort', () => reject(new Error('导入已取消')))
    request.send(form)
  })
}

export interface StreamHandlers {
  onEvent: (event: JobEvent) => void
  onClose: (reason: 'terminal' | 'error') => void
}

/** 订阅一个工程的进度流；返回取消订阅的函数。 */
export function subscribeJob(id: string, since: number, handlers: StreamHandlers): () => void {
  const query = since > 0 ? `?since=${since}` : ''
  const source = new EventSource(`${BASE}/jobs/${encodeURIComponent(id)}/events${query}`)
  let closed = false

  const stop = (reason: 'terminal' | 'error') => {
    if (closed) return
    closed = true
    source.close()
    handlers.onClose(reason)
  }

  source.onmessage = (message) => {
    let event: JobEvent
    try {
      event = JSON.parse(message.data) as JobEvent
    } catch {
      return
    }
    handlers.onEvent(event)
    if (event.type === 'done' || event.type === 'error' || event.type === 'cancelled') {
      stop('terminal')
    }
  }
  source.onerror = () => {
    // 拿到终态后是我们自己关的流，不算错误
    if (!closed) stop('error')
  }
  return () => stop('terminal')
}
