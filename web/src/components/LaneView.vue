<script setup lang="ts">
/**
 * 卷帘：小节表 / 段落 / 和弦 / 歌词四条轨道。
 *
 * 位置全是**秒**（后端 `lanes` 接口算好的），这里只按 `总时长 -> 像素宽` 线性缩放。
 * 秒↔tick 的换算不在这层做——唯一真源是小节表，抄一份到 TS 就是埋第二个真相。
 */
import { computed } from 'vue'
import Playhead from '@/components/Playhead.vue'
import { formatClock } from '@/format'
import type { LyricLine, Measure, Span } from '@/types'

const props = defineProps<{
  duration: number
  measures: Measure[]
  sections: Span[]
  chords: Span[]
  lyricLines: LyricLine[]
  lyricsRunning: boolean
}>()
const emit = defineEmits<{ seek: [fraction: number] }>()

/** 每秒多少像素。5 分钟的歌约 1800px，横向滚动看全曲。 */
const PX_PER_SECOND = 6
const MIN_WIDTH = 900
const SECTION_COLORS = ['#10b981', '#6366f1', '#f43f5e', '#f59e0b', '#0ea5e9', '#8b5cf6']

const width = computed(() => Math.max(MIN_WIDTH, Math.round(props.duration * PX_PER_SECOND)))
const ratio = computed(() => (props.duration > 0 ? width.value / props.duration : 0))

const ticks = computed(() => {
  const step = props.duration > 300 ? 30 : props.duration > 120 ? 10 : 5
  const out: { seconds: number; left: number; text: string }[] = []
  for (let seconds = 0; seconds <= props.duration; seconds += step) {
    out.push({ seconds, left: seconds * ratio.value, text: formatClock(seconds) })
  }
  return out
})

/** 小节线只在不多的时候画，否则糊成一片。 */
const barLines = computed(() =>
  props.measures.length <= 400
    ? props.measures.slice(1).map((measure) => measure.start * ratio.value)
    : [],
)

function left(seconds: number): string {
  return `${seconds * ratio.value}px`
}
function blockWidth(start: number, end: number): string {
  return `${Math.max(2, (end - start) * ratio.value - 2)}px`
}
function sectionColor(index: number): string {
  return SECTION_COLORS[index % SECTION_COLORS.length] ?? SECTION_COLORS[0]!
}

function seek(event: MouseEvent): void {
  const element = event.currentTarget as HTMLElement
  const rect = element.getBoundingClientRect()
  emit('seek', Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)))
}
</script>

<template>
  <div class="lanes">
    <div class="lane-labels">
      <div class="h-ruler" />
      <div class="h-sec">段落</div>
      <div class="h-chord">和弦</div>
      <div class="h-lyric">歌词</div>
    </div>
    <div class="lane-scroll">
      <div class="lane-inner" :style="{ width: `${width}px` }" @click="seek">
        <div class="row h-ruler">
          <span v-for="tick in ticks" :key="tick.seconds" class="tick" :style="{ left: `${tick.left}px` }">
            {{ tick.text }}
          </span>
        </div>
        <div class="row h-sec">
          <span
            v-for="line in barLines"
            :key="`bar-${line}`"
            class="bar-line"
            :style="{ left: `${line}px` }"
          />
          <div
            v-for="(section, index) in sections"
            :key="`sec-${index}`"
            class="blk sec"
            :style="{
              left: left(section.start),
              width: blockWidth(section.start, section.end),
              background: sectionColor(index),
            }"
            :title="`${section.label} ${formatClock(section.start)}~${formatClock(section.end)}`"
          >
            {{ section.label }}
          </div>
        </div>
        <div class="row h-chord">
          <div
            v-for="(chord, index) in chords"
            :key="`chord-${index}`"
            class="blk chord"
            :style="{ left: left(chord.start), width: blockWidth(chord.start, chord.end) }"
            :title="`${chord.label} @ ${formatClock(chord.start)}`"
          >
            {{ chord.label }}
          </div>
        </div>
        <div class="row h-lyric">
          <div
            v-for="(line, index) in lyricLines"
            :key="`lyric-${index}`"
            class="blk lyric"
            :style="{ left: left(line.start), width: blockWidth(line.start, line.end) }"
            :title="line.text"
          >
            {{ line.text }}
          </div>
          <div
            v-if="lyricsRunning"
            class="blk lyric pending"
            :style="{
              left: left(lyricLines.length ? lyricLines[lyricLines.length - 1]!.end : 0),
              width: blockWidth(lyricLines.length ? lyricLines[lyricLines.length - 1]!.end : 0, duration),
            }"
          >
            识别中…
          </div>
        </div>
        <Playhead :ratio="ratio" />
      </div>
    </div>
  </div>
</template>
