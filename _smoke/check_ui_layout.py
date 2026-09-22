"""UI 冒烟测试：页面结构 + 三个勾选 + 提交给后端的东西。

覆盖 2026-09-15 那一轮重构：
  * 卡片收敛成 5 张（1 拖入音频 / 2 进度 / 3 乐谱编辑 / 4 歌词填充 / 5 导出）
  * 「计算精度」「只分析前」「开始扒谱」「停止」搬进 ①
  * ③ 的「ABC 过滤」两个 chip → 三个勾选：人声主旋律 / 器乐旋律 / 和弦，**默认只勾人声**
  * ⑤ 卡片的「导出提示」已删除（不再有 #exportPlan）
  * 提交/重新生成导出发的是 `export_voices`

前置：服务已在跑。运行::

    .venv\\Scripts\\python.exe _smoke\\check_ui_layout.py [URL]
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777/"
FAILS = []


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
    expect = [["1", "拖入音频"], ["2", "进度"], ["3", "乐谱编辑"], ["4", "歌词填充"], ["5", "导出"]]
    check("卡片数量与编号标题", cards == expect, f"实际 {cards}")

    # ---- 2. 被删掉的东西不该还在 ----
    for sel in ("#ckMelody", "#segPreset", "#advToggle", "#advBody", "#advOverlap", "#advLookahead",
                "#exportPlan", ".export-plan"):
        check(f"{sel} 已移除", page.locator(sel).count() == 0)

    # ---- 3. 搬进 ① 的控件 ----
    for sel in ("#segDtype", "#selRange", "#customRange", "#btnStart", "#btnStop"):
        check(f"{sel} 在 ① 里", page.locator(f"section.card:has(#dropzone) {sel}").count() > 0)

    # ---- 4. 三个勾选：文案、数量、默认状态 ----
    chips = page.eval_on_selector_all(
        ".filter-chip[data-voice]",
        "els => els.map(e => [e.dataset.voice, e.textContent.trim(), e.classList.contains('on')])")
    check("三个勾选", [c[0] for c in chips] == ["vocal", "ins", "chords"], str(chips))
    check("文案：人声主旋律 / 器乐旋律 / 和弦",
          [c[1] for c in chips] == ["人声主旋律", "器乐旋律", "和弦"], str(chips))
    check("默认只勾人声主旋律",
          [c[2] for c in chips] == [True, False, False], str(chips))
    check("S.filter.voices 与 UI 一致",
          page.evaluate("() => JSON.stringify(S.filter.voices)") == '{"vocal":true,"ins":false,"chords":false}',
          page.evaluate("() => JSON.stringify(S.filter.voices)"))
    check("selectedVoices() 默认 ['vocal']",
          page.evaluate("() => selectedVoices().join(',')") == "vocal")

    # ---- 5. 点一下能切换，且只有一处状态 ----
    page.eval_on_selector('.filter-chip[data-voice="ins"]', "e => e.click()")
    check("勾上器乐旋律后 chip 高亮",
          page.eval_on_selector('.filter-chip[data-voice="ins"]', "e => e.classList.contains('on')"))
    check("selectedVoices() 变 vocal,ins",
          page.evaluate("() => selectedVoices().join(',')") == "vocal,ins")
    page.eval_on_selector('.filter-chip[data-voice="chords"]', "e => e.click()")
    check("再勾和弦 → vocal,ins,chords",
          page.evaluate("() => selectedVoices().join(',')") == "vocal,ins,chords")
    page.eval_on_selector('.filter-chip[data-voice="chords"]', "e => e.click()")
    page.eval_on_selector('.filter-chip[data-voice="ins"]', "e => e.click()")
    check("再点回去 → 只剩 vocal",
          page.evaluate("() => selectedVoices().join(',')") == "vocal")

    # ---- 6. 真正提交给后端的东西 ----
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

    # 勾上三个再提交：export_voices 应该是三条
    page.eval_on_selector('.filter-chip[data-voice="ins"]', "e => e.click()")
    page.eval_on_selector('.filter-chip[data-voice="chords"]', "e => e.click()")
    captured.clear()
    page.evaluate("() => { S.state='idle'; refreshButtons(); }")
    page.click("#btnStart")
    page.wait_for_timeout(600)
    body2 = captured.get("body") or {}
    check("勾满三条后 export_voices=['vocal','ins','chords']",
          body2.get("export_voices") == ["vocal", "ins", "chords"], str(body2))

    # ---- 7. 间距（用户反馈拖拽框与「计算精度」贴太近）----
    gap = page.evaluate(
        "() => { const d=document.querySelector('#dropzone')||document.querySelector('#fileWrap');"
        " const r=d.getBoundingClientRect();"
        " const o=document.querySelector('#segDtype').closest('.opt-row');"
        " return Math.round(o.getBoundingClientRect().top - r.bottom); }")
    check(f"拖拽框与「计算精度」的间距 ≥ 16px（实际 {gap}px）", gap >= 16, f"{gap}px")

    # ---- 8. 「清理缓存」按钮（2026-09-15 新增）：在状态栏、不在卡片里 ----
    check("#btnClean 文案为「清理缓存」",
          page.locator("#btnClean").count() == 1 and
          page.locator("#btnClean").inner_text().strip() == "清理缓存",
          str(page.locator("#btnClean").count()))
    check("#btnClean 在状态栏右侧（与「日志」同排）",
          page.locator(".statusbar .right #btnClean").count() == 1 and
          page.locator(".statusbar .right #btnLog").count() == 1,
          "不在状态栏右侧")
    check("#btnClean 不在任何 section.card 里（卡片仍是 5 张）",
          page.locator("section.card #btnClean").count() == 0,
          "它混进卡片了")
    # 注意：上面第 6 步点过「开始扒谱」，此刻 S.state 是 queued（busy），
    # 所以两种状态都要显式设一次再断言，别依赖残留状态。
    check("#btnClean 跑任务时禁用",
          page.evaluate("() => { S.state='queued'; refreshButtons();"
                        " return document.querySelector('#btnClean').disabled; }"))
    check("#btnClean 空闲时可用",
          page.evaluate("() => { S.state='idle'; refreshButtons();"
                        " return !document.querySelector('#btnClean').disabled; }"))

    browser.close()

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 新结构、三个勾选、提交参数与间距都符合预期")
