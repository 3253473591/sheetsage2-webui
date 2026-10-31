/**
 * 钢琴卷帘交互原型的**无头冒烟测试**。
 *
 *     cd web && node smoke-pianoroll.mjs
 *
 * 为什么需要它：卷帘的手感全在几何换算里——拖一格到底走多少 tick、吸附是吸到最近格
 * 还是四舍五入、拉边会不会把时值拉到 0、缩放锚点有没有锚在鼠标下。这些用眼睛看只能
 * 看个大概，而它们恰恰是最容易写错、且错了以后每首歌都会歪的地方。
 *
 * jsdom 没有布局引擎（`getBoundingClientRect()` 一律返回 0），所以这里把网格区的矩形
 * 打桩成一个固定值，于是 clientX → 内容坐标 的映射变成可预期的。原型那一侧的几何
 * 换算全是纯函数，DOM 只负责把坐标喂进去——这就是它能在无头环境里被驱动的原因。
 *
 * 判据：结构渲染正确 + tick↔秒 往返一致（含变速）+ 拖移/拉边/新建/删除/框选/量化/
 * 撤销重做/缩放锚定 各自的数值结果符合预期。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { JSDOM, VirtualConsole } from 'jsdom'

const htmlPath = fileURLToPath(new URL('../编辑页交互原型.html', import.meta.url))
const html = readFileSync(htmlPath, 'utf8')

// ---------------------------------------------------------------------------
// 断言
// ---------------------------------------------------------------------------
let pass = 0
const failures = []
function ok(name, cond, detail) {
  if (cond) {
    pass += 1
    console.log(`  \u2713 ${name}`)
  } else {
    failures.push(detail ? `${name} \u2014 ${detail}` : name)
    console.log(`  \u2717 ${name}${detail ? ` \u2014 ${detail}` : ''}`)
  }
}
const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps
function section(title) {
  console.log(`\n${title}`)
}

// ---------------------------------------------------------------------------
// 起 DOM
// ---------------------------------------------------------------------------
const problems = []
const virtualConsole = new VirtualConsole()
virtualConsole.on('jsdomError', (e) => problems.push(`jsdomError: ${e?.message ?? e}`))

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole,
})
const { window } = dom
const { document } = window

const PR = window.__PR__
if (!PR) {
  console.error('原型脚本没有跑起来：window.__PR__ 不存在')
  process.exit(1)
}

// ---------------------------------------------------------------------------
// 给网格区打一个确定的矩形。RECT 之后，客户端坐标 = RECT + 内容坐标。
//
// 关键：真实浏览器里内容滚到右边时，元素矩形的 left 会变成**负的**——它描述的是
// "内容原点现在在视口的哪个位置"。这里必须照抄这个语义（left 减去 scrollLeft），
// 否则「缩放锚定在鼠标下」根本模拟不出来：缩放挪动 scrollLeft 之后，同一个 clientX
// 在打桩里指向的内容坐标不会跟着变。
// ---------------------------------------------------------------------------
const RECT = { left: 200, top: 80, width: 4000, height: 1400 }
const ZERO = {
  left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0,
  toJSON() { return this },
}
const scrollEl = () => window.__PR__.els.scroll
window.HTMLElement.prototype.getBoundingClientRect = function () {
  if (this.id !== 'canvas' && this.id !== 'ruler') return ZERO
  const sx = Number(scrollEl()?.scrollLeft) || 0
  const sy = Number(scrollEl()?.scrollTop) || 0
  const left = RECT.left - sx
  const top = RECT.top - sy
  return {
    left, top, right: left + RECT.width, bottom: top + RECT.height,
    width: RECT.width, height: RECT.height, x: left, y: top,
    toJSON() { return this },
  }
}
/** 每个用得上坐标的小节开头调一次，免得上一节的滚动影响下一节的期望值。 */
function resetScroll() {
  scrollEl().scrollLeft = 0
  scrollEl().scrollTop = 0
}
/**
 * 把 A3–E6 这条带子对位到视口时，scrollTop 应该是多少（和原型 centerOnBaseRange 同源）。
 * 行号 = 108 - pitch，所以 E6 的顶边在 (108-88) 行处；带子比视口矮时才居中留白。
 */
