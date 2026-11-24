<script setup lang="ts">
/**
 * 批次视图：头卡（统计 + 操作）+ 明细表 + 结束小结。
 *
 * 两条刻意的设计：
 * 1. **总进度 = 完成首数 / 总数**（分段条：绿完成 / 红失败 / 蓝当前）。不做"加权平均
 *    百分比"——两阶段耗时不可知，一个 47% 说明不了任何事。
 * 2. 行内只放**一句人话**（失败原因）；点行进普通工程页看完整错误、卷帘、下载。
 */
import { computed } from 'vue'
import { batchStats, formatSpan } from '@/batch'
import type { BatchDetail, JobRow, PhaseKey } from '@/types'

const props = defineProps<{
  detail: BatchDetail
  paused: boolean
  busy: boolean
}>()

const emit = defineEmits<{
  open: [jobId: string]
  togglePause: []
  stop: []
  retry: []
  export: []
}>()

const rows = computed(() => props.detail.rows)
const stats = computed(() => batchStats(rows.value))
const folder = computed(() => props.detail.batch.folder ?? '')

const running = computed(() => stats.value.running > 0)
const finished = computed(() => !running.value && stats.value.pending === 0)

const bar = computed(() => ({
  ok: `${stats.value.okRatio * 100}%`,
  bad: `${stats.value.badRatio * 100}%`,
  cur: running.value ? `${(1 / Math.max(1, stats.value.total)) * 100}%` : '0%',
}))

/** 顶部那条提示：能解释"为什么慢 / 为什么停"的才出现。 */
const banner = computed(() => {
  if (props.paused) return '已暂停：当前这首跑完就不再派发下一首，点「继续」接着跑。'
  if (props.detail.stats.failed && finished.value) {
    return `${props.detail.stats.failed} 首没跑完：点「重试失败的」只重排这几首，已经好的不会重跑。`
  }
  return ''
})

/** 行内阶段格：✓ 完成 / 62% / ✗ 失败 / —。 */
function stageCell(row: JobRow, phase: PhaseKey): { text: string, tone: string, progress: number } {
  const stage = row.stages?.[phase]
  const state = stage?.state ?? 'idle'
  if (state === 'done') return { text: '✓', tone: 'ok', progress: 1 }
  if (state === 'failed') return { text: '✗', tone: 'bad', progress: 0 }
  if (state === 'cancelled') return { text: '已停', tone: 'idle', progress: stage?.progress ?? 0 }
  if (state === 'skipped') return { text: '跳过', tone: 'idle', progress: 0 }
  if (state === 'running') return { text: `${Math.round((stage?.progress ?? 0) * 100)}%`, tone: 'run', progress: stage?.progress ?? 0 }
  if (state === 'queued' && row.stage === phase) return { text: '排队', tone: 'run', progress: 0 }
  return { text: '—', tone: 'idle', progress: 0 }
}

/** 这一行为什么失败（只给一句；完整三段的在工程页）。 */
function rowError(row: JobRow): string {
  return row.stages?.transcribe?.error || row.stages?.lyrics?.error || ''
}

function elapsed(row: JobRow): string {
  return row.elapsed_seconds ? formatSpan(row.elapsed_seconds) : '—'
}

function elapsedTotal(): string {
  return formatSpan(stats.value.elapsedSeconds)
}
</script>

