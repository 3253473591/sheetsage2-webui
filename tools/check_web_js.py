"""Web 侧验收：JS 语法 + 卷帘换算与后端逐点一致 + 网格层次。

为什么需要：这个项目没有构建步骤，JS 全内联在 index.html 里，改错了只有打开浏览器
才发现。这里用 node 做语法检查，并且**拿后端的换算实现当基准**逐点比对卷帘的
"秒 ↔ 谱面位置"，把"音符吸到别的格子上 / 导出时间整体偏移"这类问题挡在交付前。

用法（在包根目录）：``python tools/check_web_js.py``
依赖 node；没有 node 就只跳过 JS 部分并明确说明。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INDEX = ROOT / "web" / "index.html"
ROLL = ROOT / "web" / "pianoroll.js"
TMP = ROOT / "_jscheck_tmp"

FAILS: list[str] = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILS.append(msg)


def run_node(args: list[str]) -> subprocess.CompletedProcess:
    """跑 node 并把输出按 UTF-8 解出来。

    必须显式指定编码：本机默认编码是 GBK，而 node 输出的是 UTF-8，脚本里的中文
    提示会让 ``text=True`` 在解码时直接抛 UnicodeDecodeError（表现为 stdout 为 None）。
    """
    return subprocess.run(
        args, capture_output=True, encoding="utf-8", errors="replace"
    )


# 用**变速**的小节表：一小节 2 秒、下一小节 1.6 秒（等价 120 → 150 BPM）。
# 单速度的夹具测不出"分段线性"是否正确，必须用变速的。
MEASURES = [
    {"start": 0.0, "end": 2.0, "score_start": 0.0, "score_end": 1.0},
    {"start": 2.0, "end": 3.6, "score_start": 1.0, "score_end": 2.0},
]
# 采样位置：覆盖首小节内、小节线上、次小节内、以及超出小节表（外推）
SAMPLE_POS = [0.0, 0.25, 0.5, 0.999, 1.0, 1.3, 1.75, 2.0, 2.5, 3.0]


def golden_from_backend() -> dict:
    """用后端自己的换算算基准值（不另写一份数学）。"""
    from app.abcp import _normalize_measures, _score_to_seconds

    table = _normalize_measures(MEASURES)
    tail_rate = (table[-1]["end"] - table[-1]["start"]) / max(
        1e-9, table[-1]["score_end"] - table[-1]["score_start"]
    )
    return {str(p): _score_to_seconds(p, table, tail_rate) for p in SAMPLE_POS}


NODE_HARNESS = r"""
/* 卷帘逻辑自检：在极简 DOM 桩上把组件跑起来，验证
   (1) posToSec 与后端逐点一致；(2) secToPos 是它的逆；(3) 网格步长与层次。
   数学不重写 —— 直接用 web/pianoroll.js 里的实现。 */
const fs = require("fs");
const [rollPath, fixturePath] = process.argv.slice(2);

function clsHas(el, c) { return (" " + (el.className || "") + " ").indexOf(" " + c + " ") >= 0; }
function clsSet(el, c, on) {
  var parts = (el.className || "").split(/\s+/).filter(Boolean);
  var i = parts.indexOf(c);
  if (on && i < 0) parts.push(c);
  if (!on && i >= 0) parts.splice(i, 1);
  el.className = parts.join(" ");
}

function mkEl(tag) {
  const e = {
    tagName: String(tag || "div").toUpperCase(), children: [], parent: null,
    style: {}, dataset: {}, className: "", __h: {},
    get classList() {
      const self = this;
      return {
        add(c) { clsSet(self, c, true); },
        remove(c) { clsSet(self, c, false); },
        toggle(c, force) { const on = force === undefined ? !clsHas(self, c) : !!force; clsSet(self, c, on); return on; },
        contains(c) { return clsHas(self, c); }
      };
    },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    remove() { if (this.parent) { const i = this.parent.children.indexOf(this); if (i >= 0) this.parent.children.splice(i, 1); } },
    addEventListener(type, fn) { (this.__h[type] = this.__h[type] || []).push(fn); },
    setAttribute() {}, removeAttribute() {},
    // 音符里有个 <span class="pr-ly"> 子元素，styleNote 会拿它显示歌词。
    // 桩返回 null 会让渲染在"有音符可见"时崩掉（不可见时反而不崩，最难查的那种）。
    querySelector() { return mkEl("span"); }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 460 }; },
    closest(sel) {
      const want = String(sel).replace(/^\./, "");
      let n = this;
      while (n) { if (clsHas(n, want)) return n; n = n.parent; }
      return null;
    },
    setPointerCapture() {}, releasePointerCapture() {},
    innerHTML: "", textContent: "", type: "", title: "", value: "",
    clientWidth: 800, clientHeight: 460, scrollLeft: 0, scrollTop: 0
  };
  return e;
}