function baseRangeScrollTop() {
  const rh = window.__PR__.state.view.rowHeight
  const vh = window.__PR__.state.view.viewport.h
  const slack = Math.max(0, vh - window.__PR__.api.baseRange().keys * rh) / 2
  return Math.max(0, (108 - 88) * rh - slack)
}

/**
 * 音符**体内**的横坐标：从左边起 1/4 处。
 *
 * 不能写死 `+4`。手柄宽度现在随音符宽度收（见原型 handleWidth），横向 100% 时
 * 一个 1/8 音符才 5px 宽，`+4` 会正好落在手柄里，于是"想拖动"变成"拉边"。
 */
function bodyX(n) {
  const x0 = window.__PR__.api.tickToX(n.tick)
  const x1 = window.__PR__.api.tickToX(n.tick + n.dur)
  return x0 + (x1 - x0) * 0.25
}

/**
 * 视口坐标 → 该点下方的时间。
 *
 * 不能拿内容坐标直接去比：滚动之后，同一个视口点对应的内容坐标是**会变**的。
 * 「缩放锚在鼠标下」保证的是**视口**下那个时间点不动——所以必须经矩形换算一次。
 * （第一版这里直接 `xToTick(anchorX)`，等于把内容坐标当成不变量，测出了个假失败。）
 */
function tickAtClient(clientX) {
  const r = window.__PR__.els.canvas.getBoundingClientRect()
  return window.__PR__.api.xToTick(clientX - r.left)
}

// 固定一个视口：100% 时 rowHeight = 750/25 = 30px，pxPerSec = 1200/60 = 20px/s。
// 都是整数，后面所有期望值都能手算核对。
window.__PR__.api.setViewport(1200, 750)

const mouse = (type, x, y, init = {}) =>
  new window.MouseEvent(type, {
    bubbles: true, cancelable: true, button: 0,
    clientX: RECT.left + x, clientY: RECT.top + y,
    ...init,
  })
const toCanvas = (type, x, y, init) => PR.els.canvas.dispatchEvent(mouse(type, x, y, init))
const toWindow = (type, x, y, init) => window.dispatchEvent(mouse(type, x, y, init))
const press = (x, y, init) => toCanvas('mousedown', x, y, init)
const move = (x, y, init) => toWindow('mousemove', x, y, init)
const release = (x, y) => toWindow('mouseup', x, y)

const key = (k, init = {}) =>
  window.dispatchEvent(new window.KeyboardEvent('keydown', { key: k, bubbles: true, ...init }))

/** 按 id 取音符（拖拽后对象是原地改的，但删/撤后要重新查）。 */
const noteById = (id) => PR.state.notes.find((n) => n.id === id)
/**
 * 直接用原型里 pitchToY 的等价算式：pitch → 内容 y。
 * （原型没导出 pitchToY，但它就是 (108-p)*rowHeight。）
 */
const pitchY = (p) => (108 - p) * PR.state.view.rowHeight

// ---------------------------------------------------------------------------
section('1. 结构渲染')
// ---------------------------------------------------------------------------
ok('琴键 97 个 (C0..C8)', document.querySelectorAll('.piano-key').length === 97,
  `实际 ${document.querySelectorAll('.piano-key').length}`)
ok('网格行 97 个', document.querySelectorAll('.grid-row').length === 97,
  `实际 ${document.querySelectorAll('.grid-row').length}`)
ok('标尺画出了小节号', document.querySelectorAll('.ruler-label.bar').length >= 8)

const track0 = PR.state.notes.filter((n) => n.track === 0)
/**
 * 全部按 **id** 索引，不缓存对象引用。
 *
 * 这不是洁癖：撤销一次「新建」时，原型用 before/after 两个全集快照整体替换
 * `state.notes`（`replay()` 里 `list.map(n => ({...n}))`），旧的对象引用当场作废。
 * 早先这里用 `track0[30]` 这种索引取音符，在第 8 节之后全部指到了幽灵对象上——
 * 测试因此报了 3 个假失败。
 */
