<script setup lang="ts">
import { computed } from 'vue'
import StageCard from '@/components/StageCard.vue'
import { FALLBACK_PARAMS, paramRows, paramsLocked } from '@/params'
import { lyricsActions, transcribeActions } from '@/stageActions'
import type { EnvInfo, JobSnapshot, PhaseInfo, StageSnapshot } from '@/types'

const props = defineProps<{
  job: JobSnapshot
  phases: PhaseInfo[]
  env: EnvInfo | null
  busy: boolean
}>()
const emit = defineEmits<{
  run: [phase: 'transcribe' | 'lyrics']
  stop: []
  auto: []
  /** 参数面板改了某一项：`key` 是 `JobOptions` 的字段名 */
  option: [key: string, value: string]
}>()

function phase(key: 'transcribe' | 'lyrics'): PhaseInfo {
  return props.phases.find((item) => item.key === key)
    ?? { key, label: key === 'lyrics' ? '歌词识别' : '扒谱', engine: key === 'lyrics' ? 'Qwen3-ASR' : 'SheetSage2' }
}

function stage(key: 'transcribe' | 'lyrics'): StageSnapshot {
  return props.job.stages?.[key] as StageSnapshot
}

const transcribed = computed(() => stage('transcribe')?.state === 'done')

// ----------------------------------------------------------------- 参数面板
// 版式与 `静态页面.html` 对齐：两行/三行标签按钮；本项目多的两处是扒谱的「量化」
// 和歌词的「语言」（下拉框，选项比原型多）。取值表来自 `/api/env`。
const specs = computed(() => props.env?.params ?? FALLBACK_PARAMS)
const rows1 = computed(() => paramRows(specs.value.transcribe, props.job.options))
const rows2 = computed(() => paramRows(specs.value.lyrics, props.job.options))

/** 某个阶段能不能改参数：服务端锁定（4G 特供）不行，它正在跑/排队也不行。 */
function lockedFor(key: 'transcribe' | 'lyrics'): boolean {
  const run = stage(key)
  const running = run?.state === 'running' || run?.state === 'queued'
  return paramsLocked(Boolean(props.env?.params_locked), running)
}

function lockNoteFor(key: 'transcribe' | 'lyrics'): string {
  if (props.env?.params_locked) return '4G 特供模式：参数已由服务端锁定'
  const label = key === 'lyrics' ? '歌词识别' : '扒谱'
  return `${label}正在跑，等它结束再改（改参数会让这一趟的结果作废）`
}

const locked1 = computed(() => lockedFor('transcribe'))
const locked2 = computed(() => lockedFor('lyrics'))
const note1 = computed(() => lockNoteFor('transcribe'))
const note2 = computed(() => lockNoteFor('lyrics'))

// ----------------------------------------------------------------- 状态
const pill1 = computed(() => {
  const run = stage('transcribe')
  if (run?.state === 'running') return { text: `运行中 ${Math.round(run.progress * 100)}%`, tone: 'run' as const }
  if (run?.state === 'failed') return { text: '失败', tone: 'bad' as const }
  if (run?.state === 'done') return { text: '已完成', tone: 'ok' as const }
  if (run?.state === 'queued') return { text: '排队中', tone: 'run' as const }
  return { text: '待开始', tone: 'idle' as const }
})

const pill2 = computed(() => {
  const run = stage('lyrics')
  if (run?.state === 'running') return { text: `识别中 ${Math.round(run.progress * 100)}%`, tone: 'run' as const }
  if (run?.state === 'failed') return { text: '失败', tone: 'bad' as const }
  if (run?.state === 'done') return { text: '已完成', tone: 'ok' as const }
  if (run?.state === 'queued') return { text: '排队中', tone: 'run' as const }
  if (run?.state === 'cancelled') return { text: '已停止 · 部分识别', tone: 'idle' as const }
  return transcribed.value
    ? { text: '未开始', tone: 'idle' as const }
    : { text: '待就绪 · 需先扒谱', tone: 'idle' as const }
})

/** 按钮的可用性规则在 `@/stageActions`（纯函数，有单测）。 */
const actions1 = computed(() => transcribeActions(props.job))
const actions2 = computed(() => lyricsActions(props.job))

function onAction(key: string, which: 'transcribe' | 'lyrics'): void {
  if (key === 'stop') emit('stop')
  else if (key === 'auto') emit('auto')
  else emit('run', which)
}

/**
 * 阶段卡下面那句话：这一趟用了哪条音源 / 下次会用哪条。
 * 音源现在是参数面板里的一项（干声（推荐）/ 混音原曲），所以这里只说事实与回退。
 */
const lyricsHint = computed(() => {
  if (!transcribed.value) return '需先完成扒谱，或点「自动提取」一键串联'
  const run = stage('lyrics')
  if (run?.state === 'running') return '正在识别'
  const used = (run?.detail?.source as string | undefined) ?? null
  const file = (run?.detail?.source_file as string | undefined) ?? null
  if (used && run?.state !== 'idle') {
    return used === 'stem' ? `本次音源：干声 ${file ?? ''}` : '本次音源：混音原曲'
  }
  const stem = props.job.stems?.vocal ?? null
  const wantStem = (props.job.options?.source ?? 'stem') === 'stem'
  if (!wantStem) return '待跑 · 将用混音原曲'
  return stem ? `待跑 · 将用干声 ${stem.name}` : '待跑 · 未上传干声，将回退到混音原曲'
})
</script>

<template>
  <div class="pipeline">
    <StageCard
      :order="1"
      :phase="phase('transcribe')"
      :stage="stage('transcribe')"
      :pill="pill1"
      :params="rows1"
      :locked="locked1"
      :lock-note="note1"
      :actions="actions1"
      :hint="'待开始'"
      @action="(key) => onAction(key, 'transcribe')"
      @option="(key, value) => emit('option', key, value)"
    />
    <div class="arrow" :class="{ on: job.stage === 'lyrics' || job.state === 'queued' }">→</div>
    <StageCard
      :order="2"
      :phase="phase('lyrics')"
      :stage="stage('lyrics')"
      :pill="pill2"
      :params="rows2"
      :locked="locked2"
      :lock-note="note2"
      :actions="actions2"
      :hint="lyricsHint"
      @action="(key) => onAction(key, 'lyrics')"
      @option="(key, value) => emit('option', key, value)"
    />
  </div>
</template>
