<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import WaveBars from '@/components/WaveBars.vue'
import { audioUrl, fileUrl, stemAudioUrl } from '@/api'
import { formatClock } from '@/format'
import { useAudioPlayer } from '@/composables/useAudio'
import type { JobSnapshot, Lanes, Peaks } from '@/types'

const props = defineProps<{
  job: JobSnapshot
  peaks: Peaks | null
  stemPeaks: Peaks | null
  lanes: Lanes | null
}>()
const emit = defineEmits<{ stem: [file: File]; dropStem: [] }>()

const player = useAudioPlayer()
const stemInput = ref<HTMLInputElement | null>(null)
const ACCEPT = '.wav,.mp3,.flac,.m4a,.aac,.ogg,.opus,.aif,.aiff,.wma,.mp4'

const duration = computed(
  () => props.job.highlights?.duration_seconds ?? props.lanes?.duration_seconds ?? 0,
)
const progress = computed(() =>
  player.duration.value ? player.currentTime.value / player.duration.value : 0,
)
const stem = computed(() => props.job.stems?.vocal ?? null)
const mixUrl = computed(() => audioUrl(props.job.id))
const stemUrl = computed(() => (stem.value ? stemAudioUrl(props.job.id, 'vocal') : null))
/** `output/` 里载入的已有产物没有原音频：波形与播放不可用（卷帘和下载照常）。 */
const hasAudio = computed(() => !props.job.imported && Boolean(props.job.audio))

const playing = computed(() => player.playing.value)
function playingHere(url: string | null): boolean {
  return playing.value && url !== null && player.source.value === url
}

function toggle(url: string | null): void {
  if (!url || !hasAudio.value) return
  // 换源时 load() 会重置时间轴；同一源再点就是暂停/继续
  player.load(url)
  player.toggle()
}

function pickStem(event: Event): void {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (file) emit('stem', file)
  target.value = ''
}

/** 人声旋律的"活跃度"包络：把音符铺到固定桶里，取最大 1 归一。 */
const vocalBars = computed<number[]>(() => {
  const notes = props.lanes?.vocal_notes ?? []
  const total = duration.value
  if (!notes.length || !total) return []
  const buckets = 240
  const values = new Array<number>(buckets).fill(0)
  const span = total / buckets
  for (const note of notes) {
    const from = Math.max(0, Math.floor(note.start / span))
    const to = Math.min(buckets - 1, Math.floor(Math.max(note.end, note.start + 0.02) / span))
    for (let index = from; index <= to; index += 1) values[index] = (values[index] ?? 0) + 1
  }
  const peak = Math.max(...values, 1)
  return values.map((value) => value / peak)
})

const midiPath = computed(() =>
  props.job.files.find((file) => file.path === 'melody_vocal.mid')?.path ?? null,
)

onMounted(() => {
  if (hasAudio.value) player.load(mixUrl.value)
})
watch(() => props.job.id, () => {
  if (hasAudio.value) player.load(mixUrl.value)
})
</script>

<template>
  <div class="card tracks">
    <!-- 1. 混音原曲：扒谱用 -->
    <div class="track">
      <button
        class="tr-play"
        :class="{ on: playingHere(mixUrl) }"
        :disabled="!hasAudio"
        :title="hasAudio ? (playingHere(mixUrl) ? '暂停' : '播放')
                         : '这个工程是从 output/ 载入的产物，没有原音频'"
        @click="toggle(mixUrl)"
      >
        {{ playingHere(mixUrl) ? '❚❚' : '▶' }}
      </button>
      <div class="tr-name">
        混音原曲
        <small v-if="hasAudio">{{ job.audio }} · 扒谱用</small>
        <small v-else>没有原音频 · 载入的已有产物</small>
      </div>
      <WaveBars
        :values="peaks?.peaks ?? []"
        :progress="playingHere(mixUrl) ? progress : 0"
        interactive
        @seek="player.seekFraction"
      />
      <div class="tr-act">{{ formatClock(duration) }}</div>
    </div>

    <!-- 2. 干声：可选音源。有就用它跑歌词，没有就自动回退到混音原曲 -->
    <div class="track">
      <button
        v-if="stemUrl"
        class="tr-play"
        :class="{ on: playingHere(stemUrl) }"
        :title="playingHere(stemUrl) ? '暂停' : '试听干声'"
        @click="toggle(stemUrl)"
      >
        {{ playingHere(stemUrl) ? '❚❚' : '▶' }}
      </button>
      <button v-else class="tr-play" disabled title="还没上传干声">—</button>
      <div class="tr-name">
        干声<small v-if="stem">{{ stem.name }} · 歌词用</small>
        <small v-else>未提供 · 歌词识别自动用混音原曲</small>
      </div>
      <WaveBars
        v-if="stemUrl"
        :values="stemPeaks?.peaks ?? []"
        :progress="playingHere(stemUrl) ? progress : 0"
        color="#a5b4fc"
        played-color="#6366f1"
        interactive
        @seek="player.seekFraction"
      />
      <div v-else class="tr-empty">把干声拖到这里，或点右侧上传（可选）</div>
      <div class="tr-act">
        <template v-if="stem">
          {{ formatClock(duration) }}
          <a href="#" @click.prevent="stemInput?.click()">更换</a>
          <a href="#" style="color: var(--danger)" @click.prevent="emit('dropStem')">移除</a>
        </template>
        <template v-else>
          <a href="#" @click.prevent="stemInput?.click()">上传干声</a>
        </template>
      </div>
      <input ref="stemInput" type="file" :accept="ACCEPT" hidden @change="pickStem" />
    </div>

    <!-- 3. 模型扒出来的人声主旋律 -->
    <div class="track">
      <button class="tr-play" disabled title="旋律轨不可播放">♪</button>
      <div class="tr-name">
        人声主旋律<small>模型提取 · {{ lanes?.vocal_notes.length ?? 0 }} 个音</small>
      </div>
      <WaveBars :values="vocalBars" :progress="progress" color="#a5b4fc" played-color="#6366f1" />
      <div class="tr-act">
        {{ formatClock(duration) }}
        <a v-if="midiPath" :href="fileUrl(job.id, midiPath)" download>MIDI</a>
      </div>
    </div>
  </div>
</template>
