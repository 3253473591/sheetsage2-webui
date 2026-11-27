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

// ---------------------------------------------------------------- 拖文件夹（批量上传）
/**
 * 拖进来的整批文件 → 一首一首的「音频 + 同名歌词」。
 *
 * 这条是**上传**路线（和"填文件夹路径"的扫描路线并行存在）：浏览器只给 File 对象，
 * 拿不到绝对路径，所以只能把文件一个个传上去。配对规则和扫描那边一致：按 stem
 * 同名、`.lrc` 优先于 `.txt`、大小写不敏感。
 */

const AUDIO_EXT = /\.(wav|mp3|flac|m4a|aac|ogg|opus|oga|aif|aiff|wma|mp4|wv|ape|alac|caf)$/i
const LYRICS_EXT = /\.(lrc|txt)$/i

export interface UploadPair {
  /** 这首歌的音频 */
  audio: File
  /** 同名歌词（没有就是 null） */
  lyrics: File | null
  /** 展示名 = 音频的 stem */
  name: string
}

function stemOf(name: string): string {
  const base = name.replace(/^.*[\\/]/, '')
  const dot = base.lastIndexOf('.')
  return (dot > 0 ? base.slice(0, dot) : base).trim()
}

export function groupForUpload(files: File[]): UploadPair[] {
  const audio = files.filter((file) => AUDIO_EXT.test(file.name))
  const lyrics = new Map<string, File>()
  for (const file of files) {
    if (!LYRICS_EXT.test(file.name)) continue
    const key = stemOf(file.name).toLocaleLowerCase()
    const current = lyrics.get(key)
    // `.lrc` 优先于 `.txt`（同名两个都在时）
    if (!current || (/\.txt$/i.test(current.name) && /\.lrc$/i.test(file.name))) {
      lyrics.set(key, file)
    }
  }
  return audio.map((file) => ({
    audio: file,
    lyrics: lyrics.get(stemOf(file.name).toLocaleLowerCase()) ?? null,
    name: stemOf(file.name),
  }))
}

/**
 * 解码拖拽进来的歌词文件字节（兼容 UTF-8 / UTF-16 / GBK / Big5 / Shift-JIS / EUC-KR）。
 *
 * 浏览器原生的 `File.text()` 只按 UTF-8 解码，Windows 上常见的 GBK `.lrc`/`.txt`
 * 会解成乱码；这里与后端 `asr.read_lrc` 保持同一套候选编码回退。
 */
export function decodeLyricsBytes(bytes: Uint8Array): string {
  if (bytes.length >= 2) {
    if (bytes[0] === 0xff && bytes[1] === 0xfe) {
      return new TextDecoder('utf-16le').decode(bytes)
    }
    if (bytes[0] === 0xfe && bytes[1] === 0xff) {
      return new TextDecoder('utf-16be').decode(bytes)
    }
  }
  for (const encoding of ['utf-8', 'gb18030', 'gbk', 'big5', 'shift-jis', 'euc-kr']) {
    try {
      return new TextDecoder(encoding, { fatal: true }).decode(bytes)
    } catch {
      continue
    }
  }
  return new TextDecoder('utf-8').decode(bytes)
}

export async function readLyricsFile(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  return decodeLyricsBytes(new Uint8Array(buffer))
}

/** `FileSystemEntry` 的最小形状（浏览器给的是只读对象，测试里用假的即可）。 */
export interface DirEntryLike {
  isFile: boolean
  isDirectory: boolean
  name: string
  file?: (ok: (file: File) => void, fail?: (error: unknown) => void) => void
  createReader?: () => {
    readEntries: (ok: (entries: DirEntryLike[]) => void, fail?: (error: unknown) => void) => void
  }
}

function fileOf(entry: DirEntryLike): Promise<File> {
  return new Promise((resolve, reject) => {
    if (!entry.file) {
      reject(new Error(`${entry.name} 读不出来`))
      return
    }
    entry.file(resolve, reject)
  })
}

async function filesInDirectory(entry: DirEntryLike): Promise<File[]> {
  const reader = entry.createReader?.()
  if (!reader) return []
  const collected: File[] = []
  // `readEntries` 一次只给一批（Chrome 是 100 个），要一直读到空
  for (;;) {
    const batch = await new Promise<DirEntryLike[]>((resolve, reject) => {
      reader.readEntries(resolve, reject)
    })
    if (!batch.length) break
    collected.push(...await collectFiles(batch))
  }
  return collected
}

/** 递归把（可能是文件夹的）条目展开成文件列表。 */
export async function collectFiles(entries: DirEntryLike[]): Promise<File[]> {
  const out: File[] = []
  for (const entry of entries) {
    if (entry.isFile) out.push(await fileOf(entry))
    else if (entry.isDirectory) out.push(...await filesInDirectory(entry))
  }
  return out
}

/**
 * 一次拖拽 → 文件列表 + 顶层文件夹名（批量命名要用它）。
 *
 * **文件夹拖拽**在 Chrome/Edge 里只能从 `items[].webkitGetAsEntry()` 拿到目录树；
 * 拿不到（浏览器不支持 / 拖的是普通文件）就返回 `null`，调用方回退到 `dt.files`。
 */
export interface DroppedFolder {
  files: File[]
  /** 拖的是单个文件夹时的名字（用来给批次起名）；散文件是空串 */
  folderName: string
}

/** 从顶层条目里认出"这是一个文件夹"的名字（纯函数，好测）。 */
export function folderNameOfEntries(entries: DirEntryLike[]): string {
  const dirs = entries.filter((entry) => entry.isDirectory)
  return entries.length === 1 && dirs.length === 1 ? dirs[0]!.name : ''
}

export async function readDroppedFiles(transfer: DataTransfer | null): Promise<DroppedFolder | null> {
  if (!transfer?.items?.length) return null
  const entries: DirEntryLike[] = []
  for (const item of [...transfer.items]) {
    const entry = (item as unknown as {
      webkitGetAsEntry?: () => DirEntryLike | null
    }).webkitGetAsEntry?.()
    if (entry) entries.push(entry)
  }
  if (!entries.length) return null
  return {
    files: await collectFiles(entries),
    folderName: folderNameOfEntries(entries),
  }
}
