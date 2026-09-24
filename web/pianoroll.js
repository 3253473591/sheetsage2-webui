/* ============================================================================
   钢琴卷帘（Piano Roll）—— SheetSage2 扒谱 ③ 的可编辑谱面
   ============================================================================

   为什么是独立文件：渲染 / 网格 / 指针交互 / 撤销栈加起来已经远超"页面上的一小块
   脚本"，内联会让 index.html 无法维护。没有构建步骤，直接 <script src> 加载。

   设计要点（每一条都是想清楚才这么定的）
   --------------------------------------
   1. **时间用秒，网格用「谱面位置」（全音符为单位）。**
      变速曲里一小节的秒数不是常数，所以"每拍多少像素"也不是常数：网格线与吸附必须
      在谱面位置上算，再换算成秒来画。

   2. **换算与后端同源。**
      ``posToSec`` / ``secToPos`` 就是 ``app/abcp.py::_score_to_seconds`` 的镜像
      （按 playback.json 的小节表做分段线性插值 + 末小节速度外推）。音符本来就是后端
      用这套映射换算成秒的，前端用同一套，音符才吸得到自己所在小节的格子上；这也正是
      **不拿拍号去硬乘**的原因 —— 模型会产出不规整小节，硬乘必然越到后面越偏。

   3. **网格线分三档，且按缩放自动降级。**
      小节线 > 拍线（四分）> 细分线。哪一档太密（间距 < MIN_LINE_PX）就不画，
      所以缩得很小时只会看到小节线，而不是糊成一片。

   4. **每条轨单声部。**
      同轨内一旦重叠，导出侧会把这条轨拆成 ``器乐旋律1`` / ``器乐旋律2``……用户要的是
      一条完整旋律线。所以编辑结束（拖动/拉伸/新增）就地规整：后一个音符的起点被推到
      前一个的结束点（见 ``normalizeMonophonic``），而**不是**等到导出再拆。

   5. **滚动条是真的。**
      宿主用原生 overflow 滚动，横向 = 时间轴，纵向 = 音高。行高固定，所以音域宽的曲子
      纵向会出现滚动条；网格内容按可视区裁剪渲染（culling），节点数不随时长线性增长。

   6. **拖动期间不整屏重绘。** 只改被拖那几条的 style；结构变化（增删/撤销/重新加载）
      才整屏重建。
   ========================================================================== */
