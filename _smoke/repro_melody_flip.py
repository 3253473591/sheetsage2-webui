"""复现：任务以 only_melody=False 提交、产物本是三轨，之后开关会不会自己翻成 true。

做法：真浏览器走一遍「上传 → 开始扒谱 → 等完成」，全程记录
  * S.filter.dropIns 的变化
  * #abcDirty 的变化
  * 任何发往 /api/tasks/{id}/abc 的 POST（那会把导出降级成单轨）
不点导出、不点保存，所以任何 abc 调用都只能是页面自己干的。

用法：
  python _smoke/repro_melody_flip.py <pkg_or_project> <audio> [port]
"""
import json
import pathlib
import subprocess
import sys
import time
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

PKG = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
AUDIO = pathlib.Path(sys.argv[2]).resolve()
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8801
URL = f"http://127.0.0.1:{PORT}/"
MAX_SECONDS = 30

py = next(p for p in (PKG / "runtime" / "python" / "python.exe",
                      PKG / ".venv" / "Scripts" / "python.exe") if p.is_file())
proc = subprocess.Popen([str(py), "-m", "app.server", "--no-browser", "--port", str(PORT)],
                        cwd=str(PKG), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# 等就绪
for _ in range(120):
    try:
        urllib.request.urlopen(f"{URL}api/health", timeout=3)
        break
    except Exception:  # noqa: BLE001
        time.sleep(1.0)
print(f"服务就绪 {URL}")

# 先上传音频，拿 token
boundary = "----repro" + uuid.uuid4().hex
data = AUDIO.read_bytes()
body = (f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{AUDIO.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + \
       f"\r\n--{boundary}--\r\n".encode()
req = urllib.request.Request(f"{URL}api/upload", data=body, method="POST",
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
token = json.loads(urllib.request.urlopen(req, timeout=300).read())["token"]
print(f"已上传 token={token[:12]}…\n")

abc_calls = []

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1100})

    def on_request(r):
        if r.method == "POST" and "/abc" in r.url:
            try:
                abc_calls.append(r.post_data_json)
            except Exception:  # noqa: BLE001
                abc_calls.append({"__unparsable": True})
    page.on("request", on_request)

    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_function("() => typeof S !== 'undefined' && !!document.querySelector('#btnStart')")

    def snap(tag):
        v = page.evaluate("() => ({dropIn: S.filter.dropIns, state: S.state, "
                          "dirty: document.querySelector('#abcDirty')?.classList.contains('on'), "
                          "taskId: S.taskId, resMelody: S.result ? S.result.only_melody : '(no result)'})")
        print(f"  [{tag}] dropIn={v['dropIn']}  dirty={v['dirty']}  state={v['state']}  "
              f"result.only_melody={v['resMelody']}")
        return v

    print("=== 初始 ===")
    snap("初始")

    page.evaluate(f"() => {{ S.token='{token}'; refreshButtons(); }}")
    page.select_option("#selRange", "30")
    page.evaluate("() => { S.filter.dropIns = false; syncFilterChips(); applyFiltersToEditor(true); }")
    print("=== 提交前（强制关掉开关）===")
    snap("提交前")

    page.click("#btnStart")
    page.wait_for_timeout(1500)
    print("=== 已提交 ===")
    snap("提交后")

    deadline = time.time() + 300
    while time.time() < deadline:
        st = page.evaluate("() => S.state")
        if st in ("done", "failed", "cancelled"):
            break
        time.sleep(2)
    print(f"=== 任务结束（{st}）===")
    page.wait_for_timeout(2500)      # 留出时间让 onResult / SSE 跑完
    snap("结束后")

    print(f"\n期间发往 /api/tasks/*/abc 的请求数: {len(abc_calls)}")
    for c in abc_calls:
        print(f"   only_melody={c.get('only_melody')}  abc 长度={len(c.get('abc') or '')}")

    task_id = page.evaluate("() => S.taskId")
    browser.close()

# 直接问服务器：这个任务的 result 变成什么了
st = json.loads(urllib.request.urlopen(f"{URL}api/state?task_id={task_id}", timeout=20).read())
t = st.get("task") or {}
r = t.get("result") or {}
print("\n=== 服务器侧最终状态 ===")
print("  params.only_melody =", (t.get("params") or {}).get("only_melody"))
print("  result.only_melody =", r.get("only_melody"))
print("  result.abc_edited  =", r.get("abc_edited"))
print("  export.exports     =", [(e.get("kind"), e.get("tracks")) for e in (r.get("export") or {}).get("exports") or []])
summary = json.loads((pathlib.Path(t["out_dir"]) / "summary.json").read_text(encoding="utf-8"))
print("  summary.only_melody=", summary.get("only_melody"))
print("  summary.export     =", [(e.get("kind"), e.get("tracks")) for e in (summary.get("export") or {}).get("exports") or []])

proc.terminate()
try:
    proc.wait(timeout=15)
except subprocess.TimeoutExpired:
    proc.kill()
print("\n服务已关闭")
