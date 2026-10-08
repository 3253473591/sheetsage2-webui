<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { LogLine } from '@/composables/useStudio'

const props = defineProps<{ logs: LogLine[] }>()
const open = ref(false)
const box = ref<HTMLElement | null>(null)

const latest = computed(() => props.logs[props.logs.length - 1] ?? null)

function label(phase: string | null): string {
  if (phase === 'transcribe') return '扒谱'
  if (phase === 'lyrics') return '歌词'
  return '系统'
}
function toneClass(line: LogLine): string {
  if (line.tone === 'error') return 'c'
  if (line.phase === 'lyrics') return 'b'
  return 'a'
}
function clock(epoch: number): string {
  const date = new Date(epoch * 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

watch(
  () => props.logs.length,
  async () => {
    await nextTick()
    if (box.value) box.value.scrollTop = box.value.scrollHeight
  },
)
</script>

<template>
  <div class="drawer" :class="{ open }">
    <div class="dr-bar" @click="open = !open">
      <b>日志</b>
      <span class="dr-latest">
        <template v-if="latest">
          <span class="lt" :class="toneClass(latest)">[{{ label(latest.phase) }}]</span>
          {{ latest.text }}
        </template>
        <template v-else>等待任务…</template>
      </span>
      <span class="dr-hint">可关闭本页，任务在后台继续 {{ open ? '▾' : '▴' }}</span>
    </div>
    <div ref="box" class="dr-body">
      <div v-for="line in logs" :key="`${line.seq}-${line.time}`">
        <span class="lt" :class="toneClass(line)">[{{ label(line.phase) }}]</span>
        {{ clock(line.time) }} {{ line.text }}
      </div>
    </div>
  </div>
</template>
