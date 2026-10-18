/**
 * 与后端 `webui/app.py` 的 JSON 契约。
 *
 * 后端一律用 snake_case，前端不转换——少一层映射就少一个能出错的地方。
 * 这些类型和后端 `jobs.py` / `runner.py` / `lanes.py` 的 `as_dict()` 一一对应。
 */

export type PhaseKey = 'transcribe' | 'lyrics'
/**
 * 任务状态。`draft` 是门禁带来的：音频传上来了、工程建好了，但**还没排任何阶段**——
 * 参数面板这时才露出来，用户点「开始扒谱」才进队列。
 */
export type JobState = 'draft' | 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
export type StageState =
  | 'idle'
  | 'queued'
  | 'running'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'skipped'

export const TERMINAL_STATES: ReadonlySet<JobState> = new Set<JobState>([
  'done',
  'failed',
  'cancelled',
])

export interface PhaseInfo {
  key: PhaseKey
  label: string
  engine: string
}

/** 歌词识别语言。`value` 空串 = 自动（让模型自己判）。 */
export interface LanguageChoice {
  value: string
  label: string
}

/** 参数面板上的一个可选值。`hint` 是鼠标悬停的说明（按钮太长会换行，就不写在按钮上）。 */
export interface ParamChoice {
  value: string
  label: string
  hint?: string
}

/** 参数面板上的一行（来自后端 `jobs.PARAM_SPECS`，是取值的唯一真源）。 */
export interface ParamGroup {
  /** `JobOptions` 里的字段名 */
  key: string
  label: string
  default: string
  /** 画成标签按钮（默认）还是下拉框——「语言」用下拉框，选项比原型多。 */
  widget?: 'buttons' | 'select'
  options: ParamChoice[]
}

/** 两个阶段各自的参数行。 */
export interface ParamSpecs {
  transcribe: ParamGroup[]
  lyrics: ParamGroup[]
}

/**
 * 工程级参数。**这就是页面上放开给用户的东西**：
 * 扒谱（精度 / 设备 / 范围 / 量化）+ 歌词（模型 / 音源 / 语言）。
 */
export interface JobOptions {
  dtype?: string
  device?: string
  range?: string
  quantize?: string
  asr_model?: string
  source?: string
  /** 段落：`vocal` = 只跑唱段（跳过前奏/间奏/尾奏） / `all` = 全部段落。 */
  skip_instrumental?: string
  language?: string
}

/** 启动预检（G15）里的一项：`error`/`warn`/`info` 会在顶栏画横幅。 */
export interface PreflightCheck {
  key: string
  level: 'ok' | 'warn' | 'error' | 'info'
  title: string
  detail?: string
}

export interface EnvInfo {
  version: string
  model: string
  device: string
  model_loaded: boolean
  asr: { model: string | null; loaded: boolean | null; device: string | null }
  ffmpeg: string | null
  data_root: string
  static_ready: boolean
  max_upload_mb: number
  queue: { pending: number; running: string | null }
  /** 数据根所在磁盘的剩余/总量（G11：顶栏显示"剩余 12.3 GB"）。 */
  disk: { free: number; total: number; used: number }
  /** 启动预检结果（G15）。 */
  preflight: PreflightCheck[]
  phases: PhaseInfo[]
  languages: LanguageChoice[]
  params: ParamSpecs
  /** 「4G 特供」模式：服务端锁死参数，面板只读（`PATCH /options` 也会 409）。 */
  params_locked: boolean
}

export interface JobFile {
  path: string
  size: number
  kind: string
}

/** 单独上传的音源。目前只有 `vocal`（干声）：有就用它，没有就自动回退到混音原曲。 */
export interface StemInfo {
  kind: 'vocal'
  name: string
  path: string
  size: number
  created_at: number
}

/** 下载弹窗里的一个可勾选项（`GET /api/jobs/{id}/exports`）。 */
export interface ExportPart {
  key: 'vocal' | 'instrumental' | 'chords'
  label: string
  notes: number
  tracks: number
  /** 默认勾不勾：只有人声是 true。 */
  default: boolean
  /** 这个工程里有没有内容（没有的置灰）。 */
  available: boolean
  detail?: string
}

