<script setup lang="ts">
/**
 * 批量导入弹层：选文件夹 → 扫描预览 → 一次设定参数 → 开始。
 *
 * 这一屏只回答一个问题：**这 N 首里哪些有词、哪些没词、哪些会出问题**。
 * 组件只负责画和收集输入；扫目录 / 建批次都在 `useBatch`（父级传入 `scan` / `start`）。
 */
import { computed, ref } from 'vue'
import * as api from '@/api'
import { formatSize, formatSpan, lrcBadge, PER_AUDIO_SECOND, PER_SONG_OVERHEAD } from '@/batch'
import { paramRows, type ParamRow } from '@/params'
import type { BatchScan, EnvInfo, JobOptions } from '@/types'

const props = defineProps<{
  env: EnvInfo | null
  scan: BatchScan | null
  scanning: boolean
  busy: boolean
  /** 服务端给的错误（目录不存在、数据目录里、没有音频…） */
  error?: string | null
}>()

const emit = defineEmits<{
  close: []
  scan: [dir: string]
  start: [payload: { dir: string; options: JobOptions }]
  error: [message: string]
}>()

const LAST_DIR_KEY = 'sheetsage-batch-dir'

const dir = ref(window.localStorage.getItem(LAST_DIR_KEY) ?? '')
const options = ref<JobOptions>({})
const showParams = ref(true)
/** 「浏览…」只在真正的本机桌面里可用：没有 tkinter/桌面时后端回 501，这里就置灰。 */
const browsing = ref(false)
const browseUnavailable = ref(false)

async function browse(): Promise<void> {
  if (browsing.value || browseUnavailable.value) return
  browsing.value = true
  try {
    const picked = await api.browseFolder()
    if (picked) {
      dir.value = picked
      window.localStorage.setItem(LAST_DIR_KEY, picked)
    }
  } catch (exc) {
    browseUnavailable.value = true
    // 501 的文案已经是人话（"…请手动粘贴文件夹路径"）；其他错误也照实说
    emitErr(exc instanceof Error ? exc.message : String(exc))
  } finally {
    browsing.value = false
  }
}

/** 把错误交给父级显示（弹层顶部那条红条）。 */
function emitErr(text: string): void {
  emit('error', text)
}

/** 参数面板：沿用两阶段面板的那套行，但**去掉「歌词LRC」**——批量里它由文件夹决定。 */
const rows = computed<ParamRow[]>(() => {
  const groups = props.env?.params
  if (!groups) return []
  return [
    ...paramRows(groups.transcribe, options.value),
    ...paramRows(groups.lyrics, options.value),
  ].filter((row) => row.key !== 'lyrics_lrc' && row.widget !== 'text')
})

const items = computed(() => props.scan?.items ?? [])
const total = computed(() => items.value.length)

const withLyrics = computed(() => items.value.filter((item) => item.lrc_kind !== 'none').length)
const withoutLyrics = computed(() => total.value - withLyrics.value)

/** 粗估总耗时：音频时长 × 系数 + 每首固定开销（和 `eta.py` 同口径）。 */
const estimate = computed(() => {
  const seconds = items.value.reduce((sum, item) => {
    return sum + PER_SONG_OVERHEAD + (item.duration_seconds ?? 240) * PER_AUDIO_SECOND
  }, 0)
  return formatSpan(seconds)
})

const problems = computed(() => props.scan?.problems ?? [])

function fireScan(): void {
  const value = dir.value.trim()
  if (!value || props.scanning) return
  window.localStorage.setItem(LAST_DIR_KEY, value)
  emit('scan', value)
}

function pickOption(key: string, value: string): void {
  options.value = { ...options.value, [key]: value }
}