const t0ids = track0.map((n) => n.id)
const rendered = document.querySelectorAll('.note:not(.ghost)').length
ok('当前轨(人声)音符全部渲染', rendered === track0.length, `${rendered} vs ${track0.length}`)
ok('其它轨渲染为幽灵线', document.querySelectorAll('.note.ghost').length === PR.state.notes.length - track0.length)
ok('播放头存在', !!document.getElementById('playhead'))
ok('总时长 > 0', PR.api.totalSec() > 0)

// ---------------------------------------------------------------------------
section('2. tick \u2194 秒 往返（常量速度）')
// ---------------------------------------------------------------------------
let roundTripOk = true
let worst = 0
for (const t of [0, 1, 240, 480, 1920, 3840, 15360, 30721]) {
  const back = PR.api.secToTick(PR.api.tickToSec(t))
  worst = Math.max(worst, Math.abs(back - t))
  if (!near(back, t, 1e-6)) roundTripOk = false
}
ok('常量速度下往返一致', roundTripOk, `最大误差 ${worst}`)

// 120bpm 下，一小节 4/4 = 2 秒
ok('120bpm · 1 小节 = 2.0 秒', near(PR.api.tickToSec(1920), 2.0, 1e-9),
  `${PR.api.tickToSec(1920)}`)

// ---------------------------------------------------------------------------
section('3. tick \u2194 秒 往返（变速：第 2 小节起 60bpm）')
// ---------------------------------------------------------------------------
const savedTempo = PR.state.tempoMap
PR.state.tempoMap = [{ tick: 0, bpm: 120 }, { tick: 1920, bpm: 60 }]
let vOk = true
for (const t of [0, 1920, 2000, 3840, 7680]) {
  const back = PR.api.secToTick(PR.api.tickToSec(t))
  if (!near(back, t, 1e-6)) { vOk = false; console.log(`    变速往返失败 @${t} -> ${back}`) }
}
ok('变速下往返一致（分段线性成立）', vOk)
// 0~1920 tick @120bpm = 2s；1920~3840 tick @60bpm = 1920/480 = 4 个四分音符 = 4s。
// 合计 6s。（第一版我按 2s 写的期望值，是算错了——测试替我把这个也算清了。）
ok('变速后第 2 小节 = 6.0 秒', near(PR.api.tickToSec(3840), 6.0, 1e-9),
  `${PR.api.tickToSec(3840)}`)
PR.state.tempoMap = savedTempo

// ---------------------------------------------------------------------------
section('4. 拖移音符：吸附到网格')
// ---------------------------------------------------------------------------
const grid = PR.api.gridTicks()          // 1/8 -> 240 tick
const pxPerGrid = (PR.api.tickToSec(grid)) * PR.state.view.pxPerSec
resetScroll()
const target = noteById(t0ids[3])
const orig = { tick: target.tick, pitch: target.pitch }
const start = { x: bodyX(target), y: pitchY(target.pitch) + 6 }

const undoBefore = PR.marks.undo.length
press(start.x, start.y)
move(start.x + pxPerGrid, start.y - 2 * PR.state.view.rowHeight)
release(start.x + pxPerGrid, start.y - 2 * PR.state.view.rowHeight)

ok('拖动一格：tick 精确 +' + grid, near(target.tick, orig.tick + grid, 1e-6),
  `${orig.tick} -> ${target.tick}`)
ok('上移两行：pitch 精确 +2', target.pitch === orig.pitch + 2,
  `${orig.pitch} -> ${target.pitch}`)
ok('一次手势 = 一条撤销记录', PR.marks.undo.length === undoBefore + 1,
  `${undoBefore} -> ${PR.marks.undo.length}`)

// ---------------------------------------------------------------------------
section('5. 拖动不足半格：吸附回原位（不抖）')
// ---------------------------------------------------------------------------
resetScroll()
const t5 = noteById(t0ids[5])
const o5 = t5.tick
const p5 = { x: bodyX(t5), y: pitchY(t5.pitch) + 6 }
// 位移取"0.3 格"——不足半格，吸附必须把它吸回原位。
const tiny = pxPerGrid * 0.3
press(p5.x, p5.y)
move(p5.x + tiny, p5.y)
release(p5.x + tiny, p5.y)
ok('吸附开启时不动', near(t5.tick, o5, 1e-6), `${o5} -> ${t5.tick}`)

