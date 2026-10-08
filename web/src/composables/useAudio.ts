/**
 * 播放器：模块级单例，因为同一时刻只应该有一路音频在响。
 *
 * 用 `Audio` 对象而不是 `<template>` 里的 `<audio>` 元素，是为了让音轨卡和卷帘（播放头）
 * 共享同一个时间轴——两边都读同一个 `currentTime`。
 */

import { ref } from 'vue'

const playing = ref(false)
const currentTime = ref(0)
const duration = ref(0)
const ready = ref(false)
const failed = ref(false)

let audio: HTMLAudioElement | null = null
let source = ''
/** 当前装着的是哪条音频（混音原曲 / 干声），界面靠它决定在哪一行显示播放状态。 */
const currentSource = ref('')

function ensure(): HTMLAudioElement {
  if (audio) return audio
  const element = new Audio()
  element.preload = 'metadata'
  element.addEventListener('play', () => (playing.value = true))
  element.addEventListener('pause', () => (playing.value = false))
  element.addEventListener('ended', () => (playing.value = false))
  element.addEventListener('timeupdate', () => (currentTime.value = element.currentTime))
  element.addEventListener('loadedmetadata', () => {
    duration.value = Number.isFinite(element.duration) ? element.duration : 0
    ready.value = true
  })
  element.addEventListener('error', () => {
    failed.value = true
    playing.value = false
  })
  audio = element
  return element
}

/** 换音频：只有换了 src 才重新加载，避免每次切换都从头缓冲。 */
export function load(url: string): void {
  if (source === url) return
  source = url
  currentSource.value = url
  const element = ensure()
  failed.value = false
  ready.value = false
  currentTime.value = 0
  duration.value = 0
  element.src = url
  element.load()
}

export function toggle(): void {
  const element = ensure()
  if (!source) return
  if (element.paused) void element.play().catch(() => (failed.value = true))
  else element.pause()
}

export function pause(): void {
  audio?.pause()
}

/** 按比例定位（0~1），卷帘点击用。 */
export function seekFraction(fraction: number): void {
  if (!audio || !duration.value) return
  const target = Math.min(Math.max(fraction, 0), 1) * duration.value
  audio.currentTime = target
  currentTime.value = target
}

export function seekSeconds(seconds: number): void {
  if (!audio || !duration.value) return
  audio.currentTime = Math.min(Math.max(seconds, 0), duration.value)
  currentTime.value = audio.currentTime
}

export function useAudioPlayer() {
  return { playing, currentTime, duration, ready, failed, source: currentSource, load, toggle,
           pause, seekFraction, seekSeconds }
}