<template>
  <section class="card batch-head">
    <div class="bh-top">
      <div class="bh-title">{{ detail.batch.title }}</div>
      <span class="chip blue" title="一台显卡同时跑两首只会一起变慢，这一版固定串行">串行</span>
      <span v-if="folder" class="chip" :title="folder">{{ folder }}</span>
      <div class="bh-actions">
        <button v-if="running || stats.pending" class="btn-secondary" :disabled="busy"
                @click="emit('togglePause')">
          {{ paused ? '继续' : '暂停' }}
        </button>
        <button v-if="running || stats.pending" class="btn-secondary danger" :disabled="busy"
                @click="emit('stop')">
          停止
        </button>
        <button v-if="stats.failed" class="btn-secondary" :disabled="busy" @click="emit('retry')">
          重试失败的 {{ stats.failed }} 首
        </button>
        <button v-if="stats.done" class="btn-primary" @click="emit('export')">
          打包下载 {{ stats.done }} 个 SVP
        </button>
      </div>
    </div>

    <div class="segbar">
      <i class="ok" :style="{ width: bar.ok }" />
      <i class="bad" :style="{ width: bar.bad }" />
      <i class="cur" :style="{ width: bar.cur }" />
    </div>

    <div class="bh-stats">
      <span><b>{{ stats.done }}</b> / {{ stats.total }} 完成</span>
      <span v-if="stats.failed" class="bad-text"><b>{{ stats.failed }}</b> 首失败</span>
      <span v-if="stats.pending || running">剩 <b>{{ stats.pending + stats.running }}</b> 首</span>
      <span v-if="stats.current">当前：<b>{{ stats.current.name }}</b></span>
      <span v-if="stats.remainingSeconds">预计还剩 <b>{{ formatSpan(stats.remainingSeconds) }}</b></span>
      <span>{{ finished ? '用时' : '已用' }} <b>{{ elapsedTotal() }}</b></span>
    </div>

    <div v-if="banner" class="banner warn">{{ banner }}</div>
  </section>

  <section class="card btable">
    <div class="btable-head">
      <div class="no">#</div><div>歌名</div><div>时长</div>
      <div>扒谱</div><div>歌词</div><div class="right">用时</div><div class="right">操作</div>
    </div>
    <div class="btable-body">
      <div
        v-for="(row, index) in rows"
        :key="row.id"
        class="brow"
        :class="{ cur: row.state === 'running', fail: row.state === 'failed' }"
        @click="emit('open', row.id)"
      >
        <div class="no">{{ index + 1 }}</div>
        <div class="nm">
          <span class="t">{{ row.name }}</span>
          <span v-if="row.has_lrc" class="lrcchip on" title="有同名 LRC：逐行精确对齐">词</span>
          <small v-if="row.state === 'failed'" :title="rowError(row)">{{ rowError(row) }}</small>
        </div>
        <div class="dim">{{ row.audio_seconds ? formatSpan(row.audio_seconds) : '—' }}</div>
        <div class="ph" :class="stageCell(row, 'transcribe').tone">
          <span v-if="stageCell(row, 'transcribe').tone === 'run' && row.stages.transcribe.state === 'running'"
                class="minibar">
            <i :style="{ width: `${stageCell(row, 'transcribe').progress * 100}%` }" />
          </span>
          {{ stageCell(row, 'transcribe').text }}
        </div>
        <div class="ph" :class="stageCell(row, 'lyrics').tone">
          <span v-if="stageCell(row, 'lyrics').tone === 'run' && row.stages.lyrics.state === 'running'"
                class="minibar">
            <i :style="{ width: `${stageCell(row, 'lyrics').progress * 100}%` }" />
          </span>
          {{ stageCell(row, 'lyrics').text }}
        </div>
        <div class="dim right">{{ elapsed(row) }}</div>
        <div class="right">
          <button v-if="row.state === 'failed'" class="link" :disabled="busy"
                  @click.stop="emit('retry')">
            重试
          </button>
          <button v-else class="link" @click.stop="emit('open', row.id)">打开</button>
        </div>
      </div>
    </div>
  </section>

  <section v-if="finished && stats.total" class="card summary">
    <div>
      <div class="big">
        {{ stats.done }}/{{ stats.total }} 完成 · 用时 {{ elapsedTotal() }}
      </div>
      <div class="sub">
        {{ stats.failed ? `其中 ${stats.failed} 首失败，可以只重试这几首` : '全部成功，可以打包拿 SVP 了' }}
      </div>
    </div>
    <div class="acts">
      <button v-if="stats.done" class="btn-primary" @click="emit('export')">
        打包下载 {{ stats.done }} 个 SVP
      </button>
      <button v-if="stats.failed" class="btn-secondary" :disabled="busy" @click="emit('retry')">
        只重试失败的 {{ stats.failed }} 首
      </button>
    </div>
  </section>
</template>
