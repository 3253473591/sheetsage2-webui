"""UI 冒烟测试（钢琴卷帘版）：页面结构 + ③ 卷帘骨架 + 导出勾选 + 提交 payload。

⚠️ 这一版对的是 **2026-09 的卷帘重写版**：
   * 卡片收敛成 **4 张**，编号是 ① ② ③ ⑤ —— ④ 整块并进了 ③（歌词不再是单独一步）；
   * ③ 里只剩一个 ``#rollHost``；卷帘本体（``.pr`` / 键列 / 播放头 / 网格 / 歌词框）
     由 ``web/pianoroll.js`` 在**有任务数据时**才挂载（``ensureRoll()``），
     空页面上 ``#rollHost`` 就是空的，只显示 ``#rollState`` 的文字占位。

   所以本脚本**只开浏览器、不跑推理**（科目 4 的定义，见打包说明 §8.2）。
   卷帘**内部**的渲染与交互由 ``_smoke\\check_export_consistency.py``
   用真实任务 + 真浏览器覆盖。

   ⚠️ 老版本这里找的是 ``#prHost`` / ``#prGrid`` / ``#prSnap`` / ``#prLyric`` …
      那一整套控件是**上一代卷帘**的 DOM，重写后已全部不存在；
      网格档位也搬进了 ``pianoroll.js`` 内部，页面上不再有那个下拉框。

覆盖：
  * 4 张卡的编号与标题；ABC 文本框 / 五线谱 / 旧歌词卡确实已删除
  * ``#rollHost`` 骨架、顶栏三个导出勾选、保存/放弃按钮、走带条
  * ``GET /pianoroll.js`` 真的被浏览器取到、``window.PianoRoll`` 就绪
  * 提交 payload：发 ``export_voices``，不再发 ``only_melody``

前置：服务已在跑。运行::

    D:\\AIGC\\YuE2\\yue2_aibbs\\env\\python.exe _smoke\\check_ui_layout.py [URL]
"""
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777/"
FAILS = []

