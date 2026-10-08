<script setup lang="ts">
/**
 * 播放头。单独一个组件是有意的：音频的 currentTime 每秒更新好几次，
 * 如果让结果区直接读它，整块卷帘（几百个和弦块）会跟着重渲染。
 * 这里只有这一条细线在动。
 */
import { useAudioPlayer } from '@/composables/useAudio'

const props = defineProps<{ ratio: number }>()
const player = useAudioPlayer()
</script>

<template>
  <span
    v-if="player.currentTime.value > 0"
    class="playhead"
    :style="{ left: `${player.currentTime.value * props.ratio}px` }"
  />
</template>
