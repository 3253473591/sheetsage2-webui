<script setup lang="ts">
/**
 * 侧栏：批次分组 + 单曲。
 *
 * 为什么分组而不平铺：40 首歌全铺进 248px 会把单曲淹掉。批次是**容器**（点组头进批次
 * 视图），组内才是工程行（点行进现有工程页）；不属于任何批次的"单曲"永远单独一段。
 * 组头自带 `12/40` 与一条细分段条——不展开也知道跑到哪了。
 */
import { computed, ref } from 'vue'
import { formatShort } from '@/format'
import {
  stageBadge, type BatchInfo, type JobSnapshot, type PhaseKey,
} from '@/types'

const props = defineProps<{
  jobs: JobSnapshot[]
  batches: BatchInfo[]
  activeId: string | null
  busy: boolean
  phases: PhaseKey[]
}>()
const emit = defineEmits<{
  select: [id: string]
  remove: [id: string]
  create: [files: File[]]
  batchOpen: [batchId: string]
  batchImport: []
}>()

const over = ref(false)
const input = ref<HTMLInputElement | null>(null)
const ACCEPT = '.wav,.mp3,.flac,.m4a,.aac,.ogg,.opus,.aif,.aiff,.wma,.mp4'

/** 哪些批次是展开的（没点过就按"正在跑才展开"决定）。 */
const expanded = ref<Record<string, boolean>>({})

function pick(event: Event): void {
  const target = event.target as HTMLInputElement
  const files = [...(target.files ?? [])]
  if (files.length) emit('create', files)
  target.value = ''            // 同一个文件也能再触发一次
}

function drop(event: DragEvent): void {
  over.value = false
  const files = [...(event.dataTransfer?.files ?? [])]
  if (files.length) emit('create', files)
}

function badge(job: JobSnapshot, phase: PhaseKey) {
  return stageBadge(job.stages?.[phase])
}

/** 批次组内的行很窄：只留"✓ / 62% / ✗ / ·"，词组留给 title。 */
function compactBadge(job: JobSnapshot, phase: PhaseKey): { text: string, tone: string } {
  const stage = job.stages?.[phase]
  if (!stage) return { text: '·', tone: 'idle' }
  switch (stage.state) {
    case 'done':
      return { text: '✓', tone: 'ok' }
    case 'running':
    case 'queued':
      return { text: `${Math.round((stage.progress || 0) * 100)}%`, tone: 'run' }
    case 'failed':
      return { text: '✗', tone: 'bad' }
    case 'cancelled':
      return { text: '停', tone: 'idle' }
    case 'skipped':
      return { text: '跳', tone: 'idle' }
    default:
      return { text: '·', tone: 'idle' }
  }
}

function members(batchId: string): JobSnapshot[] {
  return props.jobs
    .filter((job) => job.batch?.id === batchId)
    .slice()
    .sort((a, b) => ((a.batch?.index ?? 1e9) - (b.batch?.index ?? 1e9))
      || (a.created_at - b.created_at))
}

function isOpen(batch: BatchInfo): boolean {
  const explicit = expanded.value[batch.id]
  return explicit === undefined ? batch.running > 0 : explicit
}

function toggle(batch: BatchInfo): void {
  expanded.value = { ...expanded.value, [batch.id]: !isOpen(batch) }
}

/** 不属于任何批次的工程（拖进来的单曲）。 */
const singles = computed(() => props.jobs.filter((job) => !job.batch))
</script>

<template>
  <aside class="sidebar">
    <div class="sb-head">
      <div class="sb-title">工程<span>{{ jobs.length }}</span></div>
      <button class="btn-new" :disabled="busy" @click="input?.click()">+ 新建</button>
    </div>
    <div
      class="sb-drop"
      :class="{ over }"
      @dragover.prevent="over = true"
      @dragleave.prevent="over = false"
      @drop.prevent="drop"
    >
      拖入音频即新建工程<br />
      wav / mp3 / flac · 用混音原曲<br />
      一次拖两个：第二个当干声<br />
      <button class="sb-batch-link" title="填一个文件夹路径：里面放歌曲与同名 LRC"
              @click="emit('batchImport')">
        批量导入文件夹…
      </button>
    </div>
    <input ref="input" type="file" :accept="ACCEPT" hidden @change="pick" />

    <div class="proj-list">
      <div v-for="batch in batches" :key="batch.id" class="sb-group">
        <div class="sb-group-head">
          <button class="sb-caret" :title="isOpen(batch) ? '收起' : '展开'" @click.stop="toggle(batch)">
            {{ isOpen(batch) ? '▾' : '▸' }}
          </button>
          <button class="sb-group-name" :title="`打开批次：${batch.title}`"
                  @click="emit('batchOpen', batch.id)">
            {{ batch.title }}
          </button>
          <span class="sb-group-count">{{ batch.done }}/{{ batch.total }}</span>
          <span class="sb-mini" :title="`完成 ${batch.done} · 失败 ${batch.failed} · 剩 ${batch.pending + batch.running}`">
            <i class="ok" :style="{ width: `${batch.total ? (batch.done / batch.total) * 100 : 0}%` }" />
            <i class="bad" :style="{ width: `${batch.total ? (batch.failed / batch.total) * 100 : 0}%` }" />
          </span>
        </div>
        <div v-if="isOpen(batch)" class="sb-group-body">
          <div
            v-for="(job, index) in members(batch.id)"
            :key="job.id"
            class="proj-item batch-item"
            :class="{ active: job.id === activeId }"
            @click="emit('select', job.id)"
          >
            <span class="pi-no">{{ index + 1 }}</span>
            <span class="pi-name">{{ job.name }}</span>
            <span v-if="job.options?.lyrics_lrc" class="pi-lrc" title="有同名 LRC">词</span>
            <span
              v-for="phase in props.phases"
              :key="phase"
              class="st"
              :class="compactBadge(job, phase).tone"
              :title="badge(job, phase).text"
            >
              {{ compactBadge(job, phase).text }}
            </span>
          </div>
        </div>
      </div>

      <template v-if="singles.length">
        <div v-if="batches.length" class="sb-sub">单曲</div>
        <div
          v-for="job in singles"
          :key="job.id"
          class="proj-item"
          :class="{ active: job.id === activeId }"
          @click="emit('select', job.id)"
        >
          <div class="pi-row">
            <div class="pi-main">
              <div class="pi-name">
                {{ job.name }}
                <span v-if="job.imported" class="pi-tag" title="output/ 里已有的产物：只读">已有产物</span>
              </div>
              <div class="pi-meta">
                <template v-if="job.imported">载入的产物（只读）</template>
                <template v-else>rev {{ job.rev }} · {{ formatShort(job.created_at) }}</template>
              </div>
              <div class="pi-status">
                <span
                  v-for="phase in props.phases"
                  :key="phase"
                  class="st"
                  :class="badge(job, phase).tone"
                >
                  {{ badge(job, phase).text }}
                </span>
              </div>
            </div>
            <button
              class="pi-del"
              :title="job.imported ? '只读产物：要删请手动删目录' : '删除工程与产物'"
              :disabled="job.imported"
              @click.stop="emit('remove', job.id)"
            >×</button>
          </div>
        </div>
      </template>

      <div v-if="!jobs.length" class="empty-hint" style="padding: 20px 6px; font-size: 11.5px">
        还没有工程<br />拖一首歌进来就开始
      </div>
    </div>
  </aside>
</template>
