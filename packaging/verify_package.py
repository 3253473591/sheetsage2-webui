"""整合包端到端验收：把包当成「测试者的机器」跑一遍完整扒谱。

用**纯标准库**写成，所以系统里的任何 Python 3 都能跑它（不需要装的依赖）：

    python packaging\\verify_package.py ^
        --pkg "dist\\SheetSage2-Transcribe-1.0.0" ^
        --audio "tmp\\uploads\\2a0db6d74f1e4721b046ca29421d7cce.flac" ^
        --device cpu --max-seconds 20

它会：
1. 用包内的 ``runtime\\python\\python.exe`` 起服务（不弹浏览器）
2. 拉 ``/api/env`` 确认环境检测没有阻塞项
3. 上传音频 → 提交任务 → 轮询到结束
4. 核对 ``output\\<任务>\\export\\*`` 等产物是否真的写出来了
5. 关掉服务并打印 PASS / FAIL 摘要

``--device`` 用来专门验证 CPU 兜底路径（``settings.json`` 默认是 auto）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DONE_STATES = {"done", "failed", "cancelled", "idle"}


class Fail(Exception):
    pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


def http(method: str, url: str, body: bytes | None = None, content_type: str | None = None,
         timeout: float = 30.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 30.0):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    status, raw = http(method, url, body, "application/json" if body else None, timeout)
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return status, {"_raw": raw.decode("utf-8", "replace")[:2000]}


def pick_python(pkg: Path) -> Path:
    for cand in (pkg / "runtime" / "python" / "python.exe",
                 pkg / "runtime" / "python" / "Scripts" / "python.exe",
                 pkg / ".venv" / "Scripts" / "python.exe"):
        if cand.is_file():
            return cand
    raise Fail(f"包内找不到 Python 解释器：{pkg}")


def start_server(pkg: Path, py: Path, port: int, logfile: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    fh = logfile.open("wb")
    proc = subprocess.Popen(
        [str(py), "-m", "app.server", "--no-browser", "--port", str(port)],
        cwd=str(pkg), stdout=fh, stderr=subprocess.STDOUT, env=env,
    )
    return proc


def wait_up(port: int, timeout: float, proc: subprocess.Popen) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise Fail(f"服务进程提前退出（code {proc.returncode}）")
        try:
            status, _ = http("GET", f"http://127.0.0.1:{port}/api/health", timeout=3)
            if status == 200:
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)
    raise Fail(f"服务在 {timeout:.0f}s 内没有起来")


def upload_audio(port: int, audio: Path) -> str:
    boundary = "----verify" + uuid.uuid4().hex
    data = audio.read_bytes()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    status, raw = http(
        "POST", f"http://127.0.0.1:{port}/api/upload", head + data + tail,
        f"multipart/form-data; boundary={boundary}", timeout=300,
    )
    if status != 200:
        raise Fail(f"上传失败 HTTP {status}: {raw[:400]}")
    return json.loads(raw.decode("utf-8"))["token"]


def submit(port: int, token: str, device: str, max_seconds: float | None, only_melody: bool) -> str:
    payload = {
        "token": token,
        "preset": "default",
        "device": device,
        "only_melody": only_melody,
        "drop_intro_outro": False,
    }
    if max_seconds is not None:
        payload["max_seconds"] = max_seconds
    status, body = http_json("POST", f"http://127.0.0.1:{port}/api/tasks", payload, timeout=120)
    if status != 200:
        raise Fail(f"提交任务失败 HTTP {status}: {json.dumps(body, ensure_ascii=False)[:600]}")
    return body["task_id"]


def poll(port: int, task_id: str, timeout: float) -> dict:
    """轮询到任务结束，返回 ``/api/state`` 里的 task 字典（不是外层信封）。

    ``/api/state`` 的形状是 ``{"busy":…, "task":{…}, "history":[…]}``。
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        status, body = http_json("GET", f"http://127.0.0.1:{port}/api/state?task_id={task_id}", timeout=30)
        if status != 200:
            raise Fail(f"查询状态失败 HTTP {status}")
        task = body.get("task") or {}
        state = task.get("state")
        line = f"  state={state} percent={task.get('percent')} stage={task.get('stage')} " \
               f"text={task.get('text')}"
        if line != last:
            log(line)
            last = line
        if state in DONE_STATES:
            return task
        time.sleep(2.0)
    raise Fail(f"任务在 {timeout:.0f}s 内没有结束（最后状态 {last}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="整合包端到端验收")
    ap.add_argument("--pkg", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--max-seconds", type=float, default=None)
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--keep-running", action="store_true", help="跑完不关服务（方便手动点页面）")
    args = ap.parse_args()

    pkg = Path(args.pkg).resolve()
    audio = Path(args.audio).resolve()
    if not pkg.is_dir():
        log(f"[×] 包目录不存在：{pkg}")
        return 2
    if not audio.is_file():
        log(f"[×] 测试音频不存在：{audio}")
        return 2

    log(f"包目录  : {pkg}")
    log(f"测试音频: {audio}  ({audio.stat().st_size / 1024**2:.1f} MB)")
    log(f"设备    : {args.device}   最长分析: {args.max_seconds or '全曲'}")
    log()

    py = pick_python(pkg)
    log(f"解释器  : {py}")

    logfile = pkg / "verify_server.log"
    proc = start_server(pkg, py, args.port, logfile)
    failures: list[str] = []
    try:
        # ---- 1. 服务起来 ----
        log("\n[1/5] 等待服务启动…")
        wait_up(args.port, 120, proc)
        log("      服务已就绪")

        # ---- 2. 环境检测 ----
        log("\n[2/5] 环境检测 /api/env")
        status, env = http_json("GET", f"http://127.0.0.1:{args.port}/api/env?full=true", timeout=180)
        if status != 200:
            raise Fail(f"/api/env HTTP {status}")
        for item in env.get("checks", []):
            log(f"      [{item['status']:4}] {item['label']}: {item.get('value') or ''}")
        if env.get("blocking"):
            failures.append("环境检测有阻塞项：" + ", ".join(c["label"] for c in env["blocking"]))
        else:
            log("      无阻塞项")

        # ---- 3. 上传 + 提交 ----
        log("\n[3/5] 上传音频并提交任务")
        token = upload_audio(args.port, audio)
        log(f"      token={token}")
        task_id = submit(args.port, token, args.device, args.max_seconds, only_melody=False)
        log(f"      task_id={task_id}")

        # ---- 4. 等它跑完 ----
        log("\n[4/5] 等待扒谱完成（这是最慢的一步）")
        result = poll(args.port, task_id, args.timeout)
        if result.get("state") != "done":
            failures.append(f"任务状态是 {result.get('state')}，不是 done；error={result.get('error')}")

        # ---- 5. 校验产物 ----
        log("\n[5/5] 校验产物")
        out_dir = Path(result.get("out_dir") or "")
        if not out_dir.is_dir():
            failures.append(f"产物目录不存在：{out_dir}")
        else:
            must = ["score.abc", "melody.mid", "chords.mid", "playback.json", "result.json", "summary.json"]
            for name in must:
                if (out_dir / name).is_file():
                    log(f"      [OK]   {name}")
                else:
                    log(f"      [MISS] {name}")
                    failures.append(f"缺少产物 {name}")
            exports = sorted((out_dir / "export").glob("*")) if (out_dir / "export").is_dir() else []
            if not exports:
                failures.append("export/ 里没有任何导出产物")
            for f in exports:
                log(f"      [OK]   export/{f.name}  {f.stat().st_size / 1024:.0f} KB")
            summary_file = out_dir / "summary.json"
            if summary_file.is_file():
                summary = json.loads(summary_file.read_text(encoding="utf-8"))
                log(f"\n      设备={summary.get('device')} 精度={summary.get('dtype')} "
                    f"用时={summary.get('elapsed_total')}s 进度单调={summary.get('progress_monotonic')}")
                if summary.get("device_note"):
                    log(f"      设备说明：{summary['device_note']}")
                if not summary.get("progress_monotonic"):
                    failures.append("进度不是单调不减")
                if args.device != "auto" and summary.get("device") != args.device:
                    failures.append(f"要求 device={args.device}，实际跑了 {summary.get('device')}")

    except Fail as exc:
        failures.append(str(exc))
    except Exception as exc:  # noqa: BLE001
        failures.append(f"未预期异常 {type(exc).__name__}: {exc}")
    finally:
        if not args.keep_running:
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            log(f"\n服务仍在 127.0.0.1:{args.port} 运行（pid={proc.pid}），手动验证完请自行结束。")

    log("\n" + "=" * 66)
    if failures:
        log("FAIL")
        for f in failures:
            log(f"  - {f}")
        log(f"\n服务日志：{logfile}")
        return 1
    log("PASS — 整合包端到端跑通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