/** 导出弹窗的全部数据。 */
export interface ExportOptions {
  name: string
  parts: ExportPart[]
  defaults: string[]
  svp: { available: boolean; version: string; hint: string }
}

/** 耗时预估（G20）：`stages.{phase}.eta`。`null` = 还不知道音频多长，界面就不显示。 */
export interface StageEta {
  /** 中位估计（秒）：界面用它算"预计还需多久" */
  seconds: number
  /** 区间下界 / 上界（秒） */
  low: number
  high: number
  /** 现成的一句话：「预计 2~4 分钟」 */
  label: string
  /** 按什么口径估的：`cuda` / `cpu` */
  device: 'cuda' | 'cpu' | string
  /** 参与计算的音频秒数（选了"前 30 秒"时就已经截过） */
  audio_seconds: number
}

export interface StageSnapshot {
  key: PhaseKey
  label: string
  state: StageState
  progress: number
  step: string
  message: string
  started_at: number | null
  finished_at: number | null
  /** 最后一次"有进展"的时刻：看门狗用它判卡死，界面用它显示"上次进展 xx 秒前" */
  last_advance_at?: number | null
  elapsed_seconds: number
  /** 人话（横条/阶段卡上显示的那一句），不是异常原文 */
  error: string | null
  /** 稳定的错误码（`E_DISK_FULL` / `E_STALLED` …），前端按码换图标/文案 */
  error_code?: string | null
  /** 下一步该干什么（一句话） */
  error_action?: string | null
  /** 技术原文（折叠在「详情」里） */
  error_detail?: string | null
  /** 耗时预估（G20）：还没跑完时才有，`null` = 不知道（缺音频时长） */
  eta?: StageEta | null
  detail: Record<string, unknown>
}

/** 产物自检结果（阶段详情里的 `detail.artifacts`，G4 完成门禁）。 */
export interface ArtifactCheck {
  ok: boolean
  checked: number
  total: number
  missing: string[]
  problems: string[]
}

/** 回收站里的一项（`GET /api/trash`）。 */
export interface TrashEntry {
  /** 回收站里的目录名，恢复/彻底删除都用它 */
  id: string
  name: string
  job_id: string | null
  deleted_at: number
  size: number
  files: number
}

/** 导入备份包的结果（`POST /api/backup/restore`，G22）。 */
export interface BackupSummary {
  restored: { id: string; name: string; files: number }[]
  skipped: { name: string; reason: string }[]
  count: number
  backup: {
    created_at: number | null
    app_version: string | null
    projects: number
  }
}

export interface Highlights {
  duration_seconds: number
  meter: string | null
  pickup_seconds: number
  bpm: number | null
  bpm_source: string | null
  bpm_confident: boolean
  tempo_constant: boolean
  tempo_segments: number
  stable_tempos: number[]
  halftime_hint: number | null
  keys: string[]
  notes: { melody: number; vocal: number; instrumental: number; events: number }
  quantize: {
    enabled: boolean
    grid_ticks: number | null
    grid_note: string
    moved: number
    moved_ratio: number
    max_shift_seconds: number | null
    failed_tracks: number
  }
  // 旋律音高后期处理（模型旋律整体高一个八度；和弦轨不动）
  pitch: {
    enabled: boolean
    semitones: number
    label: string
    abc: string
  }
  chord_count: number
  chord_kinds: number
  section_count: number
  elapsed_seconds: number
  peak_gpu_mib: number
  device: string
}

