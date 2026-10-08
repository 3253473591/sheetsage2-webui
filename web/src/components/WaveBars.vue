<script setup lang="ts">
/**
 * 波形条。数据是 0~1 的包络（后端 `peaks` 接口算好的），这里只负责画。
 *
 * 用 canvas 而不是几百个 `<i>`：一首 5 分钟的歌要 1200 根柱子，DOM 会卡；
 * 而且 canvas 能顺手把"已播放"部分换个颜色。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    values: number[]
    color?: string
    playedColor?: string
    progress?: number
    interactive?: boolean
  }>(),
  { color: '#cbd5e1', playedColor: '#93c5fd', progress: 0, interactive: false },
)
const emit = defineEmits<{ seek: [fraction: number] }>()

const canvas = ref<HTMLCanvasElement | null>(null)
let observer: ResizeObserver | null = null

function draw(): void {
  const element = canvas.value
  if (!element) return
  const ratio = window.devicePixelRatio || 1
  const width = element.clientWidth || 300
  const height = element.clientHeight || 40
  element.width = Math.max(1, Math.floor(width * ratio))
  element.height = Math.max(1, Math.floor(height * ratio))
  const context = element.getContext('2d')
  if (!context) return
  context.scale(ratio, ratio)
  context.clearRect(0, 0, width, height)

  const values = props.values
  if (!values.length) return
  const columns = Math.max(1, Math.min(values.length, Math.floor(width / 2)))
  const step = values.length / columns
  const barWidth = width / columns
  const playedX = props.progress * width
  for (let index = 0; index < columns; index += 1) {
    // 一个显示列对应若干数据桶：取最大，波形才不会被抹平
    let peak = 0
    const from = Math.floor(index * step)
    const to = Math.max(from + 1, Math.floor((index + 1) * step))
    for (let cursor = from; cursor < to && cursor < values.length; cursor += 1) {
      peak = Math.max(peak, values[cursor] ?? 0)
    }
    const barHeight = Math.max(1, peak * (height - 2))
    const x = index * barWidth
    context.fillStyle = x < playedX ? props.playedColor : props.color
    context.fillRect(x, (height - barHeight) / 2, Math.max(0.6, barWidth - 0.6), barHeight)
  }
}

function click(event: MouseEvent): void {
  if (!props.interactive) return
  const element = canvas.value
  if (!element) return
  const rect = element.getBoundingClientRect()
  emit('seek', Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)))
}

onMounted(() => {
  draw()
  if (canvas.value && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => draw())
    observer.observe(canvas.value)
  }
})
onBeforeUnmount(() => observer?.disconnect())

watch(() => [props.values, props.progress, props.color], draw, { deep: false })
</script>

<template>
  <div class="wave" :style="{ cursor: interactive ? 'pointer' : 'default' }">
    <canvas ref="canvas" @click="click" />
  </div>
</template>
