<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppSidebar from '@/components/AppSidebar.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import LogDrawer from '@/components/LogDrawer.vue'
import PipelineStages from '@/components/PipelineStages.vue'
import ProjectHead from '@/components/ProjectHead.vue'
import ResultsPanel from '@/components/ResultsPanel.vue'
import TrackList from '@/components/TrackList.vue'
import TrashDialog from '@/components/TrashDialog.vue'
import { useAudioPlayer } from '@/composables/useAudio'
import { useStudio } from '@/composables/useStudio'
import { deleteWarning } from '@/confirm'
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
  serviceDown,
  uploadProgress,
  lanesLoading,
  trash,
  init,
  select,
  create,
  runStage,
  runChain,
  cancel,
  attachStem,
  detachStem,
  setOptions,
  cleanup,
  remove,
  loadTrash,
  restoreTrash,
  purgeTrash,
  backupUrl,
  restoreBackup,
  reset,
  dismissError,
} = useStudio()

const player = useAudioPlayer()
const theme = ref<'light' | 'dark'>('light')
const dragOver = ref(false)
const PHASES: PhaseKey[] = ['transcribe', 'lyrics']

/** 等待确认的删除（G2：删除按钮不再单击即删）。 */
const pendingDelete = ref<{ id: string; name: string; message: string } | null>(null)
const showTrash = ref(false)

/** 导入备份（G22）：隐藏的 file input，点顶栏按钮时触发。 */
const backupInput = ref<HTMLInputElement | null>(null)
const importingBackup = ref(false)

function pickBackup(): void {
  backupInput.value?.click()
}

async function onBackupPicked(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''                       // 允许同一个包再导一次
  if (!file) return
  importingBackup.value = true
  try {
    await restoreBackup(file)
  } finally {
    importingBackup.value = false
  }
}

const phases = computed(() => env.value?.phases ?? [
  { key: 'transcribe' as const, label: '扒谱', engine: 'SheetSage2' },
  { key: 'lyrics' as const, label: '歌词识别', engine: 'Qwen3-ASR' },
])

const busy = computed(() => uploadProgress.value !== null)
const anyRunning = computed(() =>
  jobs.value.some((job) => job.state === 'running' || job.state === 'queued'),
)

/** 启动预检（G15）：顶栏横幅按最严重的一项上色（error > warn > info）。 */
const preflightNote = computed(() => {
  const checks = env.value?.preflight ?? []
  const order = ['error', 'warn', 'info'] as const
  for (const level of order) {
    const hit = checks.find((item) => item.level === level)
    if (hit) return hit
  }
  return null
})

/** 磁盘剩余（G11）：顶栏显示，< 2GB 变黄。 */
const diskText = computed(() => {
  const disk = env.value?.disk
  if (!disk || !disk.total) return ''
  const free = disk.free
  const unit = free >= 1024 ** 3 ? 'GB' : 'MB'
  const value = free >= 1024 ** 3 ? free / 1024 ** 3 : free / 1024 ** 2
  return `剩余 ${value.toFixed(1)} ${unit}`
})
const diskLow = computed(() => {
  const free = env.value?.disk?.free ?? 0
  return free > 0 && free < 2 * 1024 ** 3
})

function stopAll(): void {
  void cancel()
}

function auto(): void {
  // 「自动提取」= 扒谱 → 歌词 一条龙（原型的排队链路）；走后端 /chain，不是两次 runStage
  void runChain()
}

/** 删除入口统一走这里：先弹确认（弹层里还要等 1 秒才能点确认）。 */
function askRemove(id: string): void {
  const job = jobs.value.find((item) => item.id === id)
  if (!job) return
  pendingDelete.value = {
    id,
    name: job.name,
    message: deleteWarning(job.name, job.files?.length ?? 0, Boolean(job.audio)),
  }
}

async function confirmRemove(): Promise<void> {
  const target = pendingDelete.value
  pendingDelete.value = null
  if (!target) return
  await remove(target.id)
  if (showTrash.value) await loadTrash()
}

async function openTrash(): Promise<void> {
  showTrash.value = true
  await loadTrash()
}

async function doRestore(entryId: string): Promise<void> {
  await restoreTrash(entryId)
  if (!trash.value.length) showTrash.value = false
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
  void loadTrash()
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
          <span v-if="diskText" :class="{ 'meta-warn': diskLow }" :title="diskLow ? '磁盘不多了，建议清理缓存' : ''">{{ diskText }}</span>
        </div>
        <a class="nav-tab" href="/api/diagnose" title="打包诊断信息（工程 + 日志 + 环境），出问题时发我">诊断包</a>
        <a
          class="nav-tab"
          :href="backupUrl"
          title="把所有工程（含原音频与产物）打成一个 zip：换电脑 / 重装系统前点一下"
        >备份</a>
        <button
          class="nav-tab"
          :disabled="importingBackup"
          title="导入之前导出的备份包：重名工程不会覆盖，会落成「-2」"
          @click="pickBackup"
        >
          {{ importingBackup ? '导入中…' : '导入备份' }}
        </button>
        <input
          ref="backupInput"
          type="file"
          accept=".zip,application/zip"
          style="display: none"
          @change="onBackupPicked"
        />
        <button
          class="nav-tab"
          :title="'回收站：删掉的工程 7 天内可以恢复'"
          @click="openTrash"
        >
          回收站<span v-if="trash.length"> {{ trash.length }}</span>
        </button>
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
        @remove="askRemove"
        @create="create"
      />

      <main class="main">
        <div class="workspace">
          <div class="ws-inner">
            <div v-if="serviceDown" class="error-box" style="border-left-color: #dc2626">
              <b>后台服务已停止</b>（可能被关闭或崩溃了）。
              请重新运行 <code>webui.bat</code>，然后刷新本页。
            </div>
            <div v-else-if="preflightNote" :class="preflightNote.level === 'error' ? 'error-box' : 'warn-box'">
              <b>{{ preflightNote.title }}</b>
              <span v-if="preflightNote.detail"> · {{ preflightNote.detail }}</span>
            </div>
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
                @remove="askRemove(active.id)"
                @reset="reset"
                @cleanup="cleanup"
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

    <ConfirmDialog
      v-if="pendingDelete"
      title="删除这个工程？"
      :message="pendingDelete.message"
      note="删掉之后会先进回收站（顶栏「回收站」里能恢复）；确认按钮要等 1 秒才生效，防手滑。"
      confirm-label="删除"
      @confirm="confirmRemove"
      @cancel="pendingDelete = null"
    />

    <TrashDialog
      v-if="showTrash"
      :entries="trash"
      @close="showTrash = false"
      @restore="doRestore"
      @purge="purgeTrash"
    />
  </div>
</template>
