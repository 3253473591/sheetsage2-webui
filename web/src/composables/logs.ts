/**
 * 日志抽屉里的"一行"：`pushLog` 的唯一写入点在这里，组件只负责画。
 *
 * 单独一层是为了**打断依赖环**：事件流（`useJobEvents`）、上传（`useUploads`）、
 * 回收站（`useTrashBackup`）都要写日志；如果这个类型留在 `useStudio` 里，
 * 那几个 composable 就得反过来 import 聚合入口。
 */

import { ref } from 'vue'
import type { PhaseKey } from '@/types'

export type LogTone = 'info' | 'ok' | 'warn' | 'error'

export interface LogLine {
  seq: number
  phase: PhaseKey | null
  time: number
  text: string
  tone: LogTone
}

/** 只留最近这些行：一个任务的解码事件可能上千条，全留着没意义。 */
const MAX_LOGS = 400

export function useLogs() {
  const logs = ref<LogLine[]>([])

  function pushLog(seq: number, phase: PhaseKey | null, text: string,
                   tone: LogTone = 'info', time = Date.now() / 1000): void {
    logs.value.push({ seq: seq || logs.value.length + 1, phase, time, text, tone })
    if (logs.value.length > MAX_LOGS) logs.value.splice(0, logs.value.length - MAX_LOGS)
  }

  function clearLogs(): void {
    logs.value = []
  }

  return { logs, pushLog, clearLogs }
}
