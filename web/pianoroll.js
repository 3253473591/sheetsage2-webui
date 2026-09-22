/* ============================================================================
   钢琴卷帘（Piano Roll）—— SheetSage2 扒谱的可编辑谱面
   ============================================================================

   为什么是独立文件而不是塞进 index.html：这个组件的体量（渲染 / 网格吸附 /
   指针交互 / 撤销栈 / 歌词层）已经远超"页面上的一小块脚本"，内联会让
   index.html 难以维护。没有构建步骤，直接 <script src="./pianoroll.js"> 加载。

   设计要点（都是踩过或想清楚才这么定的）
   --------------------------------------
   1. **时间一律用秒，网格一律用 blick。**
      变速曲（同一首歌多个 BPM）下"每拍多少像素"不是常数，所以网格线与
      Ctrl+G 的吸附必须在 blick（音乐位置）域里算，再换算成秒来画。
      blick 换算与后端 app/tempo.py 是同一套分段线性公式，参数从
      /notes 的 tempo_map 拿，避免前后端各自实现导致漂移。

   2. **编辑的是音符列表，不是文本。**
      旧版让用户手改 ABC 文本，直观性差；这里直接编辑 (start,end,pitch,lyric)，
      保存时由后端序列化回 ABC（app/abcs.py），导出链路完全不变。

   3. **每条轨单声部。**
      与项目既有事实一致（实测 Vocal/Ins 从不重叠）。同一轨内出现重叠会在保存时
      被后端拆成 器乐旋律1/器乐旋律2…，这里不阻止用户拖出重叠，但要给出提示。

   4. **歌词是逐音的字面字符串，包含 - 与 + 两个记号。**
      `-` = 同一音节延续（一字多音），`+` = 同一个词的下一个音节。
      它们必须**原样**进 SVP，所以这一层不做任何"智能"处理，只负责录入与显示。
      中文歌词要靠输入法，所以歌词编辑一定走真实的 <input>，不能用按键直填。

   5. **拖动期间不整屏重绘。**
      只改被拖那几条的 style；结构变化（增删/撤销/重新加载）才整屏重建。
   ========================================================================== */