/** 派发一个合成事件：直接调该元素上注册的监听器。
 *  组件把所有手势监听器都挂在同一个元素上，所以不做冒泡也等价。 */
function fire(el, type, props) {
  const ev = Object.assign({
    type, target: el, currentTarget: el, button: 0, pointerId: 1,
    clientX: 0, clientY: 0, shiftKey: false, ctrlKey: false, metaKey: false,
    repeat: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}
  }, props || {});
  ((el && el.__h && el.__h[type]) || []).forEach((fn) => fn(ev));
  return ev;
}

global.window = { addEventListener(type, fn) { (this.__h = this.__h || {})[type] = (this.__h[type] || []).concat(fn); } };
global.document = {
  createElement: mkEl,
  createElementNS: () => mkEl("svg"),
  getElementById: () => null
};
global.requestAnimationFrame = () => 0;

eval(fs.readFileSync(rollPath, "utf8"));

const PR = global.window.PianoRoll;
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
const inst = PR.create(mkEl("div"), {});
inst.load(fixture);
const d = inst._debug;

const fails = [];
function ok(cond, msg) { console.log((cond ? "  ok   " : "  FAIL ") + msg); if (!cond) fails.push(msg); }

console.log("\n[3] 秒 ↔ 谱面位置：与后端 abcp._score_to_seconds 逐点比对");
let maxErr = 0;
Object.keys(fixture.golden).forEach(function (k) {
  const want = fixture.golden[k];
  const got = d.posToSec(parseFloat(k));
  const err = Math.abs(got - want);
  if (err > maxErr) maxErr = err;
});
ok(maxErr < 1e-9, "posToSec 与后端逐点一致（最大误差 " + maxErr.toExponential(2) + " 秒）");

console.log("\n[4] secToPos 是 posToSec 的逆");
let roundErr = 0;
[0, 0.1, 0.7, 1.0, 1.9, 2.0, 2.4, 3.5, 3.6].forEach(function (pos) {
  const back = d.secToPos(d.posToSec(pos));
  roundErr = Math.max(roundErr, Math.abs(back - pos));
});
ok(roundErr < 1e-9, "往返换算无漂移（最大误差 " + roundErr.toExponential(2) + "）");
ok(Math.abs(d.secToPos(1.0) - 0.5) < 1e-9, "1.0 秒 = 谱面位置 0.5（首小节 2 秒 / 1 个全音符）");
ok(Math.abs(d.secToPos(2.8) - 1.5) < 1e-9, "2.8 秒 = 谱面位置 1.5（次小节 1.6 秒 / 1 个全音符）");

console.log("\n[5] 网格步长与档位");
ok(Math.abs(d.stepWhole() - 0.0625) < 1e-12,
   "默认网格 1/4 个四分音符(16 分) → 步长 0.0625 全音符（实际 " + d.stepWhole() + "）");
inst.setGridDiv(1);
ok(Math.abs(d.stepWhole() - 0.25) < 1e-12, "切到四分音符 → 步长 0.25");
inst.setGridDiv(6);
ok(Math.abs(d.stepWhole() - 0.25 / 6) < 1e-12, "切到 1/6 个四分音符(16 分三连音) → 步长 0.25/6");
ok(PR.GRIDS.length === 6, "网格档位就是需求点名的 6 档（实际 " + PR.GRIDS.length + "）");
ok(PR.DEFAULT_DIV === 4, "默认档位 = 1/4 个四分音符(16 分音符)");
inst.setGridDiv(4);

console.log("\n[6] 网格线层次与小节号");
const cols = d.collectGrid();
const bars = cols.lines.filter(l => l.level === 0);
ok(bars.length >= 2, "至少画出 2 条小节线（实际 " + bars.length + "）");
ok(bars.length && bars[0].barNo === 1 && bars[1].barNo === 2,
   "小节号从 1 开始递增（" + bars.slice(0, 2).map(b => b.barNo).join(", ") + "）");
ok(cols.lines.some(l => l.level === 1), "有拍线（level 1）");
ok(cols.lines.some(l => l.level === 2), "有细分线（level 2）");
const xs = cols.lines.map(l => l.x);
ok(xs.every(x => x >= -1 && x <= 900), "网格线全部落在可视区内（按可视区裁剪，不是全曲铺满）");

console.log("\n[7] 缩得太小时自动降级（只留粗档，不糊成一片）");
inst.setZoom(2, null);          // 极小缩放：细分线间距 < 6px
const tiny = d.collectGrid();
ok(!tiny.showSub, "细分级不画了");
inst.setZoom(60, null);
const normal = d.collectGrid();
ok(normal.showSub, "缩放正常时细分线回来");

