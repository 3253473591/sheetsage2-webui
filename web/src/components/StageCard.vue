<script setup lang="ts">
import { ref } from 'vue'
import type { ParamRow } from '@/params'
import type { PhaseInfo, StageSnapshot } from '@/types'

defineProps<{
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

function onSelect(key: string, event: Event): void {
  emit('option', key, (event.target as HTMLSelectElement).value)
}
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
      </span>
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