// Alt = 临时关吸附。注意是**按下之后**才按住 Alt 的——DAW 里这是最常见的动作，
// 所以原型的吸附判定必须每次 move 重读 altKey，不能冻结在 mousedown。
press(p5.x, p5.y)
move(p5.x + tiny, p5.y, { altKey: true })
release(p5.x + tiny, p5.y)
const expected = o5 + Math.round(PR.api.secToTick(tiny / PR.state.view.pxPerSec))
ok('拖动中途按住 Alt 也能绕过吸附', near(t5.tick, expected, 1e-6),
  `期望 ${expected}，实际 ${t5.tick}`)

// ---------------------------------------------------------------------------
section('6. 撤销 / 重做')
// ---------------------------------------------------------------------------
PR.api.undo()
ok('撤销后回到原 tick', near(t5.tick, o5, 1e-6), `${t5.tick}`)
PR.api.redo()
ok('重做后回到新 tick', near(t5.tick, expected, 1e-6), `${t5.tick}`)
PR.api.undo()

// 撤销第 4 节那次拖移
PR.api.undo()
ok('撤销恢复 tick 与 pitch', near(target.tick, orig.tick, 1e-6) && target.pitch === orig.pitch,
  `${target.tick}/${target.pitch}`)
PR.api.redo()
ok('重做再次应用', near(target.tick, orig.tick + grid, 1e-6) && target.pitch === orig.pitch + 2)

// ---------------------------------------------------------------------------
section('7. 拉右边缘改时值')
// ---------------------------------------------------------------------------
resetScroll()
const t7 = noteById(t0ids[10])
const dur0 = t7.dur
const y7 = pitchY(t7.pitch) + 6

/**
 * 期望值不能写成「原时值 + 像素增量」。
 *
 * 拉边吸的是**端点的绝对位置**（snapTick(xToTick(x))），不是「时值加了多少」。
 * 起手点为了落进 6px 手柄里必然偏离真实右边缘几个像素，这点偏差会一起被吸掉。
 * 所以这里把鼠标**直接停在某个网格线上**，期望值就是从这条线反推的整数格。
 */
const rightEdge = () => PR.api.tickToX(t7.tick + t7.dur)
const targetEnd = Math.ceil((t7.tick + dur0) / grid) * grid + grid * 3
press(rightEdge() - 2, y7)
move(PR.api.tickToX(targetEnd), y7)
release(PR.api.tickToX(targetEnd), y7)
ok('时值 = 目标端点 - 起始 tick', near(t7.dur, targetEnd - t7.tick, 1e-6),
  `${dur0} -> ${t7.dur}，期望 ${targetEnd - t7.tick}`)
ok('时值落在网格上', near(t7.dur % grid, 0, 1e-6), `${t7.dur} % ${grid}`)
ok('tick 未被改动', near(t7.tick, noteById(t0ids[10]).tick, 1e-6))
PR.api.undo()
ok('撤销恢复原时值', near(t7.dur, dur0, 1e-6))

// 拉回起点左边：时值不能变成 0、负数，也不该短于一格
press(rightEdge() - 2, y7)
move(PR.api.tickToX(t7.tick) - 500, y7)
release(PR.api.tickToX(t7.tick) - 500, y7)
ok('反向猛拉时值不会变 0 或负数', t7.dur > 0, `dur=${t7.dur}`)
ok('反向猛拉被夹在一格', near(t7.dur, grid, 1e-6), `dur=${t7.dur}`)
PR.api.undo()