console.log("\n[8] 歌词：按字符隔开 vs 按空格切词");
ok(JSON.stringify(d.splitLyricTokens("我爱你", true)) === JSON.stringify(["我", "爱", "你"]),
   "勾上＝一字一音（我爱你 → 我/爱/你）");
ok(JSON.stringify(d.splitLyricTokens("我爱你", false)) === JSON.stringify(["我爱你"]),
   "不勾＝整段当一个词");
ok(JSON.stringify(d.splitLyricTokens("wo ai ni", false)) === JSON.stringify(["wo", "ai", "ni"]),
   "不勾＝按空格切词（拼音/英文）");
ok(JSON.stringify(d.splitLyricTokens("wo ai ni", true)) === JSON.stringify(["w", "o", "a", "i", "n", "i"]),
   "勾上＝连空格也去掉，逐字符");
ok(d.splitLyricTokens("窗外的麻雀\n在电线杆上", true).length === 10,
   "换行/空白不影响逐字符计数（10 个字 → 10 个词）");
ok(JSON.stringify(d.splitLyricTokens("   ", false)) === JSON.stringify([]),
   "全是空白 → 空词表（调用方据此拒绝）");

console.log("\n[9] 歌词：铺词到音符（多退少不补）");
inst.load(fixture);
d.selectAll();
const sel = d.selectedList();
ok(sel.length === 1, "夹具里只有 1 个音符，全选后选中 1 个");
let r = d.fillLyricsOn(sel, ["我"]);
ok(r.filled === 1 && r.kept === 0 && r.dropped === 0, "词数与音符数相等 → 全部填入");
ok(d.state.tracks[0].notes[0].lyric === "我", "音符歌词已写入");
r = d.fillLyricsOn(sel, ["我", "爱", "你"]);
ok(r.filled === 1 && r.dropped === 2, "词比音符多 → 多余的词报回来，不静默丢");

// 三个音符的夹具：词比音符少时，剩下的必须保持原样（不能把已填的词冲掉）
inst.load({
  bpm: 120, beats_per_bar: 4, beat_unit: 4, unit_whole: 0.0625, duration: 4.0,
  measures: fixture.measures,
  tracks: [{
    voice: "Vocal", kind: "vocal", display: "主人声", is_vocal: true, editable: true,
    notes: [
      { start: 0.0, end: 0.4, pitch: 60, lyric: "已" },
      { start: 0.4, end: 0.8, pitch: 62, lyric: "有" },
      { start: 0.8, end: 1.2, pitch: 64, lyric: "词" }
    ]
  }]
});
d.selectAll();
const sel3 = d.selectedList();
ok(sel3.length === 3, "全选 3 个音符");
const r3 = d.fillLyricsOn(sel3, ["新", "词"]);
ok(r3.filled === 2 && r3.kept === 1, "只填了 2 个，剩 1 个不计入");
const lys = d.state.tracks[0].notes.map(n => n.lyric);
ok(JSON.stringify(lys) === JSON.stringify(["新", "词", "词"]),
   "未被覆盖的音符保持原文（" + lys.join("/") + "）");

console.log("\n[10] 新增音符默认歌词 la（用户点名要求）");
inst.load({
  bpm: 120, beats_per_bar: 4, beat_unit: 4, unit_whole: 0.0625, duration: 4.0,
  measures: fixture.measures,
  tracks: [{
    voice: "Vocal", kind: "vocal", display: "主人声", is_vocal: true, editable: true, notes: []
  }]
});
ok(inst.getPlaybackNotes().length === 0, "空轨：试听音符表为空");
inst.addNote(0, 0.0, 60);
const added = inst.getPlaybackNotes();
ok(added.length === 1, "addNote 后有一个音符");
ok(d.state.tracks[0].notes[0].lyric === "la", "新音符默认歌词为 la");
ok(Math.abs(added[0].end - added[0].start - 0.5) < 1e-9,
   "默认时值 = 一个四分音符（120BPM → 0.5 秒），实际 " + (added[0].end - added[0].start));

console.log("\n[11] 试听音符表与总时长");
inst.load(fixture);
const pb = inst.getPlaybackNotes();
ok(pb.length === 1 && pb[0].pitch === 60, "试听音符表带出音高");
ok(Math.abs(inst.totalSeconds() - 0.5) < 1e-9,
   "总时长按当前音符实算（不是加载时的 duration）");
d.state.tracks[0].notes[0].end = 3.0;
ok(Math.abs(inst.totalSeconds() - 3.0) < 1e-9, "改了音符长度，总时长跟着变");

