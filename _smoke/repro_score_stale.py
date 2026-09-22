"""复现：同一页面会话里连跑两首不同的歌，检查「乐谱编辑」是否跟着更新。"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import _paths  # 同目录；两首测试歌的路径见 _smoke/_paths.py

B = "http://127.0.0.1:8777/"
SONG_A = _paths.SONG_A
SONG_B = _paths.SONG_B


def run_one(pg, path, label, clear_first=False):
    print(f"\n===== {label}：{Path(path).name} =====")
    if clear_first:
        # 上一首还在，先移除，让上传区重新出现（等同用户拖入新文件）
        pg.click("#fDel")
        pg.wait_for_timeout(600)
        print("  已移除上一首")
    with pg.expect_file_chooser() as fc:
        pg.click("#dropzone")
    fc.value.set_files(path)
    pg.wait_for_selector("#fileWrap", state="visible", timeout=90000)
    pg.wait_for_timeout(2000)
    pg.select_option("#selRange", "30")
    pg.click("#btnStart")
    t0 = time.time()
    while time.time() - t0 < 240:
        if "100%" in pg.eval_on_selector("#progPct", "e=>e.textContent"):
            break
        time.sleep(2)
    pg.wait_for_timeout(3000)
    snap = pg.evaluate("""() => ({
      taskId: S.taskId,
      state: S.state,
      abcLen: ($("#abcArea")||{}).value ? document.getElementById('abcArea').value.length : -1,
      abcHead: (document.getElementById('abcArea').value||'').slice(0, 46).replace(/\\n/g,'|'),
      scoreInfo: document.getElementById('scoreInfo').textContent,
      svgPaths: document.querySelectorAll('#scoreBody svg path').length,
      key: document.getElementById('mKey').textContent,
      bpm: document.getElementById('mBpm').textContent,
      resultAbcLen: (S.result && S.result.abc) ? S.result.abc.length : 0,
      rawAbcLen: (S.rawAbc||'').length,
      playNotes: (S.playback&&S.playback.notes) ? S.playback.notes.length : 0,
      playDur: (S.playback&&S.playback.duration) ? Math.round(S.playback.duration*10)/10 : 0,
      playSig: S.notesSig,
      pianoSecs: pianoData ? Math.round(pianoData.buffer.duration*10)/10 : null,
    })""")
    print("  ", snap)
    return snap


errs = []
with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", args=["--autoplay-policy=no-user-gesture-required"])
    pg = b.new_page(viewport={"width": 1360, "height": 1000})
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)
    pg.goto(B, wait_until="domcontentloaded")
    pg.wait_for_timeout(2000)

    a = run_one(pg, SONG_A, "第一首")
    bb = run_one(pg, SONG_B, "第二首", clear_first=True)

    print("\n===== 对比 =====")
    same_abc = a["abcHead"] == bb["abcHead"]
    print("  task_id 变了:", a["taskId"] != bb["taskId"])
    print("  概览调性变了:", a["key"] != bb["key"], f"({a['key']} -> {bb['key']})")
    print("  乐谱 ABC 变了:", not same_abc, f"({a['abcHead'][:24]} -> {bb['abcHead'][:24]})")
    print("  result.abc 长度:", a["resultAbcLen"], "->", bb["resultAbcLen"])
    print("  S.rawAbc 长度:", a["rawAbcLen"], "->", bb["rawAbcLen"])
    print("  SVG path 数:", a["svgPaths"], "->", bb["svgPaths"])
    print("  试听音符数:", a["playNotes"], "->", bb["playNotes"])
    print("  试听时长:", a["playDur"], "->", bb["playDur"])
    print("  试听指纹:", a["playSig"], "->", bb["playSig"])
    print("  钢琴缓冲(秒):", a["pianoSecs"], "->", bb["pianoSecs"])
    stale = a["playSig"] != bb["playSig"] and a["playSig"] == ""
    print("  试听是否跟着换:", "是" if a["playSig"] != bb["playSig"] else "否 —— 仍是上一首")
    print("\n  结论:", "乐谱编辑【未更新】—— 复现成功" if same_abc else "乐谱编辑正常更新（未复现）")
    print("控制台/页面错误:", errs or "（无）")
    b.close()
