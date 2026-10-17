<script setup lang="ts">
/**
 * 回收站（G2）：删除是软删除，这里能把工程整回来。
 *
 * 一行的三件事：**是什么**（曲名 + 几个产物 + 多大）、**什么时候删的**、
 * **恢复 / 彻底删除**。彻底删除是终态的，所以它走同一个"等 1 秒"的确认弹层。
 */
import { ref } from 'vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import { formatShort, formatSize } from '@/format'
import type { TrashEntry } from '@/types'

defineProps<{ entries: TrashEntry[] }>()
const emit = defineEmits<{
  close: []
  restore: [id: string]
  purge: [id: string]
}>()

const pendingPurge = ref<TrashEntry | null>(null)

function confirmPurge(): void {
  if (pendingPurge.value) emit('purge', pendingPurge.value.id)
  pendingPurge.value = null
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal-card wide" role="dialog" aria-modal="true">
      <div class="modal-title">回收站</div>
      <p class="modal-note">
        删掉的工程会先进这里，7 天内可以恢复（超过 7 天或回收站超过 2GB 会自动清理最旧的）。
      </p>

      <div v-if="!entries.length" class="modal-body">回收站是空的。</div>
      <div v-else class="trash-list">
        <div v-for="entry in entries" :key="entry.id" class="trash-row">
          <div class="trash-main">
            <div class="trash-name">{{ entry.name }}</div>
            <div class="trash-meta">
              {{ entry.files }} 个产物 · {{ formatSize(entry.size) }} ·
              删除于 {{ formatShort(entry.deleted_at) }}
            </div>
          </div>
          <button class="btn-secondary" @click="emit('restore', entry.id)">恢复</button>
          <button class="btn-secondary danger" @click="pendingPurge = entry">彻底删除</button>
        </div>
      </div>

      <div class="modal-actions">
        <button class="btn-secondary" @click="emit('close')">关闭</button>
      </div>

      <ConfirmDialog
        v-if="pendingPurge"
        title="彻底删除这个工程？"
        :message="`《${pendingPurge.name}》会被永久删掉，回收站里也找不回来了。`"
        note="如果只是想腾点空间，先别删：导出 zip 备份之后再删。"
        confirm-label="永久删除"
        @confirm="confirmPurge"
        @cancel="pendingPurge = null"
      />
    </div>
  </div>
</template>
