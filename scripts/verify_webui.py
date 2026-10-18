r"""P1 端到端验证：真起服务、真上传、**扒谱 + 歌词识别**两阶段真跑、真下载 zip。

    .\.venv\Scripts\python.exe scripts\verify_webui.py
    .\.venv\Scripts\python.exe scripts\verify_webui.py --audio "samples\毛阿敏 - 相思.flac" --seconds 60
    .\.venv\Scripts\python.exe scripts\verify_webui.py --base-url http://127.0.0.1:8000 --keep

**为什么不放进 pytest**：这一步会真的加载 SheetSage2 + Qwen3-ASR 并在 GPU 上跑推理
（一两分钟到十几分钟）。它的价值是"证明整条链路真的通"——HTTP、队列、SSE、模型、产物、
卷帘数据、打包，而不是"每次提交都快"。HTTP 层由 `tests/test_webui.py`（29 项，假 runner）覆盖。

判据（全部满足才算通过）：

1. `POST /api/jobs` 收下音频但**不自动开跑**（门禁：状态 `draft`，两个阶段都 `idle`），
   点了「开始扒谱」之后两个阶段才依次到 `done`；
2. SSE 事件流按序到达，含两个阶段的 `phase`/`step`，歌词阶段还有实时 `lyrics`，
   最后一条是终态 `done`；
3. 快照里有 `highlights`（拍号 / BPM / 量化网格非空），产物 >= 29 个；
4. `/project` 可解析且有两条分轨；`/lyrics` 有逐字时间戳和歌词行；
5. `/lanes` 的段落 / 和弦 / 人声音符 / 歌词行都非空，且秒数落在曲长内；
6. `/peaks` 有波形数据；`/audio` 支持 Range（206）且越界回 416；
7. 人声音符**真的填上了字**（`assigned > 0`，且 `project.json` 里能找到 lyric 字段）；
8. zip 能下载、能解开，含 `project.json` / `report.json` / `lyrics.json`，且与单文件一致；
9. 路径穿越被拒；
10. 参数面板的取值表在 `/api/env` 里（含本项目多的「量化」与「语言」下拉框），
    改参数被接受、乱填被拒，且改扒谱参数会让旧结果作废。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from sheetsage2_webui.bootstrap import find_ffmpeg, prepare  # noqa: E402

VERIFY_ROOT = PROJECT / "tmp" / "webui-verify"
DEFAULT_AUDIO = PROJECT / "samples" / "凤凰传奇 - 光芒.flac"


class Check:
    """一行判据的记账：通过就 PASS，否则记下来，最后一起报。"""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def __call__(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"    {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)
        return ok


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def cut_clip(audio: Path, seconds: float) -> Path:
    """截一段短音频当验证输入：全曲要跑十几分钟，验证只需要证明"链路通"。"""
    target = VERIFY_ROOT / f"{audio.stem}-{seconds:g}s.flac"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        print(f"复用已有切片 {target.relative_to(PROJECT)}")
        return target
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，无法切片")
    command = [ffmpeg, "-v", "error", "-y", "-t", f"{seconds:g}", "-i", str(audio),
               "-ac", "2", "-ar", "44100", str(target)]
    print(f"切 {seconds:g}s -> {target.relative_to(PROJECT)}")
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"切片失败：{result.stderr.decode(errors='replace')[-300:]}")
    return target


def start_server(port: int, data_root: Path, extra: list[str]) -> subprocess.Popen:
    log_path = VERIFY_ROOT / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    command = [sys.executable, "-m", "sheetsage2_webui.webui", "--port", str(port),
               "--data-root", str(data_root), "--log-level", "warning", *extra]
    print(f"起服务：{' '.join(command[1:])}\n  日志 {log_path.relative_to(PROJECT)}")
    return subprocess.Popen(command, cwd=str(PROJECT), stdout=log, stderr=subprocess.STDOUT)


def wait_health(base: str, process: subprocess.Popen, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"服务启动失败（退出码 {process.returncode}），看 server.log")
        try:
            if requests.get(f"{base}/api/healthz", timeout=2).status_code == 200:
                return
        except requests.RequestException:
            time.sleep(0.3)
    raise RuntimeError("服务没有在超时内就绪")


def stream_events(base: str, job_id: str, timeout: float = 3600.0,
                  since: int = 0) -> list[dict]:
    """读 SSE 事件流，直到终态。事件就是用户看到的进度。

    `since` 用来只看"这一轮"的事件：重跑某个阶段时，从头重放会立刻撞上上一轮的终态。
    """
    events: list[dict] = []
    started = time.monotonic()
    url = f"{base}/api/jobs/{job_id}/events" + (f"?since={since}" if since else "")
    with requests.get(url, stream=True, timeout=(10, timeout)) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if time.monotonic() - started > timeout:
                raise RuntimeError("SSE 超时")
            if not line or not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            data = event["data"]
            stamp = f"[{time.monotonic() - started:7.1f}s]"
            if event["type"] == "phase":
                print(f"    {stamp} 阶段 {data.get('phase')}：{data.get('state')} "
                      f"{data.get('label')}")
            elif event["type"] == "step":
                print(f"    {stamp}   · {data.get('step')}")
            elif event["type"] == "lyrics":
                print(f"    {stamp}   歌词 {len(data.get('chars') or [])} 字 / "
                      f"{len(data.get('lines') or [])} 行")
            elif event["type"] == "progress" and data.get("message"):
                print(f"    {stamp}   {float(data.get('progress') or 0):.0%} {data['message']}")
            elif event["type"] == "log":
                print(f"    {stamp}   ! {data.get('message')}")
            elif event["type"] in ("phase_done", "phase_error", "phase_cancelled"):
                print(f"    {stamp} 阶段 {data.get('phase')} -> {event['type']}")
            elif event["type"] in ("done", "error", "cancelled"):
                print(f"    {stamp} 终态 {event['type']}: {data.get('message')}")
                break
    return events


def follow(base: str, job_id: str, last_seq: int,
           timeout: float = 3600.0) -> tuple[list[dict], int]:
    """从 `last_seq` 之后接着读事件流，返回 (这一轮的事件, 新的最新序号)。

    为什么要记账：扒谱和歌词现在是**两轮**（默认只跑扒谱，扒谱完了才点歌词），
    每次都从头重放会立刻撞上上一轮的终态 `done`，看起来像"这轮已经结束"。
    """
    events = stream_events(base, job_id, timeout=timeout, since=last_seq)
    if events:
        last_seq = max(last_seq, events[-1]["seq"])
    return events, last_seq


def in_duration(start: float, end: float, duration: float) -> bool:
    return 0 <= start <= end <= duration + 0.5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WebUI 端到端验证（真跑模型）")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO, help="源音频")
    parser.add_argument("--seconds", type=float, default=45.0, help="截取多少秒（默认 45）")
    parser.add_argument("--base-url", default=None, help="直接连已启动的服务，不自己起进程")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--keep", action="store_true", help="验证完不关服务（留着自己点页面）")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--asr-model", default=None, help="歌词识别模型（默认 1.7B）")
    parser.add_argument("--engine-weights", default=None,
                        help="扒谱模型权重精度（bf16 = 小显存卡），透传给服务")
    parser.add_argument("--asr-max-slice", type=float, default=None,
                        help="歌词识别切片上限秒数（小显存卡建议 30）")
    parser.add_argument("--asr-max-vram", default=None,
                        help="给歌词识别模型一个显存预算（如 3GiB）：放不下的层留在内存里")
    parser.add_argument("--transcribe-only", action="store_true", help="只跑扒谱（省时间）")
    args = parser.parse_args(argv)

    prepare()
    check = Check()
    if not args.audio.is_file():
        print(f"找不到音频 {args.audio}", file=sys.stderr)
        return 2
    clip = cut_clip(args.audio, args.seconds)
    data_root = args.data_root or (VERIFY_ROOT / "data")

    process: subprocess.Popen | None = None
    if args.base_url:
        base = args.base_url.rstrip("/")
        print(f"使用已启动的服务 {base}")
    else:
        port = args.port or free_port()
        base = f"http://127.0.0.1:{port}"
        extra = ["--device", args.device]
        if args.asr_model:
            extra += ["--asr-model", args.asr_model]
        if args.asr_max_vram:
            extra += ["--asr-max-vram", args.asr_max_vram]
        if args.asr_max_slice:
            extra += ["--asr-max-slice", str(args.asr_max_slice)]
        if args.engine_weights:
            extra += ["--engine-weights", args.engine_weights]
        process = start_server(port, data_root, extra)
        try:
            wait_health(base, process)
        except Exception as exc:
            print(f"FAIL  {exc}", file=sys.stderr)
            process.terminate()
            return 2
        print(f"服务就绪 {base}")

    try:
        print("\n== 1. 服务信息 /api/env ==")
        info = requests.get(f"{base}/api/env", timeout=10).json()
        for key in ("version", "model", "device", "ffmpeg", "data_root", "static_ready",
                    "max_upload_mb"):
            print(f"  {key}: {info.get(key)}")
        print(f"  asr: {info.get('asr')}")
        print(f"  phases: {info.get('phases')}")
        check("前端产物已构建（static_ready）", bool(info.get("static_ready")))
        check("ffmpeg 可用", bool(info.get("ffmpeg")))
        check("两个阶段都登记了",
              [p["key"] for p in info.get("phases") or []] == ["transcribe", "lyrics"])

        page = requests.get(f"{base}/", timeout=10)
        check("根路径返回页面（200）", page.status_code == 200, f"HTTP {page.status_code}")
        check("页面是 Vue 入口", '<div id="app">' in page.text)
        asset = re.search(r'src="(/assets/[^"]+\.js)"', page.text)
        check("页面引用了构建好的 JS", asset is not None)
        if asset:
            bundle = requests.get(f"{base}{asset.group(1)}", timeout=10)
            check("JS 资源可取到（200）", bundle.status_code == 200,
                  f"{asset.group(1)} {len(bundle.content)} 字节")
        check("API 未被静态挂载遮挡",
              requests.get(f"{base}/api/healthz", timeout=10).status_code == 200)

        print("\n== 1b. 参数面板 /api/env ==")
        params = info.get("params") or {}
        transcribe_keys = [row["key"] for row in params.get("transcribe") or []]
        lyrics_keys = [row["key"] for row in params.get("lyrics") or []]
        print(f"  扒谱参数 {transcribe_keys}")
        print(f"  歌词参数 {lyrics_keys}")
        check("扒谱参数表是 精度/设备/范围/量化",
              transcribe_keys == ["dtype", "device", "range", "quantize"],
              str(transcribe_keys))
        check("歌词参数表是 模型/音源/段落/语言",
              lyrics_keys == ["asr_model", "source", "skip_instrumental", "language"],
              str(lyrics_keys))
        # 音高修正固定降一个八度，故意不做成面板选项
        check("「音高」不出现在参数面板上",
              "pitch" not in transcribe_keys
              and "音高" not in [row["label"] for row in params.get("transcribe") or []])
        section_row = next((row for row in params.get("lyrics") or []
                            if row["key"] == "skip_instrumental"), {})
        check("「段落」默认只跑唱段（跳过前奏/间奏/尾奏）",
              section_row.get("default") == "vocal"
              and [item["value"] for item in section_row.get("options") or []]
              == ["vocal", "all"],
              f"default={section_row.get('default')}")
        language_row = next((row for row in params.get("lyrics") or []
                             if row["key"] == "language"), {})
        check("语言是本项目的下拉框（选项比原型多）",
              language_row.get("widget") == "select" and len(language_row.get("options") or []) > 4,
              f"widget={language_row.get('widget')} "
              f"options={len(language_row.get('options') or [])}")
        check("参数没有被锁定（默认模式）", info.get("params_locked") is False,
              str(info.get("params_locked")))

        print("\n== 2. 上传并建工程 POST /api/jobs ==")
        with clip.open("rb") as handle:
            payload = {"file": (clip.name, handle, "audio/flac")}
            response = requests.post(f"{base}/api/jobs", files=payload, timeout=300)
        check("上传被接受（201）", response.status_code == 201,
              f"HTTP {response.status_code} {response.text[:160]}")
        if response.status_code != 201:
            return 1
        job = response.json()
        job_id = job["id"]
        print(f"  工程 {job_id}  名称 {job['name']}  状态 {job['state']}")
        print(f"  参数 {job['options']}")
        # 门禁：丢音频只建工程，参数选完点了「开始扒谱」才真开跑
        check("上传后不自动开跑（draft，两个阶段都 idle）",
              job["state"] == "draft"
              and job["stages"]["transcribe"]["state"] == "idle"
              and job["stages"]["lyrics"]["state"] == "idle",
              f"state={job['state']} transcribe={job['stages']['transcribe']['state']} "
              f"lyrics={job['stages']['lyrics']['state']}")

        print("\n== 3. 点「开始扒谱」+ SSE 进度流 ==")
        started = requests.post(f"{base}/api/jobs/{job_id}/stages/transcribe", timeout=60)
        check("「开始扒谱」被接受（200）", started.status_code == 200,
              f"HTTP {started.status_code} {started.text[:120]}")
        events, last_seq = follow(base, job_id, 0)
        kinds = [event["type"] for event in events]
        sequences = [event["seq"] for event in events]
        phases_seen = {event["data"].get("phase") for event in events
                       if event["type"] == "phase"}
        check("事件流非空", bool(events), f"{len(events)} 条")
        check("序号严格递增且不重复", sequences == sorted(set(sequences)))
        check("扒谱阶段出现过", "transcribe" in phases_seen, str(sorted(phases_seen)))
        check("这一轮没有歌词（默认只跑扒谱）", "lyrics" not in phases_seen)
        check("有小步骤事件", "step" in kinds)
        check("最后一条是终态 done", bool(kinds) and kinds[-1] == "done",
              kinds[-1] if kinds else "（空）")

        if not args.transcribe_only:
            # 扒谱完了才点歌词（界面上的门禁就是这个条件；后端也是 409）
            print("\n== 3b. 扒谱完成后单独跑歌词识别 ==")
            started = requests.post(f"{base}/api/jobs/{job_id}/stages/lyrics", timeout=60)
            check("歌词阶段接受请求（200）", started.status_code == 200,
                  f"HTTP {started.status_code} {started.text[:120]}")
            again, last_seq = follow(base, job_id, last_seq)
            check("歌词走到终态 done",
                  bool(again) and again[-1]["type"] == "done",
                  again[-1]["type"] if again else "（空）")
            lyric_kinds = [event["type"] for event in again]
            lyric_phases = {event["data"].get("phase") for event in again
                            if event["type"] == "phase"}
            check("歌词阶段出现过", "lyrics" in lyric_phases, str(sorted(lyric_phases)))
            check("有实时歌词事件", "lyrics" in lyric_kinds,
                  f"{sum(1 for kind in lyric_kinds if kind == 'lyrics')} 条")
            check("歌词那一轮序号也是递增的",
                  [e["seq"] for e in again] == sorted({e["seq"] for e in again}))

        print("\n== 4. 工程快照与产物 ==")
        snapshot = requests.get(f"{base}/api/jobs/{job_id}", timeout=30).json()
        check("工程状态 done", snapshot["state"] == "done", snapshot.get("error") or "")
        for phase in ("transcribe", "lyrics"):
            run = snapshot["stages"][phase]
            expect = "idle" if (args.transcribe_only and phase == "lyrics") else "done"
            check(f"{phase} 阶段是 {expect}", run["state"] == expect,
                  (run.get("error") or "") + f"{run['state']} {run['elapsed_seconds']}s")
        highlights = snapshot.get("highlights") or {}
        print(f"  拍号 {highlights.get('meter')}  弱起 {highlights.get('pickup_seconds')}s  "
              f"BPM {highlights.get('bpm')}  调性 {highlights.get('keys')}")
        print(f"  音符 {highlights.get('notes')}  和弦 {highlights.get('chord_count')} "
              f"({highlights.get('chord_kinds')} 种)  段落 {highlights.get('section_count')}")
        print(f"  量化 {highlights.get('quantize')}")
        print(f"  歌词阶段统计 {snapshot['stages']['lyrics']['detail']}")
        check("拍号已判定", bool(highlights.get("meter")))
        check("BPM 已判定", highlights.get("bpm") is not None)
        check("量化网格非空", bool((highlights.get("quantize") or {}).get("grid_ticks")))
        files = snapshot.get("files") or []
        check("产物数量 >= 29", len(files) >= 29, f"{len(files)} 个")

        print("\n== 5. 工程 / 歌词 / 卷帘数据 ==")
        project = requests.get(f"{base}/api/jobs/{job_id}/project", timeout=60).json()
        measures = project.get("timeline", {}).get("measures") or []
        check("project.json 有小节表", len(measures) > 0, f"{len(measures)} 小节")
        check("有两条分轨", len(project.get("tracks") or []) == 2)

        lanes = requests.get(f"{base}/api/jobs/{job_id}/lanes", timeout=60).json()
        duration = lanes["duration_seconds"]
        print(f"  时长 {duration}s  和弦 {len(lanes['chords'])}  段落 {len(lanes['sections'])}  "
              f"人声音符 {len(lanes['vocal_notes'])}  歌词行 {len(lanes['lyric_lines'])}")
        check("卷帘有时长", duration > 0)
        check("卷帘有段落", len(lanes["sections"]) > 0)
        # 段落标签：翻成中文 + 相邻同名已经合并（模型是按乐句给标签的）
        section_labels = [row["label"] for row in lanes["sections"]]
        print(f"  段落标签 {section_labels}")
        check("段落标签是中文",
              all(any("\u4e00" <= ch <= "\u9fff" for ch in label)
                  for label in section_labels if label), str(section_labels))
        check("没有相邻重复的段落",
              all(a != b for a, b in zip(section_labels, section_labels[1:])),
              str(section_labels))
        check("卷帘有和弦", len(lanes["chords"]) > 0)
        check("卷帘有人声音符", len(lanes["vocal_notes"]) > 0)
        check("卷帘区间都在曲长内",
              all(in_duration(row["start"], row["end"], duration)
                  for row in lanes["chords"] + lanes["sections"] + lanes["lyric_lines"]))

        if not args.transcribe_only:
            lyrics = requests.get(f"{base}/api/jobs/{job_id}/lyrics", timeout=60).json()
            fill = lyrics.get("fill") or {}
            print(f"  识别文本：{str(lyrics.get('text', ''))[:60]}…")
            print(f"  逐字 {len(lyrics.get('chars') or [])}  行 "
                  f"{len(lyrics.get('lines') or [])}  填词 {fill}")
            check("歌词有逐字时间戳", len(lyrics.get("chars") or []) > 0)
            check("歌词分出了行", len(lyrics.get("lines") or []) > 0)
            check("lyrics.json 记了切片口径（跑过哪些段、跳过了哪些）",
                  "sections" in lyrics and "skipped_sections" in lyrics
                  and "max_slice_seconds" in lyrics,
                  f"跑过 {len(lyrics.get('sections') or [])} 段、"
                  f"跳过 {len(lyrics.get('skipped_sections') or [])} 段、"
                  f"上限 {lyrics.get('max_slice_seconds')}s")
            check("卷帘拿到歌词行", len(lanes["lyric_lines"]) > 0)
            check("音符真的填上了字", int(fill.get("assigned") or 0) > 0,
                  f"assigned={fill.get('assigned')}")
            filled = [note for track in project.get("tracks") or []
                      if track.get("id") == "vocal" for note in track.get("notes") or []
                      if note.get("lyric")]
            check("project.json 的人声轨带上了 lyric", len(filled) > 0, f"{len(filled)} 个音有字")

        print("\n== 6. 波形与音频（Range）==")
        peaks = requests.get(f"{base}/api/jobs/{job_id}/peaks?buckets=256", timeout=120).json()
        check("波形桶数符合请求", peaks["buckets"] == 256, str(peaks.get("buckets")))
        check("波形有数据", bool(peaks["peaks"]) and max(peaks["peaks"]) > 0.05,
              f"峰值 {max(peaks['peaks']) if peaks['peaks'] else 0:.3f}")

        whole = requests.get(f"{base}/api/jobs/{job_id}/audio", timeout=300)
        check("音频可取（200）", whole.status_code == 200 and len(whole.content) > 0,
              f"{len(whole.content)} 字节")
        part = requests.get(f"{base}/api/jobs/{job_id}/audio", timeout=60,
                            headers={"Range": "bytes=0-99"})
        check("Range 请求回 206", part.status_code == 206, f"HTTP {part.status_code}")
        check("Range 内容正确", part.content == whole.content[:100])
        check("Content-Range 正确",
              part.headers.get("content-range") == f"bytes 0-99/{len(whole.content)}",
              part.headers.get("content-range", ""))
        bad = requests.get(f"{base}/api/jobs/{job_id}/audio", timeout=60,
                           headers={"Range": f"bytes={len(whole.content) + 10}-"})
        check("越界 Range 回 416", bad.status_code == 416, f"HTTP {bad.status_code}")

        print("\n== 7. 下载 ==")
        single = requests.get(f"{base}/api/jobs/{job_id}/files/project.json", timeout=60)
        check("单文件下载成功", single.status_code == 200)
        bundle = requests.get(f"{base}/api/jobs/{job_id}/download", timeout=600)
        check("zip 下载成功", bundle.status_code == 200, f"{len(bundle.content)} 字节")
        names: list[str] = []
        if bundle.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
                check("zip 完整性自检通过", archive.testzip() is None)
                names = archive.namelist()
                inner = next((name for name in names if name.endswith("/project.json")), None)
                check("zip 内有 project.json", inner is not None)
                check("zip 内有 report.json",
                      any(name.endswith("/report.json") for name in names))
                if not args.transcribe_only:
                    check("zip 内有 lyrics.json",
                          any(name.endswith("/lyrics.json") for name in names))
                if inner:
                    check("zip 里的 project.json 与单文件下载一致",
                          archive.read(inner) == single.content)
        check("zip 条数不少于产物清单", len(names) >= len(files), f"{len(names)} / {len(files)}")

        print("\n== 7b. 导出 SVP（下载弹窗可勾的三组内容）==")
        exports = requests.get(f"{base}/api/jobs/{job_id}/exports", timeout=30)
        check("导出选项可取（200）", exports.status_code == 200, f"HTTP {exports.status_code}")
        export_body = exports.json()
        rows = {row["key"]: row for row in export_body.get("parts") or []}
        print(f"  可勾选：" + " · ".join(
            f"{key}({row['notes']} 音/{row['tracks']} 轨"
            f"{'，默认' if row['default'] else ''})" for key, row in rows.items()))
        check("三组内容都在选项里", list(rows) == ["vocal", "instrumental", "chords"],
              str(list(rows)))
        check("默认只勾人声", export_body.get("defaults") == ["vocal"],
              str(export_body.get("defaults")))
        check("和弦按「一轨只能有一个音」拆轨",
              rows.get("chords", {}).get("tracks", 0) >= 1
              and "轨" in (rows.get("chords", {}).get("detail") or ""),
              str(rows.get("chords", {}).get("detail")))

        svp = requests.get(f"{base}/api/jobs/{job_id}/download?kind=svp", timeout=180)
        check("默认下载 SVP（200）", svp.status_code == 200,
              f"HTTP {svp.status_code} {svp.text[:120]}")
        check("给的是 .svp 文件名", ".svp" in (svp.headers.get("content-disposition") or ""),
              svp.headers.get("content-disposition", ""))
        # 拿不到 SVP 时**别直接崩**：脚本要能把"哪一项不合格"报全，而不是抛个 KeyError
        # （人声那组没有音符时后端会回 400，这是合法结果，不是脚本错误）。
        try:
            svp_json = json.loads(svp.content.rstrip(b"\x00").decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            svp_json = {}
        svp_tempos = (svp_json.get("time") or {}).get("tempo") or []
        svp_meters = (svp_json.get("time") or {}).get("meter") or []
        svp_tracks = svp_json.get("tracks") or []
        # SVP 的音符挂在 track.mainGroup.notes（不是 track.groups，那个是音符组列表）
        svp_notes = [note for track in svp_tracks
                     for note in (track.get("mainGroup") or {}).get("notes") or []]
        # 没有歌词的音符会被填上占位音节（中文"啦"/日语"ラ"/其它"la"），
        # 所以"有没有真歌词"要数**不是**占位的那些。
        fillers = {"啦", "ラ", "la"}
        real_lyrics = [note for note in svp_notes
                       if str(note.get("lyrics") or "").strip() not in fillers]
        print(f"  默认（只人声）：{len(svp_tracks)} 轨 · {len(svp_notes)} 音"
              f"（其中 {len(real_lyrics)} 个有真歌词）· "
              f"{len(svp_tempos)} 个 tempo 点 · {len(svp_meters)} 个拍号")
        check("SVP 里有音符", len(svp_notes) > 0)
        check("SVP 里带速度轴（变速）", len(svp_tempos) >= 1)
        if args.transcribe_only:
            print("  （--transcribe-only，没有真歌词，只查占位音节）")
            check("没识别歌词时用占位音节", len(svp_notes) > 0)
        else:
            check("识别的歌词进了 SVP", len(real_lyrics) > 0,
                  f"真歌词 {len(real_lyrics)} / {len(svp_notes)}")
        # 位置单位是 blick（一个四分音符 705600000），libresvip 的 tick 是 480/四分音符，
        # 所以任何位置都该是 1_470_000 的整数倍。**恒速曲的 tempo 只有一条、位置正好是 0**，
        # 光看它判不出单位（以前就是这么误判成 FAIL 的），所以把音符起点一起算进来。
        blick = 705_600_000 // 480
        positions = [int(item["position"]) for item in svp_tempos] + \
            [int(note.get("onset") or 0) for note in svp_notes]
        check("位置单位是 blick（1_470_000/四分音符），不是我们的 tick",
              bool(positions) and all(item % blick == 0 for item in positions)
              and max(positions, default=0) >= blick,
              f"tempo {[item['position'] for item in svp_tempos[:3]]} · "
              f"音符起点 {[note.get('onset') for note in svp_notes[:3]]}")
        # 速度也只写变化点：恒速曲一条到底（逐小节抄的话每个小节都标一次 125），
        # 变速曲保留真的转速度点，但相邻两条不该是同一个数
        check("速度轴不写重复的相邻点",
              all(a["bpm"] != b["bpm"] for a, b in zip(svp_tempos, svp_tempos[1:])),
              str([(item["position"], item["bpm"]) for item in svp_tempos[:6]]))
        if single.status_code == 200:
            tempo_map = (single.json().get("timeline") or {}).get("tempo_map") or []
            if len(tempo_map) == 1:
                check("恒速曲的速度轴只有一条",
                      len(svp_tempos) == 1,
                      str([(item["position"], item["bpm"]) for item in svp_tempos[:6]]))
        # 拍号只写变化点：逐小节抄的话 SynthV 会在每个小节上面都标一次 4/4
        # （实测《光芒》45s 版：21 条 → 20 条一模一样的 4/4）
        check("拍号只写变化点（相邻两条不相同）",
              all((a["numerator"], a["denominator"]) != (b["numerator"], b["denominator"])
                  for a, b in zip(svp_meters, svp_meters[1:])),
              str([(m["index"], m["numerator"], m["denominator"]) for m in svp_meters[:6]]))

        everything = requests.get(
            f"{base}/api/jobs/{job_id}/download?kind=svp&parts=vocal,instrumental,chords",
            timeout=300)
        check("三组内容一起导（200）", everything.status_code == 200,
              f"HTTP {everything.status_code}")
        if everything.status_code == 200:
            full = json.loads(everything.content.rstrip(b"\x00").decode("utf-8"))
            print(f"  全部内容：{len(full['tracks'])} 轨 · "
                  f"{[track.get('name') for track in full['tracks']]}")
            check("乐器与和弦带来了更多轨", len(full["tracks"]) > len(svp_tracks),
                  f"{len(full['tracks'])} > {len(svp_tracks)}")
        bad_kind = requests.get(f"{base}/api/jobs/{job_id}/download?kind=svp&parts=drums",
                                timeout=30)
        check("未知的导出内容被拒（400）", bad_kind.status_code == 400,
              f"HTTP {bad_kind.status_code}")

        print("\n== 8. 干声（可选音源）：有就用它，没有就回退到混音原曲 ==")
        if args.transcribe_only:
            print("  （--transcribe-only，跳过）")
        else:
            before = snapshot["stages"]["lyrics"]["detail"].get("source")
            check("第一次跑歌词用的是混音原曲", before == "mix", f"source={before}")
            # 故意把混音原曲传进"干声"栏——这正是用户会干的事，不校验内容，只如实记录
            with clip.open("rb") as handle:
                uploaded = requests.post(f"{base}/api/jobs/{job_id}/stems/vocal",
                                         files={"file": ("当作干声的混音原曲.flac", handle,
                                                         "audio/flac")},
                                         timeout=300)
            check("干声上传被接受（201）", uploaded.status_code == 201,
                  f"HTTP {uploaded.status_code} {uploaded.text[:160]}")
            stems = (uploaded.json().get("stems") or {}).get("vocal") or {}
            check("快照里能看到这条干声", stems.get("name") == "当作干声的混音原曲.flac",
                  str(stems.get("name")))
            check("换音源后旧歌词作废",
                  uploaded.json()["stages"]["lyrics"]["state"] == "idle",
                  uploaded.json()["stages"]["lyrics"]["state"])

            stem_audio = requests.get(f"{base}/api/jobs/{job_id}/stems/vocal/audio",
                                      timeout=300)
            check("干声可试听（200）", stem_audio.status_code == 200
                  and len(stem_audio.content) > 0, f"{len(stem_audio.content)} 字节")
            stem_part = requests.get(f"{base}/api/jobs/{job_id}/stems/vocal/audio",
                                     timeout=60, headers={"Range": "bytes=0-99"})
            check("干声支持 Range（206）", stem_part.status_code == 206)

            requests.post(f"{base}/api/jobs/{job_id}/stages/lyrics", timeout=60)
            again, last_seq = follow(base, job_id, last_seq)
            kinds = [event["type"] for event in again]
            check("重跑歌词走到终态", bool(kinds) and kinds[-1] == "done",
                  kinds[-1] if kinds else "（空）")
            after = requests.get(f"{base}/api/jobs/{job_id}", timeout=30).json()
            detail = after["stages"]["lyrics"]["detail"]
            check("第二次跑歌词用的是干声", detail.get("source") == "stem",
                  f"source={detail.get('source')} file={detail.get('source_file')}")
            check("记录了用的是哪条干声",
                  detail.get("source_file") == "当作干声的混音原曲.flac",
                  str(detail.get("source_file")))
            check("干声也有独立波形",
                  requests.get(f"{base}/api/jobs/{job_id}/peaks?source=stem&buckets=64",
                               timeout=120).status_code == 200)
            removed = requests.delete(f"{base}/api/jobs/{job_id}/stems/vocal", timeout=60).json()
            check("移除干声后回退", not (removed.get("stems") or {})
                  and removed["stages"]["lyrics"]["state"] == "idle")

        print("\n== 8b. 参数面板：改参数 / 乱填被拒 / 旧结果作废 ==")
        env_languages = requests.get(f"{base}/api/env", timeout=10).json().get("languages") or []
        check("/env 给出了语言选项", [item["label"] for item in env_languages][:3] ==
              ["自动", "中文", "英语"], str([item["label"] for item in env_languages]))
        swapped = requests.patch(f"{base}/api/jobs/{job_id}/options",
                                 json={"language": "Japanese"}, timeout=30)
        check("能改成日语（200）", swapped.status_code == 200,
              f"HTTP {swapped.status_code} {swapped.text[:120]}")
        check("工程级设置记住了", swapped.json()["options"]["language"] == "Japanese")
        rejected = requests.patch(f"{base}/api/jobs/{job_id}/options",
                                  json={"language": "Klingon"}, timeout=30)
        check("乱填语言被拒（400）", rejected.status_code == 400,
              f"HTTP {rejected.status_code}")
        unknown = requests.patch(f"{base}/api/jobs/{job_id}/options",
                                 json={"reverb": "on"}, timeout=30)
        check("不认识的参数被拒（400）", unknown.status_code == 400,
              f"HTTP {unknown.status_code}")

        print("\n== 8c. 「段落」参数：只跑唱段 vs 全部段落 ==")
        # 这个 45s 切片的前 30s 被模型判成了前奏（人声确实从 30.0s 才开始），
        # 所以"只跑唱段"应该把整首 45s 里的前奏整段丢掉。
        # 注意：上一段的语言 PATCH 已经把歌词作废了，所以这里要重新跑一轮再看 detail。
        requests.patch(f"{base}/api/jobs/{job_id}/options", json={"language": ""}, timeout=30)
        requests.post(f"{base}/api/jobs/{job_id}/stages/lyrics", timeout=60)
        again, last_seq = follow(base, job_id, last_seq)
        check("「只跑唱段」这一轮跑完",
              bool(again) and again[-1]["type"] == "done",
              again[-1]["type"] if again else "（空）")

        lanes_now = requests.get(f"{base}/api/jobs/{job_id}/lanes", timeout=60).json()
        total = float(lanes_now["duration_seconds"])
        sung = requests.get(f"{base}/api/jobs/{job_id}", timeout=30).json()
        sung_detail = sung["stages"]["lyrics"]["detail"]
        skipped = sung_detail.get("skipped_sections") or []
        sent = sum(float(row.get("seconds") or 0.0)
                   for row in sung_detail.get("sections") or [])
        print(f"  全曲 {total:.1f}s · 送去识别 {sent:.1f}s · 跳过 "
              f"{[(item['label'], round(item['seconds'], 1)) for item in skipped]}")
        check("默认只跑唱段：前奏/间奏/尾奏被跳过",
              int(sung_detail.get("skipped_instrumental") or 0) >= 1
              and all(item["label"] in ("前奏", "间奏", "尾奏") for item in skipped),
              str([item["label"] for item in skipped]))
        check("器乐段没有送去识别（省掉的时间可见）", 0 < sent < total * 0.9,
              f"{sent:.1f}s / {total:.1f}s")
        check("跑过的段落标签是中文",
              all(any("\u4e00" <= ch <= "\u9fff" for ch in str(row.get("label") or ""))
                  for row in sung_detail.get("sections") or []),
              str([row.get("label") for row in sung_detail.get("sections") or []]))
        requests.patch(f"{base}/api/jobs/{job_id}/options",
                       json={"skip_instrumental": "all"}, timeout=30)
        requests.post(f"{base}/api/jobs/{job_id}/stages/lyrics", timeout=60)
        again, last_seq = follow(base, job_id, last_seq)
        check("改成「全部段落」后歌词跑完",
              bool(again) and again[-1]["type"] == "done",
              again[-1]["type"] if again else "（空）")
        all_detail = requests.get(f"{base}/api/jobs/{job_id}",
                                  timeout=30).json()["stages"]["lyrics"]["detail"]
        every = sum(float(row.get("seconds") or 0.0)
                    for row in all_detail.get("sections") or [])
        check("「全部段落」不再跳过任何段，送去的音频变多",
              int(all_detail.get("skipped_instrumental") or 0) == 0
              and every > sent,
              f"跳过 {all_detail.get('skipped_instrumental')} 段，送识别 {every:.1f}s")
        requests.patch(f"{base}/api/jobs/{job_id}/options",
                       json={"skip_instrumental": "vocal"}, timeout=30)

        # 扒谱参数是"换口径"：谱面作废，挂在它上面的歌词也作废
        reset = requests.patch(f"{base}/api/jobs/{job_id}/options",
                               json={"quantize": "off"}, timeout=30).json()
        check("改量化参数让扒谱与歌词都作废",
              reset["options"]["quantize"] == "off"
              and reset["stages"]["transcribe"]["state"] == "idle"
              and reset["stages"]["lyrics"]["state"] == "idle",
              f"transcribe={reset['stages']['transcribe']['state']} "
              f"lyrics={reset['stages']['lyrics']['state']}")

        # 改回去，再跑一遍把工程恢复成"扒谱+歌词都完成"——UI 冒烟要拿它当素材
        requests.patch(f"{base}/api/jobs/{job_id}/options",
                       json={"quantize": "grid", "language": ""}, timeout=30)
        if not args.transcribe_only:
            requests.post(f"{base}/api/jobs/{job_id}/chain", timeout=60)
            again, last_seq = follow(base, job_id, last_seq)
            kinds = [event["type"] for event in again]
            check("改回参数后重跑一遍能回到 done", bool(kinds) and kinds[-1] == "done",
                  kinds[-1] if kinds else "（空）")

        print("\n== 9. 会话卫生 ==")
        jobs = requests.get(f"{base}/api/jobs", timeout=10).json()["jobs"]
        check("工程出现在列表里", any(item["id"] == job_id for item in jobs))
        traversal = requests.get(f"{base}/api/jobs/{job_id}/files/..%2F..%2Fjob.json", timeout=10)
        check("路径穿越被拒（404）", traversal.status_code == 404, f"HTTP {traversal.status_code}")

    finally:
        if process is not None and not args.keep:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
            print("\n服务已关闭")
        elif process is not None:
            print(f"\n服务保留在 {base}（--keep）")

    print("\n" + "=" * 60)
    if check.failures:
        print(f"FAILED：{len(check.failures)} 项 -> {check.failures}")
        return 1
    print("全部通过：丢音频 → 扒谱 + 歌词识别 → 下载产物 这条链路成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
