"""钢琴卷帘「曲目切换 + Ctrl+A 铺歌词」的交互测试。

这个功能的存在理由（用户原话）：卷帘把三条轨叠在一起时 Ctrl+A 会跨轨混选，
没法"选中一整条轨然后把歌词按顺序铺下去"。所以约定成：

  * 一次只**编辑一条轨**（上方「曲目」切换），其余轨淡显只读；
  * **Ctrl+A 只选当前轨**；
  * 多选时，歌词框里的内容按时间顺序**一个字一个音符**铺到选中音符上；
  * 单选时仍是"改这一个音符"，回车跳到下一个。

运行（需要服务在跑）：
  D:\\AIGC\\YuE2\\yue2_aibbs\\env\\python.exe _smoke\\check_roll_tracks.py [URL]
"""
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799/"
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


# 两条轨，各 4 个音；歌词初始都是 la
SEED = """
() => {
  const mk = (pitches) => {
    let t = 0; const out = [];
    for (const p of pitches) { out.push({start:t, end:t+0.5, pitch:p, lyric:'la'}); t += 0.5; }
    return out;
  };
  window.__seed = {
    bpm:120, meter:'4/4', duration:2.0,
    tempo_map:[{t:0,bpm:120}],
    tracks:[
      {voice:'Vocal', display:'主人声', is_vocal:true,  notes:mk([60,62,64,65])},
      {voice:'Ins',   display:'器乐旋律', is_vocal:false, notes:mk([48,50,52,53])}
    ],
    chord_notes: [], has_vocal_lyrics:false
  };
  roll.load(window.__seed);
  document.querySelector('#scoreCard').classList.remove('collapsed');
}
"""

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1400, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_function("() => typeof roll === 'object' && roll")
    pg.evaluate(SEED)
    pg.wait_for_timeout(300)

    # ---- 1. 曲目切换器 ----
    btns = pg.eval_on_selector_all("#prTracks .pr-track",
                                   "els => els.map(e => [e.textContent, e.classList.contains('on')])")
    check("曲目按钮两条", len(btns) == 2, str(btns))
    check("按钮带轨名与音符数", btns and "主人声" in btns[0][0] and "4" in btns[0][0], str(btns))
    check("默认落在人声主旋律上", btns and btns[0][1] is True and btns[1][1] is False, str(btns))
    check("有「显示其他轨」开关", pg.locator("#prTracks .pr-chk input").count() == 1)

    # ---- 2. 淡显：非当前轨不可点 ----
    ghost = pg.eval_on_selector_all(".pr-note.pr-ghost",
                                    "els => els.map(e => [e.dataset.t, getComputedStyle(e).pointerEvents])")
    check("非当前轨的 4 个音符都淡显", len(ghost) == 4, str(len(ghost)))
    check("淡显音符 pointer-events=none（点不到）",
          all(g[1] == "none" for g in ghost), str(ghost))

    # ---- 3. Ctrl+A 只选当前轨 ----
    pg.eval_on_selector(".pr", "e => e.focus()")
    pg.keyboard.press("Control+a")
    pg.wait_for_timeout(200)
    sel = pg.evaluate("() => roll.selection().map(m => m.t)")
    check(f"Ctrl+A 只选中当前轨的 4 个音符（轨下标 {sorted(set(sel))}）",
          len(sel) == 4 and set(sel) == {0}, str(sel))
    check("全选后歌词框可用", pg.eval_on_selector("#prLyric", "e => !e.disabled"))
    check("全选后歌词框进入「批量」样式",
          pg.eval_on_selector("#prLyric", "e => e.classList.contains('bulk')"))

    # ---- 4. 输入歌词 → 按顺序铺到选中音符 ----
    pg.fill("#prLyric", "我爱你啊")
    pg.wait_for_timeout(250)
    got = pg.evaluate("() => roll.state().tracks[0].notes.map(n => n.lyric)")
    check(f"「我爱你啊」逐字铺到 4 个音符（{got}）", got == ["我", "爱", "你", "啊"], str(got))
    other = pg.evaluate("() => roll.state().tracks[1].notes.map(n => n.lyric)")
    check(f"另一条轨的歌词没被动（{other}）", other == ["la"] * 4, str(other))

    # ---- 5. 按空格切分 ----
    pg.select_option("#prSplit", "space")
    pg.fill("#prLyric", "wo ai ni la")
    pg.wait_for_timeout(250)
    got2 = pg.evaluate("() => roll.state().tracks[0].notes.map(n => n.lyric)")
    check(f"切到「按空格」后 wo/ai/ni/la（{got2}）", got2 == ["wo", "ai", "ni", "la"], str(got2))
    pg.select_option("#prSplit", "auto")

    # ---- 6. 记号按钮：多选时写到所有选中 ----
    pg.eval_on_selector(".pr", "e => e.focus()")
    pg.keyboard.press("Control+a")
    pg.wait_for_timeout(150)
    pg.click("#prSustain")
    pg.wait_for_timeout(200)
    got3 = pg.evaluate("() => roll.state().tracks[0].notes.map(n => n.lyric)")
    check(f"点 ‿ 后选中音符都变 -（{got3}）", got3 == ["-"] * 4, str(got3))
    pg.eval_on_selector(".pr", "e => e.focus()")
    pg.keyboard.press("Control+a")
    pg.wait_for_timeout(150)
    pg.click("#prSyllable")
    pg.wait_for_timeout(200)
    got4 = pg.evaluate("() => roll.state().tracks[0].notes.map(n => n.lyric)")
    check(f"点 ⁀ 后选中音符都变 +（{got4}）", got4 == ["+"] * 4, str(got4))

    # ---- 7. 切轨：选择清空、编辑对象换掉 ----
    pg.eval_on_selector_all("#prTracks .pr-track", "els => els[1].click()")
    pg.wait_for_timeout(250)
    check("切轨后当前轨变成第二条",
          pg.evaluate("() => document.querySelectorAll('#prTracks .pr-track')[1].classList.contains('on')"))
    check("切轨后选择被清空", pg.evaluate("() => roll.selection().length") == 0)
    gray = pg.eval_on_selector_all(".pr-note.pr-ghost",
                                   "els => els.map(e => e.dataset.t)")
    check(f"淡显的换成第一条轨（{sorted(set(gray))}）", set(gray) == {"0"}, str(gray))
    pg.eval_on_selector(".pr", "e => e.focus()")
    pg.keyboard.press("Control+a")
    pg.wait_for_timeout(150)
    pg.fill("#prLyric", "甲乙丙丁")
    pg.wait_for_timeout(250)
    got5 = pg.evaluate("() => roll.state().tracks[1].notes.map(n => n.lyric)")
    check(f"新轨也能铺歌词（{got5}）", got5 == ["甲", "乙", "丙", "丁"], str(got5))

    # 一条语义要固化下来：auto 模式下**一个拉丁词是一个单元**，只落一个音符 ——
    # 这正是 `+`（多音节拆分）记号存在的理由，不是 bug。
    pg.fill("#prLyric", "opendoor")
    pg.wait_for_timeout(250)
    got6 = pg.evaluate("() => roll.state().tracks[1].notes.map(n => n.lyric)")
    check(f"auto 下「opendoor」算一个词、只落第一个音符（{got6}）",
          got6 == ["opendoor", "乙", "丙", "丁"], str(got6))

    # ---- 8. 关掉「显示其他轨」只剩当前轨 ----
    pg.eval_on_selector("#prTracks .pr-chk input", "e => e.click()")
    pg.wait_for_timeout(250)
    check("关掉后没有淡显音符", pg.locator(".pr-note.pr-ghost").count() == 0,
          str(pg.locator(".pr-note.pr-ghost").count()))
    check("关掉后当前轨的 4 个音符还在", pg.evaluate("() => roll.renderedNotes()") == 4,
          str(pg.evaluate("() => roll.renderedNotes()")))

    # ---- 9. Ctrl+滚轮 = 卷帘横向缩放（不是浏览器整页缩放）----
    pg.evaluate("() => { roll.zoomAt(1); }")           # 归位
    pg.wait_for_timeout(150)

    def wheel(delta_y, ctrl):
        return pg.evaluate(
            """([dy, ctrl]) => {
                 const sc = document.querySelector('.pr-scroll');
                 const r = sc.getBoundingClientRect();
                 const ev = new WheelEvent('wheel', {
                   deltaY: dy, ctrlKey: ctrl, bubbles: true, cancelable: true,
                   clientX: r.left + 200, clientY: r.top + 60
                 });
                 sc.dispatchEvent(ev);
                 return { prevented: ev.defaultPrevented };
               }""",
            [delta_y, ctrl])

    pps0 = pg.evaluate("() => roll.pxPerSec()")
    r1 = wheel(-120, True)
    pg.wait_for_timeout(150)
    pps1 = pg.evaluate("() => roll.pxPerSec()")
    check(f"Ctrl+滚轮向上 → 横向放大（{pps0:.1f} → {pps1:.1f} px/s）", pps1 > pps0,
          f"{pps0} → {pps1}")
    check("Ctrl+滚轮被 preventDefault（否则浏览器会缩放整页）", r1["prevented"] is True, str(r1))

    r2 = wheel(120, True)
    pg.wait_for_timeout(150)
    pps2 = pg.evaluate("() => roll.pxPerSec()")
    check(f"Ctrl+滚轮向下 → 横向缩小（{pps1:.1f} → {pps2:.1f} px/s）", pps2 < pps1,
          f"{pps1} → {pps2}")

    r3 = wheel(-120, False)
    pg.wait_for_timeout(150)
    pps3 = pg.evaluate("() => roll.pxPerSec()")
    check("普通滚轮不缩放卷帘（那是翻页，不能抢）", abs(pps3 - pps2) < 1e-6, f"{pps2} → {pps3}")
    check("普通滚轮没有被 preventDefault（不破坏翻页）", r3["prevented"] is False, str(r3))

    # 锚点：缩放前后，指针那一列对应的时间应该基本不变。
    # 注意先把内容撑到**横向溢出**——不溢出时 scrollLeft 恒为 0，锚点无从体现
    # （这不是 bug，是"没得滚"）。
    anchor = pg.evaluate(
        """() => {
             const sc = document.querySelector('.pr-scroll');
             roll.zoomAt(12);                 // 放大到内容溢出
             sc.scrollLeft = 300;
             const r = sc.getBoundingClientRect();
             const localX = 200;
             const t0 = (sc.scrollLeft + localX) / roll.pxPerSec();
             const ev = new WheelEvent('wheel', {deltaY:-240, ctrlKey:true, bubbles:true,
                                                 cancelable:true, clientX:r.left+localX, clientY:r.top+60});
             sc.dispatchEvent(ev);
             const t1 = (sc.scrollLeft + localX) / roll.pxPerSec();
             return {t0: t0, t1: t1, overflows: sc.scrollWidth > sc.clientWidth,
                     scrollLeft: sc.scrollLeft, pps: roll.pxPerSec()};
           }""")
    check("测试前提：内容确实横向溢出了（不溢出就没什么可滚）", anchor["overflows"] is True,
          str(anchor))
    check(f"缩放以指针处为锚点（时间 {anchor['t0']:.3f}s → {anchor['t1']:.3f}s）",
          abs(anchor["t0"] - anchor["t1"]) < 0.02, str(anchor))

    # ---- 9. 点空白处 → 进度条跳转（1 秒节流，避免连点卡死）----
    pg.evaluate("() => { S.playback = {notes: [], duration: 4.0}; melodyOffset = 0; }")
    # 归零横向滚动再取几何：前面缩放测试把时间轴滚走了，此时 lanes 的视口左边是负值，
    # 照它算点击位置会点到滚动区外面（琴键列上），根本到不了 lanes。
    geo = pg.evaluate(
        """() => {
             const sc = document.querySelector('.pr-scroll');
             const ln = document.querySelector('.pr-lanes');
             // 卷帘卡片在首屏之下，不滚进视口的话算出来的坐标落在**视口外面**，
             // elementFromPoint 会返回 null，点击根本不会发生（第一次写这条踩的坑）。
             sc.scrollIntoView({block: 'center'});
             sc.scrollLeft = 0;
             const s = sc.getBoundingClientRect(), l = ln.getBoundingClientRect();
             return {sLeft: s.left, sRight: s.right, scrollLeft: sc.scrollLeft,
                     pps: roll.pxPerSec(),
                     yLo: Math.max(l.top, s.top, 0) + 6,
                     yHi: Math.min(l.bottom, s.bottom, window.innerHeight) - 6};
           }""")
    # 点**可视区内**、且**确实落在 lanes 上**的空白：纵坐标取 lanes 可见范围的底边
    # （音域下沿以下是空的）。容器可能比 lanes 高，点太低就落不到 lanes 上、
    # 事件根本到不了卷帘。
    click_x = geo["sLeft"] + 120
    click_y = geo["yHi"]
    assert geo["yLo"] <= click_y, f"点击纵坐标不在 lanes 的可见范围内：{geo}"
    assert click_x <= geo["sRight"] - 10, f"点击横坐标不在滚动区内：{geo}"
    top_el = pg.evaluate(
        "([x,y]) => { const e = document.elementFromPoint(x,y); return e ? e.className : null; }",
        [click_x, click_y])
    check(f"点击点确实在卷帘上（命中 {top_el!r}）",
          bool(top_el) and ("pr-notes" in top_el or "pr-lanes" in top_el or "pr-note" in top_el),
          f"{top_el!r} @ ({click_x:.0f},{click_y:.0f}) {geo}")

    before = pg.evaluate("() => JSON.stringify(roll.state())")
    pg.mouse.click(click_x, click_y)
    pg.wait_for_timeout(250)
    off1 = pg.evaluate("() => melodyOffset")
    want1 = (geo["scrollLeft"] + 120) / geo["pps"]
    after = pg.evaluate("() => JSON.stringify(roll.state())")
    check("点空白处没有改动任何音符（没误点成拖动/新增）", before == after)
    check(f"点空白处 → 进度条跳到 {off1:.3f}s（期望约 {want1:.3f}s）",
          abs(off1 - want1) < 0.05, f"got {off1} want {want1}")
    check("进度条填充也动了", (pg.evaluate("() => $('#tpFill').style.width") or "") != "",
          repr(pg.evaluate("() => $('#tpFill').style.width")))

    pg.mouse.click(click_x + 140, click_y)
    pg.wait_for_timeout(250)
    off2 = pg.evaluate("() => melodyOffset")
    check(f"1 秒窗口内的第二次点击先不生效（立即返回，不卡）", abs(off2 - off1) < 1e-6,
          f"{off1} → {off2}")
    pg.wait_for_timeout(900)
    off3 = pg.evaluate("() => melodyOffset")
    check(f"窗口末尾把合并的那次定位补上（{off2:.3f}s → {off3:.3f}s）", off3 > off2 + 1e-6,
          f"{off2} → {off3}")

    # ---- 9b. 拖标尺上的抓手定位（不明显就等于没有，所以要验抓手本身）----
    pg.wait_for_timeout(200)
    check("标尺上有播放头抓手（.pr-head-grab）",
          pg.locator(".pr-head-grab").count() == 1)
    check("抓手可见且有尺寸", pg.evaluate(
        """() => { const e=document.querySelector('.pr-head-grab'); const r=e.getBoundingClientRect();
                   return r.width > 4 && r.height > 8 && getComputedStyle(e).cursor === 'ew-resize'; }"""))
    pg.evaluate("() => { melodyOffset = 0; }")
    grab = pg.evaluate(
        """() => { const r = document.querySelector('.pr-head-grab').getBoundingClientRect();
                   return {x: r.left + r.width/2, y: r.top + r.height/2}; }""")
    pg.mouse.move(grab["x"], grab["y"])
    pg.mouse.down()
    mid = pg.evaluate("() => roll.playhead()")
    pg.mouse.move(grab["x"] + 160, grab["y"], steps=10)
    pg.wait_for_timeout(120)
    during = pg.evaluate("() => roll.playhead()")
    check(f"拖动过程中播放头跟着走（{mid:.3f}s → {during:.3f}s，且还没真正跳转）",
          during > mid + 1e-6 and pg.evaluate("() => melodyOffset") == 0,
          f"during={during} offset={pg.evaluate('() => melodyOffset')}")
    pg.mouse.up()
    pg.wait_for_timeout(400)
    check(f"松手后真正跳转（melodyOffset={pg.evaluate('() => melodyOffset'):.3f}s）",
          pg.evaluate("() => melodyOffset") > 0,
          str(pg.evaluate("() => melodyOffset")))

    # ---- 10. 同轨不重叠：把音符拖到前一个身上，应被推回去 ----
    pg.eval_on_selector_all("#prTracks .pr-track", "els => els[0].click()")
    pg.wait_for_timeout(200)
    boxes = pg.evaluate(
        """() => [...document.querySelectorAll('.pr-note:not(.pr-ghost)')].map(e => {
             const r = e.getBoundingClientRect();
             return {t: +e.dataset.t, i: +e.dataset.i,
                     x: r.left + r.width/2, y: r.top + r.height/2, w: r.width};
           }).sort((a,b) => a.i - b.i)""")
    check("当前轨有 4 个可编辑音符", len(boxes) == 4, str(len(boxes)))
    src, dst = boxes[3], boxes[1]           # 把最后一个拖到第二个的位置上
    pg.mouse.move(src["x"], src["y"])
    pg.mouse.down()
    pg.mouse.move(dst["x"], dst["y"], steps=6)
    pg.mouse.up()
    pg.wait_for_timeout(350)

    ov = pg.evaluate(
        """() => {
             const ns = roll.state().tracks[0].notes.slice()
               .sort((a,b) => a.start - b.start);
             let bad = 0;
             for (let i=1;i<ns.length;i++) if (ns[i].start < ns[i-1].end - 1e-9) bad++;
             return {bad: bad, notes: ns.map(n => [ +n.start.toFixed(4), +n.end.toFixed(4) ])};
           }""")
    check(f"拖出重叠后自动消解，同轨零重叠（{ov['bad']} 处）", ov["bad"] == 0, str(ov))
    starts = [n[0] for n in ov["notes"]]
    check(f"起点严格递增（{starts}）", all(starts[i] < starts[i + 1] for i in range(len(starts) - 1)),
          str(starts))

    # ---- 11. 失焦 300ms 自动保存 ----
    saved = {}

    def route_roll(route):
        try:
            saved["body"] = route.request.post_data_json
        except Exception:  # noqa: BLE001
            saved["body"] = None
        route.fulfill(status=200, json={"ok": True, "exports": [], "warnings": [],
                                        "voices": ["Vocal"], "notes": 4,
                                        "has_vocal_lyrics": True})

    pg.route("**/api/tasks/*/roll", route_roll)
    # 假装有一个已完成的任务（不跑推理）
    pg.evaluate("() => { S.taskId='stub-task'; S.state='done'; S.token='stub';"
                " refreshButtons(); roll.clearDirty(); }")
    pg.eval_on_selector(".pr", "e => e.focus()")
    pg.keyboard.press("Control+a")
    pg.wait_for_timeout(120)
    pg.fill("#prLyric", "一二三四")
    check("编辑后卷帘是脏的", pg.evaluate("() => roll.dirty()") is True)

    # 关键：**焦点一直留在卷帘里**（不失焦），改完也应当自动保存。
    # 用户明确指出过：要"修改完音符"就更新，不是"点到卷帘外面"才更新。
    check("焦点仍在卷帘内（这一次刻意不失焦）",
          pg.evaluate("() => document.querySelector('#prHost').contains(document.activeElement)")
          is True)
    pg.wait_for_timeout(120)
    check("刚改完还没存（300ms 防抖）", "body" not in saved, str(saved))
    pg.wait_for_timeout(700)
    check("停手 300ms 后自动保存了（**没有失焦**）", "body" in saved, str(saved))
    check("焦点依然在卷帘内", pg.evaluate(
        "() => document.querySelector('#prHost').contains(document.activeElement)") is True)
    # 载荷检查必须**紧跟第一次保存**：下面第 12 段还会再触发一次保存，
    # 会把 saved 覆盖掉（第一次写成"甲乙丙丁"就踩过这个）。
    _body = saved.get("body") or {}
    _tr = _body.get("tracks") or []
    _lyr = (_tr[0].get("notes") or [{}])[0].get("lyric") if _tr else None
    check(f"自动保存的载荷带上了刚填的歌词（首音 = {_lyr!r}）", _lyr == "一", str(_body)[:200])
    check("保存后不再是脏的", pg.evaluate("() => roll.dirty()") is False)

    # ---- 12. 保存/重载**不能**把横向缩放重置掉 ----
    # 用户实测反馈："每次都需要重新放大…改完一个音符后失焦，又回到 100% 了"。
    # 根因是 POST /roll 广播结果 → SSE → onResult → reloadRoll → roll.load() → fit()。
    # 修了两处：/roll 不再广播；load() 对**同一首歌**保留缩放与滚动位置。
    pg.evaluate("() => roll.zoomAt(6)")
    pg.wait_for_timeout(200)
    pps_zoom = pg.evaluate("() => roll.pxPerSec()")
    check(f"放大后 pxPerSec 确实变大了（{pps_zoom:.1f} px/s）", pps_zoom > 0, str(pps_zoom))

    pg.evaluate("() => roll.load(window.__seed)")      # 模拟保存后的重载
    pg.wait_for_timeout(250)
    pps_reload = pg.evaluate("() => roll.pxPerSec()")
    check(f"同一首歌重载后缩放**不变**（{pps_zoom:.1f} → {pps_reload:.1f}）",
          abs(pps_reload - pps_zoom) < 1e-6, f"{pps_zoom} → {pps_reload}")

    pg.evaluate("() => { const p = JSON.parse(JSON.stringify(window.__seed)); p.duration = 99;"
                " roll.load(p); }")
    pg.wait_for_timeout(250)
    pps_other = pg.evaluate("() => roll.pxPerSec()")
    check(f"换歌（时长不同）才重新适宽（{pps_reload:.1f} → {pps_other:.1f}）",
          abs(pps_other - pps_reload) > 1e-6, f"{pps_reload} → {pps_other}")

    # 再验一次"保存之后缩放还在"（这次走的是真的自动保存路径）
    pg.evaluate("() => roll.load(window.__seed)")
    pg.wait_for_timeout(200)
    pg.evaluate("() => roll.zoomAt(8)")
    pg.wait_for_timeout(200)
    pps_before_save = pg.evaluate("() => roll.pxPerSec()")
    pg.eval_on_selector(".pr", "e => e.focus()")
    pg.keyboard.press("Control+a")
    pg.wait_for_timeout(120)
    pg.fill("#prLyric", "甲乙丙丁")
    pg.wait_for_timeout(900)          # 等自动保存跑完
    pps_after_save = pg.evaluate("() => roll.pxPerSec()")
    check(f"自动保存之后缩放依然不变（{pps_before_save:.1f} → {pps_after_save:.1f}）",
          abs(pps_after_save - pps_before_save) < 1e-6,
          f"{pps_before_save} → {pps_after_save}")
    check("全程没有 JS 报错", not errs, str(errs))
    b.close()

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 曲目切换、Ctrl+A 只选本轨、歌词按顺序铺开、记号按钮与淡显都正确")