// ---------------------------------------------------------------------------
section('8. 双击空白：新建音符')
// ---------------------------------------------------------------------------
const countBefore = PR.state.notes.length
const emptyTick = PR.api.snapTick(PR.api.secToTick(PR.state.view.pxPerSec ? 0 : 0) + 60 * grid)
// 挑一个当前轨确实空着的音高区间：用一个远离人声音域的极低音
const newPitch = 24
const cx = PR.api.tickToX(emptyTick) + 2
const cy = pitchY(newPitch) + 6
toCanvas('dblclick', cx, cy)
const created = PR.state.notes.find((n) => n.pitch === newPitch && n.track === 0)
ok('新建了音符', !!created && PR.state.notes.length === countBefore + 1)
ok('位置吸附到网格', created ? near(created.tick % grid, 0, 1e-6) : false,
  created ? `tick=${created.tick}` : '')
ok('音高正确', created ? created.pitch === newPitch : false)
ok('新建后自动选中', PR.state.selection.size === 1 && created && PR.state.selection.has(created.id))
PR.api.undo()
ok('撤销后音符消失', PR.state.notes.length === countBefore)

// ---------------------------------------------------------------------------
section('9. 删除：Delete 键 / 右键 / 橡皮')
// ---------------------------------------------------------------------------
const victim = noteById(t0ids[20])
const id20 = victim.id
const n0 = PR.state.notes.length
PR.state.selection = new Set([id20])
PR.api.renderAll()
key('Delete')
ok('Delete 删除选中音符', !noteById(id20) && PR.state.notes.length === n0 - 1)
PR.api.undo()
ok('撤销恢复被删音符', !!noteById(id20) && PR.state.notes.length === n0)

const rightTarget = PR.state.notes.find((n) => n.id === id20)
toCanvas('contextmenu', bodyX(rightTarget), pitchY(rightTarget.pitch) + 6)
ok('右键删除音符', !noteById(id20))
PR.api.undo()

const eraseTarget = PR.state.notes.find((n) => n.id === id20)
PR.api.setTool('erase')
toCanvas('mousedown', bodyX(eraseTarget), pitchY(eraseTarget.pitch) + 6)
ok('橡皮单击删除', !noteById(id20))
PR.api.undo()
PR.api.setTool('select')

// ---------------------------------------------------------------------------
section('10. 框选：多选与整组移动')
// ---------------------------------------------------------------------------
resetScroll()
PR.state.selection.clear()
PR.api.renderAll()
// 纵向要包住整句的音域（人声第一句的 pitch 在 60~79），只圈几行会漏掉大半。
const xL = 2
const xR = PR.api.tickToX(noteById(t0ids[8]).tick)
const yTop = pitchY(84) - 2
const yBottom = pitchY(48)
press(xL, yTop)
move((xL + xR) / 2, yBottom)
move(xR, yBottom)
release(xR, yBottom)
ok('框选到多个音符', PR.state.selection.size >= 4, `选中 ${PR.state.selection.size}`)

const selIds = [...PR.state.selection]
const beforeTicks = selIds.map((id) => noteById(id).tick)
const anchorNote = noteById(selIds[0])
const ax = bodyX(anchorNote)
const ay = pitchY(anchorNote.pitch) + 6
press(ax, ay)
move(ax + pxPerGrid * 2, ay)
release(ax + pxPerGrid * 2, ay)
const afterTicks = selIds.map((id) => noteById(id).tick)
const deltas = afterTicks.map((t, i) => t - beforeTicks[i])
ok('整组一起移动，位移一致', deltas.every((d) => near(d, deltas[0], 1e-6)),
  deltas.join(','))
ok('整组位移 = 2 格', near(deltas[0], grid * 2, 1e-6), `${deltas[0]}`)
PR.api.undo()

// ---------------------------------------------------------------------------
section('11. 量化')
// ---------------------------------------------------------------------------
resetScroll()
const t11 = noteById(t0ids[30])
const savedTick = t11.tick
t11.tick = savedTick + 37      // 故意弄歪
t11.dur = t11.dur + 19
PR.state.selection = new Set([t11.id])
PR.api.quantize()
const q11 = noteById(t0ids[30])
ok('量化后 tick 落在网格', near(q11.tick % grid, 0, 1e-6), `tick=${q11.tick}`)
ok('量化后时值落在网格', near(q11.dur % grid, 0, 1e-6), `dur=${q11.dur}`)
ok('量化把歪掉的 37 tick 吸回最近格',
  near(q11.tick, Math.round(savedTick / grid) * grid, 1e-6), `${q11.tick}`)