console.log("\n[12] 页面用到的卷帘 API 都真的导出了");
// 这一类问题（页面调 roll.foo() 而组件没导出 foo）只有在浏览器里点一下才会暴露，
// 而它恰好是大改删改之后最容易漏的：所以拿页面里真实的调用点来核对。
let apiMissing = [];
(fixture.rollApiUsed || []).forEach(function (name) {
  if (typeof inst[name] !== "function") apiMissing.push(name);
});
ok(apiMissing.length === 0,
   "页面调用的 " + (fixture.rollApiUsed || []).length + " 个方法全部存在"
   + (apiMissing.length ? "（缺：" + apiMissing.join(", ") + "）" : ""));

console.log("\n[13] 指针接线：拖动 / 框选 / 点空白取消选择");
// 这一组是**行为**测试：把合成指针事件真的派发进组件。
// 起因是一次真实事故：lanes 上的 pointerdown/move/up 四个监听器被一次大改整段删掉，
// 而当时的验收只查语法和纯函数，全绿通过 —— 用户装上才发现"卷帘是死的"。
const FIX3 = {
  bpm: 120, beats_per_bar: 4, beat_unit: 4, unit_whole: 0.0625, duration: 4.0,
  measures: fixture.measures,
  tracks: [{
    voice: "Vocal", kind: "vocal", display: "主人声", is_vocal: true, editable: true,
    notes: [
      { start: 0.0, end: 0.4, pitch: 60, lyric: "la" },
      { start: 0.4, end: 0.8, pitch: 62, lyric: "la" },
      { start: 0.8, end: 1.2, pitch: 64, lyric: "la" }
    ]
  }]
};
const lanes = d.els.lanes, marqueeEl = d.els.marquee, scrollEl = d.els.scroll;

function noteEl(i) {
  return d.els.noteLayer.children.filter((c) => c.dataset && String(c.dataset.i) === String(i))[0];
}
function noteOf(i) { return d.state.tracks[0].notes[i]; }
function pressAndDrag(x0, y0, x1, y1, targetEl) {
  const t = targetEl || lanes;
  fire(lanes, "pointerdown", { clientX: x0, clientY: y0, target: t });
  fire(lanes, "pointermove", { clientX: x1, clientY: y1, target: t });
  fire(lanes, "pointerup", { clientX: x1, clientY: y1, target: t });
}

inst.load(FIX3);
ok(d.els.noteLayer.children.length === 3, "3 个音符都渲染出来了（" + d.els.noteLayer.children.length + "）");
ok((lanes.__h.pointerdown || []).length > 0, "lanes 上注册了 pointerdown 监听器");
ok((lanes.__h.pointermove || []).length > 0, "lanes 上注册了 pointermove 监听器");
ok((lanes.__h.pointerup || []).length > 0, "lanes 上注册了 pointerup 监听器");

// --- 全选后点空白应取消（用户报的 bug 1）
fire(global.window, "keydown", { key: "a", ctrlKey: true, target: { tagName: "DIV" } });
ok(d.selectedList().length === 3, "Ctrl+A 全选 3 个音符");
pressAndDrag(d.secToX(1.7), d.pitchToY(55) + 2, d.secToX(1.7), d.pitchToY(55) + 2, lanes);
ok(d.selectedList().length === 0, "点空白处（没拖动）取消选择");

// --- 框选（用户报的 bug 3）
// 矩形覆盖音高 59–63 那几行、时间 −0.05s–0.85s：正好罩住第 1、2 个音符
// （音高 60/62），第 3 个在音高 64 上、落在矩形上方所以不该被选中。
inst.load(FIX3);
const mx0 = d.secToX(-0.05), my0 = d.pitchToY(63) + 2;
const mx1 = d.secToX(0.85), my1 = d.pitchToY(59) + 2;
fire(lanes, "pointerdown", { clientX: mx0, clientY: my0, target: lanes });
fire(lanes, "pointermove", { clientX: mx1, clientY: my1, target: lanes });
ok(marqueeEl.style.display === "block", "框选时框选框被显示出来");
ok(parseFloat(marqueeEl.style.width) > 0 && parseFloat(marqueeEl.style.height) > 0,
   "框选框有实际大小（" + marqueeEl.style.width + " × " + marqueeEl.style.height + "）");
fire(lanes, "pointerup", { clientX: mx1, clientY: my1, target: lanes });
const selIdx = d.selectedList().map((m) => m.i).sort().join(",");
ok(selIdx === "0,1", "框选命中框内的第 1、2 个音符（实际 " + selIdx + "）");
ok(marqueeEl.style.display === "none", "松手后框选框收起");

