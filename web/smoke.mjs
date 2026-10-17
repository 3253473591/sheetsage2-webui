/**
 * UI 冒烟测试：把**构建产物**放进 jsdom，对着**真跑着的服务**渲染一遍。
 *
 *     node smoke.mjs http://127.0.0.1:8123
 *
 * 为什么需要它：`vue-tsc` 只保证类型和模板编译通过，管不了运行期——props 拿到 undefined、
 * 模板里少写一个 `.value`、后端字段改名，这些都要真跑一次才知道。这里用 jsdom 提供 DOM，
 * 用 node 的 fetch 代理前端的请求到真服务，于是整条链路（组件树 → API → 后端）都过一遍。
 *
 * 浏览器有、jsdom 没有的四个东西要打桩：fetch（转发到真服务）、EventSource（用 fetch 读 SSE）、
 * Audio（播放器）、ResizeObserver（波形 canvas 自适应）。canvas 的 2d 上下文 jsdom 给不了，
 * 组件的 `draw()` 会因为拿不到 context 直接返回——这正是它该有的降级行为。
 *
 * 判据：渲染出的 DOM 必须含有原型里的那些结构（音轨 / 两张阶段卡 / 参数面板的七行 /
 * 和弦块 / 歌词块），**包含本项目比原型多的两处**（「量化」两个取值、语言下拉框 6 个选项），
 * 且控制台没有 Vue 的报错或警告。
 */
import { JSDOM, VirtualConsole } from 'jsdom'

const baseUrl = (process.argv[2] ?? 'http://127.0.0.1:8000').replace(/\/$/, '')
/**
 * `--stem`：先往当前工程传一条"干声"再渲染（内容不校验，这里就传一段假字节），
 * 用来覆盖"有干声"那一半界面。默认不传，覆盖的是"自动回退到混音原曲"那一半。
 */
const withStem = process.argv.includes('--stem')

/**
 * jsdom 没有 canvas 实现，`getContext('2d')` 会往虚拟控制台报 "Not implemented"。
 * 我们关心的恰恰是"拿不到 context 时组件要能降级"，所以这类噪音直接吞掉，
 * 其它 jsdom 内部错误仍然记下来。
 */
const IGNORED = /Not implemented: HTMLCanvasElement/
const problems = []
const virtualConsole = new VirtualConsole()
virtualConsole.on('jsdomError', (error) => {
  if (!IGNORED.test(String(error?.message ?? error))) {
    problems.push(`jsdomError: ${error?.message ?? error}`)
  }
})

const dom = new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>', {
  url: `${baseUrl}/`,
  pretendToBeVisual: true,
  virtualConsole,
})

const originalError = console.error
const originalWarn = console.warn
console.error = (...args) => {
  const text = args.map(String).join(' ')
  if (!IGNORED.test(text)) problems.push(`console.error: ${text}`)
  originalError(...args)
}
console.warn = (...args) => {
  const text = args.map(String).join(' ')
  if (text.includes('[Vue warn]') && !IGNORED.test(text)) problems.push(`Vue warn: ${text}`)
  originalWarn(...args)
}

const { window } = dom

/** node 里有些全局（navigator）只有 getter，只能 defineProperty 覆盖。 */
function define(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
}

for (const [name, value] of Object.entries({
  window,
  document: window.document,
  navigator: window.navigator,
  location: window.location,
  localStorage: window.localStorage,
  HTMLElement: window.HTMLElement,
  SVGElement: window.SVGElement,
  MathMLElement: window.MathMLElement,
  Element: window.Element,
  Node: window.Node,
  Text: window.Text,
  Comment: window.Comment,
  DocumentFragment: window.DocumentFragment,
  Event: window.Event,
  MouseEvent: window.MouseEvent,
  DragEvent: window.DragEvent ?? window.Event,
  CustomEvent: window.CustomEvent,
  KeyboardEvent: window.KeyboardEvent,
  MutationObserver: window.MutationObserver,
  getComputedStyle: window.getComputedStyle.bind(window),
  requestAnimationFrame: (callback) => setTimeout(() => callback(Date.now()), 16),
  cancelAnimationFrame: (handle) => clearTimeout(handle),
  devicePixelRatio: 1,
})) {
  define(name, value)
}

/** fetch 转发到真服务：前端用的是相对路径（`/api/...`）。 */
const realFetch = globalThis.fetch
globalThis.fetch = (input, init) => {
  const url = typeof input === 'string' ? `${baseUrl}${input}` : input
  return realFetch(url, init)
}

/** EventSource：用 fetch 读 SSE，够这个冒烟测试用。 */
class FakeEventSource {
  constructor(url) {
    this.url = `${baseUrl}${url}`
    this.onmessage = null
    this.onerror = null
    this.closed = false
    void this.run()
  }
  async run() {
    try {
      const response = await realFetch(this.url)
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (!this.closed) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? ''
        for (const frame of frames) {
          const line = frame.split('\n').find((row) => row.startsWith('data: '))
          if (line && this.onmessage) this.onmessage({ data: line.slice(6) })
        }
      }
      if (!this.closed && this.onerror) this.onerror(new Error('stream closed'))
    } catch (error) {
      if (!this.closed && this.onerror) this.onerror(error)
    }
  }
  close() {
    this.closed = true
  }
}
globalThis.EventSource = FakeEventSource

