"""验证 歌词填充 + 播放高亮 + 音量，走真实流程。"""
import time

from playwright.sync_api import sync_playwright

import _paths  # 同目录；测试音频路径见 _smoke/_paths.py

B = "http://127.0.0.1:8777/"
AUDIO = _paths.AUDIO
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
    pg.wait_for_timeout(2000)

    checks = []
    checks.append(("⑤ 标题为「歌词填充」", "歌词填充" in (pg.text_content("#lyricsCard h2") or "")))
    checks.append(("有拖拽区", pg.eval_on_selector_all("#lyrDrop", "e=>e.length") == 1))
    checks.append(("有文本框", pg.eval_on_selector_all("#lyrText", "e=>e.length") == 1))
    checks.append(("无 ASR 开关", pg.eval_on_selector_all("#lyrSwitch", "e=>e.length") == 0))
    checks.append(("延音符可选 -/+", pg.eval_on_selector_all("#segLyrCont button", "e=>e.length") == 3))

    print("--- 跑一次扒谱（前 30s）---")
    with pg.expect_file_chooser() as fc:
        pg.click("#dropzone")
    fc.value.set_files(AUDIO)
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

    print("--- 歌词填充（粘贴 LRC）---")
    pg.click("#lyricsCard .fold")
    pg.wait_for_timeout(400)
    pg.fill("#lyrText", LRC)
    pg.click("#lyrFill")
    pg.wait_for_timeout(6000)
    stats = pg.text_content("#lyrStats") or ""
    lrcbox = pg.text_content("#lrcBox") or ""
    print("  填充统计:", stats)
    print("  LRC 前 3 行:", " / ".join(lrcbox.splitlines()[:3]))
    checks.append(("填充成功（有统计）", "命中" in stats))
    checks.append(("生成了 LRC", len(lrcbox) > 20))

    print("--- 导出 SVP 是否带歌词 ---")
    svp = pg.evaluate("""async () => {
      const r = await fetch(`/api/tasks/${S.taskId}/export/svp`);
      const j = await r.json();
      const n = j.tracks[0].mainGroup.notes.map(x=>x.lyrics);
      return {version:j.version, notes:n.length, first:n.slice(0,16)};
    }""")
    print("  ", svp)
    checks.append(("SVP 有真实歌词", any(x not in ("la", "-", "+") for x in svp["first"])))

    print("--- 高亮映射 ---")
    hl = pg.evaluate("""() => ({
      noteEls: (S.noteEls||[]).length,
      hlNotes: (S.highlightNotes||[]).length,
      ok: S.hlOk
    })""")
    print("  ", hl)
    checks.append(("高亮映射建立成功", hl["ok"] is True))

    print("--- 播放并检查高亮在动 ---")
    pg.evaluate("""() => { document.getElementById('tpPlay').disabled=false; }""")
    pg.click("#tpPlay")
    pg.wait_for_timeout(2500)
    hl2 = pg.evaluate("""() => ({
      playing: document.querySelectorAll('#scoreBody .abcjs-note.playing').length,
      hlLast: hlLast,
      idx: [...document.querySelectorAll('#scoreBody .abcjs-note')].findIndex(e=>e.classList.contains('playing')),
      t: document.getElementById('tpTime').textContent
    })""")
    print("  ", hl2)
    checks.append(("播放时有音符高亮", hl2["playing"] >= 1))
    pg.evaluate("()=>stopMelody()")
    pg.wait_for_timeout(300)
    after = pg.evaluate("()=>document.querySelectorAll('#scoreBody .abcjs-note.playing').length")
    checks.append(("停止后高亮清除", after == 0))

    print("--- 音量 +8dB ---")
    boost = pg.evaluate("()=>({boost: Math.round(PIANO_BOOST*1000)/1000, limiter: !!tpLimiter})")
    print("  ", boost)
    checks.append((f"+8dB 提升生效 ({boost['boost']}x)", abs(boost["boost"] - 2.512) < 0.01))

    pg.screenshot(path=str(_paths.ROOT / "_shot" / "ui-lyricsfill.png"), full_page=True)
    print()
    for n, v in checks:
        print(f"  {'✓' if v else '✗'}  {n}")
    print("\n失败:", [n for n, v in checks if not v] or "无")
    print("控制台/页面错误:", errs or "（无）")
    b.close()