// --- 拖动音符改音高（用户报的 bug 2：无法上下移动）
// 这里守着一个真 bug：gesture 忘了记 y0 → 纵向位移算出 NaN → 音高被写成 NaN。
inst.load(FIX3);
const el0 = noteEl(0);
const px = d.secToX(0.2), py = d.pitchToY(60) + 3;
pressAndDrag(px, py, px, py - 2 * d.rowH, el0);
ok(noteOf(0).pitch === 62, "往上拖 2 行 → 音高 60 → " + noteOf(0).pitch);
pressAndDrag(px, py, px, py + 3 * d.rowH, el0);
ok(noteOf(0).pitch === 59, "往下拖 3 行 → 音高 " + noteOf(0).pitch);
ok(Number.isFinite(noteOf(0).pitch) && Number.isFinite(noteOf(0).start),
   "拖完之后音高与起点都还是有效数字");

// 横向位移：吸附开着会落到 16 分网格上，关掉才是自由摆放
inst.load(FIX3);
pressAndDrag(px, py, px + d.secToX(0.2), py, noteEl(0));
ok(Math.abs(noteOf(0).start - 0.25) < 1e-6,
   "吸附开着：右拖 0.2s 落到最近的网格（0.250，实际 " + noteOf(0).start.toFixed(3) + "）");
inst.setSnap(false);
inst.load(FIX3);
pressAndDrag(px, py, px + d.secToX(0.2), py, noteEl(0));
ok(Math.abs(noteOf(0).start - 0.2) < 0.01,
   "关掉吸附：右拖 0.2s 就停在 0.2（实际 " + noteOf(0).start.toFixed(3) + "）");
inst.setSnap(true);

// --- 拖右边缘改长度（用户报的 bug 2：无法拖长度）
inst.load(FIX3);
const gripR = noteEl(0).children.filter((c) => c.dataset && c.dataset.grip === "r")[0];
ok(!!gripR, "音符上有右边缘把手");
const gy = d.pitchToY(60) + 3;
fire(lanes, "pointerdown", { clientX: d.secToX(0.4), clientY: gy, target: gripR });
fire(lanes, "pointermove", { clientX: d.secToX(0.7), clientY: gy, target: gripR });
fire(lanes, "pointerup", { clientX: d.secToX(0.7), clientY: gy, target: gripR });
ok(noteOf(0).end > 0.5, "拖右边缘把结束时间拉到 " + noteOf(0).end.toFixed(3));

console.log("\n[14] Ctrl+L 填词：有框选就从第一个被框选的音符开始");
inst.load(FIX3);
ok(d.lyricFillTarget().picked === false && d.lyricFillTarget().list.length === 3,
   "没选中时目标是整条轨（3 个音符）");
fire(lanes, "pointerdown", { clientX: d.secToX(0.35), clientY: d.pitchToY(65) + 2, target: lanes });
fire(lanes, "pointermove", { clientX: d.secToX(1.15), clientY: d.pitchToY(61) + 2, target: lanes });
fire(lanes, "pointerup", { clientX: d.secToX(1.15), clientY: d.pitchToY(61) + 2, target: lanes });
const tgt = d.lyricFillTarget();
ok(tgt.picked === true, "有框选时目标是选中集");
ok(tgt.list.map((m) => m.i).sort().join(",") === "1,2",
   "选中集是第 2、3 个音符（实际 " + tgt.list.map((m) => m.i).sort().join(",") + "）");
const rr = d.fillLyricsOn(tgt.list, ["甲", "乙"]);
ok(rr.filled === 2, "填入 2 个词");
const L3 = d.state.tracks[0].notes.map((n) => n.lyric);
ok(L3[0] === "la" && L3[1] === "甲" && L3[2] === "乙",
   "从第一个被框选的音符开始填、前面的音符不受影响（" + L3.join("/") + "）");

console.log("\n[15] 跟随走带位置（播放时视图跟着滚）");
inst.load(FIX3);
inst.setZoom(1000, null);
scrollEl.scrollLeft = 0;
inst.setPlaying(true);
inst.setPlayhead(1.2);
ok(scrollEl.scrollLeft > 0, "播放中走带跑出可视区 → 视图跟过去（scrollLeft=" + scrollEl.scrollLeft + "）");
inst.setPlaying(false);
const held = scrollEl.scrollLeft;
inst.setPlayhead(1.2);
ok(scrollEl.scrollLeft === held, "没在播放就不跟随（手动滚去看别处不会被打断）");
inst.setFollow(false);
inst.setPlaying(true);
scrollEl.scrollLeft = 0;
inst.setPlayhead(1.2);
ok(scrollEl.scrollLeft === 0, "关掉「跟随」后不再自动滚");
inst.setFollow(true);