/** Audio：只要接口长得像就行，冒烟测试不播声音。 */
class FakeAudio {
  constructor() {
    this.currentTime = 0
    this.duration = 0
    this.paused = true
    this.src = ''
    this.preload = ''
    this.listeners = {}
  }
  addEventListener(name, handler) {
    ;(this.listeners[name] ??= []).push(handler)
  }
  emit(name) {
    for (const handler of this.listeners[name] ?? []) handler()
  }
  load() {}
  play() {
    this.paused = false
    this.emit('play')
    return Promise.resolve()
  }
  pause() {
    this.paused = true
    this.emit('pause')
  }
}
globalThis.Audio = FakeAudio
window.Audio = FakeAudio

class FakeResizeObserver {
  observe() {}
  disconnect() {}
}
globalThis.ResizeObserver = FakeResizeObserver
window.ResizeObserver = FakeResizeObserver

// ---------------------------------------------------------------- 跑
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

if (withStem) {
  const listing = await (await realFetch(`${baseUrl}/api/jobs`)).json()
  const target = listing.jobs.find((job) => job.state === 'done')
  if (target) {
    const form = new FormData()
    form.append('file', new Blob([new Uint8Array(64)], { type: 'audio/wav' }), '测试干声.wav')
    const response = await realFetch(`${baseUrl}/api/jobs/${target.id}/stems/vocal`,
                                     { method: 'POST', body: form })
    console.log(`预先传了一条干声 -> HTTP ${response.status}`)
  }
}

const assets = await (await realFetch(`${baseUrl}/`)).text()
const match = assets.match(/src="(\/assets\/[^"]+\.js)"/)
if (!match) {
  console.error('拿不到构建产物：页面里没有 /assets/*.js')
  process.exit(2)
}
// node 不能 import http://，先把产物落到磁盘再用 file:// 加载
const here = dirname(fileURLToPath(import.meta.url))
const local = join(here, '.smoke', 'app.mjs')
mkdirSync(dirname(local), { recursive: true })
writeFileSync(local, await (await realFetch(`${baseUrl}${match[1]}`)).text(), 'utf-8')
await import(pathToFileURL(local).href)

await new Promise((resolve) => setTimeout(resolve, 1500))

const root = window.document.querySelector('#app')
const text = root?.textContent ?? ''
const counts = {
  tracks: root?.querySelectorAll('.track').length ?? 0,
  stages: root?.querySelectorAll('.stage').length ?? 0,
  sections: root?.querySelectorAll('.blk.sec').length ?? 0,
  chords: root?.querySelectorAll('.blk.chord').length ?? 0,
  lyrics: root?.querySelectorAll('.blk.lyric').length ?? 0,
  projectItems: root?.querySelectorAll('.proj-item').length ?? 0,
  kv: root?.querySelectorAll('.kv b').length ?? 0,
}

/** 阶段卡上的按钮（含禁用状态）——门禁规则靠这个在真界面上验证。 */
const buttons = [...(root?.querySelectorAll('.stage button') ?? [])].map((node) => ({
  text: (node.textContent ?? '').trim(),
  disabled: Boolean(node.disabled),
}))
const stageButtons = buttons.map((item) => `${item.text}${item.disabled ? '(禁用)' : ''}`)

/** 参数面板：原型那几行 + 本项目多的两处（「量化」和「语言」下拉框）。 */
const paramLabels = [...(root?.querySelectorAll('.stage .params > span') ?? [])]
  .map((node) => (node.textContent ?? '').trim())
const tagButtons = buttons.map((item) => item.text)
const selects = [...(root?.querySelectorAll('.stage .params select') ?? [])]
const languageSelect = selects.find((node) => node.querySelectorAll('option').length > 4)

/** 工程头：下载主按钮默认是 SVP（zip 降级到小箭头展开的菜单里）。 */
const headButtons = [...(root?.querySelectorAll('.proj-head .ph-actions button') ?? [])]
  .map((node) => (node.textContent ?? '').trim())

console.log('渲染结果：')
console.log(`  工程列表 ${counts.projectItems} 项 · 音轨 ${counts.tracks} 条 · 阶段卡 ${counts.stages} 张`)
console.log(`  段落块 ${counts.sections} · 和弦块 ${counts.chords} · 歌词块 ${counts.lyrics} · 概要数字 ${counts.kv}`)
console.log(`  参数面板行：${paramLabels.join(' / ')}`)
console.log(`  阶段按钮：${stageButtons.join(' / ')}`)
console.log(`  工程头按钮：${headButtons.join(' / ')}`)
console.log(`  语言下拉框：${languageSelect ? languageSelect.querySelectorAll('option').length : 0} 个选项`)
console.log(`  页面文字片段：${text.replace(/\s+/g, ' ').slice(0, 160)}`)

