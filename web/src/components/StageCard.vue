<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ParamRow } from '@/params'
import type { ArtifactCheck, PhaseInfo, StageSnapshot } from '@/types'

const props = defineProps<{
  order: number
  phase: PhaseInfo
  stage: StageSnapshot
  pill: { text: string; tone: 'ok' | 'run' | 'idle' | 'bad' }
  /** 参数面板的行（来自 `/api/env` 的取值表 + 工程当前值，见 `@/params`） */
  params: ParamRow[]
  /** 只读：4G 特供锁死，或这一阶段正在跑（那一趟用的是改动前的值） */
  locked?: boolean
  /** 为什么不能改（鼠标悬停） */
  lockNote?: string
  actions: { key: string; label: string; kind: 'primary' | 'secondary';
             disabled?: boolean; title?: string }[]
  hint?: string
}>()
const emit = defineEmits<{
  action: [key: string]
  /** 改了某个参数：`key` 是 `JobOptions` 的字段名 */
  option: [key: string, value: string]
}>()

const showParams = ref(true)
const showErrorDetail = ref(false)

function onSelect(key: string, event: Event): void {
  emit('option', key, (event.target as HTMLSelectElement).value)
}

/** 产物自检（G4）：`done` 旁边那个"自检 21/21"，有缺失时标黄。 */
const check = computed<ArtifactCheck | null>(() => {
  const raw = props.stage.detail?.artifacts
  if (!raw || typeof raw !== 'object') return null
  const data = raw as Partial<ArtifactCheck>
  if (typeof data.total !== 'number') return null
  return {
    ok: Boolean(data.ok),
    checked: Number(data.checked ?? 0),
    total: data.total,
    missing: data.missing ?? [],
    problems: data.problems ?? [],
  }
})

/**
 * "上次进展 xx 秒前"（G6）：心跳只证明 HTTP 连接活着，不证明任务在推进。
 * 超过 60 秒没动静就标黄——那时候用户该怀疑是不是卡住了。
 */
const idleSeconds = computed(() => {
  if (props.stage.state !== 'running') return 0
  const last = props.stage.last_advance_at
  if (!last) return 0
  return Math.max(0, Date.now() / 1000 - last)
})
const stagnant = computed(() => idleSeconds.value >= 60)
</script>

<template>
  <div class="card stage" :class="[stage.state]">
    <div class="sg-head">
      <div class="sg-no">{{ stage.state === 'done' ? '✓' : stage.state === 'failed' ? '!' : order }}</div>
      <div class="sg-title">{{ phase.label }}</div>
      <div class="sg-engine">{{ phase.engine }}</div>
      <span class="pill" :class="pill.tone">{{ pill.text }}</span>
    </div>

    <div class="bar">
      <div :style="{ width: `${Math.round((stage.progress || 0) * 100)}%` }" />
    </div>

    <div class="sg-step">
      <span>
        {{ stage.error || stage.step || hint || '待开始' }}
        <template v-if="stage.message && stage.state === 'running'"> · {{ stage.message }}</template>
      </span>
      <span v-if="stage.elapsed_seconds">
        {{ stage.state === 'running' ? '已用' : '用时' }} {{ stage.elapsed_seconds.toFixed(0) }}s
        <template v-if="stage.state === 'running' && idleSeconds >= 5">
          · <b :class="{ 'sg-stagnant': stagnant }">上次进展 {{ idleSeconds.toFixed(0) }}s 前</b>
        </template>
      </span>
    </div>

    <!-- 失败时的"下一步"（人话，来自后端的错误码表；技术原文折叠起来） -->
    <div v-if="stage.error_action" class="sg-error-action">
      <span>下一步：{{ stage.error_action }}</span>
      <button
        v-if="stage.error_detail"
        class="link"
        @click="showErrorDetail = !showErrorDetail"
      >
        {{ showErrorDetail ? '收起详情' : '详情' }}
      </button>
    </div>
    <pre v-if="showErrorDetail && stage.error_detail" class="sg-error-detail">{{ stage.error_detail }}</pre>

    <div v-if="check" class="sg-check" :class="{ bad: !check.ok }" :title="check.problems.join('\n')">
      自检 {{ check.checked }}/{{ check.total }}
      <template v-if="!check.ok && check.missing.length">
        · 缺 {{ check.missing.slice(0, 3).join('、') }}{{ check.missing.length > 3 ? ' 等' : '' }}
      </template>
      <template v-else-if="check.ok">通过</template>
    </div>

    <div class="params" :class="{ collapsed: !showParams }">
      <template v-for="row in params" :key="row.key">
        <span>{{ row.label }}</span>
        <div class="btn-group">
          <select
            v-if="row.widget === 'select'"
            class="lang-select"
            :value="row.value"
            :disabled="locked"
            :title="locked ? lockNote : ''"
            @change="onSelect(row.key, $event)"
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
            :disabled="locked"
            :title="locked ? lockNote : (item.hint ?? '')"
            @click="emit('option', row.key, item.value)"
          >
            {{ item.label }}
          </button>
        </div>
      </template>
      <p v-if="locked" class="param-lock">{{ lockNote || '参数已锁定' }}</p>
    </div>

    <div class="sg-foot">
      <button class="link" @click="showParams = !showParams">
        参数 {{ showParams ? '▴' : '▾' }}
      </button>
      <span class="spacer" />
      <button
        v-for="action in actions"
        :key="action.key"
        :class="action.kind === 'primary' ? 'btn-primary' : 'btn-secondary'"
        :disabled="action.disabled"
        :title="action.title"
        @click="emit('action', action.key)"
      >
        {{ action.label }}
      </button>
    </div>
  </div>
</template>