console.log("\n[16] 框选后拖动 = 整组一起动（用户要求「统一上下移动」）");
inst.load(FIX3);
fire(lanes, "pointerdown", { clientX: d.secToX(-0.05), clientY: d.pitchToY(63) + 2, target: lanes });
fire(lanes, "pointermove", { clientX: d.secToX(0.85), clientY: d.pitchToY(59) + 2, target: lanes });
fire(lanes, "pointerup", { clientX: d.secToX(0.85), clientY: d.pitchToY(59) + 2, target: lanes });
ok(d.selectedList().length === 2, "框选到 2 个音符");
const before = d.state.tracks[0].notes.map((n) => n.pitch);
const grab = d.pitchToY(before[0]) + 3;
pressAndDrag(d.secToX(0.2), grab, d.secToX(0.2), grab - 2 * d.rowH, noteEl(0));
const after = d.state.tracks[0].notes.map((n) => n.pitch);
ok(after[0] === before[0] + 2 && after[1] === before[1] + 2,
   "选中的两个一起 +2 行（" + before.join("/") + " → " + after.join("/") + "）");
ok(after[2] === 64, "没选中的第 3 个音符不受影响（" + after[2] + "）");

console.log("\n[17] 往左拖后一个音符 = 侵占前一个的尾巴（用户反馈 1）");
// 一条长音符 A(0–0.8s) + 一条 B(0.8–1.2s)。把 B 的左边往左拖进 A 里：
// 以前 B 一松手就被推回 A.end（看着像"拖不动"）；现在**谁被拖谁赢**，A 收尾让位。
const FIX2 = {
  bpm: 120, beats_per_bar: 4, beat_unit: 4, unit_whole: 0.0625, duration: 4.0,
  measures: fixture.measures,
  tracks: [{
    voice: "Vocal", kind: "vocal", display: "主人声", is_vocal: true, editable: true,
    notes: [
      { start: 0.0, end: 0.8, pitch: 60, lyric: "la" },
      { start: 0.8, end: 1.2, pitch: 60, lyric: "la" }
    ]
  }]
};
inst.load(FIX2);
const gripL = noteEl(1).children.filter((c) => c.dataset && c.dataset.grip === "l")[0];
ok(!!gripL, "第二个音符有左边缘把手");
const yy = d.pitchToY(60) + 3;
fire(lanes, "pointerdown", { clientX: d.secToX(0.8), clientY: yy, target: gripL });
fire(lanes, "pointermove", { clientX: d.secToX(0.4), clientY: yy, target: gripL });
fire(lanes, "pointerup", { clientX: d.secToX(0.4), clientY: yy, target: gripL });
const A2 = d.state.tracks[0].notes[0], B2 = d.state.tracks[0].notes[1];
ok(B2.start < 0.7, "B 的起点确实往左走了（" + B2.start.toFixed(3) + "s）");
ok(Math.abs(A2.end - B2.start) < 1e-6,
   "A 的尾巴被收到 B 的起点，两者不重叠（A.end=" + A2.end.toFixed(3) + " B.start=" + B2.start.toFixed(3) + "）");
ok(A2.start === 0.0 && A2.end > A2.start, "A 仍然是一个有效长度的音符");

// 反方向：拖前一个的右边缘往右 → 后一个被推开（原来的行为，一个音都不丢）
inst.load(FIX2);
const gripR2 = noteEl(0).children.filter((c) => c.dataset && c.dataset.grip === "r")[0];
fire(lanes, "pointerdown", { clientX: d.secToX(0.8), clientY: yy, target: gripR2 });
fire(lanes, "pointermove", { clientX: d.secToX(1.0), clientY: yy, target: gripR2 });
fire(lanes, "pointerup", { clientX: d.secToX(1.0), clientY: yy, target: gripR2 });
const A3 = d.state.tracks[0].notes[0], B3 = d.state.tracks[0].notes[1];
ok(A3.end > 0.8, "A 被拉长了（" + A3.end.toFixed(3) + "）");
ok(Math.abs(B3.start - A3.end) < 1e-6, "B 被推到 A 的结束点之后，仍然存在（不丢音）");
ok(Math.abs((B3.end - B3.start) - 0.4) < 0.05, "B 自己的时值没被改坏");

console.log("\n[18] 拖动 / 改时值 / 上下移动 都能撤销（用户反馈 2）");
inst.load(FIX3);
ok(inst.isDirty() === false, "刚加载时不是脏的");
pressAndDrag(d.secToX(0.2), d.pitchToY(60) + 3, d.secToX(0.2), d.pitchToY(60) + 3 - 2 * d.rowH, noteEl(0));
ok(noteOf(0).pitch === 62, "上拖后音高 62");
ok(inst.isDirty() === true, "纯上下拖也算改动（以前只看横向位移，这种改动不会被保存）");
fire(global.window, "keydown", { key: "z", ctrlKey: true, target: { tagName: "DIV" } });
ok(noteOf(0).pitch === 60, "Ctrl+Z 撤销回了音高 60（实际 " + noteOf(0).pitch + "）");
fire(global.window, "keydown", { key: "z", ctrlKey: true, shiftKey: true, target: { tagName: "DIV" } });
ok(noteOf(0).pitch === 62, "Ctrl+Shift+Z 重做回 62（实际 " + noteOf(0).pitch + "）");

