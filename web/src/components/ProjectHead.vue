<script setup lang="ts">
import { computed, ref } from 'vue'
import ExportDialog from '@/components/ExportDialog.vue'
import { bundleUrl, fileUrl } from '@/api'
import { formatClock, formatSize, formatStamp } from '@/format'
import type { JobSnapshot } from '@/types'

const props = defineProps<{ job: JobSnapshot }>()
const emit = defineEmits<{ remove: []; reset: [] }>()

/** 左边主按钮：直接下 SVP（先弹勾选框）；右边小箭头：展开产物菜单（zip + 单个文件）。 */
const openMenu = ref(false)
const openExport = ref(false)
const duration = computed(() => props.job.highlights?.duration_seconds ?? 0)
const created = computed(() => formatStamp(props.job.created_at))
</script>

<template>
  <div class="proj-head">
    <div>
      <div class="ph-name">{{ job.name }}</div>
      <div class="ph-meta">
        rev {{ job.rev }} · 创建于 {{ created }}
        <template v-if="duration"> · {{ formatClock(duration) }}</template>
        <template v-if="job.audio"> · {{ job.audio }}</template>
      </div>
    </div>
    <div class="ph-actions">
      <button class="btn-secondary danger" @click="emit('remove')">删除</button>
      <div class="dl-group">
        <button
          class="btn-secondary dl-main"
          :disabled="!job.highlights"
          :title="job.highlights ? '导出 Synthesizer V Studio 工程（.svp，带变速与歌词）'
                                 : '先完成扒谱才能导出'"
          @click="openExport = true"
        >
          下载 SVP
        </button>
        <button
          class="btn-secondary dl-caret"
          :class="{ on: openMenu }"
          title="更多下载：整包 zip 与单个产物"
          @click="openMenu = !openMenu"
        >
          ▾
        </button>
        <div v-if="openMenu" class="dl-menu" @click="openMenu = false">
          <a class="all" :href="bundleUrl(job.id)" download>
            下载全部（zip，{{ job.files.length }} 个产物）
          </a>
          <a v-for="file in job.files" :key="file.path" :href="fileUrl(job.id, file.path)" download>
            <span>{{ file.path }}</span>
            <span class="sz">{{ formatSize(file.size) }}</span>
          </a>
          <div v-if="!job.files.length" style="padding: 6px 8px; color: var(--light)">
            还没有产物
          </div>
        </div>
      </div>
      <button class="btn-primary" :disabled="!job.highlights" @click="emit('reset')">
        新建工程
      </button>
    </div>

    <ExportDialog
      v-if="openExport"
      :job-id="job.id"
      :job-name="job.name"
      @close="openExport = false"
    />
  </div>
</template>