(function (global) {
  "use strict";

  // 与 app/tempo.py 的 BLICK_PER_QUARTER 保持一致（SVP 153 的谱面单位）
  var BPQ = 705600000;

  // ---------------------------------------------------------------- 网格档位
  // 数值 = 一个四分音符被切成几份。标签按用户给的说法（"1/N 个四分音符"）。
  var GRIDS = [
    { div: 1, label: "四分音符", short: "1/4" },
    { div: 2, label: "1/2 个四分音符（8 分音符）", short: "1/8" },
    { div: 4, label: "1/4 个四分音符（16 分音符）", short: "1/16" },
    { div: 6, label: "1/6 个四分音符（16 分三连音）", short: "1/16T" },
    { div: 8, label: "1/8 个四分音符（32 分音符）", short: "1/32" },
    { div: 12, label: "1/12 个四分音符（32 分三连音）", short: "1/32T" },
    { div: 16, label: "1/16 个四分音符（64 分音符）", short: "1/64" },
    { div: 24, label: "1/24 个四分音符（64 分三连音）", short: "1/64T" },
    { div: 32, label: "1/32 个四分音符（128 分音符）", short: "1/128" }
  ];
  var DEFAULT_GRID = 4;

  // ---------------------------------------------------------------- 音名
  var NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  var BLACK = { 1: 1, 3: 1, 6: 1, 8: 1, 10: 1 };

  function pitchName(p) {
    return NAMES[((p % 12) + 12) % 12] + (Math.floor(p / 12) - 1);
  }
  function isBlack(p) { return !!BLACK[((p % 12) + 12) % 12]; }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  // ---------------------------------------------------------------- 速度表
  /** 秒 → blick。与 app/tempo.py::sec_to_blick 同一套分段线性公式。 */
  function secToBlick(sec, tm) {
    if (!(sec > 0)) return 0;
    var m = (tm && tm.length) ? tm : [{ t: 0, bpm: 120 }];
    var b = 0, pt = 0, pb = m[0].bpm;
    for (var i = 1; i < m.length; i++) {
      if (sec <= m[i].t) break;
      b += (m[i].t - pt) * (pb / 60) * BPQ;
      pt = m[i].t; pb = m[i].bpm;
    }
    return b + (sec - pt) * (pb / 60) * BPQ;
  }
  /** blick → 秒。与 app/tempo.py::blick_to_sec 同一套公式。 */
  function blickToSec(b, tm) {
    if (!(b > 0)) return 0;
    var m = (tm && tm.length) ? tm : [{ t: 0, bpm: 120 }];
    var remain = b, pt = 0, pb = m[0].bpm;
    for (var i = 1; i < m.length; i++) {
      var seg = (m[i].t - pt) * (pb / 60) * BPQ;
      if (remain <= seg) break;
      remain -= seg; pt = m[i].t; pb = m[i].bpm;
    }
    return pt + remain / ((pb / 60) * BPQ);
  }

  /** 拍号字符串 → 每小节几个四分音符（"4/4"→4，"3/4"→3，"6/8"→3）。 */
  function quartersPerBar(meter) {
    var s = String(meter || "4/4").trim();
    if (/^c\|?$/i.test(s)) return 4;
    var m = s.match(/^(\d+)\s*\/\s*(\d+)$/);
    if (!m) return 4;
    var num = parseInt(m[1], 10), den = parseInt(m[2], 10);
    if (!num || !den) return 4;
    return num * (4 / den);
  }

  /** 给歌词做点显示上的美化：- 和 + 是记号，用符号代替，一眼能认出来。 */
  function lyricGlyph(text) {
    var t = String(text == null ? "" : text);
    if (t === "-") return "‿";   // 延音：连到前一个音
    if (t === "+") return "⁀";   // 拆音节：从前一个词接着切
    return t;
  }

  // ==========================================================================
  //  组件
  // ==========================================================================
  function mount(host, opts) {
    opts = opts || {};
    var KEYS_W = 58;
    var RULER_H = 24;          // 必须与 CSS 里 .pr-ruler 的高度一致（抓手就那 24px）
    var MIN_ROWH = 9;
    var MAX_ROWH = 26;
    // 横向缩放（每秒多少像素）的上下限
    var MIN_PPS = 4;
    var MAX_PPS = 4000;
    /* 点空白处跳转进度条的**节流窗口**。
       为什么必须节流：跳转要重建钢琴音频（几十毫秒到几百毫秒），连点会把主线程
       打满、界面直接卡住。留 1 秒窗口是"第一次立刻响应、之后的连点被丢掉"，
       手感上仍然即时。 */
    var SEEK_THROTTLE_MS = 1000;

    var S = {
      tempoMap: [{ t: 0, bpm: 120 }],
      bpm: 120,
      meter: "4/4",
      duration: 0,
      tracks: [],          // [{voice, display, isVocal, notes:[{start,end,pitch,lyric}]}]
      /* 当前**正在编辑**的轨下标。一次只编一条轨（与导出的 SVP 一致：一轨一条单声部线），
         其余轨淡显成只读背景。不这么做的话 Ctrl+A 会把各轨的音符混在一起选，
         "选中然后铺歌词"就无从谈起。 */
      activeTrack: 0,
      /* 非活动轨是否淡显。关掉就只剩当前轨（谱面更干净，但失去上下文）。 */
      ghostOthers: true,
      /* 歌词分词：auto（中文逐字、英文按词）/ space（按空格）/ char（逐字符） */
      splitMode: "auto",
      chords: [],          // 只读显示的和弦音（来自 chords.mid）
      showChords: false,
      grid: DEFAULT_GRID,
      pxPerSec: 60,
      rowH: 14,
      lowPitch: 48,        // 可视范围的最低音（最下面一行）
      rows: 36,
      active: null,        // {t: 轨道下标, i: 音符下标}
      selection: {},       // "t:i" -> true
      dirty: false,
      undo: [],
      redo: [],
      maxUndo: 80
    };

    if (opts.grid && GRIDS.some(function (g) { return g.div === opts.grid; })) S.grid = opts.grid;

    // ---------------------------------------------------------------- DOM
    host.innerHTML = "";
    host.classList.add("pr");

    var bar = document.createElement("div");
    bar.className = "pr-bar";

    // 网格档位
    var gridWrap = document.createElement("div");
    gridWrap.className = "pr-grid-pick";
    var gridLab = document.createElement("span");
    gridLab.className = "pr-lab";
    gridLab.textContent = "网格";
    var gridSel = document.createElement("select");
    gridSel.id = "prGrid";
    GRIDS.forEach(function (g) {
      var o = document.createElement("option");
      o.value = String(g.div);
      o.textContent = g.label;
      gridSel.appendChild(o);
    });
    gridSel.value = String(S.grid);
    var snapBtn = document.createElement("button");
    snapBtn.className = "pr-btn";
    snapBtn.id = "prSnap";
    snapBtn.textContent = "吸附 Ctrl+G";
    snapBtn.title = "把选中（未选中则全部）音符量化到当前网格";
    gridWrap.appendChild(gridLab);
    gridWrap.appendChild(gridSel);
    gridWrap.appendChild(snapBtn);

    // 曲目切换：一次只编一条轨（与导出的 SVP 一致）
    var trackWrap = document.createElement("div");
    trackWrap.className = "pr-tracks";
    var trackLab = document.createElement("span");
    trackLab.className = "pr-lab";
    trackLab.textContent = "曲目";
    var trackBtns = document.createElement("div");
    trackBtns.className = "pr-trackbtns";
    trackBtns.id = "prTracks";
    trackWrap.appendChild(trackLab);
    trackWrap.appendChild(trackBtns);

    // 歌词
    var lyrWrap = document.createElement("div");
    lyrWrap.className = "pr-lyr";
    var lyrLab = document.createElement("span");
    lyrLab.className = "pr-lab";
    lyrLab.textContent = "歌词";
    var lyrInput = document.createElement("input");
    lyrInput.id = "prLyric";
    lyrInput.className = "pr-lyr-input";
    lyrInput.type = "text";
    lyrInput.autocomplete = "off";
    lyrInput.placeholder = "选中音符后在此输入（中文可直接用输入法）";
    lyrInput.disabled = true;
    var splitSel = document.createElement("select");
    splitSel.id = "prSplit";
    splitSel.title = "歌词怎么切分：自动 = 中文逐字、英文按词；按空格 = 只按空格切";
    [["auto", "自动分词"], ["space", "按空格"], ["char", "逐字符"]].forEach(function (o) {
      var op = document.createElement("option");
      op.value = o[0];
      op.textContent = o[1];
      splitSel.appendChild(op);
    });
    var susBtn = document.createElement("button");
    susBtn.className = "pr-btn mono";
    susBtn.id = "prSustain";
    susBtn.textContent = "‿";
    susBtn.title = "延音符 -：同一音节延续到本音符（一字多音）";
    var sylBtn = document.createElement("button");
    sylBtn.className = "pr-btn mono";
    sylBtn.id = "prSyllable";
    sylBtn.textContent = "⁀";
    sylBtn.title = "多音节 +：本音符唱上一个词的下一个音节（open → open +）";
    var nextBtn = document.createElement("button");
    nextBtn.className = "pr-btn";
    nextBtn.id = "prNext";
    nextBtn.textContent = "下一音 ▸";
    nextBtn.title = "把焦点移到下一个音符（Enter 同效）";
    lyrWrap.appendChild(lyrLab);
    lyrWrap.appendChild(lyrInput);
    lyrWrap.appendChild(splitSel);
    lyrWrap.appendChild(susBtn);
    lyrWrap.appendChild(sylBtn);
    lyrWrap.appendChild(nextBtn);

    // 视图
    var viewWrap = document.createElement("div");
    viewWrap.className = "pr-view";
    var octUp = document.createElement("button");
    octUp.className = "pr-btn";
    octUp.textContent = "八度 ▲";
    octUp.title = "可视音域整体上移一个八度";
    var octDn = document.createElement("button");
    octDn.className = "pr-btn";
    octDn.textContent = "八度 ▼";
    octDn.title = "可视音域整体下移一个八度";
    var zin = document.createElement("button");
    zin.className = "pr-btn";
    zin.textContent = "＋";
    zin.title = "横向放大（也可以按住 Ctrl 滚滚轮，会以指针位置为中心缩放）";
    var zout = document.createElement("button");
    zout.className = "pr-btn";
    zout.textContent = "－";
    zout.title = "横向缩小（也可以按住 Ctrl 滚滚轮）";
    var zfit = document.createElement("button");
    zfit.className = "pr-btn";
    zfit.textContent = "适宽";
    zfit.title = "整首歌缩放到一屏";
    viewWrap.appendChild(octDn); viewWrap.appendChild(octUp);
    viewWrap.appendChild(zout); viewWrap.appendChild(zin); viewWrap.appendChild(zfit);

    var legend = document.createElement("div");
    legend.className = "pr-legend";

    bar.appendChild(trackWrap);
    bar.appendChild(gridWrap);
    bar.appendChild(lyrWrap);
    bar.appendChild(viewWrap);
    bar.appendChild(legend);

    var body = document.createElement("div");
    body.className = "pr-body";
    var scroll = document.createElement("div");
    scroll.className = "pr-scroll";
    var canvas = document.createElement("div");
    canvas.className = "pr-canvas";

    var ruler = document.createElement("div");
    ruler.className = "pr-ruler";
    // 刻度单独放一层：这样播放头手柄不会被"重画刻度"顺手清掉
    var rulerTicks = document.createElement("div");
    rulerTicks.className = "pr-ruler-ticks";
    ruler.appendChild(rulerTicks);
    // 播放头在标尺上的**抓手**：没有它，用户根本看不出进度条能拖
    var headGrab = document.createElement("div");
    headGrab.className = "pr-head-grab";
    headGrab.title = "按住左右拖动 = 定位播放位置（松手才真正跳转，拖动中不会卡）";
    ruler.appendChild(headGrab);
    var lanes = document.createElement("div");
    lanes.className = "pr-lanes";
    var gridSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    gridSvg.setAttribute("class", "pr-gridsvg");
    var noteLayer = document.createElement("div");
    noteLayer.className = "pr-notes";
    var head = document.createElement("div");
    head.className = "pr-playhead";
    var mq = document.createElement("div");
    mq.className = "pr-marquee";

    lanes.appendChild(gridSvg);
    lanes.appendChild(noteLayer);
    lanes.appendChild(mq);
    lanes.appendChild(head);
    canvas.appendChild(ruler);
    canvas.appendChild(lanes);
    scroll.appendChild(canvas);

    // 键列放在横向滚动容器**之外**，这样左右滚动时琴键始终贴边；
    // 纵向滚动交给 .pr-body，两列一起走，不会错位。
    var keysWrap = document.createElement("div");
    keysWrap.className = "pr-keys-wrap";
    keysWrap.style.paddingTop = RULER_H + "px";

    body.appendChild(keysWrap);
    body.appendChild(scroll);

    host.appendChild(bar);
    host.appendChild(body);
    host.tabIndex = 0;

    var empty = document.createElement("div");
    empty.className = "pr-empty";
    empty.textContent = "扒谱完成后，这里显示可编辑的钢琴卷帘";
    lanes.appendChild(empty);

    // ---------------------------------------------------------------- 几何
    function timeW() { return Math.max(320, S.duration * S.pxPerSec + 40); }
    function totalH() { return S.rows * S.rowH; }
    function pitchToY(p) { return (S.lowPitch + S.rows - 1 - p) * S.rowH; }
    function yToPitch(y) { return S.lowPitch + S.rows - 1 - Math.floor(y / S.rowH); }
    function secToX(sec) { return sec * S.pxPerSec; }
    function xToSec(x) { return x / S.pxPerSec; }

    /** 当前网格在 blick 域的步长 */
    function stepBlick() { return BPQ / S.grid; }
    /** 把一个 blick 位置吸附到网格 */
    function snapB(b) { var st = stepBlick(); return Math.round(b / st) * st; }

    // ---------------------------------------------------------------- 撤销
    function snapshot() {
      return JSON.stringify(S.tracks);
    }
    function pushUndo() {
      S.undo.push(snapshot());
      if (S.undo.length > S.maxUndo) S.undo.shift();
      S.redo.length = 0;
      refreshUndoBtns();
    }
    function restore(json) {
      var t = JSON.parse(json);
      S.tracks = t;
      S.selection = {};
      S.active = null;
      render();
      syncActiveUI();
    }
    function doUndo() {
      if (!S.undo.length) return;
      S.redo.push(snapshot());
      restore(S.undo.pop());
      refreshUndoBtns();
      changed();
    }
    function doRedo() {
      if (!S.redo.length) return;
      S.undo.push(snapshot());
      restore(S.redo.pop());
      refreshUndoBtns();
      changed();
    }
    var undoBtn = document.createElement("button");
    undoBtn.className = "pr-btn";
    undoBtn.textContent = "↶ 撤销";
    undoBtn.title = "Ctrl+Z";
    var redoBtn = document.createElement("button");
    redoBtn.className = "pr-btn";
    redoBtn.textContent = "↷ 重做";
    redoBtn.title = "Ctrl+Shift+Z / Ctrl+Y";
    undoBtn.onclick = doUndo;
    redoBtn.onclick = doRedo;
    viewWrap.insertBefore(redoBtn, viewWrap.firstChild);
    viewWrap.insertBefore(undoBtn, viewWrap.firstChild);

    function refreshUndoBtns() {
      undoBtn.disabled = !S.undo.length;
      redoBtn.disabled = !S.redo.length;
    }

    function changed() {
      S.dirty = true;
      if (opts.onChange) opts.onChange();
    }

    // ---------------------------------------------------------------- 命中
    function noteAt(trackIdx, noteIdx) { return S.tracks[trackIdx].notes[noteIdx]; }
    function key(t, i) { return t + ":" + i; }
    function isSelected(t, i) { return !!S.selection[key(t, i)]; }
    function selectedList() {
      var out = [];
      Object.keys(S.selection).forEach(function (k) {
        var p = k.split(":"), t = +p[0], i = +p[1];
        if (S.tracks[t] && S.tracks[t].notes[i]) out.push({ t: t, i: i });
      });
      return out;
    }
    function selectOnly(t, i) {
      S.selection = {};
      S.selection[key(t, i)] = true;
      S.active = { t: t, i: i };
    }

    // ---------------------------------------------------------------- 渲染
    var noteEls = {};       // "t:i" -> element
    var rowEls = [];

    function computeRange() {
      var lo = Infinity, hi = -Infinity;
      S.tracks.forEach(function (tr) {
        tr.notes.forEach(function (n) { if (n.pitch < lo) lo = n.pitch; if (n.pitch > hi) hi = n.pitch; });
      });
      if (S.showChords) S.chords.forEach(function (n) { if (n.pitch < lo) lo = n.pitch; if (n.pitch > hi) hi = n.pitch; });
      if (!isFinite(lo)) { lo = 60; hi = 72; }
      return { lo: lo, hi: hi };
    }

    function layoutKeys() {
      // 音域自动框住内容 + 上下各留 2 个半音，再夹到钢琴范围内
      var r = computeRange();
      var need = (r.hi + 2) - (r.lo - 2) + 1;
      S.rows = clamp(need, 12, 96);
      S.lowPitch = clamp(r.lo - 2, 21, 108 - S.rows + 1);

      var avail = Math.max(200, body.clientHeight || 380);
      S.rowH = clamp(Math.floor((avail - RULER_H) / S.rows), MIN_ROWH, MAX_ROWH);
    }

    function render() {
      layoutKeys();

      var W = timeW(), H = totalH();
      canvas.style.width = W + "px";
      lanes.style.width = W + "px";
      lanes.style.height = H + "px";
      ruler.style.width = W + "px";

      renderKeys();
      renderRuler(W);
      renderGrid(W, H);
      renderTracks();
      renderNotes();
      renderPlayhead();

      empty.style.display = S.tracks.length ? "none" : "";
      renderLegend();
    }

    /** 构件「曲目」切换按钮。一次只编一条轨——这是 Ctrl+A 铺歌词能成立的前提。 */
    function renderTracks() {
      trackBtns.innerHTML = "";
      S.tracks.forEach(function (tr, i) {
        var b = document.createElement("button");
        b.className = "pr-track" + (i === S.activeTrack ? " on" : "");
        b.dataset.t = i;
        b.textContent = tr.display + "（" + tr.notes.length + "）";
        b.title = i === S.activeTrack
          ? "当前编辑中（Ctrl+A 只选这条轨）"
          : "点一下切到这条轨编辑";
        b.onclick = function () { setActiveTrack(i); };
        trackBtns.appendChild(b);
      });
      var g = document.createElement("label");
      g.className = "pr-chk";
      g.title = "勾上时其他轨淡显作对照；不勾只看当前轨";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = S.ghostOthers;
      cb.onchange = function () { S.ghostOthers = cb.checked; render(); };
      g.appendChild(cb);
      g.appendChild(document.createTextNode("显示其他轨"));
      trackBtns.appendChild(g);
    }

    /** 切换当前编辑的轨：清空选择、重画、同步歌词框。 */
    function setActiveTrack(i) {
      if (i < 0 || i >= S.tracks.length || i === S.activeTrack) return;
      S.activeTrack = i;
      S.selection = {};
      S.active = null;
      render();
      syncActiveUI();
      var tr = S.tracks[i];
      if (opts.onToast) opts.onToast("已切到「" + tr.display + "」，Ctrl+A 可全选这条轨", "");
    }

    function renderLegend() {
      legend.innerHTML = "";
      S.tracks.forEach(function (tr, i) {
        var s = document.createElement("span");
        s.className = "pr-lg pr-lg-" + (i % 4);
        s.textContent = tr.display + "（" + tr.notes.length + "）";
        legend.appendChild(s);
      });
      if (S.showChords && S.chords.length) {
        var c = document.createElement("span");
        c.className = "pr-lg pr-lg-chord";
        c.textContent = "和弦（只读，" + S.chords.length + "）";
        legend.appendChild(c);
      }
      var hint = document.createElement("span");
      hint.className = "pr-lg pr-lg-hint";
      hint.textContent = "一次只编一条轨（上方「曲目」切换）· 双击空白新增 · 拖动改音高与时间 · "
        + "右边缘拉伸 · Delete 删除 · Shift+拖框选 · Ctrl+A 全选本轨 · Ctrl+G 吸附 · "
        + "Ctrl+L 导入歌词 · Ctrl+滚轮 横向缩放 · 拖标尺/点空白定位 · Ctrl+Z 撤销";
      legend.appendChild(hint);
    }

    function renderRuler(W) {
      rulerTicks.innerHTML = "";
      var qpb = quartersPerBar(S.meter);
      var barB = qpb * BPQ;
      var total = secToBlick(S.duration + 2, S.tempoMap);
      var i = 0;
      // 小节线太密时按 2/4/8… 间隔标注
      var perBar = W > 2400 ? 1 : W > 1200 ? 2 : 4;
      for (var b = 0; b <= total; b += barB) {
        var x = secToX(blickToSec(b, S.tempoMap));
        if (i % perBar === 0) {
          var t = document.createElement("div");
          t.className = "pr-tick";
          t.style.left = x + "px";
          t.textContent = String(Math.round(b / barB) + 1);
          rulerTicks.appendChild(t);
        }
        i++;
      }
    }

    function renderGrid(W, H) {
      var NS = "http://www.w3.org/2000/svg";
      gridSvg.setAttribute("width", W);
      gridSvg.setAttribute("height", H);
      gridSvg.setAttribute("viewBox", "0 0 " + W + " " + H);
      while (gridSvg.firstChild) gridSvg.removeChild(gridSvg.firstChild);

      var frag = document.createDocumentFragment();

      // 行底色：黑键行深一点，方便对齐音高
      for (var p = S.lowPitch; p < S.lowPitch + S.rows; p++) {
        if (!isBlack(p)) continue;
        var r = document.createElementNS(NS, "rect");
        r.setAttribute("x", 0);
        r.setAttribute("y", pitchToY(p));
        r.setAttribute("width", W);
        r.setAttribute("height", S.rowH);
        r.setAttribute("class", "pr-row-black");
        frag.appendChild(r);
      }

      // 网格竖线：按当前档位画；三连音档位用不同样式，避免和 2 的幂混淆
      var step = stepBlick();
      var total = secToBlick(S.duration + 2, S.tempoMap);
      var isTriplet = (S.grid % 3 === 0) && (S.grid & (S.grid - 1)) !== 0;
      // 线太密就不画细线，只保留小节线
      var pxPerStep = (BPQ / S.grid) / BPQ * 60 / S.tempoMap[0].bpm * S.pxPerSec;
      var drawFine = pxPerStep >= 5;
      if (drawFine) {
        for (var b = 0; b <= total; b += step) {
          var x = secToX(blickToSec(b, S.tempoMap));
          var ln = document.createElementNS(NS, "line");
          ln.setAttribute("x1", x); ln.setAttribute("x2", x);
          ln.setAttribute("y1", 0); ln.setAttribute("y2", H);
          ln.setAttribute("class", "pr-gl" + (isTriplet ? " pr-gl-trip" : ""));
          frag.appendChild(ln);
        }
      }
      // 小节线
      var barB = quartersPerBar(S.meter) * BPQ;
      for (var bb = 0; bb <= total; bb += barB) {
        var bx = secToX(blickToSec(bb, S.tempoMap));
        var bl = document.createElementNS(NS, "line");
        bl.setAttribute("x1", bx); bl.setAttribute("x2", bx);
        bl.setAttribute("y1", 0); bl.setAttribute("y2", H);
        bl.setAttribute("class", "pr-gl-bar");
        frag.appendChild(bl);
      }
      gridSvg.appendChild(frag);
    }

    function renderNotes() {
      noteLayer.innerHTML = "";
      noteEls = {};
      S.tracks.forEach(function (tr, t) {
        var editable = (t === S.activeTrack);
        var ghost = !editable && S.ghostOthers;
        if (!editable && !S.ghostOthers) return;      // 只看当前轨
        tr.notes.forEach(function (n, i) {
          var el = document.createElement("div");
          el.className = "pr-note pr-v" + (t % 4) + (editable ? "" : " pr-ghost");
          el.dataset.t = t; el.dataset.i = i;
          var body = document.createElement("span");
          body.className = "pr-note-ly";
          // 淡显的背景轨不显示歌词：它们不可编辑，显示歌词只会干扰读谱
          body.textContent = editable ? lyricGlyph(n.lyric) : "";
          el.appendChild(body);
          var grip = document.createElement("i");
          grip.className = "pr-grip";
          el.appendChild(grip);
          el.title = pitchName(n.pitch) + "  " + n.start.toFixed(2) + "–" + n.end.toFixed(2) + "s"
            + (editable ? "" : "（" + tr.display + "，切到该轨才能编辑）");
          noteLayer.appendChild(el);
          noteEls[key(t, i)] = el;
          placeNote(el, n);
          if (editable && isSelected(t, i)) el.classList.add("sel");
          if (S.active && S.active.t === t && S.active.i === i) el.classList.add("act");
        });
      });
      if (S.showChords) {
        S.chords.forEach(function (n) {
          var el = document.createElement("div");
          el.className = "pr-note pr-chord";
          el.style.pointerEvents = "none";
          placeNote(el, n);
          noteLayer.appendChild(el);
        });
      }
    }

    function placeNote(el, n) {
      var x = secToX(n.start), w = Math.max(3, secToX(n.end) - x);
      el.style.left = x + "px";
      el.style.width = w + "px";
      el.style.top = pitchToY(n.pitch) + "px";
      el.style.height = Math.max(3, S.rowH - 1) + "px";
      var ly = el.querySelector(".pr-note-ly");
      if (ly) ly.style.display = w >= 16 ? "" : "none";
    }

    function renderPlayhead() {
      var x = secToX(playSec);
      head.style.left = x + "px";
      headGrab.style.left = x + "px";
    }

    // ---------------------------------------------------------------- 键列
    function renderKeys() {
      keysWrap.innerHTML = "";
      for (var p = S.lowPitch + S.rows - 1; p >= S.lowPitch; p--) {
        var row = document.createElement("div");
        row.className = "pr-key" + (isBlack(p) ? " black" : "");
        row.style.height = S.rowH + "px";
        // 行太矮时黑键不写音名，否则文字挤成一团反而看不清
        row.textContent = (!isBlack(p) || S.rowH >= 13) ? pitchName(p) : "";
        row.onclick = (function (pp) {
          return function () { if (opts.onAudition) opts.onAudition(pp); };
        })(p);
        keysWrap.appendChild(row);
      }
    }

    // ---------------------------------------------------------------- 播放头
    var playSec = 0;
    function setPlayhead(sec) {
      playSec = Math.max(0, sec || 0);
      renderPlayhead();
      var lo = playSec;
      // 正在发声的音符高亮（参考项目没做这个，这里顺手做了）
      Object.keys(noteEls).forEach(function (kk) {
        var p = kk.split(":"), n = S.tracks[+p[0]].notes[+p[1]];
        if (!n) return;
        noteEls[kk].classList.toggle("playing", lo >= n.start && lo < n.end);
      });
    }

    // ---------------------------------------------------------------- 交互
    var drag = null;

    function localPoint(ev) {
      var r = lanes.getBoundingClientRect();
      return { x: ev.clientX - r.left, y: ev.clientY - r.top };
    }

    function beginGesture(ev) {
      host.focus();
      var pt = localPoint(ev);
      var target = ev.target;
      var noteEl = target.closest ? target.closest(".pr-note") : null;

      if (noteEl && noteEl.dataset.t != null && !noteEl.classList.contains("pr-chord")) {
        var t = +noteEl.dataset.t, i = +noteEl.dataset.i;
        if (ev.shiftKey) {
          var kk = key(t, i);
          if (S.selection[kk]) delete S.selection[kk]; else S.selection[kk] = true;
          S.active = { t: t, i: i };
          refreshSelectionUI();
          syncActiveUI();
          return;
        }
        if (!isSelected(t, i)) { selectOnly(t, i); refreshSelectionUI(); syncActiveUI(); }
        var resizing = target.classList && target.classList.contains("pr-grip");
        var members = selectedList();
        pushUndo();
        drag = {
          mode: resizing ? "resize" : "move",
          x0: pt.x, y0: pt.y,
          members: members.map(function (m) {
            var n = noteAt(m.t, m.i);
            return { t: m.t, i: m.i, s: n.start, e: n.end, p: n.pitch };
          })
        };
        lanes.setPointerCapture(ev.pointerId);
        ev.preventDefault();
        return;
      }

      // 空白处：拖 = 框选，**只是点一下 = 跳转进度条**
      if (!ev.shiftKey) { S.selection = {}; S.active = null; refreshSelectionUI(); syncActiveUI(); }
      drag = {
        mode: "marquee", x0: pt.x, y0: pt.y, add: !!ev.shiftKey,
        base: Object.assign({}, S.selection), moved: false
      };
      lanes.setPointerCapture(ev.pointerId);
      ev.preventDefault();
    }

    /* 进度条定位。**节流改成"合并"而不是"丢弃"**：
       丢弃会让用户"拖了三下只有一下生效"，而且线条（跟着手走）与音频（没跳）
       会脱节。现在窗口内的多次请求合并成窗口末尾的一次 —— 第一次立刻响应，
       之后的连点/拖动只在最后落一次，既不会卡也不会漏。 */
    var lastSeekAt = 0, seekTimer = null, seekPending = null;

    function flushSeek() {
      if (seekTimer) { clearTimeout(seekTimer); seekTimer = null; }
      if (seekPending == null) return;
      var sec = seekPending;
      seekPending = null;
      lastSeekAt = Date.now();
      if (opts.onSeek) opts.onSeek(Math.max(0, sec));
    }

    function seekAtTime(sec) {
      seekPending = sec;
      var wait = SEEK_THROTTLE_MS - (Date.now() - lastSeekAt);
      if (wait <= 0) { flushSeek(); return true; }
      if (!seekTimer) seekTimer = setTimeout(flushSeek, wait);
      return false;
    }

    // ---------------------------------------------------------------- 拖动定位
    /* 标尺上按住左右拖 = 定位。**拖动过程中只动线条**（改一个 style，几乎零成本），
       松手才真正跳转 —— 跳转要重建钢琴音频，跟着指针每像素跳一次必然卡死。 */
    var scrubbing = false;

    function rulerX(ev) {
      return ev.clientX - ruler.getBoundingClientRect().left;
    }
    function scrubTo(ev) {
      var sec = xToSec(rulerX(ev));
      if (sec < 0) sec = 0;
      if (S.duration > 0 && sec > S.duration) sec = S.duration;
      playSec = sec;
      renderPlayhead();
    }
    function beginScrub(ev) {
      host.focus();
      scrubbing = true;
      try { ruler.setPointerCapture(ev.pointerId); } catch (e) { /* 老浏览器 */ }
      scrubTo(ev);
      ev.preventDefault();
    }
    function moveScrub(ev) { if (scrubbing) scrubTo(ev); }
    function endScrub(ev) {
      if (!scrubbing) return;
      scrubbing = false;
      try { ruler.releasePointerCapture(ev.pointerId); } catch (e) { /* 已释放 */ }
      seekAtTime(playSec);          // 松手才真正跳
    }
    ruler.addEventListener("pointerdown", beginScrub);
    ruler.addEventListener("pointermove", moveScrub);
    ruler.addEventListener("pointerup", endScrub);
    ruler.addEventListener("pointercancel", endScrub);
    headGrab.addEventListener("pointerdown", beginScrub);

    function moveGesture(ev) {
      if (!drag) return;
      var pt = localPoint(ev);
      if (drag.mode === "marquee") {
        if (Math.abs(pt.x - drag.x0) > 3 || Math.abs(pt.y - drag.y0) > 3) drag.moved = true;
        var x = Math.min(pt.x, drag.x0), y = Math.min(pt.y, drag.y0);
        var w = Math.abs(pt.x - drag.x0), h = Math.abs(pt.y - drag.y0);
        mq.style.display = "";
        mq.style.left = x + "px"; mq.style.top = y + "px";
        mq.style.width = w + "px"; mq.style.height = h + "px";
        // 实时把框内的音符加进选择集。**只收当前轨**：跨轨混合选择会让
        // "选中然后铺歌词"失去意义（一轨一条单声部线，歌词只铺在一条线上）。
        S.selection = drag.add ? Object.assign({}, drag.base) : {};
        var s0 = xToSec(x), s1 = xToSec(x + w);
        var pHi = yToPitch(y), pLo = yToPitch(y + h);
        var only = S.tracks[S.activeTrack];
        if (only) {
          only.notes.forEach(function (n, i) {
            if (n.end > s0 && n.start < s1 && n.pitch >= pLo && n.pitch <= pHi) {
              S.selection[key(S.activeTrack, i)] = true;
            }
          });
        }
        refreshSelectionUI();
        return;
      }

      var dSec = xToSec(pt.x - drag.x0);
      var dPitch = -Math.round((pt.y - drag.y0) / S.rowH);

      drag.members.forEach(function (m) {
        var n = noteAt(m.t, m.i);
        if (!n) return;
        if (drag.mode === "move") {
          var ns = m.s + dSec;
          if (ns < 0) ns = 0;
          n.start = ns;
          n.end = ns + (m.e - m.s);
          n.pitch = clamp(m.p + dPitch, 21, 108);
        } else {
          var ne = m.e + dSec;
          if (ne <= n.start + 0.01) ne = n.start + 0.01;
          n.end = ne;
        }
        placeNote(noteEls[key(m.t, m.i)], n);
      });
    }

    function endGesture(ev) {
      if (!drag) return;
      if (drag.mode === "marquee") {
        mq.style.display = "none";
        var wasClick = !drag.moved;            // 没怎么动 = 就是"点了一下"
        drag = null;
        try { lanes.releasePointerCapture(ev.pointerId); } catch (e) { /* 已释放 */ }
        if (wasClick && ev) {
          // 点空白处 → 进度条跳到那一秒（节流 1 秒，见 SEEK_THROTTLE_MS）
          seekAtTime(xToSec(localPoint(ev).x));
        }
        return;
      }
      drag = null;
      // 拖动结束才吸附：拖动过程中跟手，松手落格（与参考项目"实时吸附"不同，
      // 实时吸附会让慢速拖动一卡一卡的，手感差）
      if (opts.snapWhileDragging !== false) quantize(selectedList(), true);
      // 拖完立刻恢复"同轨不重叠"：把被压到的后续音符往后推
      normalizeMonophonic(S.activeTrack);
      render();
      changed();
      try { lanes.releasePointerCapture(ev.pointerId); } catch (e) { /* 已释放 */ }
    }

    /** 同一条轨内**不允许重叠**：后一个音符的起点被推到前一个的结束点。
     *
     *  为什么必须这样：ABC 的一条 ``V:`` 天生单声部，重叠的音符在导出侧会被
     *  `monophonic_groups()` 拆成 `器乐旋律1` / `器乐旋律2`…… 用户要的是**一条完整
     *  的旋律线**，不是被拆开的碎片。所以在编辑时就维持单声部，而不是等到导出再拆。
     *
     *  就地改对象、**不重排 `tr.notes`**：重排会让 `S.selection` / `S.active` 里存的
     *  下标全部错位，选中状态会莫名其妙跳到别的音符上。
     *
     *  Returns: 被推动过的音符个数。
     */
    function normalizeMonophonic(ti) {
      var tr = S.tracks[ti];
      if (!tr || tr.notes.length < 2) return 0;
      var ns = tr.notes.slice().sort(function (a, b) {
        return a.start - b.start || a.pitch - b.pitch;
      });
      var moved = 0;
      for (var i = 1; i < ns.length; i++) {
        var prev = ns[i - 1], cur = ns[i];
        if (cur.start >= prev.end - 1e-9) continue;
        // 至少保一个网格步长，绝不产出零长度音符（那在后端是非法输入）
        var minDur = Math.max(0.01,
          blickToSec(secToBlick(prev.end, S.tempoMap) + stepBlick(), S.tempoMap) - prev.end);
        var dur = Math.max(minDur, cur.end - cur.start);
        cur.start = prev.end;
        if (cur.end <= cur.start) cur.end = cur.start + dur;
        moved++;
      }
      return moved;
    }

    /** 把给定音符量化到当前网格；silent=true 时不推撤销（调用方已推过）。 */
    function quantize(list, silent) {
      if (!list.length) return 0;
      if (!silent) pushUndo();
      var st = stepBlick();
      var tm = S.tempoMap;
      var n = 0;
      list.forEach(function (m) {
        var note = noteAt(m.t, m.i);
        if (!note) return;
        var bs = snapB(secToBlick(note.start, tm));
        var be = snapB(secToBlick(note.end, tm));
        if (be - bs < st) be = bs + st;
        var ns = blickToSec(bs, tm), ne = blickToSec(be, tm);
        if (Math.abs(ns - note.start) > 1e-9 || Math.abs(ne - note.end) > 1e-9) n++;
        note.start = ns; note.end = ne;
      });
      return n;
    }

    function refreshSelectionUI() {
      Object.keys(noteEls).forEach(function (kk) {
        var p = kk.split(":");
        noteEls[kk].classList.toggle("sel", isSelected(+p[0], +p[1]));
      });
    }

    lanes.addEventListener("pointerdown", beginGesture);
    lanes.addEventListener("pointermove", moveGesture);
    lanes.addEventListener("pointerup", endGesture);
    lanes.addEventListener("pointercancel", endGesture);

    // 双击空白 → 在最近的半音/网格上新增一个当前网格长度的音符
    lanes.addEventListener("dblclick", function (ev) {
      var noteEl = ev.target.closest ? ev.target.closest(".pr-note") : null;
      if (noteEl && !noteEl.classList.contains("pr-chord")) {
        // 双击已有音符 → 去编辑它的歌词，比"新增"更符合直觉
        S.active = { t: +noteEl.dataset.t, i: +noteEl.dataset.i };
        selectOnly(S.active.t, S.active.i);
        refreshSelectionUI();
        syncActiveUI();
        lyrInput.focus();
        lyrInput.select();
        return;
      }
      var tr = S.tracks[S.activeTrack];
      if (!tr) return;
      var pt = localPoint(ev);
      var tm = S.tempoMap;
      var bs = snapB(secToBlick(xToSec(pt.x), tm));
      var be = bs + stepBlick();
      var note = {
        start: blickToSec(bs, tm),
        end: blickToSec(be, tm),
        pitch: clamp(yToPitch(pt.y), 21, 108),
        lyric: "la"
      };
      pushUndo();
      tr.notes.push(note);
      tr.notes.sort(function (a, b) { return a.start - b.start || a.pitch - b.pitch; });
      var idx = tr.notes.indexOf(note);
      selectOnly(S.activeTrack, idx);
      normalizeMonophonic(S.activeTrack);
      render();
      syncActiveUI();
      changed();
    });

    // 右键删除（与参考项目一致的手感）
    lanes.addEventListener("contextmenu", function (ev) {
      var noteEl = ev.target.closest ? ev.target.closest(".pr-note") : null;
      if (!noteEl || noteEl.classList.contains("pr-chord")) return;
      ev.preventDefault();
      pushUndo();
      S.tracks[+noteEl.dataset.t].notes.splice(+noteEl.dataset.i, 1);
      S.selection = {}; S.active = null;
      render(); syncActiveUI(); changed();
    });

    /** Ctrl+G 与「吸附」按钮的共同实现：选中优先，没选中就整条当前轨。 */
    function doSnap() {
      var list = selectedList();
      var all = [];
      var cur = S.tracks[S.activeTrack];
      if (cur) {
        cur.notes.forEach(function (_n, i) { all.push({ t: S.activeTrack, i: i }); });
      }
      if (!all.length) return;
      var useAll = list.length === 0;
      var n = quantize(useAll ? all : list, false);
      normalizeMonophonic(S.activeTrack);
      render();
      if (n) changed();
      if (opts.onToast) {
        var g = GRIDS.filter(function (x) { return x.div === S.grid; })[0];
        opts.onToast(
          useAll
            ? "已把「" + (cur ? cur.display : "当前轨") + "」全部 " + all.length
              + " 个音符吸附到 " + (g ? g.label : S.grid)
            : "已吸附 " + n + " 个音符到 " + (g ? g.label : S.grid),
          n ? "ok" : ""
        );
      }
    }

    // ---------------------------------------------------------------- 键盘
    host.addEventListener("keydown", function (ev) {
      var tag = (ev.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "select" || tag === "textarea") return;
      var mod = ev.ctrlKey || ev.metaKey;

      if (mod && ev.key.toLowerCase() === "z") {
        ev.preventDefault();
        if (ev.shiftKey) doRedo(); else doUndo();
        return;
      }
      if (mod && ev.key.toLowerCase() === "y") { ev.preventDefault(); doRedo(); return; }

      // ★ Ctrl+A：全选**当前曲目**的音符。
      // 刻意不跨轨：卷帘一次只编一条轨（与导出的 SVP 一致，一轨一条单声部线），
      // 全选当前轨之后再输入歌词，就能一个字一个音符地铺下去。
      if (mod && ev.key.toLowerCase() === "a") {
        ev.preventDefault();
        S.selection = {};
        var cur = S.tracks[S.activeTrack];
        if (cur) {
          cur.notes.forEach(function (_n, i) { S.selection[key(S.activeTrack, i)] = true; });
          // 让歌词框可用（它靠 S.active 判断"有没有在编的音符"）
          if (cur.notes.length) S.active = { t: S.activeTrack, i: 0 };
        }
        refreshSelectionUI();
        if (opts.onToast) {
          opts.onToast(cur
            ? ("已全选「" + cur.display + "」的 " + cur.notes.length + " 个音符，可直接输入歌词")
            : "当前没有曲目", "");
        }
        syncActiveUI();
        return;
      }

      // ★ Ctrl+L：歌词填充（弹窗在 index.html，这里只转发）
      if (mod && ev.key.toLowerCase() === "l") {
        ev.preventDefault();
        if (opts.onLyrics) opts.onLyrics();
        return;
      }

      // ★ Ctrl+G：一键把选中（或全部）吸附到当前网格
      if (mod && ev.key.toLowerCase() === "g") {
        ev.preventDefault();
        doSnap();
        return;
      }

      if (ev.key === "Delete" || ev.key === "Backspace") {
        var sel = selectedList();
        if (!sel.length) return;
        ev.preventDefault();
        pushUndo();
        // 从后往前删，避免下标错位
        sel.sort(function (a, b) { return b.t - a.t || b.i - a.i; });
        sel.forEach(function (m) { S.tracks[m.t].notes.splice(m.i, 1); });
        S.selection = {}; S.active = null;
        render(); syncActiveUI(); changed();
        return;
      }

      if (ev.key === "Enter") {
        ev.preventDefault();
        lyrInput.focus();
        lyrInput.select();
        return;
      }

      var dirs = { ArrowUp: 1, ArrowDown: -1, ArrowLeft: -1, ArrowRight: 1 };
      if (dirs[ev.key] !== undefined) {
        var l2 = selectedList();
        if (!l2.length) return;
        ev.preventDefault();
        pushUndo();
        var oct = ev.shiftKey ? 12 : 1;
        var tm2 = S.tempoMap;
        var st2 = stepBlick();
        l2.forEach(function (m) {
          var n = noteAt(m.t, m.i);
          if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
            n.pitch = clamp(n.pitch + dirs[ev.key] * oct, 21, 108);
          } else {
            var d = dirs[ev.key] * st2 * oct;
            var bs = snapB(secToBlick(n.start, tm2) + d);
            var dur = secToBlick(n.end, tm2) - secToBlick(n.start, tm2);
            n.start = blickToSec(bs, tm2);
            n.end = blickToSec(bs + dur, tm2);
          }
        });
        normalizeMonophonic(S.activeTrack);
        render(); changed();
      }
    });

    // ---------------------------------------------------------------- 工具栏
    gridSel.onchange = function () {
      S.grid = +gridSel.value;
      render();
      if (opts.onGrid) opts.onGrid(S.grid);
    };
    snapBtn.onclick = doSnap;
    octUp.onclick = function () { S.lowPitch = clamp(S.lowPitch + 12, 21, 108 - S.rows + 1); render(); };
    octDn.onclick = function () { S.lowPitch = clamp(S.lowPitch - 12, 21, 108 - S.rows + 1); render(); };
    // 缩放按钮也走同一套"以中心为锚"的缩放，保证与 Ctrl+滚轮行为一致
    zin.onclick = function () { zoomAt(1.3); };
    zout.onclick = function () { zoomAt(1 / 1.3); };
    zfit.onclick = function () { fit(); render(); };

    /** 横向缩放。**以 clientX 处的时间点为锚**——锚点下面的音符保持不动，
        否则每缩放一次视图就整体往左跑，根本没法对着某个音看细节。
        clientX 省略时用可视区中心（工具栏的 ＋/－ 按钮走这条）。 */
    function zoomAt(factor, clientX) {
      var rect = scroll.getBoundingClientRect();
      var localX = (clientX == null || !isFinite(clientX)) ? rect.width / 2 : (clientX - rect.left);
      var tAt = (scroll.scrollLeft + localX) / S.pxPerSec;
      var next = clamp(S.pxPerSec * factor, MIN_PPS, MAX_PPS);
      if (next === S.pxPerSec) return false;
      S.pxPerSec = next;
      render();
      scroll.scrollLeft = Math.max(0, tAt * S.pxPerSec - localX);
      return true;
    }

    /* ★ Ctrl（macOS 上是 Cmd）+ 滚轮 = **卷帘横向缩放**，而不是浏览器整页缩放。
       两件事必须一起做：
         1. `passive:false` 才拦得住 —— 用默认的 passive 监听器调 preventDefault 无效，
            Chrome 照样去缩放整个页面；
         2. 触控板的双指捏合在 Chrome 里就是**以 ctrlKey 的 wheel 事件**送达的，
            所以这一处顺手把"在卷帘上捏合缩放页面"也挡掉了，正合预期。
       普通滚轮不拦：那是纵向翻页，用户还需要它。 */
    function onWheelZoom(ev) {
      if (!ev.ctrlKey && !ev.metaKey) return;
      ev.preventDefault();
      // 不同设备的 deltaY 量级差很多（鼠标滚轮 ±100、触控板 ±1、按行模式 ±3），
      // 所以先把 deltaMode 归一成像素，再取指数 —— 这样滚轮和触控板手感一致。
      var d = ev.deltaY;
      if (ev.deltaMode === 1) d *= 16;
      else if (ev.deltaMode === 2) d *= 400;
      var step = clamp(Math.exp(-d * 0.0015), 0.5, 2);
      zoomAt(step, ev.clientX);
    }
    // 挂在整块卷帘上（含工具栏）：在卷帘范围内按 Ctrl+滚轮都算缩放卷帘
    host.addEventListener("wheel", onWheelZoom, { passive: false });

    function fit() {
      var w = Math.max(200, scroll.clientWidth - 8);
      if (S.duration > 0) S.pxPerSec = clamp(w / S.duration, MIN_PPS, MAX_PPS);
    }

    // ---------------------------------------------------------------- 歌词
    function activeNote() {
      if (!S.active) return null;
      var tr = S.tracks[S.active.t];
      return tr ? tr.notes[S.active.i] : null;
    }

    /** 选中的音符按时间排序（同刻按音高）—— 铺歌词的顺序。 */
    function selectedOrdered() {
      return selectedList().sort(function (a, b) {
        var na = noteAt(a.t, a.i), nb = noteAt(b.t, b.i);
        if (!na || !nb) return 0;
        return na.start - nb.start || na.pitch - nb.pitch;
      });
    }

    /** 歌词分词。**必须与后端 `app.lyrics._split_units` 同一套规则** ——
        两边不一致的话，同一句话在"卷帘上手动铺"和"批量导入"里会切出不同结果。 */
    function splitUnits(text, mode) {
      var t = String(text == null ? "" : text);
      if (mode === "char") {
        return t.split("").filter(function (c) { return !/\s/.test(c) && c !== "\u3000"; });
      }
      if (mode === "space") {
        return t.split(/[\s\u3000]+/).filter(function (u) { return u.length > 0; });
      }
      var units = [], buf = "";
      function flush() { if (buf) { units.push(buf); buf = ""; } }
      for (var i = 0; i < t.length; i++) {
        var ch = t.charAt(i);
        var isAscii = t.charCodeAt(i) < 128;
        if (isAscii && (/[0-9A-Za-z]/.test(ch) || ch === "'" || ch === "-")) {
          buf += ch;
        } else if (/\s/.test(ch) || ch === "\u3000") {
          flush();
        } else if (/[\p{L}\p{N}]/u.test(ch)) {
          flush();
          units.push(ch);              // CJK 等非拉丁字母：各自成一个单元
        } else if (buf) {
          buf += ch;                   // 标点并入当前拉丁单元；中文标点丢弃
        }
      }
      flush();
      return units;
    }

    /** 歌词编辑的撤销粒度：一次"聚焦到失焦"算一步，而不是每个按键一步。
        否则打个五个字的歌词要按五次 Ctrl+Z。 */
    var lyricUndoMarked = false;
    function beginLyricEdit() {
      if (lyricUndoMarked) return;
      pushUndo();
      lyricUndoMarked = true;
    }

    /** 把一段文字按时间顺序铺到**选中的音符**上（一个字/词一个音符）。
        这是 Ctrl+A 之后输入歌词的实现：选中 12 个音符、贴一句 12 个字的歌词
        （或直接一个字一个字打），依次落到 12 个音符上。
        Returns: 实际填了几个音符。 */
    function distributeLyrics(text) {
      var sel = selectedOrdered();
      if (sel.length < 2) return 0;
      var units = splitUnits(text, S.splitMode);
      if (!units.length) return 0;
      beginLyricEdit();
      var n = Math.min(units.length, sel.length);
      for (var k = 0; k < n; k++) {
        var note = noteAt(sel[k].t, sel[k].i);
        if (!note) continue;
        note.lyric = units[k];
        var el = noteEls[key(sel[k].t, sel[k].i)];
        if (el) {
          var ly = el.querySelector(".pr-note-ly");
          if (ly) ly.textContent = lyricGlyph(units[k]);
        }
      }
      changed();
      return n;
    }

    /** 输入框内容变了：**多选就铺开、单选就改那一个**。
        这个分派是"卷帘上填歌词"的全部逻辑，所以放在一个地方，别散开。 */
    function applyLyricInput() {
      if (selectedList().length > 1) {
        distributeLyrics(lyrInput.value);
        updateLyricHint();
      } else {
        setActiveLyric(lyrInput.value);
      }
    }

    function updateLyricHint() {
      var sel = selectedList().length;
      var cur = S.tracks[S.activeTrack];
      if (sel > 1) {
        lyrInput.placeholder = "已选中 " + sel + " 个音符：输入/粘贴歌词会按时间顺序铺上去";
        lyrInput.classList.add("bulk");
      } else {
        lyrInput.placeholder = cur
          ? "「" + cur.display + "」——选中音符后在此输入（Ctrl+A 全选后可整段铺歌词）"
          : "选中音符后在此输入（中文可直接用输入法）";
        lyrInput.classList.remove("bulk");
      }
    }

    function syncActiveUI() {
      var n = activeNote();
      var sel = selectedList().length;
      lyrInput.disabled = !n && sel === 0;
      // 多选时输入框是"待铺的整段文字"，不要去回填某个音符的歌词，
      // 否则一按 Ctrl+A 就把它清成第一个音符的 la，看着像坏了。
      lyrInput.value = (sel > 1 || !n) ? "" : String(n.lyric == null ? "" : n.lyric);
      // 第一个音符不可能"延续"，给了 - 就是错的，直接标出来
      lyrInput.classList.toggle("bad",
        sel <= 1 && !!n && n.lyric === "-" && isFirstNote());
      updateLyricHint();
    }
    function isFirstNote() {
      if (!S.active) return false;
      var tr = S.tracks[S.active.t];
      if (!tr || !tr.notes.length) return false;
      var sorted = tr.notes.slice().sort(function (a, b) { return a.start - b.start; });
      return sorted[0] === tr.notes[S.active.i];
    }
    function setActiveLyric(v) {
      var n = activeNote();
      if (!n) return;
      if (n.lyric === v) return;
      beginLyricEdit();
      n.lyric = v;
      var el = noteEls[key(S.active.t, S.active.i)];
      if (el) {
        var ly = el.querySelector(".pr-note-ly");
        if (ly) ly.textContent = lyricGlyph(v);
      }
      syncActiveUI();
      changed();
    }

    /** 把一个记号（`-` / `+`）写到**所有选中**的音符上。 */
    function setSelectionLyric(v) {
      var sel = selectedOrdered();
      if (!sel.length) { setActiveLyric(v); return; }
      beginLyricEdit();
      sel.forEach(function (m) {
        var note = noteAt(m.t, m.i);
        if (!note) return;
        note.lyric = v;
        var el = noteEls[key(m.t, m.i)];
        if (el) {
          var ly = el.querySelector(".pr-note-ly");
          if (ly) ly.textContent = lyricGlyph(v);
        }
      });
      changed();
      syncActiveUI();
    }

    /* 输入法（中文）组合期间不要铺歌词：组合过程中的拼音字母会被当成内容铺出去，
       音符上会闪出一串 a/o/n/i。用 compositionstart/end 把它挡掉。 */
    var composing = false;
    lyrInput.addEventListener("compositionstart", function () { composing = true; });
    lyrInput.addEventListener("compositionend", function () {
      composing = false;
      applyLyricInput();
    });
    lyrInput.addEventListener("focus", function () {
      lyricUndoMarked = false;      // 一次编辑会话 = 一步撤销
      updateLyricHint();
    });
    lyrInput.addEventListener("input", function () {
      if (composing) return;
      applyLyricInput();
    });
    lyrInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        if (selectedList().length > 1) {
          // 多选时内容已经随输入实时铺开了，回车只是"收工"
          var cnt = selectedList().length;
          if (opts.onToast) opts.onToast("已把歌词按顺序铺到 " + cnt + " 个音符上", "ok");
          host.focus();
          syncActiveUI();
        } else {
          stepActive(ev.shiftKey ? -1 : 1);
        }
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        host.focus();
      }
    });
    splitSel.onchange = function () {
      S.splitMode = splitSel.value;
      if (selectedList().length > 1) applyLyricInput();
      if (opts.onToast) {
        opts.onToast("歌词分词："
          + (S.splitMode === "space" ? "按空格切" : S.splitMode === "char" ? "逐字符" : "自动（中文逐字、英文按词）"),
          "");
      }
    };
    susBtn.onclick = function () { setSelectionLyric("-"); lyrInput.focus(); };
    sylBtn.onclick = function () { setSelectionLyric("+"); lyrInput.focus(); };
    nextBtn.onclick = function () { stepActive(1); };

    /** 在**同一条轨**内按时间顺序移动到上/下一个音符。 */
    function stepActive(d) {
      if (!S.active) return;
      var tr = S.tracks[S.active.t];
      if (!tr) return;
      var order = tr.notes.map(function (n, i) { return { i: i, s: n.start }; })
        .sort(function (a, b) { return a.s - b.s; });
      var pos = order.findIndex(function (o) { return o.i === S.active.i; });
      var nxt = order[pos + d];
      if (!nxt) return;
      selectOnly(S.active.t, nxt.i);
      refreshSelectionUI();
      syncActiveUI();
      lyrInput.focus();
      lyrInput.select();
      // 让激活的音符滚进视野
      var el = noteEls[key(S.active.t, S.active.i)];
      if (el) {
        var x = parseFloat(el.style.left);
        if (x < scroll.scrollLeft + 40 || x > scroll.scrollLeft + scroll.clientWidth - 40) {
          scroll.scrollLeft = Math.max(0, x - scroll.clientWidth / 3);
        }
      }
    }

    // ---------------------------------------------------------------- 公开 API
    function load(payload) {
      payload = payload || {};
      // 重新加载**同一首歌**（保存后、切勾选后、导入歌词后）时，不要重置横向缩放：
      // 用户放大到能看清单个音符，一保存就跳回"整首适宽"，非常难用（用户实测反馈）。
      // 判据是"已经有数据且时长没变"——换歌时仍然该适宽。
      var prevPps = S.pxPerSec;
      var prevScroll = scroll.scrollLeft;
      var sameSong = S.tracks.length > 0
        && Math.abs((payload.duration || 0) - S.duration) < 0.05;

      S.tempoMap = (payload.tempo_map && payload.tempo_map.length) ? payload.tempo_map : [{ t: 0, bpm: payload.bpm || 120 }];
      S.bpm = payload.bpm || 120;
      S.meter = payload.meter || "4/4";
      S.duration = payload.duration || 0;
      S.tracks = (payload.tracks || []).map(function (tr) {
        return {
          voice: tr.voice,
          display: tr.display || tr.voice,
          isVocal: !!tr.is_vocal,
          notes: (tr.notes || []).map(function (n) {
            return {
              start: +n.start, end: +n.end, pitch: +n.pitch,
              lyric: n.lyric == null ? "la" : String(n.lyric)
            };
          }).sort(function (a, b) { return a.start - b.start || a.pitch - b.pitch; })
        };
      });
      S.chords = payload.chord_notes || [];
      S.showChords = S.chords.length > 0;
      S.active = null;
      S.selection = {};
      S.undo.length = 0; S.redo.length = 0;
      S.dirty = false;
      // 新数据到达时把"当前编辑的轨"夹回合法范围，并默认落在**人声主旋律**上
      // （歌词主要填这条），其次第一条。
      var prefer = S.tracks.findIndex(function (t) { return t.isVocal; });
      S.activeTrack = clamp(prefer >= 0 ? prefer : 0, 0, Math.max(0, S.tracks.length - 1));
      splitSel.value = S.splitMode;
      refreshUndoBtns();
      if (sameSong) {
        // 只夹一下范围，不动用户的缩放；并把横向滚动位置也还原回去
        S.pxPerSec = clamp(prevPps, MIN_PPS, MAX_PPS);
        render();
        scroll.scrollLeft = prevScroll;
      } else {
        fit();
        render();
      }
      syncActiveUI();
    }

    function state() {
      return {
        tracks: S.tracks.map(function (tr) {
          return {
            voice: tr.voice,
            notes: tr.notes.map(function (n) {
              return { start: n.start, end: n.end, pitch: n.pitch, lyric: n.lyric };
            })
          };
        })
      };
    }

    /** 检查同一轨内的重叠，返回提示文本数组（保存前给用户看）。 */
    function overlaps() {
      var out = [];
      S.tracks.forEach(function (tr) {
        var ns = tr.notes.slice().sort(function (a, b) { return a.start - b.start; });
        var bad = 0;
        for (var i = 1; i < ns.length; i++) if (ns[i].start < ns[i - 1].end - 1e-9) bad++;
        if (bad) out.push(tr.display + " 有 " + bad + " 处重叠，导出时会自动拆成 " + tr.display + "1/" + tr.display + "2…");
      });
      return out;
    }

    function setGrid(div) {
      if (!GRIDS.some(function (g) { return g.div === div; })) return;
      S.grid = div;
      gridSel.value = String(div);
      render();
    }

    // 首屏就要画一次：否则没有任务时 lanes 高度为 0，连"扒谱完成后这里显示…"
    // 那句占位提示都看不见。load() 之后还会再画一次。
    render();

    // 窗口变化时重算"多少像素一秒"与行高（行高是按可用高度反推的）
    var resizeTimer = null;
    window.addEventListener("resize", function () {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        if (!host.isConnected) return;   // 组件已从页面上摘掉就别再动它
        render();
      }, 150);
    });

    return {
      load: load,
      state: state,
      setPlayhead: setPlayhead,
      setGrid: setGrid,
      grid: function () { return S.grid; },
      dirty: function () { return S.dirty; },
      clearDirty: function () { S.dirty = false; },
      overlaps: overlaps,
      // ---- 给验收脚本用的只读访问器（闭包里的东西外面看不到） ----
      /** 当前选中的音符（{t,i} 列表）。 */
      selection: function () {
        return selectedList().map(function (m) { return { t: m.t, i: m.i }; });
      },
      /** 当前正在编辑的轨下标。 */
      activeTrack: function () { return S.activeTrack; },
      /** 切到某条轨（等价于点上方「曲目」按钮）。 */
      setActiveTrack: setActiveTrack,
      /** 界面上实际渲染出来的音符元素个数（淡显隐藏时会更少）。 */
      renderedNotes: function () { return Object.keys(noteEls).length; },
      /** 当前歌词分词模式。 */
      splitMode: function () { return S.splitMode; },
      /** 当前播放头位置（秒），给验收脚本用。 */
      playhead: function () { return playSec; },
      /** 横向缩放（每秒像素）与缩放函数，给验收脚本用。 */
      pxPerSec: function () { return S.pxPerSec; },
      zoomAt: zoomAt,
      /** 手动跑一次「同轨不重叠」规整（正常编辑路径会自动跑），给验收脚本用。 */
      normalizeMonophonic: function (ti) {
        return normalizeMonophonic(ti == null ? S.activeTrack : ti);
      },
      requireVocalLyrics: function () {
        // 人声主旋律一个非 la 的歌词都没有 → 返回 true（导出前要弹确认框）
        var found = false;
        S.tracks.forEach(function (tr) {
          if (!tr.isVocal) return;
          tr.notes.forEach(function (n) {
            var t = String(n.lyric == null ? "" : n.lyric).trim();
            if (t && t !== "la") found = true;
          });
        });
        return !found;
      },
      resize: function () { fit(); render(); },
      GRIDS: GRIDS
    };
  }

  global.PianoRoll = { mount: mount, GRIDS: GRIDS, DEFAULT_GRID: DEFAULT_GRID };
})(window);
