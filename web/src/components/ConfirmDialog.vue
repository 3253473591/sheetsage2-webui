<script setup lang="ts">
/**
 * 通用确认弹层（目前只有"删除工程"用）。
 *
 * 两个刻意的设计：
 * 1. **挂出来之后先等 `CONFIRM_DELAY_MS`**（默认 1 秒）确认按钮才可点——防连点，
 *    规则在 `@/confirm`（纯函数，有单测）；
 * 2. 点「取消」或按 Esc 都可以关，**默认焦点不落在危险按钮上**。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { CONFIRM_DELAY_MS, armProgress } from '@/confirm'

defineProps<{
  title: string
  message: string
  /** 次要说明（灰色小字），比如"回收站保留 7 天" */
  note?: string
  confirmLabel?: string
  cancelLabel?: string
}>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()

const startedAt = ref<number | null>(null)
const now = ref(Date.now())
let timer: number | null = null

onMounted(() => {
  startedAt.value = Date.now()
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 100)
})
onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer)
  timer = null
})

const ratio = computed(() => armProgress(startedAt.value, now.value))
const ready = computed(() => ratio.value >= 1)
const waitHint = computed(() => {
  if (ready.value) return ''
  const left = Math.ceil((1 - ratio.value) * (CONFIRM_DELAY_MS / 1000))
  return `确认按钮还要等 ${left} 秒（防手滑）`
})

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('cancel')
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('cancel')" @keydown="onKey">
    <div class="modal-card" role="dialog" aria-modal="true">
      <div class="modal-title">{{ title }}</div>
      <p class="modal-body">{{ message }}</p>
      <p v-if="note" class="modal-note">{{ note }}</p>
      <div class="modal-actions">
        <button class="btn-secondary" @click="emit('cancel')">{{ cancelLabel ?? '取消' }}</button>
        <button
          class="btn-secondary danger"
          :disabled="!ready"
          :title="waitHint"
          @click="ready && emit('confirm')"
        >
          {{ confirmLabel ?? '删除' }}
        </button>
      </div>
      <div class="modal-progress">
        <div :style="{ width: `${Math.round(ratio * 100)}%` }" />
      </div>
    </div>
  </div>
</template>
