"""专项验证：LRC 断行保留 + 播放高亮（把播放头 seek 到有音符的位置再测）。"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import _paths  # 同目录；测试音频与本机脚本路径见 _smoke/_paths.py

B = "http://127.0.0.1:8777/"
shot = _paths.ROOT / "_shot"
LRC = """[00:14.64]这里是第一行测试歌词
[00:18.00]这里是第二行测试歌词
[00:21.52]这里是第三行测试歌词
[00:26.00]这里是第四行测试歌词
"""
errs = []
with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", args=["--autoplay-policy=no-user-gesture-required"])
    pg = b.new_page(viewport={"width": 1360, "height": 1000}, device_scale_factor=1.4)
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)
    pg.goto(B, wait_until="domcontentloaded")
    pg.wait_for_timeout(2500)

    # 服务端应已有上一次的任务；没有就跳过
    st = pg.evaluate("""async () => (await (await fetch('/api/state')).json()).task?.task_id || null""")
    print("当前任务:", st)
    # 没有任务就自己跑一次扒谱（前 30s）
    if not st:
        print("没有任务，先跑一次扒谱…")
        with pg.expect_file_chooser() as fc:
            pg.click("#dropzone")
        fc.value.set_files(_paths.AUDIO)
        pg.wait_for_selector("#fileWrap", state="visible", timeout=60000)
        pg.wait_for_timeout(2000)
        pg.select_option("#selRange", "30")
        pg.click("#btnStart")
        t0 = time.time()
        while time.time() - t0 < 220:
            if "100%" in pg.eval_on_selector("#progPct", "e=>e.textContent"):
                break
            time.sleep(2)
        pg.wait_for_timeout(2500)
        print("  扒谱完成:", pg.text_content("#stateText"))
    else:
        pg.evaluate("""async () => { const s = await (await fetch('/api/state')).json(); applyTask(s.task, true); }""")
        pg.wait_for_timeout(2500)

    checks = []
    # 1) 填入 LRC 并检查导出的 LRC 是否保留 4 行
    pg.click("#lyricsCard .fold")
    pg.wait_for_timeout(400)
    pg.fill("#lyrText", LRC)
    pg.click("#lyrFill")
    pg.wait_for_timeout(6000)
    lrcbox = pg.text_content("#lrcBox") or ""
    lines = [x for x in lrcbox.splitlines() if x.strip() and not x.startswith("[")]
    print("\n导出的 LRC 歌词行数:", len(lines))
    for x in lrcbox.splitlines():
        print("   ", x)
    checks.append((f"LRC 保留原文断行（{len(lines)} 行，期望 4）", len(lines) == 4))

    # 2) 高亮：把播放头 seek 到有音符的位置（旋律从 ~14.7s 起）再测
    hl = pg.evaluate("""() => ({els:(S.noteEls||[]).length, want:(S.highlightNotes||[]).length,
                               ok:S.hlOk, firstStart:(S.highlightNotes||[])[0]?.start})""")
    print("\n高亮映射:", hl)
    checks.append(("高亮映射建立", hl["ok"] is True))

    pg.evaluate("""(t) => { melodyOffset = t; }""", max(0.0, (hl.get("firstStart") or 0) + 0.2))
    pg.evaluate("() => { document.getElementById('tpPlay').disabled=false; }")
    pg.click("#tpPlay")
    pg.wait_for_timeout(1800)
    r = pg.evaluate("""() => ({playing:document.querySelectorAll('#scoreBody .abcjs-note.playing').length,
                                idx:[...document.querySelectorAll('#scoreBody .abcjs-note')].findIndex(e=>e.classList.contains('playing')),
                                t:document.getElementById('tpTime').textContent})""")
    print("播放中:", r)
    checks.append(("播放时有音符高亮", r["playing"] >= 1))
    pg.screenshot(path=str(shot / "ui-highlight.png"), full_page=False)
    pg.evaluate("()=>stopMelody()")
    pg.wait_for_timeout(300)
    checks.append(("停止后高亮清除", pg.evaluate("()=>document.querySelectorAll('#scoreBody .abcjs-note.playing').length") == 0))

    print()
    for n, v in checks:
        print(f"  {'✓' if v else '✗'}  {n}")
    print("\n失败:", [n for n, v in checks if not v] or "无")
    print("控制台/页面错误:", errs or "（无）")
    b.close()