PR.api.undo()

// ---------------------------------------------------------------------------
section('12. 画笔：按住拖出新音符并按网格定长')
// ---------------------------------------------------------------------------
const n12 = PR.state.notes.length
PR.api.setTool('pencil')
const px12 = PR.api.tickToX(60 * grid)
const py12 = pitchY(31) + 6
press(px12, py12)
move(px12 + pxPerGrid * 2, py12)
release(px12 + pxPerGrid * 2, py12)
const pen = PR.state.notes.find((n) => n.pitch === 31 && n.track === 0)
ok('画笔新建了音符', !!pen && PR.state.notes.length === n12 + 1)
ok('拖出的长度按网格', pen ? near(pen.dur % grid, 0, 1e-6) && pen.dur >= grid * 2 - 1e-6 : false,
  pen ? `dur=${pen.dur}` : '')
PR.api.undo()
PR.api.setTool('select')
ok('撤销后画笔音符消失', PR.state.notes.length === n12)

// ---------------------------------------------------------------------------
section('13. 横向缩放锚定在鼠标下')
// ---------------------------------------------------------------------------
resetScroll()
const anchorX = 700
const clientX = RECT.left + anchorX
const tickBeforeZoom = tickAtClient(clientX)
const ppsBefore = PR.state.view.pxPerSec
PR.api.zoomX(2, clientX)
const tickAfterZoom = tickAtClient(clientX)
ok('缩放后鼠标下的时间点不变',
  near(tickBeforeZoom, tickAfterZoom, 1e-3),
  `${tickBeforeZoom} -> ${tickAfterZoom}`)
ok('pxPerSec 确实变了', PR.state.view.pxPerSec === ppsBefore * 2,
  `${ppsBefore} -> ${PR.state.view.pxPerSec}`)
ok('scrollLeft 有跟着补偿', scrollEl().scrollLeft > 0, `scrollLeft=${scrollEl().scrollLeft}`)
PR.api.zoomX(0.5, clientX)
ok('缩放有上下限（不会到 0 或 NaN）',
  Number.isFinite(PR.state.view.pxPerSec) && PR.state.view.pxPerSec > 0,
  `${PR.state.view.pxPerSec}`)
resetScroll()

// ---------------------------------------------------------------------------
section('14. 纵向缩放')
// ---------------------------------------------------------------------------
const rowBefore = PR.state.view.rowHeight
PR.api.zoomY(1.2, RECT.top + 400)
ok('rowHeight 变化', PR.state.view.rowHeight !== rowBefore, `${rowBefore} -> ${PR.state.view.rowHeight}`)
ok('琴键高度跟着变',
  document.querySelector('.piano-key').style.height === `${PR.state.view.rowHeight}px`,
  document.querySelector('.piano-key').style.height)
ok('网格行数仍是 97', document.querySelectorAll('.grid-row').length === 97)

// ---------------------------------------------------------------------------
section('15. 播放/暂停状态机')
// ---------------------------------------------------------------------------
PR.api.play()
ok('播放后状态为播放中', PR.api.isPlaying() === true)
PR.api.pause()
ok('暂停后状态为暂停', PR.api.isPlaying() === false)
PR.state.playheadSec = 5
PR.api.stop()
ok('停止回到开头', PR.state.playheadSec === 0)

// ---------------------------------------------------------------------------
section('16. 选区一致性')
// ---------------------------------------------------------------------------
PR.state.selection = new Set([999999])
PR.api.renderAll()
PR.api.undo()   // 触发一次重放，选区里的幽灵 id 应当被清掉
ok('选区不会残留已不存在的 id',
  [...PR.state.selection].every((id) => PR.state.notes.some((n) => n.id === id)))

// ---------------------------------------------------------------------------
section('17. 视图基准：纵向 100% = A3~E6 满屏')
// ---------------------------------------------------------------------------
PR.state.view.vZoom = 100
PR.api.setViewport(1200, 750)
resetScroll()
const vp = () => PR.state.view.viewport
const base = PR.api.baseRange()

