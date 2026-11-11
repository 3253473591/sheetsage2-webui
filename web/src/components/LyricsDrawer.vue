<script setup lang="ts">
/**
 * 右侧「歌词源」抽屉：拖入工程后任何时刻（扒谱中/扒谱完）都能打开，
 * 粘贴官方 LRC/TXT，可复制、可修改，点「应用」存进工程选项。
 *
 * 应用之后「识别歌词」会跳过语音识别，直接拿这份词对齐（见 asr.LyricLine）。
 * 歌词识别正在跑时「应用/清空」禁用——后端 set_options 也会 409 拦一道，
 * 那一趟用的是开始时的词源，改了界面就和结果对不上。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  /** 当前工程选项里的歌词源（空串 = 没设，走语音识别） */
  value: string
  /** 歌词识别正在跑：应用/清空锁死 */
  running?: boolean
}>()
const emit = defineEmits<{ apply: [text: string] }>()

const open = ref(false)
const draft = ref('')
const copied = ref(false)

watch(
  () => [props.value, open.value] as const,
  () => {
    if (open.value && draft.value !== props.value) draft.value = props.value
  },
  { immediate: true },
)

const stats = computed(() => {
  const lines = draft.value.split('\n').map((s) => s.trim()).filter(Boolean)
  const timed = /\[\d+:\d+/.test(draft.value)
  const chars = lines.join('').length
  return { lines: lines.length, chars, timed, head: lines[0] ?? '' }
})

/** 有改动还没应用：tab 上挂点、面板里黄条提醒。 */
const dirty = computed(() => draft.value !== props.value)

function apply(): void {
  emit('apply', draft.value)
}
async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(draft.value)
    copied.value = true
    setTimeout(() => (copied.value = false), 1500)
  } catch {
    /* 剪贴板被拒：textarea 本身可以手动复制 */
  }
}
</script>

<template>
  <div class="lyr-drawer" :class="{ open }">
    <div class="lyr-tab" @click="open = !open">
      歌词源<span v-if="dirty" class="lyr-dot" />
    </div>
    <div class="lyr-panel">
      <div class="lyr-head">
        <b>歌词源</b>
        <button class="link" @click="open = false">收起</button>
      </div>
      <p class="lyr-note">
        粘贴官方 LRC 或 TXT 歌词，可随时复制、修改。点「应用」后，
        <b>识别歌词将跳过语音识别</b>，直接拿这份词对齐；清空则恢复语音识别。
        建议先跑扒谱再粘贴——对齐用的音符来自扒谱结果。
      </p>
      <textarea
        v-model="draft"
        class="lyr-text"
        spellcheck="false"
        :placeholder="'[00:02:16]XXXXXXXXX…（带时间戳的 LRC 可逐行精确对齐；纯 TXT 不建议）'"
      />
      <div
        v-if="draft.trim() && !stats.timed"
        class="lyr-warn"
      >
        未检测到时间戳：纯 TXT 不建议使用——对齐只能估算，行位置会漂移。
        请用带时间戳的 LRC（[分:秒.厘秒] 词）。
      </div>
      <div class="lyr-meta" :class="{ warn: dirty }">
        <template v-if="draft.trim()">
          {{ stats.lines }} 行 · {{ stats.chars }} 字 ·
          {{ stats.timed ? '带时间戳，逐行精确对齐' : '纯文本，按段落时长均分（估）' }}
        </template>
        <template v-else>空 = 走语音识别</template>
        <span v-if="dirty"> · 有未应用的修改</span>
      </div>
      <div class="lyr-actions">
        <button class="tag-btn" :disabled="!draft.trim()" @click="copy">
          {{ copied ? '已复制' : '复制' }}
        </button>
        <button
          class="tag-btn"
          :disabled="!draft.trim() || running"
          :title="running ? '歌词识别正在跑，请等它结束或先取消' : ''"
          @click="draft = ''"
        >
          清空
        </button>
        <button
          class="lyr-apply"
          :disabled="running"
          :title="running ? '歌词识别正在跑，请等它结束或先取消' : ''"
          @click="apply"
        >
          应用
        </button>
      </div>
    </div>
  </div>
</template>
