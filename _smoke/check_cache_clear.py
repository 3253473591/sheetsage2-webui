"""「清理缓存」按钮的真浏览器回归（2026-09-15 新增功能）。

点状态栏那个按钮 → 弹二次确认 → 确认后 output\\ 与 tmp\\ 被**永久删除**。
这个脚本用真浏览器、真点击、真 dialog，验的就是这条链路。

⚠️ **它会清空 <pkg> 的 output\\ 与 tmp\\** —— 只能对着**可丢弃的目录**跑：
   * 整合包目录（`dist\\full`，两个目录本来就是空的）；
   * 或者只拷了 app/ + web/ 的一次性沙盒目录。
   脚本里有防呆：**发现 `.venv` 或 `_smoke` 就直接拒绝运行**（那些是开发目录的特征），
   避免有人顺手对着开发机目录跑一遍，把 `tmp\\uploads` 和历次任务的产物全清了。

运行（需要带 playwright 的解释器 + 系统 Chrome）::

    .venv\\Scripts\\python.exe _smoke\\check_cache_clear.py <pkg> [port]

<pkg> 自己没有解释器时（例如只拷了 app/ + web/ 的沙盒目录），用环境变量指一个::

    $env:SS2_SMOKE_PYTHON = "<项目根>\\.venv\\Scripts\\python.exe"

断言：
  1. 按钮存在、在状态栏右侧；
  2. 点「取消」→ 一个文件都不能少；
  3. 点「确定」→ output\\ 与 tmp\\ 里的东西全没了、目录还在；
  4. 目录**外面**的哨兵文件原封不动（接口只认白名单，删不到别处）；
  5. 清理后 /api/cache 归零，且 toast 报出了释放的字节数。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

PKG = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8805
URL = f"http://127.0.0.1:{PORT}/"
FAILS: list[str] = []

OUT_SENTINEL = PKG / "output" / "_smoke_cache" / "fake_task"
TMP_LOOSE = PKG / "tmp" / "_smoke_cache_loose.bin"
KEEP = PKG / "app" / "_smoke_cache_keep.txt"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILS.append(name)


def cache_info() -> dict:
    with urllib.request.urlopen(f"{URL}api/cache", timeout=10) as resp:
        return json.loads(resp.read())


def seed() -> None:
    OUT_SENTINEL.mkdir(parents=True, exist_ok=True)
    (OUT_SENTINEL / "song.svp").write_bytes(b"x" * 1500)
    TMP_LOOSE.parent.mkdir(parents=True, exist_ok=True)
    TMP_LOOSE.write_bytes(b"y" * 2500)
    KEEP.parent.mkdir(parents=True, exist_ok=True)
    if not KEEP.exists():
        KEEP.write_bytes(b"keep")


# ---- 防呆：别对着开发目录跑 ------------------------------------------------
for marker in (".venv", "_smoke"):
    if (PKG / marker).exists():
        print(f"拒绝在开发目录上运行：{PKG} 里有 {marker}\\，本脚本会清空 output\\ 与 tmp\\。")
        print("请改对 dist\\full 之类可丢弃的目录运行。")
        raise SystemExit(2)
if not (PKG / "app" / "server.py").is_file():
    print(f"不像一个整合包目录：{PKG}")
    raise SystemExit(2)

py = next((p for p in (PKG / "runtime" / "python" / "python.exe",
                       PKG / ".venv" / "Scripts" / "python.exe") if p.is_file()), None)
if py is None and os.environ.get("SS2_SMOKE_PYTHON"):
    py = pathlib.Path(os.environ["SS2_SMOKE_PYTHON"])
if py is None or not pathlib.Path(py).is_file():
    print(f"包内找不到 Python 解释器，也没设 SS2_SMOKE_PYTHON：{PKG}")
    raise SystemExit(2)
print(f"包  : {PKG}")
print(f"端口: {PORT}\n")

seed()
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

    before = cache_info()
    before_total = before["total_bytes"]
    # ⚠️ 断言一律用**前后差值**，不要写死绝对字节数 —— 这个脚本必须能对
    #    "已经有历史产物"的目录跑（验收刚跑完的包就是这样，实测踩过一次：
    #    包里已有 94 个任务文件 + 78.9 MB 上传缓存，写死 4000 B 会误报 4 项 FAIL）。
    check("种下的假数据在统计里（含既有内容也不影响）",
          before_total >= 4000 and OUT_SENTINEL.is_dir() and TMP_LOOSE.is_file(),
          json.dumps(before["items"]))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        answer = {"accept": False}
        seen: list[str] = []

        def on_dialog(d):
            seen.append(d.message)
            d.accept() if answer["accept"] else d.dismiss()

        page.on("dialog", on_dialog)
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_function("() => typeof S !== 'undefined' && !!document.querySelector('#btnClean')")

        check("按钮在状态栏右侧", page.locator(".statusbar .right #btnClean").count() == 1)
        check("按钮文案", page.locator("#btnClean").inner_text().strip() == "清理缓存")

        # ---- 情形 1：点「取消」→ 一个文件都不能少 ----
        answer["accept"] = False
        page.click("#btnClean")
        page.wait_for_timeout(1500)
        check("弹了二次确认，且写明「永久删除」",
              len(seen) == 1 and "永久删除" in seen[0] and "不进回收站" in seen[0],
              str(seen))
        check("确认框里列出了两个目录与合计", bool(seen) and "合计" in seen[0] and "output" in seen[0] and "tmp" in seen[0], str(seen))
        check("点取消后 output\\ 里的文件还在", (OUT_SENTINEL / "song.svp").is_file())
        check("点取消后 tmp\\ 里的散文件还在", TMP_LOOSE.is_file())
        check("点取消后 /api/cache 一个字节没变",
              cache_info()["total_bytes"] == before_total, f"before={before_total}")

        # ---- 情形 2：点「确定」→ 清空 ----
        answer["accept"] = True
        seen.clear()
        page.click("#btnClean")
        page.wait_for_timeout(2500)
        check("确认后 output\\ 被清空", not OUT_SENTINEL.exists())
        check("确认后 tmp\\ 的散文件也没了", not TMP_LOOSE.exists())
        check("output\\ 目录本身还在", (PKG / "output").is_dir())
        check("tmp\\ 目录本身还在", (PKG / "tmp").is_dir())
        check("目录外面的哨兵文件没被动", KEEP.is_file())
        after = cache_info()
        check("清理后 /api/cache 归零", after["total_bytes"] == 0, json.dumps(after["items"]))
        toast = page.locator("#toast").inner_text()
        check(f"toast 报出释放量（{toast!r}）", "已清理" in toast, toast)
        # 清完之后再点一次：应当提示"没有可清理的缓存"，而不是报错
        page.click("#btnClean")
        page.wait_for_timeout(1200)
        check("空目录再点一次提示「没有可清理的缓存」",
              "没有可清理的缓存" in page.locator("#toast").inner_text(),
              page.locator("#toast").inner_text())

        browser.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    for p in (OUT_SENTINEL, TMP_LOOSE, KEEP):
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                import shutil
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass

print()
if FAILS:
    print(f"FAIL（{len(FAILS)} 项）")
    for f in FAILS:
        print("  -", f)
    raise SystemExit(1)
print("PASS —— 取消不动、确定清空、目录保留、外面没碰、报告数字对")