ok('基准音域是 A3..E6', base.low === 57 && base.high === 88,
  `${PR.api.pitchName(base.low)}..${PR.api.pitchName(base.high)}`)
ok('音名换算对得上（A3=57, E6=88）',
  PR.api.pitchName(57) === 'A3' && PR.api.pitchName(88) === 'E6',
  `${PR.api.pitchName(57)} / ${PR.api.pitchName(88)}`)
ok('A3..E6 是 32 个键（偶数，没有正中那一行）', base.keys === 32 && 88 - 57 + 1 === 32)
ok('100% 时 32 行正好等于视口高',
  near(PR.state.view.rowHeight * base.keys, vp().h, 1e-9),
  `${PR.state.view.rowHeight} × ${base.keys} vs ${vp().h}`)

PR.api.centerOnBaseRange()
ok('对位后的 scrollTop 与公式一致',
  near(scrollEl().scrollTop, baseRangeScrollTop(), 1e-6),
  `${scrollEl().scrollTop} vs ${baseRangeScrollTop()}`)
ok('视口顶部正好是 E6',
  PR.api.yToPitch(scrollEl().scrollTop) === 88,
  `顶部音高 ${PR.api.pitchName(PR.api.yToPitch(scrollEl().scrollTop))}`)
ok('视口底部正好是 A3',
  PR.api.yToPitch(scrollEl().scrollTop + vp().h - 1) === 57,
  `底部音高 ${PR.api.pitchName(PR.api.yToPitch(scrollEl().scrollTop + vp().h - 1))}`)
ok('可见恰好 32 行（没有多露半行）',
  PR.api.yToPitch(scrollEl().scrollTop) - PR.api.yToPitch(scrollEl().scrollTop + vp().h - 1) + 1 === 32,
  `${PR.api.yToPitch(scrollEl().scrollTop) - PR.api.yToPitch(scrollEl().scrollTop + vp().h - 1) + 1}`)
ok('C4 已不在正中（32 键偶数，旧规则不再适用）',
  Math.abs(PR.api.pitchToY(60) + PR.state.view.rowHeight / 2 - scrollEl().scrollTop - vp().h / 2) > 1,
  `C4 中线距顶 ${PR.api.pitchToY(60) + PR.state.view.rowHeight / 2 - scrollEl().scrollTop}`)

// 纵向缩小时带子比视口矮，应当居中留白而不是贴顶
PR.state.view.vZoom = 50
PR.api.setViewport(1200, 750)
PR.api.centerOnBaseRange()
ok('缩到 50% 时带子在视口里居中（上下留白相等）',
  near(scrollEl().scrollTop, baseRangeScrollTop(), 1e-6)
  && scrollEl().scrollTop > 0,
  `${scrollEl().scrollTop}`)
PR.state.view.vZoom = 100
PR.api.setViewport(1200, 750)

// ---------------------------------------------------------------------------
section('18. 视图基准：横向 100% = 一分钟满屏')
// ---------------------------------------------------------------------------
PR.state.view.hZoom = 100
PR.api.setViewport(1200, 750)
ok('100% 时 60 秒正好等于视口宽',
  near(PR.state.view.pxPerSec * 60, vp().w, 1e-9),
  `${PR.state.view.pxPerSec} × 60 vs ${vp().w}`)
// 视口变宽，同样 100% 下 pxPerSec 必须跟着变——证明基准是相对视口的，不是写死像素
PR.api.setViewport(2400, 750)
ok('视口翻倍 → 100% 的 pxPerSec 也翻倍（基准相对视口）',
  near(PR.state.view.pxPerSec, 40, 1e-9), `${PR.state.view.pxPerSec}`)
PR.api.setViewport(1200, 750)

