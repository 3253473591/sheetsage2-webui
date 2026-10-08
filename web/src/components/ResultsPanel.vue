<script setup lang="ts">
/** 结果区：概要数字 + 卷帘 + 三个明细页（和弦 / 歌词 / 音符）。 */
import { computed, ref } from 'vue'
import LaneView from '@/components/LaneView.vue'
import { formatClock, formatKey, formatNumber } from '@/format'
import { useAudioPlayer } from '@/composables/useAudio'
import type { JobSnapshot, Lanes, LyricLine } from '@/types'

const props = defineProps<{
  job: JobSnapshot
  lanes: Lanes | null
  streamed: LyricLine[]
  loading: boolean
}>()

const tab = ref<'chords' | 'lyrics' | 'notes'>('chords')
const player = useAudioPlayer()

const highlights = computed(() => props.job.highlights)
const lyricsStage = computed(() => props.job.stages?.lyrics)
const lyricsRunning = computed(() => lyricsStage.value?.state === 'running')
const lyricLines = computed<LyricLine[]>(() => {
  const stored = props.lanes?.lyric_lines ?? []
  return stored.length ? stored : props.streamed
})
const duration = computed(
  () => props.lanes?.duration_seconds || highlights.value?.duration_seconds || 0,
)
const keys = computed(() => {
  const list = props.lanes?.keys ?? highlights.value?.keys ?? []
  return list.length ? list.map(formatKey).join(' / ') : '—'
})
const tempoText = computed(() => {
  const data = highlights.value
  if (!data || data.bpm === null) return '—'
  return `${formatNumber(data.bpm, 3)}`
})
const tempoNote = computed(() => {
  const data = highlights.value
  if (!data) return ''
  return data.tempo_constant ? '恒速' : `变速 · ${data.tempo_segments} 段`
})

/** 每个段落里挂了哪些和弦——原型里"和弦"页的排版。 */
const chordSections = computed(() => {
  const lanes = props.lanes
  if (!lanes) return []
  return lanes.sections.map((section) => ({
    label: section.label,
    start: section.start,
    end: section.end,
    chords: lanes.chords.filter((chord) => chord.start >= section.start - 0.05
      && chord.start < section.end - 0.05),
  }))
})

const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
function pitchName(pitch: number): string {
  if (!pitch) return '—'
  const index = ((pitch % 12) + 12) % 12
  return `${NOTE_NAMES[index]}${Math.floor(pitch / 12) - 1}`
}

const noteRows = computed(() =>
  (props.lanes?.vocal_notes ?? []).map((note) => ({
    ...note,
    name: pitchName(note.pitch),
  })),
)
</script>

<template>
  <div class="card results">
    <div class="rs-head">
      <div class="rs-title">结果</div>
      <div class="kv">
        <span>调性<b>{{ keys }}</b></span>
        <span>速度<b>{{ tempoText }}</b><em>{{ tempoNote }}</em></span>
        <span>拍号<b>{{ highlights?.meter ?? '—' }}</b>
          <em>弱起 {{ formatNumber(highlights?.pickup_seconds ?? 0, 2) }}s</em></span>
        <span>人声音符<b>{{ highlights?.notes.vocal ?? 0 }}</b></span>
        <span>和弦<b>{{ highlights?.chord_count ?? 0 }}</b>
          <em>{{ highlights?.chord_kinds ?? 0 }} 种</em></span>
        <span>歌词<b>{{ lyricLines.length }}</b>
          <em>行{{ lyricsRunning ? ' · 识别中' : lyricsStage?.state === 'done' ? '' : ' · 未识别' }}</em></span>
      </div>
      <div class="tabs">
        <button class="tab" :class="{ active: tab === 'chords' }" @click="tab = 'chords'">和弦</button>
        <button class="tab" :class="{ active: tab === 'lyrics' }" @click="tab = 'lyrics'">歌词</button>
        <button class="tab" :class="{ active: tab === 'notes' }" @click="tab = 'notes'">音符</button>
      </div>
    </div>

    <LaneView
      v-if="duration"
      :duration="duration"
      :measures="lanes?.measures ?? []"
      :sections="lanes?.sections ?? []"
      :chords="lanes?.chords ?? []"
      :lyric-lines="lyricLines"
      :lyrics-running="lyricsRunning"
      @seek="player.seekFraction"
    />
    <div v-else class="detail-empty">
      {{ loading ? '正在读取工程…' : '还没有工程：先跑「扒谱」，段落 / 和弦 / 卷帘就会有内容。' }}
    </div>

    <div v-if="job.warnings.length" class="warn-box">
      <b>需要留意</b>
      <div v-for="(warning, index) in job.warnings" :key="index">· {{ warning }}</div>
    </div>

    <div v-show="tab === 'chords'" class="detail">
      <div v-if="!chordSections.length" class="detail-empty">还没有和弦。</div>
      <div v-for="(section, index) in chordSections" :key="index" class="section-row">
        <div class="section-header">
          <span>{{ section.label }} {{ formatClock(section.start) }}~{{ formatClock(section.end) }}</span>
          <span>{{ section.chords.length }} 个和弦</span>
        </div>
        <div class="chord-tags">
          <div v-for="(chord, order) in section.chords" :key="order" class="chord-tag">
            <span class="time">{{ formatClock(chord.start) }}</span><strong>{{ chord.label }}</strong>
          </div>
        </div>
      </div>
    </div>

    <div v-show="tab === 'lyrics'" class="detail">
      <div v-if="!lyricLines.length && !lyricsRunning" class="detail-empty">
        尚未识别 · 点阶段 2 的「开始识别歌词」或「自动提取」
      </div>
      <div v-for="(line, index) in lyricLines" :key="index" class="ly-row">
        <span class="ly-time">{{ formatClock(line.start) }} ~ {{ formatClock(line.end) }}</span>
        <span>{{ line.text }}</span>
      </div>
      <div v-if="lyricsRunning" class="ly-row pending">
        <span class="ly-time">…</span><span>░░ 识别中 ░░</span>
      </div>
    </div>

    <div v-show="tab === 'notes'" class="detail">
      <div v-if="!noteRows.length" class="detail-empty">还没有人声音符。</div>
      <div v-else class="notes-strip">
        <span v-for="(note, index) in noteRows" :key="index">
          <span class="nk">{{ formatClock(note.start) }} {{ note.name }}</span>
          {{ note.lyric || '·' }}
        </span>
      </div>
    </div>
  </div>
</template>
