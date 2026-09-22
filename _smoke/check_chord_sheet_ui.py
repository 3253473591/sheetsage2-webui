"""UI 验证：③ 分析概览里的「和弦进行」面板（路径 A）。

前置：服务在 http://127.0.0.1:8777 跑着，且有一个已完成的任务
（其 summary.json 里带 chords）。纯前端功能，不需要重跑推理。

    .venv\\Scripts\\python.exe _smoke\\check_chord_sheet_ui.py
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8777/"
SHOT = str(pathlib.Path(__file__).resolve().parent.parent / "_shot" / "chord-sheet")
FAILS = []
pathlib.Path(SHOT).parent.mkdir(parents=True, exist_ok=True)


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAILS.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    ctx = browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
    page = ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof S !== 'undefined' && S.state==='done' && (S.result||{}).abc",
        timeout=30000,
    )
    page.wait_for_timeout(800)

    stat = page.inner_text("#chordStat")
    chords = page.evaluate("() => S.chords.length")
    groups = page.evaluate("() => document.querySelectorAll('#chordList .chord-sec').length")
    chips = page.evaluate("() => document.querySelectorAll('#chordList .chord-chip').length")
    print(f"  stat={stat!r}  S.chords={chords}  段落组={groups}  chips={chips}")

    check("和弦面板可见", page.is_visible("#chordBox"))
    check("任务里有和弦数据", chords > 0, str(chords))
    check("统计行含「去重」和「覆盖」", "去重" in stat and "覆盖" in stat, stat)
    check("按段落分组", groups >= 2, str(groups))
    check("每个和弦一个 chip", chips == chords, f"{chips} != {chords}")

    # 第一个 chip 的和弦名应当来自 S.chords
    first_ui = page.evaluate("() => document.querySelector('#chordList .chord-chip b').textContent")
    labels = page.evaluate("() => [...new Set(S.chords.map(c=>c.label))]")
    check("chip 显示的确实是模型和弦名", first_ui in labels, f"{first_ui!r} not in {labels[:6]}")

    # 段落外的和弦要能落到「其他」组（有的话）。注意段落是首尾相接的，
    # 所以"段落外"= 起点早于第一段起点（用区间包含判定会把边界和弦重复计数）。
    outside = page.evaluate(
        "() => { const ss=(S.result.sections||[]).slice().sort((a,b)=>a.start-b.start);"
        " if(!ss.length) return S.chords.length;"
        " return S.chords.filter(c=>c.start < ss[0].start-1e-9).length; }"
    )
    has_other = page.evaluate("() => [...document.querySelectorAll('#chordList .chord-sec-head b')].some(b=>b.textContent==='其他')")
    check("段落外的和弦归入「其他」", (outside == 0) or has_other, f"outside={outside} has_other={has_other}")

    # --- 下载 .txt ---
    with page.expect_download() as info:
        page.click("#chordDlTxt")
    dl = info.value
    txt_path = pathlib.Path(SHOT + "-probe.txt")
    dl.save_as(str(txt_path))
    txt = txt_path.read_text(encoding="utf-8")
    print(f"  下载 txt: {dl.suggested_filename} ({len(txt)} 字符)")
    check("txt 文件名以 .chords.txt 结尾", dl.suggested_filename.endswith(".chords.txt"),
          dl.suggested_filename)
    check("txt 有表头", txt.startswith("# 和弦进行"), txt[:40])
    check("txt 含调性/速度行", "# 调性：" in txt and "BPM" in txt)
    check("txt 列出全部和弦", sum(1 for line in txt.splitlines() if line.startswith("    ") and not line.strip().startswith("（")) == chords,
          str(sum(1 for line in txt.splitlines() if line.startswith("    "))))

    # --- 下载 .csv ---
    with page.expect_download() as info2:
        page.click("#chordDlCsv")
    dl2 = info2.value
    csv_path = pathlib.Path(SHOT + "-probe.csv")
    dl2.save_as(str(csv_path))
    raw = csv_path.read_bytes()
    csv = raw.decode("utf-8-sig")
    lines = [l for l in csv.splitlines() if l.strip()]
    print(f"  下载 csv: {dl2.suggested_filename} ({len(lines)} 行)")
    check("csv 文件名以 .chords.csv 结尾", dl2.suggested_filename.endswith(".chords.csv"),
          dl2.suggested_filename)
    check("csv 带 UTF-8 BOM（Excel 中文不乱码）", raw[:3] == b"\xef\xbb\xbf")
    check("csv 表头正确", lines[0] == "section,start_s,end_s,start_mmss,chord,duration_s", lines[0])
    check("csv 行数 = 和弦数 + 表头", len(lines) == chords + 1, f"{len(lines)} != {chords+1}")

    page.screenshot(path=SHOT + ".png", full_page=False)
    # 展开④之前先滚到概览，保证截图里能看到和弦面板
    page.evaluate("() => document.getElementById('chordBox').scrollIntoView({block:'center'})")
    page.wait_for_timeout(200)
    page.screenshot(path=SHOT + "-panel.png")

    txt_path.unlink(missing_ok=True)
    csv_path.unlink(missing_ok=True)
    browser.close()

print("\nFAILED:", FAILS if FAILS else "none")
sys.exit(1 if FAILS else 0)