inst.load(FIX3);
const gripR3 = noteEl(0).children.filter((c) => c.dataset && c.dataset.grip === "r")[0];
const endBefore = noteOf(0).end;
fire(lanes, "pointerdown", { clientX: d.secToX(0.4), clientY: d.pitchToY(60) + 3, target: gripR3 });
fire(lanes, "pointermove", { clientX: d.secToX(0.7), clientY: d.pitchToY(60) + 3, target: gripR3 });
fire(lanes, "pointerup", { clientX: d.secToX(0.7), clientY: d.pitchToY(60) + 3, target: gripR3 });
ok(noteOf(0).end > endBefore + 0.1, "右边缘拉伸生效（" + noteOf(0).end.toFixed(3) + "）");
fire(global.window, "keydown", { key: "z", ctrlKey: true, target: { tagName: "DIV" } });
ok(Math.abs(noteOf(0).end - endBefore) < 1e-6,
   "Ctrl+Z 把时值撤回来了（" + noteOf(0).end.toFixed(3) + " vs " + endBefore.toFixed(3) + "）");

// 一次拖动只应产生一步撤销（否则撤销要按很多次）
inst.load(FIX3);
pressAndDrag(d.secToX(0.2), d.pitchToY(60) + 3, d.secToX(0.2), d.pitchToY(60) + 3 - 3 * d.rowH, noteEl(0));
ok(noteOf(0).pitch === 63, "拖 3 行 → 63");
fire(global.window, "keydown", { key: "z", ctrlKey: true, target: { tagName: "DIV" } });
ok(noteOf(0).pitch === 60, "一步撤销就回到拖动前（说明整段拖动只压了一次快照）");

console.log("\n[19] 音高范围固定 C0–C8，纵向滚动条常驻（用户反馈 3）");
inst.load(FIX3);
ok(d.state.lowPitch === 12, "最低音 = C0（MIDI 12，实际 " + d.state.lowPitch + "）");
ok(d.state.rows === 97, "行数固定 97（C0–C8，实际 " + d.state.rows + "）");
ok(d.state.lowPitch + d.state.rows - 1 === 108, "最高音 = C8（MIDI 108）");
ok(d.els.lanes.style.height === (97 * d.rowH) + "px",
   "内容高度 " + d.els.lanes.style.height + " 远超视口 → 纵向滚动条永远在");
ok(d.pitchToY(60) > 0 && d.pitchToY(60) < 97 * d.rowH, "C4 落在内容范围内");
// 加载后应自动滚到音符所在音区，否则打开卷帘只看到最低那一段空行
inst.load(FIX2);   // 音符都在 C4(60) 附近
ok(scrollEl.scrollTop > 0, "加载后自动滚到了音符所在音区（scrollTop=" + scrollEl.scrollTop + "）");
const visMid = scrollEl.scrollTop + (scrollEl.clientHeight - 20) / 2;
ok(Math.abs(visMid - d.pitchToY(60)) < 40, "C4 大致落在视口中间（偏差 " + Math.abs(visMid - d.pitchToY(60)).toFixed(0) + "px）");
// 音域固定之后，往上拖不会因为"行数重算"而中断
const pitchHiBefore = d.state.lowPitch + d.state.rows - 1;
pressAndDrag(d.secToX(0.2), d.pitchToY(60) + 3, d.secToX(0.2), d.pitchToY(60) + 3 - 7 * d.rowH, noteEl(0));
ok(d.state.lowPitch + d.state.rows - 1 === pitchHiBefore, "拖动过程中音域范围不变（不再重算布局）");
ok(noteOf(0).pitch === 67, "一口气上拖 7 个半音也一步到位（" + noteOf(0).pitch + "）");

