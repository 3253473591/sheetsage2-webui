/** 格式化：时间 / 时长 / 字节数。 */

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 秒 -> `1:24.51`（原型的写法，两位小数）。 */
export function formatTime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const total = Math.max(0, seconds)
  const minutes = Math.floor(total / 60)
  const rest = total - minutes * 60
  return `${minutes}:${rest.toFixed(2).padStart(5, '0')}`
}

/** 秒 -> `1:24`。 */
export function formatClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const total = Math.round(Math.max(0, seconds))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

export function formatNumber(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return String(Number(value.toFixed(digits)))
}

export function formatStamp(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return '—'
  const date = new Date(epochSeconds * 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function formatShort(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return '—'
  const date = new Date(epochSeconds * 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:` +
    pad(date.getMinutes())
}

/** 调性标签：`F:major` -> `F 大调`。 */
export function formatKey(label: string | null | undefined): string {
  if (!label) return '—'
  const [tonic, mode = 'major'] = label.split(':')
  const name = mode.startsWith('min') ? '小调' : '大调'
  return `${tonic} ${name}`
}