(function (global) {
  "use strict";

  /* ---------------------------------------------------------------- 网格档位
     需求里点名的六档，顺序照写。div = 一个四分音符被切成几份。
     默认 1/4 个四分音符（16 分音符）= div 4。 */
  var GRIDS = [
    { div: 1,  label: "四分音符",                    short: "1/4" },
    { div: 2,  label: "1/2 个四分音符（8 分音符）",   short: "1/8" },
    { div: 4,  label: "1/4 个四分音符（16 分音符）",  short: "1/16" },
    { div: 6,  label: "1/6 个四分音符（16 分三连音）", short: "1/16T" },
    { div: 8,  label: "1/8 个四分音符（32 分音符）",  short: "1/32" },
    { div: 12, label: "1/12 个四分音符（32 分三连音）", short: "1/32T" }
  ];
  var DEFAULT_DIV = 4;

  //: 音高范围**固定为 C0–C8**（MIDI 12–108），不跟着曲子音域变。
  //
  //  为什么不做成"跟着数据自适应"：自适应会让键盘的行数与每行的 y 在编辑过程中变化，
  //  于是"往上拖高五度"要分几步——拖到当前最高点、松手、等布局重算、再接着拖。
  //  固定全音域后纵向滚动条**永远存在**，往上拖就是一路拖过去，不用停。
  //  代价是行数多（97 行 × 14px ≈ 1.36k px），所以要靠滚动条，加载时自动滚到音符所在音区。
  var PITCH_LO = 12, PITCH_HI = 108;
  //: 音符可落到的音高范围（与视图一致，免得看得见却放不下）
  var PITCH_MIN = PITCH_LO, PITCH_MAX = PITCH_HI;
  var ROW_H = 14;                        // 行高（固定，才有纵向滚动条）
  var MIN_LINE_PX = 6;                   // 比这更密的网格档位就不画
  var KEYS_W = 46;                       // 左侧音名列宽
  var RULER_H = 20;
  var DEFAULT_DUR_WHOLE = 0.25;          // 双击新增音符的默认时值 = 一个四分音符
  var MIN_NOTE_WHOLE = 1 / 128;          // 最短音符，防止拖出零长度
  var UNDO_DEPTH = 50;
  var SELECT_COLOR_TRACKS = 4;

  var NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  var BLACK = { 1: 1, 3: 1, 6: 1, 8: 1, 10: 1 };

  function isBlack(p) { return !!BLACK[((p % 12) + 12) % 12]; }

  function pitchName(p) {
    return NAMES[((p % 12) + 12) % 12] + (Math.floor(p / 12) - 1);
  }

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  function el(tag, cls, parent) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }

  /* ==================================================================== */
  /*  组件主体                                                             */
  /* ==================================================================== */

  function create(host, opts) {
    opts = opts || {};
    var onStatus = opts.onStatus || function () {};
    var onToast = opts.onToast || function () {};
    var onEdit = opts.onEdit || function () {};
    var onSeek = opts.onSeek || function () {};
    var onRequestLyrics = opts.onRequestLyrics || null;

    /* ------------------------------------------------------------ 状态 */
    var S = {
      tracks: [],
      measures: [],
      beatsPerBar: 4,
      beatUnit: 4,
      unitWhole: 0.0625,
      bpm: 120,
      duration: 0,
      pxPerSec: 60,
      lowPitch: 55,
      rows: 36,
      gridDiv: DEFAULT_DIV,
      snap: true,
      follow: true,        // 播放时视图跟随走带位置
      playing: false,
      activeTrack: 0,
      selection: {},        // "t:i" → true
      active: null,         // {t, i}
      loaded: false
    };
    var undoStack = [], redoStack = [], dirty = false;

    /* ------------------------------------------------------------ 骨架 */
    host.classList.add("pr");

    var bar = el("div", "pr-bar", host);

    // 吸附开关（Ctrl+G）。按钮本身显示状态，省得用户猜现在到底吸不吸。
    var snapBtn = el("button", "pr-btn pr-snap", bar);
    snapBtn.type = "button";

    var snapLab = el("span", "pr-lab", bar);
    snapLab.textContent = "SNAP:";

    var gridSel = el("select", "pr-select", bar);
    GRIDS.forEach(function (g, i) {
      var o = document.createElement("option");
      o.value = String(g.div);
      o.textContent = g.label;
      if (g.div === DEFAULT_DIV) o.selected = true;
      gridSel.appendChild(o);
    });

    // 网格吸附的另一种理解："把选中的音符对齐到网格"。与开关并存，消除歧义。
    var quantBtn = el("button", "pr-btn", bar);
    quantBtn.type = "button";
    quantBtn.textContent = "对齐选中";
    quantBtn.title = "把选中的音符吸附到当前网格（没选中就整条当前轨）";

    el("span", "pr-sep", bar);

    var trackWrap = el("div", "pr-tracks", bar);
    var trackLab = el("span", "pr-lab", trackWrap);
    trackLab.textContent = "当前轨:";
    var trackBtns = el("div", "pr-trackbtns", trackWrap);

    el("span", "pr-sep", bar);

    var undoBtn = el("button", "pr-btn", bar);
    undoBtn.type = "button"; undoBtn.textContent = "撤销"; undoBtn.title = "Ctrl+Z";
    var redoBtn = el("button", "pr-btn", bar);
    redoBtn.type = "button"; redoBtn.textContent = "重做"; redoBtn.title = "Ctrl+Shift+Z / Ctrl+Y";

    el("span", "pr-sep", bar);

    var zoomOut = el("button", "pr-btn pr-mono", bar);
    zoomOut.type = "button"; zoomOut.textContent = "−"; zoomOut.title = "横向缩小（也可 Ctrl+滚轮）";
    var zoomIn = el("button", "pr-btn pr-mono", bar);
    zoomIn.type = "button"; zoomIn.textContent = "＋"; zoomIn.title = "横向放大（也可 Ctrl+滚轮）";
    var zoomFit = el("button", "pr-btn", bar);
    zoomFit.type = "button"; zoomFit.textContent = "适应宽度";

    var followBtn = el("button", "pr-btn", bar);
    followBtn.type = "button";
    followBtn.title = "播放时视图跟随走带位置（快捷键 F）";

    var hint = el("span", "pr-hint", bar);
    hint.innerHTML = "双击空白=加音符 · Delete=删除 · Shift+拖=框选 · Ctrl+A=全选本轨 · "
      + "Ctrl+G=吸附开关 · Ctrl+滚轮=横向缩放 · 点空白=定位 · Ctrl+Z=撤销";

    /* 第二行：歌词。
       为什么用真实的 <input> 而不是"选中音符后直接按键输入"：中文歌词要走输入法，
       输入法需要真实的可聚焦元素来挂候选框，键盘直填打不出中文。 */
    var lyrBar = el("div", "pr-bar pr-lyrbar", host);
    var lyrLab = el("span", "pr-lab", lyrBar);
    lyrLab.textContent = "歌词:";
    var lyricInput = el("input", "pr-lyr-input", lyrBar);
    lyricInput.type = "text";
    lyricInput.spellcheck = false;
    lyricInput.placeholder = "选中音符后在此输入（中文可直接用输入法），Tab 到下一个音符";
    var lyrInfo = el("span", "pr-lyr-info", lyrBar);

    var lyrBtn = el("button", "pr-btn", lyrBar);
    lyrBtn.type = "button";
    lyrBtn.textContent = "填词…（Ctrl+L）";

    var body = el("div", "pr-body", host);
    var keysWrap = el("div", "pr-keys-wrap", body);
    var keys = el("div", "pr-keys", keysWrap);
    var scroll = el("div", "pr-scroll", body);

    var content = el("div", "pr-content", scroll);
    var ruler = el("div", "pr-ruler", content);
    var rulerTicks = el("div", "pr-ruler-ticks", ruler);
    var lanes = el("div", "pr-lanes", content);
    var gridSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    gridSvg.setAttribute("class", "pr-gridsvg");
    lanes.appendChild(gridSvg);
    var noteLayer = el("div", "pr-notes", lanes);
    var marquee = el("div", "pr-marquee", lanes);
    var head = el("div", "pr-playhead", lanes);
    var empty = el("div", "pr-empty", lanes);
    empty.textContent = "扒谱完成后，这里显示可编辑的钢琴卷帘";

    /* 填词框（Ctrl+L）。做成组件内部的一层覆盖，而不是页面上的模态：
       填词要看卷帘上选中了哪些音符，放在组件里两者天然同步。
       用 class 而不是 hidden 属性：CSS 里 .pr-dlg 要 display:flex，
       会把 [hidden] 的 display:none 盖掉（UA 样式优先级最低）。 */
    var dlg = el("div", "pr-dlg off", host);
    var dlgBox = el("div", "pr-dlg-box", dlg);
    var dlgTitle = el("div", "pr-dlg-title", dlgBox);
    dlgTitle.textContent = "填词";
    var dlgCount = el("div", "pr-dlg-count", dlgBox);
    // 「按字符隔开」放在填词框里而不是工具栏：它只对这次铺词生效，
    // 摆在框里才不会让人以为它会影响别处的行为。
    var splitWrap = el("label", "pr-chk", dlgBox);
    var splitChk = el("input", "", splitWrap);
    splitChk.type = "checkbox";
    var splitTxt = el("span", "", splitWrap);
    splitTxt.textContent = "按字符隔开（一字一音）";
    splitWrap.title = "勾上：一字一音（我爱你 → 我/爱/你）。不勾：按空格切词（wo ai ni → wo/ai/ni）。";
    var dlgText = el("textarea", "pr-dlg-text", dlgBox);
    dlgText.spellcheck = false;
    dlgText.placeholder = "粘贴或输入歌词。\n"
      + "勾上「按字符隔开」= 一字一音（我爱你 → 我/爱/你）；\n"
      + "不勾 = 按空格切词（wo ai ni → wo/ai/ni）。\n"
      + "没覆盖到的音符保持原样（默认 la）。";
    var dlgRow = el("div", "pr-dlg-row", dlgBox);
    var dlgApply = el("button", "pr-btn primary", dlgRow);
    dlgApply.type = "button";
    dlgApply.textContent = "填入";
    var dlgCancel = el("button", "pr-btn", dlgRow);
    dlgCancel.type = "button";
    dlgCancel.textContent = "取消";
    var dlgTarget = null;

    var noteEls = {};         // "t:i" → element
    var playSec = 0;

    /* ================================================================ */
    /*  坐标换算（与 app/abcp.py::_score_to_seconds 同一套）             */
    /* ================================================================ */

    function uniformWholeSec() {
      // 没有小节表时的等速回退：一个全音符 = 4 拍
      return S.bpm > 0 ? 240 / S.bpm : 2.0;
    }

    function measureRate(m) {
      var span = m.score_end - m.score_start;
      return span > 1e-9 ? (m.end - m.start) / span : uniformWholeSec();
    }

    /**
     * 谱面位置（全音符）→ 秒。**逐条镜像**后端 ``app/abcp.py::_score_to_seconds``：
     *   1. 位置 <= 0 → 首小节起点（不是外推）；
     *   2. 落在某个小节内 → 该小节内按 score 跨度做线性插值；
     *   3. 都不匹配 → 用末小节的速率外推。
     * 边角行为也必须一致，否则"音符吸到哪个格子"和"后端把音符放在哪一秒"会分叉。
     */
    function posToSec(pos) {
      var ms = S.measures;
      if (!ms.length) return pos * uniformWholeSec();
      if (pos <= 0) return ms[0].start;
      for (var i = 0; i < ms.length; i++) {
        var m = ms[i];
        var span = m.score_end - m.score_start;
        if (span <= 1e-9) continue;
        if (m.score_start <= pos && pos <= m.score_end) {
          return m.start + (pos - m.score_start) / span * (m.end - m.start);
        }
      }
      var last = ms[ms.length - 1];
      return last.end + (pos - last.score_end) * measureRate(last);
    }

    /** 秒 → 谱面位置（全音符）。上面那张分段线性映射的逆。 */
    function secToPos(sec) {
      var ms = S.measures;
      if (!ms.length) return sec / uniformWholeSec();
      for (var i = 0; i < ms.length; i++) {
        var m = ms[i];
        var span = m.end - m.start;
        if (span <= 1e-9) continue;
        if (m.start <= sec && sec <= m.end) {
          return m.score_start + (sec - m.start) / span * (m.score_end - m.score_start);
        }
      }
      var first = ms[0];
      if (sec < first.start) {
        var r0 = measureRate(first);
        return r0 > 0 ? first.score_start + (sec - first.start) / r0 : first.score_start;
      }
      var last = ms[ms.length - 1];
      var r = measureRate(last);
      return r > 0 ? last.score_end + (sec - last.end) / r : last.score_end;
    }

    function secToX(sec) { return sec * S.pxPerSec; }
    function xToSec(x) { return S.pxPerSec > 0 ? x / S.pxPerSec : 0; }

    function topPitch() { return S.lowPitch + S.rows - 1; }
    function pitchToY(p) { return (topPitch() - p) * ROW_H; }
    function yToPitch(y) { return clamp(topPitch() - Math.floor(y / ROW_H), PITCH_MIN, PITCH_MAX); }

    /** 当前网格的一步 = 多少全音符（一个四分音符 = 0.25 全音符）。 */
    function stepWhole() { return 0.25 / S.gridDiv; }

    function snapPos(pos) {
      if (!S.snap) return pos;
      var st = stepWhole();
      return Math.round(pos / st) * st;
    }

    /* ================================================================ */
    /*  布局                                                             */
    /* ================================================================ */

    function contentWidth() {
      return Math.max(200, secToX(Math.max(S.duration, 1)) + 80);
    }

    function relayout() {
      // 音高范围固定成整个 C0–C8：行数不再随曲子音域变化，也就没有"拖到一半布局变了"
      // 那个问题。视图该看哪一段由滚动位置决定（load 之后会滚到音符所在音区）。
      S.rows = PITCH_HI - PITCH_LO + 1;
      S.lowPitch = PITCH_LO;

      var w = contentWidth();
      content.style.width = w + "px";
      ruler.style.width = w + "px";
      lanes.style.width = w + "px";
      lanes.style.height = (S.rows * ROW_H) + "px";
      keys.style.height = (S.rows * ROW_H) + "px";
      ruler.style.height = RULER_H + "px";
      // 音名列要让出标尺那一条的高度，否则音名会和行整体错开 RULER_H。
      // 这里用一个像素值把两侧对齐，比在 CSS 里再写一遍 20px 更不容易失配。
      keysWrap.style.paddingTop = RULER_H + "px";
      renderKeys();
    }

    /** 纵向滚到某个音高"居中可见"。C0–C8 全展开后不滚一下会停在最低音区。 */
    function scrollToPitch(pitch) {
      var visH = (scroll.clientHeight || 460) - RULER_H;
      var y = pitchToY(clamp(pitch, PITCH_LO, PITCH_HI));
      scroll.scrollTop = Math.max(0, y - Math.max(0, (visH - ROW_H) / 2));
      keys.style.transform = "translateY(" + (-scroll.scrollTop) + "px)";
    }

    /** 加载后把视图带到音符所在的音区（没有音符就停在中音区 C4 附近）。 */
    function scrollToData() {
      var lo = PITCH_HI, hi = PITCH_LO, any = false;
      S.tracks.forEach(function (tr) {
        tr.notes.forEach(function (n) {
          any = true;
          if (n.pitch < lo) lo = n.pitch;
          if (n.pitch > hi) hi = n.pitch;
        });
      });
      scrollToPitch(any ? (lo + hi) / 2 : 60);
    }

    function renderKeys() {
      keys.innerHTML = "";
      for (var i = S.rows - 1; i >= 0; i--) {
        var p = S.lowPitch + i;
        var row = el("div", "pr-key" + (isBlack(p) ? " black" : ""), keys);
        row.style.height = ROW_H + "px";
        row.textContent = (!isBlack(p) || ROW_H >= 13) ? pitchName(p) : "";
      }
    }

    /* ================================================================ */
    /*  渲染：网格（三档层次 + 小节号）                                   */
    /* ================================================================ */

    var SVG_NS = "http://www.w3.org/2000/svg";

    function visibleRange() {
      var x0 = scroll.scrollLeft;
      var w = scroll.clientWidth || 800;
      return { x0: x0, x1: x0 + w, w: w };
    }

    /**
     * 收集可视区内要画的网格线。
     *
     * 三级层次：小节线(0) > 拍线(1，四分位置) > 细分线(2，当前网格步长)。
     * **太密的档位直接不画**（间距 < MIN_LINE_PX）：这既是"层次"的来源，也是缩小时
     * 不会糊成一片的原因 —— 缩得越小，剩下的档位越粗，而不是线变多。
     *
     * 小节线取模型真实的小节划分（``S.measures``），不拿拍号硬乘：变速曲里一小节的
     * 秒数不是常数，模型也可能产出不规整小节。
     */
    function collectGrid() {
      var vis = visibleRange();
      var posA = secToPos(xToSec(vis.x0));
      var posB = secToPos(xToSec(vis.x1));
      var st = stepWhole();
      var out = [];

      var subPx = Math.abs(posToSec(st) - posToSec(0)) * S.pxPerSec;
      var beatPx = Math.abs(posToSec(0.25) - posToSec(0)) * S.pxPerSec;
      var showBeat = beatPx >= MIN_LINE_PX;
      var showSub = showBeat && subPx >= MIN_LINE_PX;

      if (!S.measures.length) {
        // 无小节表（没有 playback.json）：按等速的均匀小节兜底
        var barWhole = S.beatsPerBar * 4 / S.beatUnit / 4;
        var i0 = Math.max(0, Math.floor(posA / barWhole));
        var i1 = Math.ceil(posB / barWhole) + 1;
        for (var b = i0; b <= i1; b++) {
          var base = b * barWhole;
          pushLine(out, posToSec(base), 0, b + 1, true);
          if (!showBeat) continue;
          var nSteps = showSub ? Math.round(barWhole / st) : S.beatsPerBar;
          for (var k = 1; k < nSteps; k++) {
            var p = base + k * (showSub ? st : 0.25);
            if (p > posB) break;
            pushLine(out, posToSec(p), isQuarter(p) ? 1 : 2, 0, false);
          }
        }
        return { lines: out, showSub: showSub };
      }

      for (var mi = 0; mi < S.measures.length; mi++) {
        var m = S.measures[mi];
        if (m.score_end < posA - 1e-9) continue;
        if (m.score_start > posB + 1e-9) break;
        pushLine(out, m.start, 0, mi + 1, true);

        if (!showBeat) continue;
        var span = m.score_end - m.score_start;
        if (span <= 1e-9) continue;
        // 不画小节末端那条（它就是下一个小节的小节线，重复画会加深一次）
        var nSub = Math.min(4096, Math.round(span / (showSub ? st : 0.25)));
        for (var s = 1; s < nSub; s++) {
          var pp = m.score_start + s * (showSub ? st : 0.25);
          if (pp > posB) break;
          pushLine(out, posToSec(pp), isQuarter(pp) ? 1 : 2, 0, false);
        }
      }
      return { lines: out, showSub: showSub };
    }

    function isQuarter(pos) {
      return Math.abs(pos / 0.25 - Math.round(pos / 0.25)) < 1e-6;
    }

    function pushLine(out, sec, level, barNo, isBar) {
      var x = secToX(sec);
      out.push({ x: x, level: level, barNo: barNo, bar: isBar });
    }

    function renderGrid() {
      var vis = visibleRange();
      var g = collectGrid();
      gridSvg.setAttribute("width", vis.w);
      gridSvg.setAttribute("height", S.rows * ROW_H);
      gridSvg.setAttribute("viewBox", vis.x0 + " 0 " + vis.w + " " + (S.rows * ROW_H));
      gridSvg.style.width = vis.w + "px";
      gridSvg.style.height = (S.rows * ROW_H) + "px";
      gridSvg.style.left = vis.x0 + "px";

      var parts = [];
      // 黑白键行底色（层次的一部分：黑键行更深）
      for (var i = 0; i < S.rows; i++) {
        var p = S.lowPitch + i;
        if (!isBlack(p)) continue;
        parts.push('<rect class="pr-row-black" x="' + vis.x0 + '" y="' + ((S.rows - 1 - i) * ROW_H)
          + '" width="' + vis.w + '" height="' + ROW_H + '"/>');
      }
      g.lines.forEach(function (L) {
        var cls = L.level === 0 ? "pr-gl-bar" : (L.level === 1 ? "pr-gl-beat" : "pr-gl-sub");
        parts.push('<line class="' + cls + '" x1="' + L.x.toFixed(2) + '" x2="' + L.x.toFixed(2)
          + '" y1="0" y2="' + (S.rows * ROW_H) + '"/>');
      });
      gridSvg.innerHTML = parts.join("");
    }

    /** 标尺：小节号 + 前导刻度；太密时按 2/5/10 的步长抽稀。 */
    function renderRuler() {
      var vis = visibleRange();
      var secA = xToSec(vis.x0), secB = xToSec(vis.x1);
      var posA = secToPos(secA), posB = secToPos(secB);
      var html = [];
      var barWhole = S.measures.length
        ? Math.max(1e-6, S.measures[0].score_end - S.measures[0].score_start)
        : (S.beatsPerBar * 4 / S.beatUnit / 4);
      var barPx = (S.measures.length
        ? Math.abs(S.measures[0].end - S.measures[0].start)
        : posToSec(barWhole) - posToSec(0)) * S.pxPerSec;
      var step = 1;
      if (barPx > 0) {
        while (barPx * step < 46) step *= (step === 1 ? 2 : (step === 2 ? 2.5 : 2));
      }

      if (!S.measures.length) {
        var i0 = Math.max(0, Math.floor(posA / barWhole));
        var i1 = Math.ceil(posB / barWhole) + 1;
        for (var b = i0; b <= i1; b++) {
          if (b % step !== 0) continue;
          var x = secToX(posToSec(b * barWhole));
          if (x < vis.x0 - 60 || x > vis.x1 + 60) continue;
          html.push('<i class="pr-tick" style="left:' + x.toFixed(1) + 'px"></i>'
            + '<b class="pr-barno" style="left:' + (x + 5).toFixed(1) + 'px">' + (b + 1) + '</b>');
        }
      } else {
        for (var mi = 0; mi < S.measures.length; mi++) {
          var m = S.measures[mi];
          if (m.score_end < posA - 1e-9) continue;
          if (m.score_start > posB + 1e-9) break;
          if (mi % step !== 0) continue;
          var x2 = secToX(m.start);
          if (x2 < vis.x0 - 60 || x2 > vis.x1 + 60) continue;
          html.push('<i class="pr-tick" style="left:' + x2.toFixed(1) + 'px"></i>'
            + '<b class="pr-barno" style="left:' + (x2 + 5).toFixed(1) + 'px">' + (mi + 1) + '</b>');
        }
      }
      rulerTicks.innerHTML = html.join("");
    }

    /* ================================================================ */
    /*  渲染：音符                                                       */
    /* ================================================================ */

    function noteKey(t, i) { return t + ":" + i; }

    function isSelected(t, i) { return !!S.selection[noteKey(t, i)]; }

    function collectVisibleNotes() {
      var vis = visibleRange();
      var secA = xToSec(vis.x0) - 1, secB = xToSec(vis.x1) + 1;
      var out = [];
      S.tracks.forEach(function (tr, t) {
        tr.notes.forEach(function (n, i) {
          if (n.end < secA || n.start > secB) return;
          out.push({ t: t, i: i, n: n, tr: tr });
        });
      });
      return out;
    }

    function renderNotes() {
      var vis = collectVisibleNotes();
      var seen = {};
      vis.forEach(function (it) {
        var k = noteKey(it.t, it.i);
        seen[k] = true;
        var e = noteEls[k];
        if (!e) {
          e = el("div", "pr-note", noteLayer);
          e.dataset.t = String(it.t);
          e.dataset.i = String(it.i);
          var gripL = el("i", "pr-grip pr-grip-l", e);
          var gripR = el("i", "pr-grip pr-grip-r", e);
          gripL.dataset.grip = "l"; gripR.dataset.grip = "r";
          el("span", "pr-ly", e);
          noteEls[k] = e;
        }
        styleNote(e, it.n, it.tr, it.t, it.i);
      });
      // 移出可视区的音符元素回收
      Object.keys(noteEls).forEach(function (k) {
        if (seen[k]) return;
        noteEls[k].remove();
        delete noteEls[k];
      });
      syncLyricInput();
    }

    function styleNote(e, n, tr, t, i) {
      var x = secToX(n.start);
      var w = Math.max(3, secToX(n.end) - x);
      e.style.left = x + "px";
      e.style.width = w + "px";
      e.style.top = pitchToY(n.pitch) + "px";
      e.style.height = (ROW_H - 1) + "px";
      for (var k = 0; k < SELECT_COLOR_TRACKS; k++) {
        e.classList.toggle("pr-v" + k, k === (t % SELECT_COLOR_TRACKS));
      }
      e.classList.toggle("pr-ghost", !tr.editable);
      e.classList.toggle("sel", isSelected(t, i));
      e.classList.toggle("act", !!S.active && S.active.t === t && S.active.i === i);
      e.classList.toggle("playing", noteKey(t, i) === playingKey);
      var ly = e.querySelector(".pr-ly");
      var txt = n.lyric == null ? "" : String(n.lyric);
      if (ly.textContent !== txt) ly.textContent = txt;
      ly.style.display = w >= 14 ? "" : "none";
    }

    function renderPlayhead() {
      head.style.left = secToX(playSec) + "px";
      head.style.height = (S.rows * ROW_H) + "px";
    }

    function renderTracks() {
      trackBtns.innerHTML = "";
      S.tracks.forEach(function (tr, t) {
        var b = el("button", "pr-track" + (t === S.activeTrack ? " on" : ""), trackBtns);
        b.type = "button";
        b.textContent = tr.display + "（" + tr.notes.length + "）";
        if (!tr.editable) b.title = "和弦轨来自 chords.mid，不是 ABC 声部，只能查看";
        b.onclick = function () {
          S.activeTrack = t;
          renderTracks();
          onStatus("当前轨：" + tr.display);
        };
      });
    }

    function render() {
      empty.style.display = S.tracks.length ? "none" : "";
      snapBtn.textContent = S.snap ? "吸附：开（Ctrl+G）" : "吸附：关（Ctrl+G）";
      snapBtn.classList.toggle("primary", S.snap);
      followBtn.textContent = S.follow ? "跟随：开（F）" : "跟随：关（F）";
      followBtn.classList.toggle("primary", S.follow);
      renderGrid();
      renderRuler();
      renderNotes();
      renderPlayhead();
      renderTracks();
      syncLyricInput();
    }

    /* ================================================================ */
    /*  撤销 / 重做                                                       */
    /* ================================================================ */

    function snapshot() {
      return JSON.stringify(S.tracks.map(function (tr) {
        return {
          voice: tr.voice, display: tr.display, kind: tr.kind,
          editable: tr.editable, isVocal: tr.isVocal,
          notes: tr.notes.map(function (n) {
            return { start: n.start, end: n.end, pitch: n.pitch, lyric: n.lyric };
          })
        };
      }));
    }

    function restore(json) {
      S.tracks = JSON.parse(json).map(function (tr) { tr.is_vocal = tr.isVocal; return tr; });
      S.selection = {};
      S.active = null;
      relayout();
      render();
    }

    function pushUndo() {
      undoStack.push(snapshot());
      if (undoStack.length > UNDO_DEPTH) undoStack.shift();
      redoStack.length = 0;
      markDirty();
    }

    function undo() {
      if (!undoStack.length) { onStatus("没有可撤销的操作"); return; }
      redoStack.push(snapshot());
      restore(undoStack.pop());
      markDirty();
      onStatus("已撤销");
    }

    function redo() {
      if (!redoStack.length) { onStatus("没有可重做的操作"); return; }
      undoStack.push(snapshot());
      restore(redoStack.pop());
      markDirty();
      onStatus("已重做");
    }

    function markDirty() {
      dirty = true;
      onEdit();
    }

    /* ================================================================ */
    /*  选中                                                             */
    /* ================================================================ */

    function clearSelection() { S.selection = {}; S.active = null; }

    function selectOnly(t, i) { clearSelection(); S.selection[noteKey(t, i)] = true; S.active = { t: t, i: i }; }

    function toggleSelect(t, i) {
      var k = noteKey(t, i);
      if (S.selection[k]) delete S.selection[k];
      else S.selection[k] = true;
      S.active = { t: t, i: i };
    }

    function selectedList() {
      return Object.keys(S.selection).map(function (k) {
        var p = k.split(":");
        return { t: +p[0], i: +p[1] };
      });
    }

    function editable(t) { return S.tracks[t] && S.tracks[t].editable; }

    function noteAt(t, i) {
      var tr = S.tracks[t];
      return tr ? tr.notes[i] : null;
    }

    /* ================================================================ */
    /*  同轨不重叠                                                       */
    /* ================================================================ */

    /**
     * 同一条轨内**不允许重叠**：后一个音符的起点被推到前一个的结束点。
     *
     * 为什么不等到导出再处理：ABC 的一个 ``V:`` 天生单声部，重叠音符在导出侧会被
     * ``monophonic_groups()`` 拆成 ``器乐旋律1`` / ``器乐旋律2``……用户要的是一条完整
     * 旋律线，不是碎片。所以在编辑时就维持单声部。
     *
     * **谁被拖动谁赢**（用户 2026 反馈后定的规则）：
     *   * 后一个音符被左拖、压到前一个 → **把前一个的尾巴收到它的起点**（"侵占"）；
     *   * 前一个被右拖、压到后一个 → 把后一个往后推（原来的行为，一个音都不少）。
     *
     * 一开始只有"把后一个往后推"一种处理，结果是**后一个音符永远没法往左拖过前一个的
     * 结束点**——一松手就被推回来，看着像"拖不动"。谁在动就得让谁说话。
     *
     * 就地改对象、**不重排 ``tr.notes``**：重排会让 ``S.selection`` / ``S.active`` 里
     * 存的按下标错位，选中状态会莫名跳到别的音符上。
     *
     * Args:
     *     movedIdx: 本次手势刚动过的音符下标集合；``null`` 表示不知道谁动的
     *         （比如整体规整、加载），此时一律退化成"把后一个往后推"。
     *
     * Returns: 被调整过的音符个数。
     */
    function normalizeMonophonic(t, movedIdx) {
      var tr = S.tracks[t];
      if (!tr || tr.notes.length < 2) return 0;
      var isMoved = function (i) { return !!movedIdx && movedIdx[i]; };
      var ns = tr.notes.map(function (n, i) { return { n: n, i: i }; })
        .sort(function (a, b) { return a.n.start - b.n.start || a.n.pitch - b.n.pitch; });
      var touched = 0;
      for (var k = 1; k < ns.length; k++) {
        var prev = ns[k - 1], cur = ns[k];
        if (cur.n.start >= prev.n.end - 1e-9) continue;

        if (isMoved(cur.i) && !isMoved(prev.i)) {
          // 被拖的是后一个 → 它侵占前一个：前一个的尾巴收到它的起点。
          // 前一个至少要留 MIN_NOTE_WHOLE：压成零长度后端会直接 400（"音符时长为 0"）。
          var keep = MIN_NOTE_WHOLE;
          if (cur.n.start - prev.n.start >= keep + 1e-9) {
            var newEnd = Math.max(prev.n.start + keep, cur.n.start);
            if (newEnd < prev.n.end - 1e-9) { prev.n.end = newEnd; touched++; }
          }
          // 前一个已经短到不能再短（两者起点几乎重合，例如模型给的 `[CEG]` 同时音）
          // → 只能把后一个整体推回去，保证不重叠。
          if (cur.n.start < prev.n.end - 1e-9) {
            var d = Math.max(MIN_NOTE_WHOLE, cur.n.end - cur.n.start);
            cur.n.start = prev.n.end;
            cur.n.end = cur.n.start + d;
            touched++;
          }
          continue;
        }

        // 其余情况（拖的是前一个 / 两个都动了 / 不知道谁动的）→ 把后一个**整体往后推**：
        // 起点和终点一起走，**时值原样保留**。只挪起点会把音符越推越短（实测出来的），
        // 而"推"的语义就是整条让开，不是被压扁。
        var dur = Math.max(MIN_NOTE_WHOLE, cur.n.end - cur.n.start);
        cur.n.start = prev.n.end;
        cur.n.end = cur.n.start + dur;
        touched++;
      }
      return touched;
    }

    /** 全部轨规整。``movedByTrack`` 形如 ``{轨下标: {音符下标: true}}``。 */
    function normalizeAll(movedByTrack) {
      var touched = 0;
      S.tracks.forEach(function (tr, t) {
        if (!tr.editable) return;
        touched += normalizeMonophonic(t, movedByTrack ? movedByTrack[t] : null);
      });
      return touched;
    }

    /** 保存前的重叠自检（给页面层弹提示用）。 */
    function overlaps() {
      var out = [];
      S.tracks.forEach(function (tr) {
        if (!tr.editable) return;
        var bad = 0;
        var ns = tr.notes.slice().sort(function (a, b) { return a.start - b.start; });
        for (var i = 1; i < ns.length; i++) {
          if (ns[i].start < ns[i - 1].end - 1e-9) bad++;
        }
        if (bad) out.push(tr.display + " 有 " + bad + " 处重叠");
      });
      return out;
    }

    /* ================================================================ */
    /*  编辑操作                                                         */
    /* ================================================================ */

    function deleteSelected() {
      var list = selectedList().filter(function (m) { return editable(m.t); });
      if (!list.length) { onStatus("没有可删除的音符（和弦轨不可编辑）"); return; }
      pushUndo();
      // 按轨分组、从后往前删，避免下标位移
      var byTrack = {};
      list.forEach(function (m) { (byTrack[m.t] = byTrack[m.t] || []).push(m.i); });
      Object.keys(byTrack).forEach(function (t) {
        byTrack[t].sort(function (a, b) { return b - a; })
          .forEach(function (i) { S.tracks[t].notes.splice(i, 1); });
      });
      clearSelection();
      relayout(); render();
      onStatus("已删除 " + list.length + " 个音符");
    }

    function addNote(t, pos, pitch) {
      var tr = S.tracks[t];
      if (!tr || !tr.editable) { onToast("和弦轨不可编辑，请先切到人声/器乐轨", "warn"); return; }
      pushUndo();
      var start = snapPos(pos);
      var dur = Math.max(stepWhole(), DEFAULT_DUR_WHOLE);
      // 默认时值对齐网格，但至少一步
      if (S.snap) dur = Math.max(stepWhole(), Math.round(dur / stepWhole()) * stepWhole());
      tr.notes.push({
        start: posToSec(start),
        end: posToSec(start + dur),
        pitch: clamp(pitch, PITCH_MIN, PITCH_MAX),
        lyric: "la"
      });
      var idx = tr.notes.length - 1;
      // 新音符就是"这次在动的那一个"：双击落在一条长音符中间时，长音符收到新音符的
      // 起点（侵占）；落在前一个之前则把前一个往后推。见 normalizeMonophonic。
      var moved = {};
      moved[idx] = true;
      normalizeMonophonic(t, moved);
      selectOnly(t, idx);
      render();
      onStatus("已插入音符（默认歌词 la）");
    }

    function quantizeSelected() {
      // 选中优先；没选中就整条当前轨（与 Ctrl+A 的语义一致）
      var targets = selectedList().filter(function (m) { return editable(m.t); });
      if (!targets.length) {
        var at = S.tracks[S.activeTrack];
        if (at && at.editable) {
          targets = at.notes.map(function (_, i) { return { t: S.activeTrack, i: i }; });
        }
      }
      if (!targets.length) { onStatus("没有可吸附的音符"); return; }
      pushUndo();
      var st = stepWhole(), n = 0;
      var movedByTrack = {};
      targets.forEach(function (m) {
        var note = noteAt(m.t, m.i);
        if (!note) return;
        (movedByTrack[m.t] = movedByTrack[m.t] || {})[m.i] = true;
        var ps = snapPos(secToPos(note.start));
        var pe = snapPos(secToPos(note.end));
        if (pe - ps < st) pe = ps + st;
        var ns = posToSec(ps), ne = posToSec(pe);
        if (Math.abs(ns - note.start) > 1e-9 || Math.abs(ne - note.end) > 1e-9) n++;
        note.start = ns; note.end = ne;
      });
      normalizeAll(movedByTrack);
      relayout(); render();
      onStatus("已吸附 " + n + " 个音符到 " + currentGrid().label);
    }

    function currentGrid() {
      for (var i = 0; i < GRIDS.length; i++) if (GRIDS[i].div === S.gridDiv) return GRIDS[i];
      return GRIDS[2];
    }

    function selectAllInActiveTrack() {
      var tr = S.tracks[S.activeTrack];
      if (!tr) return;
      clearSelection();
      tr.notes.forEach(function (_, i) { S.selection[noteKey(S.activeTrack, i)] = true; });
      if (tr.notes.length) S.active = { t: S.activeTrack, i: 0 };
      renderNotes();
      onStatus("已全选「" + tr.display + "」的 " + tr.notes.length + " 个音符");
    }

    function setGridDiv(div) {
      S.gridDiv = div;
      render();
      onStatus("网格：" + currentGrid().label + "（吸附随网格）");
    }

    function setSnap(on) {
      S.snap = !!on;
      render();
      onStatus(S.snap ? "吸附已打开" : "吸附已关闭（可自由摆放）");
    }

    function setZoom(pps, anchorClientX) {
      var next = clamp(pps, 4, 2000);
      var wrapRect = scroll.getBoundingClientRect();
      var px = (anchorClientX == null ? wrapRect.width / 2 : anchorClientX - wrapRect.left);
      var secAt = xToSec(scroll.scrollLeft + px);
      S.pxPerSec = next;
      content.style.width = contentWidth() + "px";
      ruler.style.width = contentWidth() + "px";
      lanes.style.width = contentWidth() + "px";
      scroll.scrollLeft = Math.max(0, secToX(secAt) - px);
      render();
    }

    /** 把整首歌缩放到可视宽度，并**回到开头**。
     *
     *  不能复用 setZoom：那个是"以锚点为中心缩放"，专门用来保持用户当前看的位置。
     *  拟合时若还去保持锚点，加载完曲子视图会停在中间（首屏就看不到第 1 小节）。 */
    function fitWidth() {
      var w = scroll.clientWidth || 800;
      S.pxPerSec = clamp((w - 24) / Math.max(1, S.duration || 1), 4, 2000);
      content.style.width = contentWidth() + "px";
      ruler.style.width = contentWidth() + "px";
      lanes.style.width = contentWidth() + "px";
      scroll.scrollLeft = 0;
      render();
      onStatus("已适应宽度");
    }

    /* ================================================================ */
    /*  指针交互                                                         */
    /* ================================================================ */

    var gesture = null;

    function localPoint(ev) {
      var r = lanes.getBoundingClientRect();
      return { x: ev.clientX - r.left, y: ev.clientY - r.top };
    }

    function hitNote(target) {
      var e = target.closest ? target.closest(".pr-note") : null;
      if (!e || e.dataset.t == null) return null;
      return { t: +e.dataset.t, i: +e.dataset.i, el: e, grip: target.dataset ? target.dataset.grip : null };
    }

    function beginGesture(ev) {
      if (!S.loaded || ev.button !== 0) return;
      var pt = localPoint(ev);
      var hit = hitNote(ev.target);

      if (hit) {
        if (!editable(hit.t)) {
          onToast("和弦轨来自 chords.mid，不能编辑", "warn");
          return;
        }
        if (ev.shiftKey) toggleSelect(hit.t, hit.i);
        else if (!isSelected(hit.t, hit.i)) selectOnly(hit.t, hit.i);
        else S.active = { t: hit.t, i: hit.i };

        var notes = selectedList().filter(function (m) { return editable(m.t); });
        gesture = {
          mode: hit.grip ? "resize" : "move",
          grip: hit.grip,
          x0: pt.x,
          // y0 必须一起记：纵向位移是拿 pt.y 减它算的，漏了它 dPitch 会变成 NaN，
          // 音符音高直接被写成 NaN（表现为"拖一下就没了 / 拖不动"）。
          y0: pt.y,
          moved: false,
          pushed: false,     // 第一次真正移动时才压撤销快照
          items: notes.map(function (m) {
            var n = noteAt(m.t, m.i);
            return { t: m.t, i: m.i, s0: n.start, e0: n.end, p0: n.pitch, el: noteEls[noteKey(m.t, m.i)] };
          })
        };
        lanes.setPointerCapture(ev.pointerId);
        ev.preventDefault();
        renderNotes();
        return;
      }

      // 空白：Shift+拖 = 框选；不按 Shift 拖 = 也是框选（松手没动就是定位）
      gesture = {
        mode: "marquee",
        x0: pt.x, y0: pt.y, additive: ev.shiftKey,
        moved: false, base: ev.shiftKey ? Object.assign({}, S.selection) : {}
      };
      marquee.style.display = "block";
      marquee.style.left = pt.x + "px";
      marquee.style.top = pt.y + "px";
      marquee.style.width = "0px";
      marquee.style.height = "0px";
      lanes.setPointerCapture(ev.pointerId);
      ev.preventDefault();
    }

    function moveGesture(ev) {
      if (!gesture) return;
      var pt = localPoint(ev);
      var dxs = pt.x - gesture.x0;

      if (gesture.mode === "marquee") {
        var dy = pt.y - gesture.y0;
        if (Math.abs(dxs) > 3 || Math.abs(dy) > 3) gesture.moved = true;
        var w = Math.abs(dxs), h = Math.abs(dy);
        marquee.style.left = (Math.min(pt.x, gesture.x0)) + "px";
        marquee.style.top = (Math.min(pt.y, gesture.y0)) + "px";
        marquee.style.width = w + "px";
        marquee.style.height = h + "px";
        return;
      }

      // 纵向也要算"动过了"：只看横向位移的话，纯上下拖会被 endGesture 当成"没动"，
      // 于是不规整、不标脏、**改动不会被保存**（音高明明已经变了）。
      if (Math.abs(dxs) > 2 || Math.abs(pt.y - gesture.y0) > 2) gesture.moved = true;
      // 第一次真正动到音符时压一次撤销快照 —— 必须在**改之前**压，
      // 而且只压一次（否则每帧一个快照，撤销要按到天荒地老）。
      // 之前拖动/改时值完全没有压栈，导致"拖完撤不回去"。
      if (gesture.moved && !gesture.pushed) { pushUndo(); gesture.pushed = true; }
      // 位移在**谱面位置域**里算：指针这次与按下时的位置差，就是音符该走的距离。
      // 变速曲里同样一段像素在不同 BPM 段对应不同时值，用像素差直接改秒会拉伸音符。
      var dPos = secToPos(xToSec(pt.x)) - secToPos(xToSec(gesture.x0));
      var dPitch = Math.round(-(pt.y - gesture.y0) / ROW_H);
      dPitch = clamp(dPitch, -48, 48);

      gesture.items.forEach(function (it) {
        var n = noteAt(it.t, it.i);
        if (!n) return;
        if (gesture.mode === "move") {
          n.start = posToSec(snapPos(secToPos(it.s0) + dPos));
          n.end = posToSec(snapPos(secToPos(it.e0) + dPos));
          if (n.start < 0) { n.end -= n.start; n.start = 0; }
          n.pitch = clamp(it.p0 + dPitch, PITCH_MIN, PITCH_MAX);
        } else if (gesture.grip === "l") {
          var s = snapPos(secToPos(it.s0) + dPos);
          var maxS = secToPos(it.e0) - MIN_NOTE_WHOLE;
          n.start = posToSec(Math.min(s, maxS));
        } else {
          var e2 = snapPos(secToPos(it.e0) + dPos);
          n.end = posToSec(Math.max(e2, secToPos(it.s0) + MIN_NOTE_WHOLE));
        }
        if (it.el) styleNote(it.el, n, S.tracks[it.t], it.t, it.i);
      });
    }

    function endGesture(ev) {
      if (!gesture) return;
      var g = gesture;
      gesture = null;
      try { lanes.releasePointerCapture(ev.pointerId); } catch (e) { /* 已释放 */ }
      marquee.style.display = "none";

      if (g.mode === "marquee") {
        if (!g.moved) {
          // 点空白而不拖 = 定位进度条（需求：立即跳转）
          var sec = Math.max(0, xToSec(localPoint(ev).x));
          setPlayhead(sec);
          onSeek(sec);
          if (!g.additive) { clearSelection(); renderNotes(); }
          return;
        }
        applyMarquee(g);
        return;
      }

      if (!g.moved) return;
      // 告诉规整逻辑"这次是谁在动"：拖谁谁赢，另一个让位（见 normalizeMonophonic）。
      var movedByTrack = {};
      g.items.forEach(function (it) {
        (movedByTrack[it.t] = movedByTrack[it.t] || {})[it.i] = true;
      });
      normalizeAll(movedByTrack);
      relayout(); render();
      markDirty();
      onStatus(g.mode === "move" ? "已移动音符" : "已改变音符长度");
    }

    /** 按选框矩形（存于 marquee 的 style）挑选音符；Shift 时为追加选择。 */
    function applyMarquee(g) {
      var left = parseFloat(marquee.style.left) || 0;
      var top = parseFloat(marquee.style.top) || 0;
      var w = parseFloat(marquee.style.width) || 0;
      var h = parseFloat(marquee.style.height) || 0;

      S.selection = g.additive ? Object.assign({}, g.base) : {};
      S.tracks.forEach(function (tr, t) {
        if (!tr.editable) return;                    // 和弦轨不参与框选
        tr.notes.forEach(function (n, i) {
          var nx0 = secToX(n.start), nx1 = Math.max(nx0 + 1, secToX(n.end));
          var ny0 = pitchToY(n.pitch), ny1 = ny0 + ROW_H;
          if (nx1 < left || nx0 > left + w || ny1 < top || ny0 > top + h) return;
          S.selection[noteKey(t, i)] = true;
        });
      });
      renderNotes();
      onStatus("框选 " + Object.keys(S.selection).length + " 个音符");
    }

    /* ------------------------------------------------------------ 指针接线 */
    /* ⚠️ 这四行是**必须**的：没有它们，拖动音符、拖边缘改长度、框选、点空白取消选择
       全都不会发生（表现是"卷帘是死的"，但代码看起来又都对）。
       曾经在一次大改里被整段删掉而没人发现 —— 现在 tools/check_web_js.py 里有一组
       直接派发合成指针事件的行为测试守着它。 */
    lanes.addEventListener("pointerdown", beginGesture);
    lanes.addEventListener("pointermove", moveGesture);
    lanes.addEventListener("pointerup", endGesture);
    lanes.addEventListener("pointercancel", endGesture);

    /* ------------------------------------------------------------ 标尺定位 */
    // 标尺是 lanes 的兄弟节点（不属于 lanes），所以它的点击不会被 lanes 的指针逻辑
    // 接住；这里单独接一条，否则标尺上那个 ew-resize 光标是骗人的。
    ruler.addEventListener("pointerdown", function (ev) {
      if (!S.loaded || ev.button !== 0) return;
      var r = lanes.getBoundingClientRect();
      var sec = Math.max(0, xToSec(ev.clientX - r.left));
      setPlayhead(sec);
      onSeek(sec);
    });

    /* ------------------------------------------------------------ 双击新增 */
    lanes.addEventListener("dblclick", function (ev) {
      if (!S.loaded) return;
      var hit = hitNote(ev.target);
      if (hit) {
        // 双击已有音符 → 选中它（P3 起用于编辑歌词）
        if (editable(hit.t)) { selectOnly(hit.t, hit.i); renderNotes(); }
        return;
      }
      var pt = localPoint(ev);
      addNote(S.activeTrack, secToPos(xToSec(pt.x)), yToPitch(pt.y));
    });

    /* ------------------------------------------------------------ 滚轮缩放 */
    function onWheel(ev) {
      if (!(ev.ctrlKey || ev.metaKey)) return;   // 普通滚轮留给纵向翻页
      ev.preventDefault();
      var d = ev.deltaY;
      if (ev.deltaMode === 1) d *= 16;
      else if (ev.deltaMode === 2) d *= 100;
      var k = Math.exp(-d * 0.0015);
      setZoom(S.pxPerSec * k, ev.clientX);
    }
    host.addEventListener("wheel", onWheel, { passive: false });

    /* ------------------------------------------------------------ 滚动重绘 */
    var rafPending = false;
    function onScroll() {
      if (rafPending) return;
      rafPending = true;
      requestAnimationFrame(function () {
        rafPending = false;
        renderGrid();
        renderRuler();
        renderNotes();
        renderPlayhead();
      });
    }
    scroll.addEventListener("scroll", onScroll);
    // 键盘列要跟着纵向滚，否则音名和行对不上
    scroll.addEventListener("scroll", function () {
      keys.style.transform = "translateY(" + (-scroll.scrollTop) + "px)";
    });

    /* ------------------------------------------------------------ 快捷键 */
    function onKey(ev) {
      if (!S.loaded) return;
      var tag = (ev.target && ev.target.tagName) || "";
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

      var mod = ev.ctrlKey || ev.metaKey;
      var key = (ev.key || "").toLowerCase();

      // 焦点还在工具栏按钮上时空格是"再按一次这个按钮"，不该被当成播放开关
      if ((key === " " || ev.code === "Space") && tag === "BUTTON") return;
      if (key === " " || ev.code === "Space") {
        ev.preventDefault();
        // 按住空格会连发 keydown；不过滤的话播放/停止会被切成一串抖动
        if (!ev.repeat && opts.onTogglePlay) opts.onTogglePlay();
        return;
      }
      if (key === "delete" || key === "backspace") { ev.preventDefault(); deleteSelected(); return; }
      if (key === "escape") { clearSelection(); renderNotes(); onStatus("已取消选择"); return; }
      if (mod && key === "a") { ev.preventDefault(); selectAllInActiveTrack(); return; }
      if (mod && key === "g") { ev.preventDefault(); setSnap(!S.snap); return; }
      if (key === "f" && !mod) { ev.preventDefault(); setFollow(!S.follow); return; }
      if (mod && key === "l") {
        ev.preventDefault();
        // 页面层可以接管（老实现挂在 ④ 歌词卡片上）；没人接管就用组件自带的填词框
        if (onRequestLyrics) onRequestLyrics(); else openLyricDialog();
        return;
      }
      if (mod && key === "z") { ev.preventDefault(); if (ev.shiftKey) redo(); else undo(); return; }
      if (mod && key === "y") { ev.preventDefault(); redo(); return; }
      if (mod && key === "s") { ev.preventDefault(); if (opts.onSave) opts.onSave(); return; }

      if (ev.key && ev.key.indexOf("Arrow") === 0) {
        var list = selectedList().filter(function (m) { return editable(m.t); });
        if (!list.length) return;
        ev.preventDefault();
        pushUndo();
        var st = stepWhole();
        var movedByTrack = {};
        list.forEach(function (m) {
          var n = noteAt(m.t, m.i);
          if (!n) return;
          (movedByTrack[m.t] = movedByTrack[m.t] || {})[m.i] = true;
          if (ev.key === "ArrowLeft") {
            var s = Math.max(0, secToPos(n.start) - st), d = secToPos(n.end) - secToPos(n.start);
            n.start = posToSec(s); n.end = posToSec(s + d);
          } else if (ev.key === "ArrowRight") {
            var s2 = secToPos(n.start) + st, d2 = secToPos(n.end) - secToPos(n.start);
            n.start = posToSec(s2); n.end = posToSec(s2 + d2);
          } else if (ev.key === "ArrowUp") n.pitch = clamp(n.pitch + 1, PITCH_MIN, PITCH_MAX);
          else if (ev.key === "ArrowDown") n.pitch = clamp(n.pitch - 1, PITCH_MIN, PITCH_MAX);
        });
        normalizeAll(movedByTrack);
        relayout(); render();
        onStatus("已按方向键移动选中音符");
        return;
      }
    }
    global.addEventListener("keydown", onKey);

    /* ------------------------------------------------------------ 工具栏事件 */
    snapBtn.onclick = function () { setSnap(!S.snap); };
    gridSel.onchange = function () { setGridDiv(+gridSel.value); };
    quantBtn.onclick = quantizeSelected;
    undoBtn.onclick = undo;
    redoBtn.onclick = redo;
    zoomIn.onclick = function () { setZoom(S.pxPerSec * 1.3, null); };
    zoomOut.onclick = function () { setZoom(S.pxPerSec / 1.3, null); };
    zoomFit.onclick = fitWidth;
    followBtn.onclick = function () { setFollow(!S.follow); };

    window.addEventListener("resize", function () {
      if (!S.loaded) return;
      renderGrid(); renderRuler(); renderPlayhead();
    });

    /* ================================================================ */
    /*  歌词编辑                                                         */
    /* ================================================================ */

    function selCount() { return Object.keys(S.selection).length; }

    function activeNote() {
      if (!S.active) return null;
      return noteAt(S.active.t, S.active.i);
    }

    var lyricPushed = false;
    var lyricKey = null;

    /** 输入框跟随"当前音符"。多选时不猜用户想改哪一个，直接禁用并说明。
     *
     *  什么时候可以覆盖输入框里正在输入的内容？——只有当**当前音符换了**的时候。
     *  用"正在输入"当作保护条件是不行的：点/敲到另一个音符时焦点可能还在输入框里，
     *  那样输入框会一直显示上一个音符的歌词，填错音高还看不出来。 */
    function syncLyricInput() {
      if (!S.loaded) return;
      var key = S.active ? noteKey(S.active.t, S.active.i) : null;
      var switched = key !== lyricKey;
      lyricKey = key;

      var n = selCount() > 1 ? null : activeNote();
      if (selCount() > 1) {
        lyricInput.value = "";
        lyricInput.disabled = true;
        lyricInput.placeholder = "已选中 " + selCount() + " 个音符：用「填词…（Ctrl+L）」按顺序铺词";
        lyrInfo.textContent = "已选 " + selCount() + " 个";
        return;
      }
      lyricInput.disabled = !n;
      lyrInfo.textContent = "";
      if (!n) {
        lyricInput.value = "";
        lyricInput.placeholder = "选中音符后在此输入（中文可直接用输入法），Tab 到下一个音符";
        return;
      }
      lyricInput.placeholder = "「" + S.tracks[S.active.t].display + "」第 "
        + (S.active.i + 1) + " 个音符的歌词，Tab 到下一个";
      if (switched || document.activeElement !== lyricInput) {
        lyricInput.value = n.lyric == null ? "" : String(n.lyric);
      }
    }

    /** 输入框 → 当前音符。按第一次键时压一次撤销，整段输入算一步。 */
    function commitLyricInput() {
      var n = activeNote();
      if (!n || !editable(S.active.t)) return;
      if (!lyricPushed) { pushUndo(); lyricPushed = true; }
      n.lyric = lyricInput.value;
      var e = noteEls[noteKey(S.active.t, S.active.i)];
      if (e) styleNote(e, n, S.tracks[S.active.t], S.active.t, S.active.i);
      markDirty();
    }

    /** 当前轨的音符按时间排序后，移动到上/下一个。连续填词靠它。 */
    function stepActiveNote(dir) {
      var tr = S.tracks[S.activeTrack];
      if (!tr || !tr.notes.length) return;
      var order = tr.notes.map(function (n, i) { return { n: n, i: i }; })
        .sort(function (a, b) { return a.n.start - b.n.start || a.n.pitch - b.n.pitch; });
      var cur = -1;
      if (S.active && S.active.t === S.activeTrack) {
        for (var k = 0; k < order.length; k++) if (order[k].i === S.active.i) { cur = k; break; }
      }
      if (cur < 0) cur = dir > 0 ? -1 : order.length;
      var next = clamp(cur + dir, 0, order.length - 1);
      selectOnly(S.activeTrack, order[next].i);
      scrollNoteIntoView(S.activeTrack, order[next].i);
      renderNotes();
      syncLyricInput();
      if (!lyricInput.disabled) { lyricInput.focus(); lyricInput.select(); }
    }

    /** 把某个音符横向滚进可视区（跑出屏幕还继续 Tab 就看不见在填哪个了）。 */
    function scrollNoteIntoView(t, i) {
      var n = noteAt(t, i);
      if (!n) return;
      var x = secToX(n.start);
      var visW = scroll.clientWidth || 800;
      if (x < scroll.scrollLeft + 40) scroll.scrollLeft = Math.max(0, x - 80);
      else if (x > scroll.scrollLeft + visW - 60) scroll.scrollLeft = Math.max(0, x - visW + 160);
    }

    /** 填词的目标：**有选中就从选中的第一个开始**，没选中才整条当前轨。
     *
     *  用户明确要求过这个语义：框选之后填词要从第一个被框选的音符开始，
     *  而不是永远从这条轨的第一个音符开始。 */
    function lyricFillTarget() {
      var sel = selectedList().filter(function (m) { return editable(m.t); });
      if (sel.length) return { list: sel, picked: true };
      var tr = S.tracks[S.activeTrack];
      if (tr && tr.editable) {
        return {
          list: tr.notes.map(function (_, i) { return { t: S.activeTrack, i: i }; }),
          picked: false
        };
      }
      return { list: [], picked: false };
    }

    /* ------------------------------------------------------------ 填词框 */
    function openLyricDialog() {
      if (!S.loaded) return;
      var got = lyricFillTarget();
      var target = got.list;
      if (!target.length) { onToast("没有可填词的音符（和弦轨不可编辑）", "warn"); return; }
      var tr = S.tracks[S.activeTrack];
      dlgTarget = target;
      dlgCount.textContent = got.picked
        ? "将按时间顺序填入选中的 " + target.length + " 个音符（从最早选中的那个开始）"
        : "未选中音符，将填入当前轨「" + (tr ? tr.display : "") + "」的全部 " + target.length + " 个音符";
      dlg.classList.remove("off");
      dlgText.value = "";
      dlgText.focus();
    }

    function closeLyricDialog() {
      dlg.classList.add("off");
      dlgTarget = null;
    }

    /**
     * 歌词文本 → 词表。
     *
     * 勾上「按字符隔开」= **一字一音**，且先去掉所有空白（中文歌词常按词组排版，
     * 空格是排版不是发音）；不勾 = 按空白切词（拼音/英文按词走，wo ai ni → wo/ai/ni）。
     */
    function splitLyricTokens(raw, byChar) {
      var s = String(raw == null ? "" : raw);
      if (byChar) return Array.from(s.replace(/\s/g, ""));
      return s.split(/[\s\u3000]+/).filter(function (x) { return x; });
    }

    /**
     * 把词表按时间顺序铺到给定音符上（**就地改 lyric**，不推撤销）。
     *
     * 词比音符少时，多出来的音符**保持原样** —— 不覆盖是有意的：用户常常只想补
     * 后半段，不想把前面已经填好的词冲掉（默认值本来就是 ``la``）。
     * 词比音符多时，多出来的词被忽略，并把数量报回去，让调用方明确告知用户，
     * 而不是静默丢掉。
     *
     * Returns: ``{filled, dropped, kept}``。
     */
    function fillLyricsOn(target, parts) {
      var ordered = (target || []).slice().sort(function (a, b) {
        var na = noteAt(a.t, a.i), nb = noteAt(b.t, b.i);
        if (!na || !nb) return 0;
        return na.start - nb.start || na.pitch - nb.pitch;
      });
      var n = Math.min(ordered.length, parts.length);
      for (var k = 0; k < n; k++) {
        var note = noteAt(ordered[k].t, ordered[k].i);
        if (note) note.lyric = parts[k];
      }
      return { filled: n, dropped: parts.length - n, kept: ordered.length - n };
    }

    /* 工具栏与填词框的事件 */
    function applyLyricDialog() {
      var parts = splitLyricTokens(dlgText.value, splitChk.checked);
      if (!parts.length) { onToast("填词框是空的", "warn"); return; }
      pushUndo();
      var r = fillLyricsOn(dlgTarget || [], parts);
      markDirty();
      renderNotes();
      syncLyricInput();
      closeLyricDialog();
      onStatus("已填词 " + r.filled + " 个音符"
        + (r.dropped > 0 ? "（还有 " + r.dropped + " 个词没地方放，已忽略）" : "")
        + (r.kept > 0 ? "（剩余 " + r.kept + " 个音符保持原样）" : ""));
    }

    lyricInput.addEventListener("input", commitLyricInput);
    lyricInput.addEventListener("focus", function () { lyricPushed = false; });
    lyricInput.addEventListener("blur", function () { lyricPushed = false; });
    lyricInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Tab") {
        ev.preventDefault();
        commitLyricInput();
        lyricPushed = false;
        stepActiveNote(ev.shiftKey ? -1 : 1);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        commitLyricInput();
        lyricPushed = false;
        lyricInput.blur();
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        syncLyricInput();
        lyricInput.blur();
      } else if ((ev.ctrlKey || ev.metaKey) && (ev.key || "").toLowerCase() === "l") {
        ev.preventDefault();
        openLyricDialog();
      }
    });
    lyrBtn.onclick = openLyricDialog;
    dlgApply.onclick = applyLyricDialog;
    dlgCancel.onclick = closeLyricDialog;
    // 点遮罩空白处也关掉（点框体内部不算）
    dlg.addEventListener("click", function (ev) {
      if (ev.target === dlg) closeLyricDialog();
    });

    /* ================================================================ */
    /*  对外 API                                                         */
    /* ================================================================ */

    function setPlayhead(sec) {
      playSec = Math.max(0, sec || 0);
      renderPlayhead();
      updatePlaying();
      followPlayhead();
    }

    /**
     * 播放时让视图跟着走带位置走（用户要求）。
     *
     * 只在**播放中**且**走带跑出可视区**时才滚 —— 每帧无条件居中会让视图一直抖，
     * 而且用户手动滚去看别处时会被立刻拽回来，那样没法边听边看。
     * 留 15% 的右边距：走带贴到右边缘才滚，视线有提前量。
     */
    function followPlayhead() {
      if (!S.playing || !S.follow) return;
      var visW = scroll.clientWidth || 0;
      if (!visW) return;
      var x = secToX(playSec);
      var left = scroll.scrollLeft;
      var pad = Math.max(24, visW * 0.15);
      if (x < left || x > left + visW - pad) {
        scroll.scrollLeft = Math.max(0, x - visW * 0.3);
      }
    }

    function setPlaying(on) {
      S.playing = !!on;
      // 开始播放也当成一次"跟上"：否则从末尾倒回去再播时会停在老位置
      if (S.playing) followPlayhead();
    }

    function setFollow(on) {
      S.follow = !!on;
      render();
      onStatus(S.follow ? "已打开跟随走带位置" : "已关闭跟随");
    }

    /* 播放到哪个音，就在卷帘上点亮哪个音。
       只记录"当前在响的是哪一个"，位置没跨过音符边界时直接返回 ——
       播放循环每帧都会调进来，每帧全量改 classList 会让长曲子掉帧。 */
    var playingKey = null;
    function updatePlaying() {
      var key = null;
      for (var t = 0; t < S.tracks.length; t++) {
        var ns = S.tracks[t].notes;
        for (var i = 0; i < ns.length; i++) {
          if (playSec >= ns[i].start && playSec < ns[i].end) { key = noteKey(t, i); break; }
        }
        if (key) break;
      }
      if (key === playingKey) return;
      if (playingKey && noteEls[playingKey]) noteEls[playingKey].classList.remove("playing");
      playingKey = key;
      if (key && noteEls[key]) noteEls[key].classList.add("playing");
    }

    function clearPlaying() {
      if (playingKey && noteEls[playingKey]) noteEls[playingKey].classList.remove("playing");
      playingKey = null;
    }

    function load(data) {
      S.tracks = (data.tracks || []).map(function (tr) {
        return {
          voice: tr.voice, kind: tr.kind, display: tr.display,
          isVocal: !!tr.is_vocal, is_vocal: !!tr.is_vocal,
          editable: tr.editable !== false,
          notes: (tr.notes || []).map(function (n) {
            return { start: +n.start, end: +n.end, pitch: +n.pitch, lyric: n.lyric == null ? "la" : String(n.lyric) };
          })
        };
      });
      S.measures = (data.measures || []).map(function (m) {
        return { start: +m.start, end: +m.end, score_start: +m.score_start, score_end: +m.score_end };
      }).sort(function (a, b) { return a.score_start - b.score_start; });
      S.beatsPerBar = +data.beats_per_bar || 4;
      S.beatUnit = +data.beat_unit || 4;
      S.unitWhole = +data.unit_whole || 0.0625;
      S.bpm = +data.bpm || 120;
      S.duration = +data.duration || 0;
      S.activeTrack = Math.max(0, S.tracks.findIndex(function (t) { return t.editable; }));
      undoStack.length = 0; redoStack.length = 0;
      dirty = false;
      clearSelection();
      S.loaded = true;
      relayout();
      fitWidth();
      render();
      // 音域固定成 C0–C8 之后，默认视口停在最低那一段；把视图带到音符所在的音区，
      // 否则扒完谱打开卷帘只看到一片空行。
      scrollToData();
    }

    function getTracks() { return S.tracks; }

    /* ================================================================ */
    /*  平行和声轨                                                       */
    /* ================================================================ */

    /** 主人声轨（和声的来源）。``isVocal`` 与 ``kind === 'vocal'`` 都算。 */
    function vocalTrack() {
      for (var i = 0; i < S.tracks.length; i++) {
        var t = S.tracks[i];
        if (t.isVocal || String(t.kind || "").toLowerCase() === "vocal") return t;
      }
      return null;
    }

    /**
     * 把一条**外部算好的**轨放进卷帘（目前只有平行和声用）。
     *
     * 为什么音高换算不在这里做：**音程只有放在调性里才有意义** —— C 上方的三度是 E、
     * D 上方的三度是 F，半音数并不相同。调性来自乐谱表头，所以音乐理论只在
     * `app/harmony.py` 实现一份，前端拿到的是算好的音符（见 `POST /harmony`）。
     *
     * 同 voice 重复放进 = 覆盖：改完主人声再生成一次就重新对齐。
     * 时值、起点、歌词一律照搬传入的音符 —— 平行和声唱同一套词同一个节奏。
     */
    function addTrack(track) {
      if (!track || !track.voice || !track.notes || !track.notes.length) {
        return { ok: false, error: "轨道数据不完整" };
      }
      var notes = track.notes.map(function (n) {
        return {
          start: +n.start, end: +n.end, pitch: Math.round(+n.pitch),
          lyric: n.lyric == null ? "" : String(n.lyric)
        };
      });
      var tr = {
        voice: String(track.voice),
        // 跟源声部同 kind：这样它跟着「人声主旋律」那个勾选一起导出，
        // 不会要求用户再学一个新的导出类别。
        kind: track.kind || "vocal",
        display: track.display || String(track.voice),
        isVocal: !!track.isVocal,
        is_vocal: !!track.isVocal,
        editable: true,
        notes: notes
      };
      var at = -1;
      S.tracks.forEach(function (t, i) { if (t.voice === tr.voice) at = i; });
      if (at >= 0) S.tracks[at] = tr; else S.tracks.push(tr);
      S.activeTrack = at >= 0 ? at : S.tracks.length - 1;

      render();
      markDirty();   // → onEdit()：页面据此刷新按钮并安排自动保存
      return {
        ok: true, voice: tr.voice, display: tr.display, notes: notes.length,
        replaced: at >= 0
      };
    }

    function isDirty() { return dirty; }
    function markSaved() { dirty = false; }

    return {
      load: load,
      render: render,
      setPlayhead: setPlayhead,
      setGridDiv: setGridDiv,
      setSnap: setSnap,
      setFollow: setFollow,
      setPlaying: setPlaying,
      setZoom: setZoom,
      fitWidth: fitWidth,
      normalizeAll: normalizeAll,
      overlaps: overlaps,
      getTracks: getTracks,
      addTrack: addTrack,
      vocalTrack: vocalTrack,
      isDirty: isDirty,
      markSaved: markSaved,
      selectedCount: function () { return Object.keys(S.selection).length; },
      activeTrackDisplay: function () {
        var tr = S.tracks[S.activeTrack];
        return tr ? tr.display : "";
      },
      /** 试听用的扁平音符表（含只读的和弦轨，与页面试听"总谱"语义一致）。 */
      getPlaybackNotes: function () {
        var out = [];
        S.tracks.forEach(function (tr) {
          tr.notes.forEach(function (n) {
            out.push({ start: n.start, end: n.end, pitch: n.pitch });
          });
        });
        out.sort(function (a, b) { return a.start - b.start || a.pitch - b.pitch; });
        return out;
      },
      /** 当前音符的结束时间（编辑后弦长会变，不能用加载时的 duration）。 */
      totalSeconds: function () {
        var d = 0;
        S.tracks.forEach(function (tr) {
          tr.notes.forEach(function (n) { if (n.end > d) d = n.end; });
        });
        return d;
      },
      clearPlaying: clearPlaying,
      openLyricDialog: openLyricDialog,
      addNote: addNote,
      GRIDS: GRIDS,

      /* 给验收脚本（tools/check_web_js.py）用。
         "秒 ↔ 谱面位置"必须和后端逐点一致，否则音符会吸到别的格子上；这条一致性
         没法靠肉眼看，只能拿后端的实现当基准做逐点比对。 */
      _debug: {
        posToSec: posToSec,
        secToPos: secToPos,
        stepWhole: stepWhole,
        measureRate: measureRate,
        collectGrid: collectGrid,
        splitLyricTokens: splitLyricTokens,
        fillLyricsOn: fillLyricsOn,
        selectAll: selectAllInActiveTrack,
        selectedList: selectedList,
        lyricFillTarget: lyricFillTarget,
        setActiveTrack: function (t) { S.activeTrack = t; renderTracks(); },
        // 交互测试要从"外面"派发指针事件，所以得拿到真实元素与几何
        els: {
          host: host, scroll: scroll, lanes: lanes, marquee: marquee,
          noteLayer: noteLayer, ruler: ruler, lyricInput: lyricInput
        },
        rowH: ROW_H,
        secToX: secToX,
        pitchToY: pitchToY,
        state: S
      }
    };
  }

  global.PianoRoll = { create: create, GRIDS: GRIDS, DEFAULT_DIV: DEFAULT_DIV };
})(window);