console.log("\n[20] 模型给的同声部同时音（ABC 的 `[CEG]` 和弦写法）");
// 这种"重叠"是**合法的**：导出侧会拆成 和弦1/和弦2…，一个音都不丢。
// 加载时不许自动规整（否则和弦被推成一条旋律线）；编辑时才按"谁被拖谁赢"处理，
// 而且绝不能压出零长度音符 —— 后端会直接 400 拒绝。
const FIXCH = {
  bpm: 120, beats_per_bar: 4, beat_unit: 4, unit_whole: 0.0625, duration: 4.0,
  measures: fixture.measures,
  tracks: [{
    voice: "Vocal", kind: "vocal", display: "主人声", is_vocal: true, editable: true,
    notes: [
      { start: 0.0, end: 0.4, pitch: 60, lyric: "la" },
      { start: 0.0, end: 0.4, pitch: 64, lyric: "la" }
    ]
  }]
};
inst.load(FIXCH);
ok(d.state.tracks[0].notes[0].start === 0 && d.state.tracks[0].notes[1].start === 0,
   "加载后两个音仍然同时起（没有被加载逻辑动过）");
ok(inst.overlaps().length === 1, "overlaps() 如实报出这处重叠（导出侧会拆轨处理）");
// 拖动其中一个（起点重合 → 无法靠收前一个的尾巴解决）→ 必须整体推开，且不得产生零长度
pressAndDrag(d.secToX(0.2), d.pitchToY(64) + 3, d.secToX(0.5), d.pitchToY(64) + 3, noteEl(1));
const chNotes = d.state.tracks[0].notes;
ok(chNotes.every((n) => n.end > n.start + 1e-9),
   "两个音符都还有正的长度（后端不会因为零长度拒收）");
ok(inst.overlaps().length === 0, "编辑之后不再重叠");

console.log("\n" + (fails.length ? "失败 " + fails.length + " 项" : "全部通过"));
process.exit(fails.length ? 1 : 0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("找不到 node：跳过 JS 语法与卷帘逻辑检查（这些检查需要 node）")
        return 0

    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    print("[1] index.html 内联脚本语法")
    html = INDEX.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    check(len(blocks) >= 1, f"抽到 {len(blocks)} 段内联脚本")
    bad = 0
    for i, code in enumerate(blocks):
        f = TMP / f"inline_{i}.js"
        f.write_text(code, encoding="utf-8")
        r = run_node([node, "--check", str(f)])
        if r.returncode != 0:
            bad += 1
            print(f"  FAIL 第 {i} 段语法错误：\n{r.stderr.strip()}")
    check(bad == 0, f"index.html 的 {len(blocks)} 段内联脚本全部通过")

    print("\n[2] web/pianoroll.js 语法")
    r = run_node([node, "--check", str(ROLL)])
    check(r.returncode == 0, "pianoroll.js 语法通过" if r.returncode == 0 else r.stderr.strip())

    print("\n[2b] 换算基准取自后端（app/abcp.py::_score_to_seconds）")
    try:
        golden = golden_from_backend()
        check(len(golden) == len(SAMPLE_POS), f"{len(golden)} 个采样点")
    except Exception as e:  # noqa: BLE001
        check(False, f"取后端基准失败：{e}")
        golden = {}

    print("\n[1b] 页面引用的 DOM id 都必须真的存在")
    # 大段删改最容易留下的坑：$("#abcArea") 这种引用指向已经被删掉的元素，
    # 页面一加载就 TypeError（整个脚本挂掉，表现为"点了没反应"）。
    def _ids_in(text: str) -> set[str]:
        return set(re.findall(r'id="([^"]+)"', text))

    html_ids = _ids_in(html)
    js_ids = set(re.findall(r'\$\("#([^"]+)"\)', html))
    roll_ids = _ids_in(ROLL.read_text(encoding="utf-8"))
    dangling = sorted(i for i in js_ids if i not in html_ids and i not in roll_ids)
    check(not dangling, f"页面引用的 {len(js_ids)} 个 id 全部存在"
                        + (f"（悬空：{dangling}）" if dangling else ""))

    fixture = {
        "bpm": 120.0,
        "beats_per_bar": 4,
        "beat_unit": 4,
        "unit_whole": 0.0625,
        "duration": 4.0,
        "measures": MEASURES,
        "tracks": [{
            "voice": "Vocal", "kind": "vocal", "display": "主人声",
            "is_vocal": True, "editable": True,
            "notes": [{"start": 0.0, "end": 0.5, "pitch": 60, "lyric": "la"}],
        }],
        "golden": golden,
        # 页面里真实调用到的卷帘方法，交给 node 核对组件是否都导出了
        "rollApiUsed": sorted(set(re.findall(r"\broll\.([A-Za-z_$][\w$]*)\s*\(", html))),
    }
    fix_path = TMP / "fixture.json"
    fix_path.write_text(json.dumps(fixture), encoding="utf-8")
    harness = TMP / "roll_logic.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")

    r = run_node([node, str(harness), str(ROLL), str(fix_path)])
    print(r.stdout.rstrip())
    if r.returncode != 0:
        print(r.stderr.strip())
    check(r.returncode == 0, "卷帘换算 / 网格逻辑自检通过")

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：{FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