// ------------------------------------------------------------------ 回收站弹层
// P0 新加的弹层只编译过、没渲染过，这里点开看一眼：按钮在、弹层能挂出来、能关掉。
// 不点「删除」——那会动真工程；删除链路在 `pytest tests/test_webui.py` 里验过。
const trashButton = [...(root?.querySelectorAll('.nav-right button') ?? [])]
  .find((node) => (node.textContent ?? '').includes('回收站'))
const dialogProblems = []
if (!trashButton) {
  dialogProblems.push('顶栏没有「回收站」入口')
} else {
  trashButton.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  await new Promise((resolve) => setTimeout(resolve, 300))
  const title = window.document.querySelector('.modal-card .modal-title')?.textContent?.trim()
  if (title !== '回收站') dialogProblems.push(`回收站弹层没挂出来（标题是 ${title ?? '空'}）`)
  const closeButton = [...(window.document.querySelectorAll('.modal-card .modal-actions button') ?? [])]
    .find((node) => (node.textContent ?? '').includes('关闭'))
  if (!closeButton) dialogProblems.push('回收站弹层没有「关闭」按钮')
  else {
    closeButton.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
    await new Promise((resolve) => setTimeout(resolve, 200))
    if (window.document.querySelector('.modal-card')) dialogProblems.push('回收站弹层关不掉')
  }
}
console.log(`  回收站弹层：${dialogProblems.length ? dialogProblems.join('；') : '能打开、能关闭'}`)

const failures = []
if (counts.projectItems < 1) failures.push('工程列表是空的')
if (counts.tracks !== 3) failures.push(`音轨数应为 3（混音原曲 / 干声 / 人声主旋律），实际 ${counts.tracks}`)
if (counts.stages !== 2) failures.push(`阶段卡应为 2，实际 ${counts.stages}`)
if (counts.chords < 1) failures.push('没有和弦块')
if (!/扒谱/.test(text) || !/歌词识别/.test(text)) failures.push('两张阶段卡的文字不对')
if (!/已完成/.test(text)) failures.push('没有"已完成"状态')
if (!/干声/.test(text)) failures.push('没有干声音轨')
if (!/自动用混音原曲|更换/.test(text)) failures.push('干声槽既没显示回退说明，也没显示更换入口')
if (withStem && !/更换/.test(text)) failures.push('传了干声但界面没有"更换/移除"入口')
if (!withStem && !/自动用混音原曲/.test(text)) failures.push('没传干声但界面没写明会自动回退')

// 工作流门禁：这个工程是"扒谱 + 歌词都完成"的，所以歌词按钮应当**放开的**
if (!buttons.some((item) => item.text.includes('自动提取'))) {
  failures.push('阶段卡上没有「自动提取」按钮')
}
const lyricsButton = buttons.find((item) => /识别歌词|重新识别/.test(item.text))
if (!lyricsButton) {
  failures.push('阶段卡上没有歌词按钮')
} else if (lyricsButton.disabled) {
  failures.push('扒谱已完成，歌词按钮不该是禁用的')
}

// 参数面板：原型的三行（精度/设备/范围、模型/音源）+ 本项目多的几处
for (const label of ['精度', '设备', '范围', '量化', '模型', '音源', '段落', '语言']) {
  if (!paramLabels.includes(label)) failures.push(`参数面板少了「${label}」这一行`)
}
if (!tagButtons.includes('128 分三连音网格') || !tagButtons.includes('不量化')) {
  failures.push('参数面板上没有「量化」的两个取值（本项目比原型多出来的那一处）')
}
// 音高修正一律降一个八度，故意不做成选项
if (paramLabels.includes('音高')) {
  failures.push('参数面板上不该有「音高」这一行（固定降一个八度，不给用户选）')
}
if (!tagButtons.includes('只跑唱段') || !tagButtons.includes('全部段落')) {
  failures.push('参数面板上没有「段落」的两个取值（跳过前奏/间奏/尾奏）')
}
if (!languageSelect || languageSelect.querySelectorAll('option').length <= 4) {
  failures.push('参数面板上的识别语言不是多选项下拉框（本项目比原型多出来的那一处）')
}

// 下载默认是 SVP：主按钮点开勾选框，zip 退到小箭头展开的菜单里
if (!headButtons.some((label) => label.includes('SVP'))) {
  failures.push(`工程头没有「下载 SVP」主按钮（实际：${headButtons.join(' / ')}）`)
}
if (headButtons.some((label) => label.includes('下载全部'))) {
  failures.push('整包 zip 不该还是主按钮（应降到小箭头菜单里）')
}
for (const problem of problems) failures.push(problem)
for (const problem of dialogProblems) failures.push(problem)

if (failures.length) {
  console.error('\n冒烟测试失败：')
  for (const failure of failures) console.error(`  - ${failure}`)
  process.exit(1)
}
console.log('\n冒烟测试通过：组件树在真实数据上渲染正常，控制台无 Vue 报错')
// 显式退出：非终态的工程会挂着一个永不结束的 SSE 流，别让 node 卡在那儿
process.exit(0)