export interface JobSnapshot {
  id: string
  name: string
  audio: string
  rev: number
  /** 产物版本戳：每完成一轮阶段 +1，所有派生产物（zip/SVP/波形）的缓存都跟着它失效 */
  artifacts_rev?: number
  state: JobState
  stage: PhaseKey | null
  stages: Record<PhaseKey, StageSnapshot>
  /** 当前阶段的扁平视图（列表/顶栏方便用） */
  stage_label: string
  step: string
  progress: number
  /** 当前阶段的耗时预估（G20）：没在跑就是 null */
  eta?: StageEta | null
  message: string
  created_at: number
  elapsed_seconds: number
  error: string | null
  warnings: string[]
  files: JobFile[]
  stems: Partial<Record<'vocal', StemInfo>>
  /** 上传体检（G10）结果：时长/采样率/是否静音等。 */
  audio_info?: Record<string, unknown>
  /** 内容指纹（G10 去重用）。 */
  audio_fingerprint?: string | null
  /** 内容指纹命中了已有工程时，指向那个工程 id（G10 去重提示）。 */
  duplicate_of?: string | null
  /** 工程级参数（页面上可调的那些，见 `ParamSpecs`）。 */
  options: JobOptions
  highlights: Highlights | null
  decisions: Record<string, unknown> | null
  summary: Record<string, unknown> | null
  events: number
  /**
   * `true` = 这个工程是从 `output/` 里**载入的已有产物**（命令行跑出来的那种）：
   * 能看卷帘 / 下载 / 导 SVP，但没有原音频、也不能重跑阶段。
   */
  imported?: boolean
}

/** SSE 事件。`seq` 单调递增，用来在断线重连时续传。 */
export interface JobEvent {
  seq: number
  type:
    | 'phase'
    | 'step'
    | 'progress'
    | 'lyrics'
    | 'log'
    | 'phase_done'
    | 'phase_error'
    | 'phase_cancelled'
    | 'done'
    | 'error'
    | 'cancelled'
  time: number
  data: {
    phase?: PhaseKey | null
    label?: string
    state?: string
    step?: string
    progress?: number
    message?: string
    detail?: Record<string, unknown>
    /** `phase` 事件带上的耗时预估（G20） */
    eta?: StageEta | null
    /** 失败事件带的三段（`error` 是人话，`detail` 是技术原文） */
    code?: string
    action?: string
    // lyrics 事件带的内容
    text?: string
    chars?: LyricChar[]
    lines?: LyricLine[]
    slices?: number
    slices_total?: number
  }
}

export interface LyricChar {
  text: string
  start: number
  end: number
}

export interface LyricLine {
  text: string
  start: number
  end: number
}

export interface Span {
  start: number
  end: number
  label: string
}

export interface Measure {
  start: number
  end: number
  bpm: number | null
  numerator: number | null
  denominator: number | null
  is_pickup: boolean
}

export interface VocalNote {
  start: number
  end: number
  pitch: number
  lyric: string
}

export interface Lanes {
  duration_seconds: number
  pickup_seconds: number
  measures: Measure[]
  sections: Span[]
  chords: Span[]
  vocal_notes: VocalNote[]
  lyric_lines: LyricLine[]
  lyrics_ready: boolean
  keys: string[]
}

export interface Peaks {
  buckets: number
  sample_rate: number
  duration: number
  peaks: number[]
}

export const STATE_TEXT: Record<JobState, string> = {
  draft: '待开始',
  queued: '排队中',
  running: '进行中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

/** 侧栏状态小标签（原型的 `扒谱 ✓` / `歌词 45%` / `歌词 —`）。 */
export function stageBadge(stage: StageSnapshot | undefined): {
  text: string
  tone: 'ok' | 'run' | 'idle' | 'bad'
} {
  if (!stage) return { text: '—', tone: 'idle' }
  const short = stage.label.replace('识别', '')
  switch (stage.state) {
    case 'done':
      return { text: `${short} ✓`, tone: 'ok' }
    case 'running':
    case 'queued':
      return { text: `${short} ${Math.round(stage.progress * 100)}%`, tone: 'run' }
    case 'failed':
      return { text: `${short} ✗`, tone: 'bad' }
    case 'cancelled':
      return { text: `${short} 已停`, tone: 'idle' }
    default:
      return { text: `${short} —`, tone: 'idle' }
  }
}