function start(): void {
  if (!props.scan || props.busy) return
  emit('start', { dir: dir.value.trim(), options: options.value })
}
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div class="modal-card wide batch-dialog">
      <div class="batch-dlg-head">
        <div class="modal-title" style="margin: 0">批量导入</div>
        <button class="batch-close" title="关闭" @click="emit('close')">✕</button>
      </div>

      <div class="batch-dlg-body">
        <div class="path-row">
          <input
            v-model="dir"
            class="path-input"
            spellcheck="false"
            placeholder="把文件夹路径粘进来，例如 D:\音乐\2026-11"
            @keydown.enter="fireScan"
          >
          <button class="btn-secondary" :disabled="browsing || browseUnavailable"
                  :title="browseUnavailable
                    ? '这个环境弹不了目录选择器（没有图形界面）：请手动粘贴路径'
                    : '在服务端弹一个原生目录选择器'"
                  @click="browse">
            {{ browsing ? '选择中…' : '浏览…' }}
          </button>
          <button class="btn-primary" :disabled="!dir.trim() || scanning" @click="fireScan">
            {{ scanning ? '扫描中…' : '扫描' }}
          </button>
        </div>
        <p class="modal-note" style="margin-top: 6px">
          只读扫描，不改文件夹；音频会放进各自工程里（同盘硬链接，不占双份空间）。
        </p>

        <div v-if="error" class="error-box" style="margin-top: 8px">{{ error }}</div>

        <template v-if="scan">
          <div class="scan-summary">
            <span><b>{{ total }}</b> 首</span>
            <span><b>{{ withLyrics }}</b> 首带 LRC（逐行对齐）</span>
            <span :class="{ dim: !withoutLyrics }"><b>{{ withoutLyrics }}</b> 首走语音识别</span>
            <span v-if="estimate">预计 <b>{{ estimate }}</b>（串行）</span>
            <span class="dim">{{ formatSpan(scan.total_seconds) }} 音频 ·
              {{ formatSize(scan.items.reduce((s, i) => s + i.size, 0)) }}</span>
          </div>

          <div v-if="withoutLyrics" class="batch-banner warn">
            <b>{{ withoutLyrics }} 首没有同名 LRC</b>：会走语音识别——比精确对齐慢，词也可能不对。
          </div>
          <div v-if="problems.length" class="batch-banner warn">
            <b>有 {{ problems.length }} 处要注意</b>：{{ problems.slice(0, 3).join('；') }}
            <span v-if="problems.length > 3">…</span>
          </div>

          <div class="ptable">
            <div class="ptable-head">
              <div class="no">#</div><div>歌名</div><div>大小</div><div>时长</div><div>歌词源</div>
            </div>
            <div class="ptable-body">
              <div v-for="(item, index) in items" :key="item.audio" class="prow"
                   :title="item.problem || lrcBadge(item).text">
                <div class="no">{{ index + 1 }}</div>
                <div class="nm">
                  {{ item.name }}
                  <span v-if="item.problem" class="pi-warn" :title="item.problem">!</span>
                </div>
                <div class="dim">{{ formatSize(item.size) }}</div>
                <div class="dim">{{ item.duration_seconds ? formatSpan(item.duration_seconds) : '—' }}</div>
                <div class="lrc" :class="lrcBadge(item).tone">{{ lrcBadge(item).short }}</div>
              </div>
            </div>
          </div>

          <div class="params-block">
            <button class="link" @click="showParams = !showParams">
              参数（这批统一）{{ showParams ? '▴' : '▾' }}
            </button>
            <div v-if="showParams" class="param-grid">
              <template v-for="row in rows" :key="row.key">
                <span class="p-label">{{ row.label }}</span>
                <div class="btn-group">
                  <select
                    v-if="row.widget === 'select'"
                    class="lang-select"
                    :value="row.value"
                    @change="pickOption(row.key, ($event.target as HTMLSelectElement).value)"
                  >
                    <option v-for="item in row.options" :key="item.value" :value="item.value">
                      {{ item.label }}
                    </option>
                  </select>
                  <button
                    v-for="item in row.options"
                    v-else
                    :key="item.value"
                    type="button"
                    class="tag-btn"
                    :class="{ active: item.value === row.value }"
                    :title="item.hint ?? ''"
                    @click="pickOption(row.key, item.value)"
                  >
                    {{ item.label }}
                  </button>
                </div>
              </template>
              <span class="p-label">歌词源</span>
              <div class="ro">按文件夹里的同名 LRC（{{ withLyrics }}/{{ total }}）——这一行只读</div>
            </div>
          </div>
        </template>
      </div>

      <div class="modal-actions">
        <span class="modal-note" style="margin: 0 auto 0 0">
          跑起来是串行：一首跑完自动接下一首，可以随时暂停。
        </span>
        <button class="btn-secondary" @click="emit('close')">取消</button>
        <button class="btn-primary" :disabled="!scan || busy" @click="start">
          {{ busy ? '正在建工程…' : `开始 ${total || ''} 首` }}
        </button>
      </div>
    </div>
  </div>
</template>