// ---------------------------------------------------------------------------
section('19. 缩放范围：横向 50~500%，纵向 50~200%')
// ---------------------------------------------------------------------------
PR.state.view.hZoom = 100
PR.api.zoomX(100, RECT.left + 600)          // 猛放大
ok('横向上限 500%', near(PR.state.view.hZoom, 500, 1e-9), `${PR.state.view.hZoom}`)
PR.api.zoomX(0.0001, RECT.left + 600)       // 猛缩小
ok('横向下限 50%', near(PR.state.view.hZoom, 50, 1e-9), `${PR.state.view.hZoom}`)
PR.state.view.vZoom = 100
PR.api.zoomY(100, RECT.top + 400)
ok('纵向上限 200%', near(PR.state.view.vZoom, 200, 1e-9), `${PR.state.view.vZoom}`)
PR.api.zoomY(0.0001, RECT.top + 400)
ok('纵向下限 50%', near(PR.state.view.vZoom, 50, 1e-9), `${PR.state.view.vZoom}`)
ok('缩放后 pxPerSec / rowHeight 都是有限正数',
  Number.isFinite(PR.state.view.pxPerSec) && PR.state.view.pxPerSec > 0
  && Number.isFinite(PR.state.view.rowHeight) && PR.state.view.rowHeight > 0)

// ---------------------------------------------------------------------------
section('20. 网格细分线随吸附粒度变化')
// ---------------------------------------------------------------------------
// 细分线有 4px 的最小间距门槛，先把横向放大到 500% 让 1/16、1/32 都画得出来
PR.state.view.hZoom = 500
PR.state.view.vZoom = 100
PR.api.setViewport(1200, 750)
PR.api.renderAll()

PR.api.setSnap(8)
const sub8 = PR.api.subLineCount()
const base8 = PR.api.gridLineCount() - sub8

PR.api.setSnap(16)
const sub16 = PR.api.subLineCount()

PR.api.setSnap(32)
const sub32 = PR.api.subLineCount()

PR.api.setSnap(4)
const sub4 = PR.api.subLineCount()

PR.api.setSnap(0)
const sub0 = PR.api.subLineCount()
const base0 = PR.api.gridLineCount() - sub0

/**
 * 每拍之间插几根细分线 = den/4 - 1。
 *
 * 不是"翻倍"：1/8 每拍插 1 根（半拍处），1/16 插 3 根，1/32 插 7 根。
 * 我在原型注释里一开始就写错了这条，是测试把它逼出来的。
 */
const beatsInProject = Math.round(
  PR.api.secToTick(PR.api.totalSec()) / (PR.state.ppq * 4 / PR.state.timeSig.den),
)
const expectSub = (den) => beatsInProject * (den / 4 - 1)

ok('1/8：每拍插 1 根', sub8 === expectSub(8), `${sub8}，期望 ${expectSub(8)}`)
ok('1/16：每拍插 3 根', sub16 === expectSub(16), `${sub16}，期望 ${expectSub(16)}`)
ok('1/32：每拍插 7 根', sub32 === expectSub(32), `${sub32}，期望 ${expectSub(32)}`)
ok('1/4 的细分与拍线重合 → 不出细分线', sub4 === 0, `${sub4}`)
ok('吸附关闭 → 不出细分线', sub0 === 0, `${sub0}`)
ok('细分线变化不影响小节/拍线的数量', base8 === base0, `${base8} vs ${base0}`)

// 放大/缩小到细分线太密时应当自动隐藏，不能糊成一片
const sub32far = sub32
PR.state.view.hZoom = 50
PR.api.renderAll()
PR.api.setSnap(32)
const sub32near = PR.api.subLineCount()
ok('横向缩到 50% 时过密的细分线自动隐藏',
  sub32far > 0 && sub32near === 0, `500%: ${sub32far} 条, 50%: ${sub32near} 条`)

PR.state.view.hZoom = 100
PR.api.setSnap(8)
PR.api.renderAll()

// ---------------------------------------------------------------------------
console.log(`\n${'='.repeat(56)}`)
if (problems.length) {
  console.log('jsdom 报错：')
  for (const p of problems) console.log(`  ! ${p}`)
}
if (failures.length) {
  console.log(`失败 ${failures.length} 项：`)
  for (const f of failures) console.log(`  - ${f}`)
  console.log(`\n通过 ${pass} 项，失败 ${failures.length} 项`)
  process.exit(1)
}
console.log(`全部通过：${pass} 项判据`)
