<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppSidebar from '@/components/AppSidebar.vue'
import LogDrawer from '@/components/LogDrawer.vue'
import PipelineStages from '@/components/PipelineStages.vue'
import ProjectHead from '@/components/ProjectHead.vue'
import ResultsPanel from '@/components/ResultsPanel.vue'
import TrackList from '@/components/TrackList.vue'
import { useAudioPlayer } from '@/composables/useAudio'
import { useStudio } from '@/composables/useStudio'
import type { PhaseKey } from '@/types'

const {
  env,
  jobs,
  active,
  activeId,
  lanes,
  peaks,
  stemPeaks,
  streamedLyrics,
  logs,
  error,
  uploadProgress,
  lanesLoading,
  init,
  select,
  create,
  runStage,
  runChain,
  cancel,
  attachStem,
  detachStem,
  setOptions,
  remove,
  reset,
  dismissError,
} = useStudio()

const player = useAudioPlayer()
const theme = ref<'light' | 'dark'>('light')
const dragOver = ref(false)
const PHASES: PhaseKey[] = ['transcribe', 'lyrics']

const phases = computed(() => env.value?.phases ?? [
  { key: 'transcribe' as const, label: '扒谱', engine: 'SheetSage2' },
  { key: 'lyrics' as const, label: '歌词识别', engine: 'Qwen3-ASR' },
])

const busy = computed(() => uploadProgress.value !== null)
const anyRunning = computed(() =>
  jobs.value.some((job) => job.state === 'running' || job.state === 'queued'),
)

function stopAll(): void {
  void cancel()
}

function auto(): void {
  // 「自动提取」= 扒谱 → 歌词 一条龙（原型的排队链路）；走后端 /chain，不是两次 runStage
  void runChain()
}

let dragDepth = 0
function onDragEnter(event: DragEvent): void {
  if (!event.dataTransfer?.types.includes('Files')) return
  dragDepth += 1
  dragOver.value = true
}
function onDragLeave(): void {
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) dragOver.value = false
}
function onDrop(event: DragEvent): void {
  dragDepth = 0
  dragOver.value = false
  // 一次拖两个也行：第一个当混音原曲、第二个当干声（认名字见 @/drop）
  const files = [...(event.dataTransfer?.files ?? [])]
  if (files.length) void create(files)
}

onMounted(async () => {
  const saved = window.localStorage.getItem('sheetsage-theme')
  theme.value = saved === 'dark' ? 'dark' : 'light'
  document.documentElement.dataset.theme = theme.value
  await init()
})

onUnmounted(() => {
  player.pause()
})

function toggleTheme(): void {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
  document.documentElement.dataset.theme = theme.value
  window.localStorage.setItem('sheetsage-theme', theme.value)
}
</script>

<template>
  <div
    class="shell"
    style="height: 100%; display: flex; flex-direction: column"
    @dragenter.prevent="onDragEnter"
    @dragover.prevent
    @dragleave.prevent="onDragLeave"
    @drop.prevent="onDrop"
  >
    <header class="navbar">
      <div class="logo">SheetSage2 <span>Studio</span></div>
      <div class="nav-tabs">
        <button class="nav-tab active">工作台</button>
        <button class="nav-tab" disabled title="编辑器还没做（原型的「编辑」页）">编辑</button>
      </div>
      <div class="nav-right">
        <div class="nav-meta">
          <span v-if="env">扒谱 {{ env.model }}</span>
          <span v-if="env">歌词 {{ env.asr?.model ?? '—' }}</span>
          <span v-if="env && env.queue.pending">排队 {{ env.queue.pending }}</span>
        </div>
        <button class="theme-toggle" title="切换主题" @click="toggleTheme">◐</button>
      </div>
    </header>

    <div class="app">
      <AppSidebar
        :jobs="jobs"
        :active-id="activeId"
        :busy="busy"
        :phases="PHASES"
        @select="select"
        @remove="remove"
        @create="create"
      />

      <main class="main">
        <div class="workspace">
          <div class="ws-inner">
            <div v-if="error" class="error-box">
              <span>{{ error }}</span>
              <button @click="dismissError()">×</button>
            </div>

            <div v-if="!active" class="empty-hint">
              <b>拖入一首歌，开始识别</b>
              拖进来只是新建工程——先选参数（精度 / 范围 / 量化 / 语言…），再点「开始扒谱」<br />
              请用<b>含人声的完整混音</b>——伴奏版的中段网格会明显变差
            </div>

            <template v-else>
              <ProjectHead
                :job="active"
                @remove="remove(active.id)"
                @reset="reset"
              />
              <TrackList
                :job="active"
                :peaks="peaks"
                :stem-peaks="stemPeaks"
                :lanes="lanes"
                @stem="attachStem"
                @drop-stem="detachStem"
              />
              <PipelineStages
                :job="active"
                :phases="phases"
                :env="env"
                :busy="busy"
                @run="runStage"
                @stop="stopAll"
                @auto="auto"
                @option="(key, value) => setOptions({ [key]: value })"
              />
              <ResultsPanel
                :job="active"
                :lanes="lanes"
                :streamed="streamedLyrics"
                :loading="lanesLoading"
              />
            </template>

            <div v-if="env && !env.static_ready" class="warn-box">
              <b>前端产物缺失</b>：请在 <code>web/</code> 里跑 <code>npm run build</code>。
            </div>
          </div>
        </div>

        <LogDrawer :logs="logs" />
      </main>
    </div>

    <div v-if="dragOver" class="drop-overlay">松开鼠标，新建工程（拖两个：第一个当混音原曲，第二个当干声）</div>
    <div
      v-if="uploadProgress !== null"
      class="drop-overlay"
      style="background: rgba(37, 99, 235, 0.12)"
    >
      正在上传 {{ Math.round(uploadProgress * 100) }}%
    </div>
    <div
      v-if="anyRunning && !active"
      class="empty-hint"
      style="position: fixed; bottom: 40px; left: 0; right: 0"
    >
      有任务在后台运行
    </div>
  </div>
</template>
