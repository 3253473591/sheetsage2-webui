<script setup lang="ts">
/**
 * 下载弹窗：勾选 SVP 里要装的内容。
 *
 * 为什么要有弹窗：人声 / 乐器 / 和弦三组内容都能进 SVP，但代价不一样——
 * **和弦一轨只能有一个音**，所以选它就会多拆出好几轨（实测《光芒》4 轨）。
 * 默认只勾人声（扒谱 + 歌词的成果），乐器与和弦要用户明确点头才加。
 */
import { computed, ref, watch } from 'vue'
import { fetchExports, svpUrl } from '@/api'
import type { ExportOptions } from '@/types'

const props = defineProps<{ jobId: string; jobName: string }>()
const emit = defineEmits<{ close: [] }>()

const data = ref<ExportOptions | null>(null)
const checked = ref<string[]>([])
const error = ref<string | null>(null)
const loading = ref(true)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const body = await fetchExports(props.jobId)
    data.value = body
    // 默认勾选由后端说了算（目前只有人声）
    checked.value = body.parts
      .filter((part) => part.default && part.available)
      .map((part) => part.key)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loading.value = false
  }
}

watch(() => props.jobId, load, { immediate: true })

const selected = computed(() =>
  (data.value?.parts ?? []).filter((part) => checked.value.includes(part.key)),
)
const totalNotes = computed(() => selected.value.reduce((sum, part) => sum + part.notes, 0))
const totalTracks = computed(() => selected.value.reduce((sum, part) => sum + part.tracks, 0))

function toggle(key: string): void {
  checked.value = checked.value.includes(key)
    ? checked.value.filter((item) => item !== key)
    : [...checked.value, key]
}

function download(): void {
  if (!selected.value.length) return
  // 用隐藏的 <a> 触发：带 Content-Disposition，浏览器直接存盘、不跳页
  const link = document.createElement('a')
  link.href = svpUrl(props.jobId, selected.value.map((part) => part.key))
  link.download = `${props.jobName || 'sheetsage2'}.svp`
  document.body.appendChild(link)
  link.click()
  link.remove()
  emit('close')
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <b>下载 SVP 工程</b>
        <button class="modal-x" title="关闭" @click="emit('close')">×</button>
      </div>

      <p class="modal-sub">
        Synthesizer V Studio 工程，<b>带变速与歌词</b>，可直接打开。
        勾选要装进去的内容（默认只勾人声）。
      </p>

      <div v-if="loading" class="modal-loading">正在读取工程…</div>
      <div v-else-if="error" class="modal-error">{{ error }}</div>

      <template v-else-if="data">
        <label
          v-for="part in data.parts"
          :key="part.key"
          class="modal-row"
          :class="{ off: !part.available }"
        >
          <input
            type="checkbox"
            :checked="checked.includes(part.key)"
            :disabled="!part.available"
            @change="toggle(part.key)"
          />
          <span class="mr-main">
            <b>{{ part.label }}</b>
            <small v-if="!part.available">这个工程里没有</small>
            <small v-else>
              {{ part.notes }} 个音<template v-if="part.tracks > 1"> ·
                {{ part.tracks }} 轨</template>
              <template v-if="part.detail"> · {{ part.detail }}</template>
            </small>
          </span>
        </label>

        <div class="modal-foot">
          <span class="modal-sum">
            选中 {{ totalNotes }} 个音 / {{ totalTracks }} 轨 · SVP {{ data.svp.version }}
          </span>
          <button class="btn-secondary" @click="emit('close')">取消</button>
          <button class="btn-primary" :disabled="!selected.length" @click="download">
            下载 SVP
          </button>
        </div>
      </template>
    </div>
  </div>
</template>
