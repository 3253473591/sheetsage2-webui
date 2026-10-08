<script setup lang="ts">
import { ref } from 'vue'
import { formatShort } from '@/format'
import { stageBadge, type JobSnapshot, type PhaseKey } from '@/types'

const props = defineProps<{
  jobs: JobSnapshot[]
  activeId: string | null
  busy: boolean
  phases: PhaseKey[]
}>()
const emit = defineEmits<{
  select: [id: string]
  remove: [id: string]
  create: [files: File[]]
}>()

const over = ref(false)
const input = ref<HTMLInputElement | null>(null)
const ACCEPT = '.wav,.mp3,.flac,.m4a,.aac,.ogg,.opus,.aif,.aiff,.wma,.mp4'

function pick(event: Event): void {
  const target = event.target as HTMLInputElement
  const files = [...(target.files ?? [])]
  if (files.length) emit('create', files)
  target.value = ''            // 同一个文件也能再触发一次
}

function drop(event: DragEvent): void {
  over.value = false
  // 一次拖两个：第一个混音原曲、第二个干声（认名字的规则在 @/drop）
  const files = [...(event.dataTransfer?.files ?? [])]
  if (files.length) emit('create', files)
}

function badge(job: JobSnapshot, phase: PhaseKey) {
  return stageBadge(job.stages?.[phase])
}
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
      拖入音频即新建工程<br />wav / mp3 / flac · 用混音原曲<br />一次拖两个：第二个当干声
    </div>
    <input ref="input" type="file" :accept="ACCEPT" hidden @change="pick" />

    <div class="proj-list">
      <div
        v-for="job in jobs"
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
      <div v-if="!jobs.length" class="empty-hint" style="padding: 20px 6px; font-size: 11.5px">
        还没有工程<br />拖一首歌进来就开始
      </div>
    </div>
  </aside>
</template>
