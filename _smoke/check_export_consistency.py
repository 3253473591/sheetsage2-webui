"""端到端一致性测试：③ 勾了什么，⑤ 下载到的 SVP 就**必须**是什么。

这是针对实测问题的回归测试：
  * 界面勾了三条，下载到的却是单轨；
  * 取消勾选后再导出，下到的还是上一次的文件（浏览器缓存 / 磁盘旧货）。

现在的模型（2026-09-15 起）：③ 有三个勾选 —— 人声主旋律 / 器乐旋律 / 和弦，
默认只勾人声。**勾什么导什么，由用户决定**，⑤ 不再有「导出提示」文字。
所以断言方式从「读说明文字」改成「直接数下载到的 SVP 轨道名」，
并且每次导出前都会重新生成（`regenerateExports()`），这正是防旧货的关键路径。

运行：
  .venv\\Scripts\\python.exe _smoke\\check_export_consistency.py [pkg] [audio] [port]
"""
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
PKG = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else HERE.parent).resolve()
AUDIO = pathlib.Path(sys.argv[2] if len(sys.argv) > 2
                    else PKG / "tmp" / "duet_in" / "duet.flac").resolve()
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8802
URL = f"http://127.0.0.1:{PORT}/"
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


def svp_tracks(data: bytes) -> list:
    txt = data.decode("utf-8", "replace").rstrip("\x00 \r\n\t")
    return [t.get("name") for t in (json.loads(txt).get("tracks") or [])]


def svp_signature(data: bytes):
    """SVP 的「音乐内容」指纹：轨名 + 每轨的音符 (onset, duration, pitch)。

    不能直接比对字节 —— `app/svpw.py` 每次生成都会 `uuid.uuid4()` 造新的组 ID，
    字节必然不同。要验的是「同样勾选 ⇒ 同样的音」。
    """
    txt = data.decode("utf-8", "replace").rstrip("\x00 \r\n\t")
    sig = []
    for t in (json.loads(txt).get("tracks") or []):
        notes = []
        for g in (t.get("mainGroup", {}).get("notes") or []):
            notes.append((g.get("onset"), g.get("duration"), g.get("pitch")))
        sig.append((t.get("name"), tuple(sorted(notes, key=lambda x: (x[0] or 0, x[2] or 0)))))
    return tuple(sig)


if not AUDIO.is_file():
    print(f"找不到测试音频：{AUDIO}")
    sys.exit(2)

py = next(p for p in (PKG / "runtime" / "python" / "python.exe",
                      PKG / ".venv" / "Scripts" / "python.exe") if p.is_file())
proc = subprocess.Popen([str(py), "-m", "app.server", "--no-browser", "--port", str(PORT)],
                        cwd=str(PKG), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(120):
        try:
            urllib.request.urlopen(f"{URL}api/health", timeout=3)
            break
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    print(f"服务就绪 {URL}")

    boundary = "----consist" + uuid.uuid4().hex
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{AUDIO.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + AUDIO.read_bytes() + \
           f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{URL}api/upload", data=body, method="POST",
                                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    token = json.loads(urllib.request.urlopen(req, timeout=300).read())["token"]

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        # 切换勾选可能弹「会丢弃手动修改」的确认框；测试里一律接受
        page.on("dialog", lambda d: d.accept())
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_function("() => typeof S !== 'undefined' && !!document.querySelector('#btnStart')")

        # ---- 跑一个短任务（默认只勾人声）----
        page.evaluate(f"() => {{ S.token='{token}'; refreshButtons(); }}")
        page.select_option("#selRange", "30")
        page.click("#btnStart")
        deadline = time.time() + 420
        while time.time() < deadline:
            if page.evaluate("() => S.state") in ("done", "failed", "cancelled"):
                break
            time.sleep(2)
        state = page.evaluate("() => S.state")
        check("任务完成", state == "done", state)
        page.wait_for_timeout(2500)

        def set_voices(*kinds):
            """用真实点击把勾选调成 kinds，并等 UI 与状态一致。"""
            want = set(kinds)
            for _ in range(9):
                cur = set(page.evaluate("() => selectedVoices()"))
                if cur == want:
                    break
                for k in ("vocal", "ins", "chords"):
                    in_cur, in_want = k in cur, k in want
                    if in_cur != in_want:
                        page.eval_on_selector(f'.filter-chip[data-voice="{k}"]', "e => e.click()")
                        page.wait_for_timeout(450)
                        break
            got = set(page.evaluate("() => selectedVoices()"))
            check(f"勾选调成 {sorted(kinds)}", got == want, f"实际 {sorted(got)}")

        def download_tracks():
            with page.expect_download(timeout=180000) as dl:
                page.eval_on_selector("#dlSvp", "e => e.click()")
            path = pathlib.Path(dl.value.path())
            raw = path.read_bytes()
            return svp_tracks(raw), path.name, raw

        # ---- 情形 1：只勾人声（默认）----
        set_voices("vocal")
        t1, f1, raw1 = download_tracks()
        check(f"只勾人声 → 1 轨 {t1}", t1 == ["主人声"], f"{t1} ({f1})")

        # ---- 情形 2：加器乐旋律 ----
        set_voices("vocal", "ins")
        t2, f2, raw2 = download_tracks()
        check(f"人声+器乐 → 2 轨 {t2}", t2 == ["主人声", "器乐旋律"], f"{t2} ({f2})")
        check("两次下载内容确实不同（没有发旧货）", raw1 != raw2, f"len {len(raw1)} vs {len(raw2)}")

        # ---- 情形 3：三条全勾，和弦应被拆成 和弦1/和弦2… ----
        set_voices("vocal", "ins", "chords")
        t3, f3, raw3 = download_tracks()
        chords = [n for n in t3 if re.fullmatch(r"和弦\d+", str(n) or "")]
        check(f"全勾 → 含人声与器乐 {t3}", "主人声" in t3 and "器乐旋律" in t3, f"{t3} ({f3})")
        check(f"全勾 → 和弦被拆成 和弦1/和弦2…（{chords}）", len(chords) >= 1, f"{t3} ({f3})")
        check(f"全勾 → 至少 3 轨（实际 {len(t3)}）", len(t3) >= 3, str(t3))

        # ---- 情形 4：再关回只勾人声 —— 用户实测踩过的「下到旧文件」场景 ----
        set_voices("vocal")
        t4, f4, raw4 = download_tracks()
        check(f"关回只勾人声 → 回到 1 轨 {t4}", t4 == ["主人声"], f"{t4} ({f4})")
        check("关回后音符与情形 1 逐音一致（真的按当前勾选重新生成了）",
              svp_signature(raw4) == svp_signature(raw1),
              f"轨名同但音符不同：{t4}")

        # ---- 情形 5：只勾和弦（ABC 里没有音符，也不能崩）----
        set_voices("chords")
        t5, f5, _ = download_tracks()
        chords5 = [n for n in t5 if re.fullmatch(r"和弦\d+", str(n) or "")]
        check(f"只勾和弦 → 只有和弦轨 {t5}", len(chords5) == len(t5) and len(t5) >= 1, f"{t5} ({f5})")

        browser.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("PASS —— 勾什么就导什么，且每次都是重新生成的")
