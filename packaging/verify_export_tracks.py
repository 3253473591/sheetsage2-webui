"""回归测试：「导出即重新生成」是否真的能把单轨导出补成多轨。

背景（实测踩过的坑）
--------------------
磁盘上的 ``export/*.svp|mid`` 是**上一次生成时**的声部集合。以前点导出是
直接 ``window.location`` 下载，所以：

1. 提交任务时只要了主旋律（``only_melody=True``，现在是 ``export_voices=["vocal"]``）
   → 任务产物只有 1 轨（主人声）；
2. 用户跑完才改主意，把「导出内容」里另外两个勾上；
3. 此时直接点「导出 SVP」→ 下到的**还是那个单轨旧文件**。

现在前端每次点导出都**无条件先 POST ``/api/tasks/{id}/roll`` 重新生成再下载**
（③ 的「保存并重新生成导出」走的就是这个接口），不再依赖"检测到改动才重生成"，
所以那条漏网路径已经堵死。本脚本复现完整链路 —— **不点浏览器，直接打接口**。

接口契约（2026-09 卷帘版）::

    GET  /api/tasks/{id}/roll?export_voices=vocal,ins,chords
         → {tracks: [{voice, kind, display, editable, notes:[{start,end,pitch,lyric}]}], bpm, ...}
    POST /api/tasks/{id}/roll  {tracks: <上面拿到的>, export_voices: [...], bpm: ...}
         → {ok, exports: [{kind: "svp", tracks: [...]}, ...]}

⚠️ **老版本这里打的是 ``POST /api/tasks/{id}/abc``**（ABC 文本路径：把
   ``score.abc`` 原样回传 + ``only_melody=False``）。那条路径随 ABC 编辑器一起
   删掉了，服务端现在只有卷帘这一个入口 —— 断言保留，接口换掉。

用法::

    python packaging\\verify_export_tracks.py --pkg "dist\\1.0" --audio "<一首 flac/mp3>"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PASS, FAIL = "PASS", "FAIL"
DONE_STATES = {"done", "failed", "cancelled", "idle"}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def http(method: str, url: str, body: bytes | None = None,
         content_type: str | None = None, timeout: float = 300.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 300.0):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    status, raw = http(method, url, body, "application/json" if body else None, timeout)
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return status, {"_raw": raw.decode("utf-8", "replace")[:800]}


def svp_tracks(data: bytes) -> list[str]:
    return list(json.loads(data.decode("utf-8")).get("tracks") or [])


def svp_track_names(data: bytes) -> list[str]:
    return [t.get("name") for t in json.loads(data.decode("utf-8")).get("tracks") or []]


def main() -> int:
    ap = argparse.ArgumentParser(description="导出轨道数回归测试")
    ap.add_argument("--pkg", required=True, help="整合包目录（含 runtime\\python）")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--port", type=int, default=8796)
    ap.add_argument("--max-seconds", type=float, default=30.0)
    ap.add_argument("--timeout", type=float, default=1800.0)
    args = ap.parse_args()

    pkg = Path(args.pkg).resolve()
    audio = Path(args.audio).resolve()
    if not pkg.is_dir() or not audio.is_file():
        log("[×] --pkg 或 --audio 路径不对")
        return 2

    py = next((p for p in (pkg / "runtime" / "python" / "python.exe",
                           pkg / "runtime" / "python" / "Scripts" / "python.exe",
                           pkg / ".venv" / "Scripts" / "python.exe") if p.is_file()), None)
    if py is None:
        log("[×] 包里找不到 Python 解释器")
        return 2

    log(f"包      : {pkg}")
    log(f"音频    : {audio.name}")
    log(f"端口    : {args.port}")
    log(f"截取    : 前 {args.max_seconds:.0f} 秒（测试用，不跑全曲）\n")

    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    logfile = pkg / "verify_export_server.log"
    fh = logfile.open("wb")
    proc = subprocess.Popen([str(py), "-m", "app.server", "--no-browser", "--port", str(args.port)],
                            cwd=str(pkg), stdout=fh, stderr=subprocess.STDOUT, env=env)
    base = f"http://127.0.0.1:{args.port}"
    failures: list[str] = []
    try:
        # ---- 等服务起来 ----
        deadline = time.time() + 120
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"服务提前退出（code {proc.returncode}）")
            try:
                if http("GET", f"{base}/api/health", timeout=3)[0] == 200:
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(1.0)
        else:
            raise RuntimeError("服务 120s 内没起来")
        log("[1/5] 服务已就绪")

        # ---- 上传 ----
        boundary = "----regress" + uuid.uuid4().hex
        payload = audio.read_bytes()
        head = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode()
        status, raw = http("POST", f"{base}/api/upload",
                           head + payload + f"\r\n--{boundary}--\r\n".encode(),
                           f"multipart/form-data; boundary={boundary}")
        if status != 200:
            raise RuntimeError(f"上传失败 HTTP {status}: {raw[:300]}")
        token = json.loads(raw.decode())["token"]
        log(f"[2/5] 上传完成 token={token[:12]}…")

        # ---- 关键：**只勾人声主旋律**提交（复现用户那次操作）----
        status, body = http_json("POST", f"{base}/api/tasks", {
            "token": token, "preset": "default", "device": "auto",
            "only_melody": True, "max_seconds": args.max_seconds, "lyrics": "la",
        })
        if status != 200:
            raise RuntimeError(f"提交失败 HTTP {status}: {json.dumps(body, ensure_ascii=False)[:400]}")
        task_id = body["task_id"]
        log(f"[3/5] 已提交 task={task_id}，only_melody=True（预期只有 1 轨）")

        deadline = time.time() + args.timeout
        task: dict = {}
        while time.time() < deadline:
            _, state = http_json("GET", f"{base}/api/state?task_id={task_id}")
            task = state.get("task") or {}
            if task.get("state") in DONE_STATES:
                break
            time.sleep(2.0)
        if task.get("state") != "done":
            raise RuntimeError(f"任务没跑完：state={task.get('state')} error={task.get('error')}")
        out_dir = Path(task["out_dir"])
        log(f"[4/5] 扒谱完成：{out_dir.name}")

        # ---- 断言 1：只勾人声导出只有 1 轨 ----
        status, data = http("GET", f"{base}/api/tasks/{task_id}/export/svp")
        if status != 200:
            raise RuntimeError(f"下载 SVP 失败 HTTP {status}")
        before = svp_track_names(data)
        log(f"      导出 SVP 轨道 = {before}")
        if len(before) != 1:
            failures.append(f"只勾人声时本该 1 轨，实际 {len(before)} 轨：{before}")

        # ---- 关键动作：照前端「切勾选 → 读卷帘 → 保存并重新生成」的做法 ----
        # 读卷帘时必须**带上三个声部**，否则拿不到被过滤掉的 Ins ——
        # 这一步等价于老脚本"把原始 score.abc 读出来"。
        status, roll = http_json(
            "GET", f"{base}/api/tasks/{task_id}/roll?export_voices=vocal,ins,chords")
        if status != 200:
            raise RuntimeError(
                f"读取卷帘失败 HTTP {status}: {json.dumps(roll, ensure_ascii=False)[:400]}")
        roll_tracks = roll.get("tracks") or []
        voices = [t.get("voice") for t in roll_tracks]
        log(f"      卷帘里的声部 = {voices}")
        if not any(str(v).strip().lower() in ("ins", "instrumental", "accompaniment")
                   for v in voices):
            failures.append(f"卷帘里没有器乐声部，这个样本无法验证多轨：{voices}")

        status, result = http_json("POST", f"{base}/api/tasks/{task_id}/roll", {
            "tracks": roll_tracks, "bpm": roll.get("bpm"),
            "export_voices": ["vocal", "ins", "chords"],
        })
        if status != 200:
            raise RuntimeError(f"重新生成失败 HTTP {status}: {json.dumps(result, ensure_ascii=False)[:400]}")
        tracks = next((e.get("tracks") for e in result.get("exports") or []
                       if e.get("kind") == "svp"), None)
        log(f"[5/5] 切到三个勾选后重新生成 → {tracks}")

        # ---- 断言 2：重新生成后应该多轨 ----
        status, data = http("GET", f"{base}/api/tasks/{task_id}/export/svp")
        after = svp_track_names(data)
        log(f"      再次下载的 SVP 轨道 = {after}")
        if len(after) < 2:
            failures.append(f"切到三个勾选重新生成后仍只有 {len(after)} 轨：{after} —— "
                            f"「导出即重新生成」没生效")
        if "器乐旋律" not in after:
            failures.append(f"器乐旋律轨没出来：{after}（多半是卷帘里没读到 Ins 声部）")
        # ⚠️ 2026-09-15 第三轮起（交接文档 §24.4）复音轨默认按「同轨不重叠」拆成
        #    和弦1/和弦2…，所以这里必须按**前缀**判断；写死 `"和弦" in after`
        #    会在功能完全正常时误报 FAIL（实际踩到过：轨道名明明是
        #    主人声/器乐旋律/和弦1…和弦5）。判据是**轨名前缀**，不是轨数。
        if not any(n == "和弦" or n.startswith("和弦") for n in after):
            failures.append(f"和弦轨没出来：{after}")

    except Exception as exc:  # noqa: BLE001
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    log("\n" + "=" * 66)
    if failures:
        log(FAIL)
        for f in failures:
            log(f"  - {f}")
        log(f"\n服务日志：{logfile}")
        return 1
    log(PASS + " —— 单轨任务在不重跑推理的前提下，导出能补成多轨")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
