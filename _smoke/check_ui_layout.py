"""UI 冒烟测试（钢琴卷帘版）：页面结构 + 卷帘控件 + 网格档位 + 提交 payload。

覆盖 2026-09 这一轮改造：
  * 卡片从 5 张收敛到 **4 张**：① 拖入音频 / ② 进度 / ③ 钢琴卷帘 / ④ 导出
  * ③ 里的 ABC 文本框与五线谱**整块删除**，换成钢琴卷帘；④ 歌词卡也删除，
    歌词功能并入卷帘（逐音编辑）+ 一个「导入歌词」弹窗
  * 网格 9 档细分，默认 1/4 个四分音符（16 分音符）
  * Ctrl+G 吸附 / Ctrl+A 全选 / Ctrl+L 歌词填充

前置：服务已在跑。运行::

    D:\\AIGC\\YuE2\\yue2_aibbs\\env\\python.exe _smoke\\check_ui_layout.py [URL]
"""
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777/"
FAILS = []

# 9 档网格的标签片段（用户给的说法）
GRID_HINTS = ["四分音符", "8 分音符", "16 分音符", "16 分三连音", "32 分音符",
              "32 分三连音", "64 分音符", "64 分三连音", "128 分音符"]


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1100})

    captured = {}

    def route_tasks(route):
        try:
            captured["body"] = route.request.post_data_json
        except Exception:  # noqa: BLE001
            captured["body"] = None
        route.fulfill(status=200, json={"task_id": "smoke", "state": "queued", "out_dir": ""})

    page.route("**/api/tasks", route_tasks)
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_function("() => typeof S !== 'undefined' && !!document.querySelector('#btnStart')")

    # ---- 1. 卡片 ----
    cards = page.eval_on_selector_all(
        "section.card .card-head",
        "els => els.map(e => [e.querySelector('.num')?.textContent, e.querySelector('h2')?.textContent])")
    cards = [[n, t] for n, t in cards if t]
    expect = [["1", "拖入音频"], ["2", "进度"], ["3", "钢琴卷帘"], ["4", "导出"]]
    check("卡片数量与编号标题（4 张，③ 已改名钢琴卷帘）", cards == expect, f"实际 {cards}")

    # ---- 2. 被删掉的东西不该还在 ----
    for sel in ("#abcArea", "#scoreBody", "#scoreInfo", "#abcSave", "#abcReset", "#abcDirty",
                "#lyricsCard", "#lyrText", "#lyrDrop", "#lyrFill", "#lyrClear", "#lyrState",
                "#segLyrFmt", "#segLyrCont", "#exportPlan", ".editor", ".abc-area", ".score-body"):
        check(f"{sel} 已移除", page.locator(sel).count() == 0)

    # ---- 3. 卷帘已经挂载 ----
    check("③ 里有卷帘容器 #prHost", page.locator("#prHost").count() == 1)
    check("卷帘已挂载（#prHost 本身带 .pr 类）", page.locator("#prHost.pr").count() == 1)
    check("琴键列已渲染（≥12 个键）", page.locator(".pr-key").count() >= 12,
          str(page.locator(".pr-key").count()))
    check("空状态提示可见", page.locator(".pr-empty").count() == 1)
    check("播放头存在", page.locator(".pr-playhead").count() == 1)

    # ---- 4. 网格 9 档 + 默认 16 分音符 ----
    opts = page.eval_on_selector_all("#prGrid option", "els => els.map(e => [e.value, e.textContent])")
    check("网格下拉有 9 档", len(opts) == 9, str(opts))
    check("网格数值是 1/2/4/6/8/12/16/24/32",
          [o[0] for o in opts] == ["1", "2", "4", "6", "8", "12", "16", "24", "32"],
          str([o[0] for o in opts]))
    labels = " | ".join(o[1] for o in opts)
    for h in GRID_HINTS:
        check(f"网格档位含「{h}」", h in labels, labels)
    check("默认网格 = 1/4 个四分音符（16 分音符，value=4）",
          page.eval_on_selector("#prGrid", "e => e.value") == "4",
          page.eval_on_selector("#prGrid", "e => e.value"))
    check("默认档位文案就是 16 分音符",
          "16 分音符" in page.eval_on_selector('#prGrid option[value="4"]', "e => e.textContent"))

    # ---- 5. 卷帘上的歌词与吸附控件 ----
    for sel, what in (("#prSnap", "Ctrl+G 吸附按钮"), ("#prLyric", "歌词输入框"),
                      ("#prSustain", "延音符 - 按钮"), ("#prSyllable", "多音节 + 按钮"),
                      ("#prNext", "下一音按钮"), ("#prSplit", "歌词分词下拉"),
                      ("#prTracks", "曲目切换区")):
        check(f"{what} {sel} 存在", page.locator(sel).count() == 1)
    check("曲目区有「显示其他轨」开关", page.locator("#prTracks .pr-chk input").count() == 1)
    check("分词下拉有 自动/按空格/逐字符 三档",
          page.eval_on_selector_all("#prSplit option", "els => els.map(e => e.value)")
          == ["auto", "space", "char"],
          str(page.eval_on_selector_all("#prSplit option", "els => els.map(e => e.value)")))
    check("分词默认是 auto", page.eval_on_selector("#prSplit", "e => e.value") == "auto")
    check("延音按钮的 title 说明是「同一音节延续」",
          "同一音节延续" in (page.get_attribute("#prSustain", "title") or ""),
          page.get_attribute("#prSustain", "title") or "")
    check("多音节按钮的 title 说明提到「下一个音节」",
          "下一个音节" in (page.get_attribute("#prSyllable", "title") or ""),
          page.get_attribute("#prSyllable", "title") or "")
    check("三个导出内容勾选还在", page.locator(".filter-chip[data-voice]").count() == 3)
    check("默认只勾人声主旋律",
          page.evaluate("() => selectedVoices().join(',')") == "vocal")

    # ---- 6. 快捷键在提示里露出（可发现性）----
    legend = page.eval_on_selector(".pr-legend", "e => e.textContent")
    for key in ("Ctrl+G", "Ctrl+A", "Ctrl+L", "Ctrl+Z", "曲目", "Ctrl+滚轮"):
        check(f"操作提示里写了 {key}", key in legend, legend)

    # ---- 7. 保存行与弹窗 ----
    for sel in ("#prSave", "#prRevert", "#lyrImport", "#lrcDownload", "#prDirty", "#prStats"):
        check(f"③ 保存行里有 {sel}", page.locator(sel).count() == 1)
    check("通用弹窗存在且默认隐藏",
          page.locator("#modalMask").count() == 1 and page.locator("#modalMask").is_hidden())
    for sel in ("#modalTitle", "#modalBody", "#modalActs"):
        check(f"弹窗部件 {sel} 存在", page.locator(sel).count() == 1)

    # ---- 8. 移进 ① 的控件与间距 ----
    for sel in ("#segDtype", "#selRange", "#btnStart", "#btnStop"):
        check(f"{sel} 在 ① 里", page.locator(f"section.card:has(#dropzone) {sel}").count() > 0)
    gap = page.evaluate(
        "() => { const d=document.querySelector('#dropzone')||document.querySelector('#fileWrap');"
        " const r=d.getBoundingClientRect();"
        " const o=document.querySelector('#segDtype').closest('.opt-row');"
        " return Math.round(o.getBoundingClientRect().top - r.bottom); }")
    check(f"拖拽框与「计算精度」的间距 ≥ 16px（实际 {gap}px）", gap >= 16, f"{gap}px")

    # ---- 9. 真正提交给后端的东西 ----
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

    # ---- 10. Ctrl+G / Ctrl+A 在空卷帘上不应报错 ----
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.eval_on_selector("#scoreCard", "e => e.classList.remove('collapsed')")
    page.eval_on_selector(".pr", "e => e.focus()")
    page.keyboard.press("Control+g")
    page.keyboard.press("Control+a")
    page.wait_for_timeout(300)
    check("空卷帘上按 Ctrl+G / Ctrl+A 不报错", not errors, str(errors))

    browser.close()

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 4 卡结构、钢琴卷帘、9 档网格、快捷键与提交参数都符合预期")