#: ③ 顶栏三个导出勾选的文案（顺序即声明顺序）
VOICE_CHIPS = [("vocal", "人声主旋律"), ("ins", "器乐旋律"), ("chords", "和弦")]


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1100})

    captured = {}
    errors = []
    roll_js = {}

    def route_tasks(route):
        try:
            captured["body"] = route.request.post_data_json
        except Exception:  # noqa: BLE001
            captured["body"] = None
        route.fulfill(status=200, json={"task_id": "smoke", "state": "queued", "out_dir": ""})

    page.on("pageerror", lambda e: errors.append(str(e)))

    def on_response(resp):
        if resp.url.endswith("/pianoroll.js"):
            roll_js["status"] = resp.status
            try:
                roll_js["len"] = len(resp.body())
            except Exception:  # noqa: BLE001
                roll_js["len"] = -1

    page.on("response", on_response)
    page.route("**/api/tasks", route_tasks)

    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof S !== 'undefined' && !!document.querySelector('#btnStart')")
    page.wait_for_function("() => !!window.PianoRoll")

    # ③ 默认折叠；展开走应用自己的折叠按钮（顺带验那条路径不会炸）。
    if page.evaluate("() => document.getElementById('scoreCard')"
                     ".classList.contains('collapsed')"):
        page.eval_on_selector("button.fold[data-fold='scoreCard']", "e => e.click()")
        page.wait_for_timeout(600)

    # ---- 1. 卡片：4 张，编号 ① ② ③ ⑤ ----
    cards = page.eval_on_selector_all(
        "section.card .card-head",
        "els => els.map(e => [e.querySelector('.num')?.textContent, e.querySelector('h2')?.textContent])")
    cards = [[n, t] for n, t in cards if t]
    expect = [["1", "拖入音频"], ["2", "进度"], ["3", "钢琴卷帘"], ["5", "导出"]]
    check("卡片数量与编号标题（4 张：① ② ③ ⑤，④ 已并入 ③）", cards == expect, f"实际 {cards}")

    # ---- 2. 被删掉的东西不该还在 ----
    for sel in ("#abcArea", "#scoreBody", "#scoreInfo", "#abcSave", "#abcReset", "#abcDirty",
                "#lyricsCard", "#lyrText", "#lyrDrop", "#lyrFill", "#lyrClear", "#lyrState",
                "#segLyrFmt", "#segLyrCont", "#exportPlan", ".editor", ".abc-area", ".score-body"):
        check(f"{sel} 已移除", page.locator(sel).count() == 0)

    # 上一代卷帘的控件也必须彻底不在（否则说明混进了旧文件）
    for sel in ("#prHost", "#prGrid", "#prSnap", "#prLyric", "#prTracks", "#prSave"):
        check(f"旧卷帘控件 {sel} 已移除", page.locator(sel).count() == 0)

    # ---- 3. ③ 的卷帘骨架（只开浏览器，没有任务数据）----
    check("③ 里有卷帘容器 #rollHost", page.locator("#rollHost").count() == 1)
    check("#rollHost 确实在 ③ 那张卡里",
          page.locator("section.card:has(#rollHost)").count() == 1)
    check("卷帘状态行 #rollState 存在且可见",
          page.locator("#rollState").count() == 1 and page.locator("#rollState").is_visible())
    state_text = page.eval_on_selector("#rollState", "e => e.textContent").strip()
    check("状态行有文字（没数据时给占位说明）", len(state_text) > 0, repr(state_text))
    check("没有任务时卷帘本体不挂载（#rollHost 为空）",
          page.evaluate("() => document.getElementById('rollHost').childElementCount") == 0)

    # ---- 4. 顶栏：三个导出勾选 + 保存/放弃 ----
    check("导出内容有三个勾选", page.locator(".filter-chip[data-voice]").count() == 3)
    chips = page.eval_on_selector_all(
        ".filter-chip[data-voice]", "els => els.map(e => [e.dataset.voice, e.textContent.trim()])")
    check("勾选的 token 与文案正确", chips == [[k, t] for k, t in VOICE_CHIPS], str(chips))
    check("默认只勾人声主旋律",
          page.evaluate("() => selectedVoices().join(',')") == "vocal",
          page.evaluate("() => selectedVoices().join(',')"))
    check("默认态在 DOM 上真的亮着（不是只改了状态）",
          page.locator(".filter-chip[data-voice='vocal'].on").count() == 1)

    # 点击能切换（切过去再切回来，别把默认态留在后面）
    page.eval_on_selector(".filter-chip[data-voice='ins']", "e => e.click()")
    page.wait_for_timeout(200)
    after_on = page.evaluate("() => selectedVoices().join(',')")
    check("点「器乐旋律」后勾选变成 vocal,ins", after_on == "vocal,ins", after_on)
    page.eval_on_selector(".filter-chip[data-voice='ins']", "e => e.click()")
    page.wait_for_timeout(200)
    check("再点一次切回只勾人声",
          page.evaluate("() => selectedVoices().join(',')") == "vocal")

    for sel, what in (("#rollDirty", "未保存提示"), ("#rollSave", "保存并重新生成按钮"),
                      ("#rollReload", "放弃改动按钮")):
        check(f"③ 顶栏有 {what} {sel}", page.locator(sel).count() == 1)
    check("没有任务时保存按钮是禁用的", page.locator("#rollSave").is_disabled())
    check("没有任务时放弃改动按钮是禁用的", page.locator("#rollReload").is_disabled())

    # ---- 5. 走带条（试听控件）----
    check("走带条 .transport 存在", page.locator("#scoreCard .transport").count() == 1)
    for sel, what in (("#tpPlay", "播放按钮"), ("#tpSeek", "进度条"), ("#tpTime", "时间显示"),
                      ("#tpSpeed", "速度"), ("#tpVol", "音量"), ("#toneHint", "音色提示")):
        check(f"走带条里有 {what} {sel}", page.locator(sel).count() == 1)
    check("没有任务时播放按钮是禁用的", page.locator("#tpPlay").is_disabled())

    # ---- 6. 卷帘脚本真的被浏览器取到了 ----
    check("GET /pianoroll.js 返回 200", roll_js.get("status") == 200, str(roll_js))
    check("pianoroll.js 有实际内容（> 50 KB）", (roll_js.get("len") or 0) > 50_000, str(roll_js))
    check("window.PianoRoll 已就绪",
          page.evaluate("() => !!(window.PianoRoll && window.PianoRoll.create)"))

    # ---- 7. 移进 ① 的控件与间距 ----
    for sel in ("#segDtype", "#selRange", "#btnStart", "#btnStop"):
        check(f"{sel} 在 ① 里", page.locator(f"section.card:has(#dropzone) {sel}").count() > 0)
    gap = page.evaluate(
        "() => { const d=document.querySelector('#dropzone')||document.querySelector('#fileWrap');"
        " const r=d.getBoundingClientRect();"
        " const o=document.querySelector('#segDtype').closest('.opt-row');"
        " return Math.round(o.getBoundingClientRect().top - r.bottom); }")
    check(f"拖拽框与「计算精度」的间距 ≥ 16px（实际 {gap}px）", gap >= 16, f"{gap}px")

    # ---- 8. 真正提交给后端的东西 ----
    page.evaluate("() => { S.token = 'smoke-token'; refreshButtons(); }")
    page.click("#btnStart")
    page.wait_for_timeout(600)
    body = captured.get("body") or {}
    check("提交体含 token", body.get("token") == "smoke-token", str(body))
    check("默认 export_voices=['vocal']", body.get("export_voices") == ["vocal"], str(body))
    check("preset 固定 default", body.get("preset") == "default", str(body))
    check("不再发旧的 only_melody 字段", "only_melody" not in body, str(body))
    check("不再发送窗口/高级参数",
          not any(k in body for k in ("overlap", "lookahead", "window")), str(body))

    # ---- 9. 空卷帘上按快捷键不应报错 ----
    before_errs = len(errors)
    page.keyboard.press("Control+g")
    page.keyboard.press("Control+a")
    page.keyboard.press("Control+l")
    page.wait_for_timeout(300)
    check("空卷帘上按 Ctrl+G / Ctrl+A / Ctrl+L 不报错",
          len(errors) == before_errs, str(errors[before_errs:]))

    # ---- 10. 整场下来没有任何 JS 异常 ----
    check("全程无 pageerror", not errors, str(errors))

    browser.close()

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 4 卡结构、③ 卷帘骨架、导出勾选、走带条与提交参数都符合预期")
