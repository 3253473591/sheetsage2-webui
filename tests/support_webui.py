"""WebUI 测试的共用夹具：假 runner、临时客户端、等待与音频样本。

**故意不是 `conftest.py`**：这里的每一样都是"显式导入才生效"的工具，看得见谁在用。
（`conftest.py` 一旦变成大杂烩，改动一行的爆炸半径就是整套测试。）

- `fake_transcribe` / `fake_lyrics`：不加载模型的阶段实现（写最小产物）；
- `make_client`：`create_app(runners=...)` + `TestClient`，且 `probe_upload=False`（不上 ffmpeg）；
- `wait_stage` / `wait_for_state`：**走快照**轮询——直接读 `job.stages[...]` 会和收尾线程抢；
- `collect_events`：把 SSE 读成 list（终态事件算收尾）；
- `real_wav`：真 WAV 字节，给需要 ffmpeg 解码的用例。
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.webui.app import create_app  # noqa: E402
from sheetsage2_webui.webui.job_models import Job  # noqa: E402
from sheetsage2_webui.webui.jobs import JobManager, default_options  # noqa: E402
from sheetsage2_webui.webui.runner import (  # noqa: E402
    build_highlights,
    group_lyric_lines,
)
from sheetsage2_webui.webui.job_files import project_folder_name  # noqa: E402

#: 新建工程的默认参数（参数面板上默认高亮的那一项）。
DEFAULT_OPTIONS = default_options()




WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64


FAKE_CHARS = [
    {"text": "晚", "start": 1.0, "end": 1.3},
    {"text": "风", "start": 1.3, "end": 1.6},
    {"text": "停", "start": 1.6, "end": 1.9},
    {"text": "在", "start": 1.9, "end": 2.2},
    {"text": "旧", "start": 5.0, "end": 5.3},
    {"text": "站", "start": 5.3, "end": 5.6},
    {"text": "台", "start": 5.6, "end": 5.9},
]


PROJECT_STUB = {
    "version": 1, "ppq": 1920, "audio": "t.wav", "duration_seconds": 8.0,
    "timeline": {
        "measures": [
            {"index": 0, "start": 0.0, "end": 4.0, "tick_start": 0, "tick_end": 7680,
             "numerator": 4, "denominator": 4, "bpm": 120.0, "is_pickup": False},
            {"index": 1, "start": 4.0, "end": 8.0, "tick_start": 7680, "tick_end": 15360,
             "numerator": 4, "denominator": 4, "bpm": 120.0, "is_pickup": False},
        ],
        "pickup_seconds": 0.0,
    },
    "keys": [{"tonic": "C", "mode": "major", "tick": 0, "dur": 15360}],
    "sections": [{"tick": 0, "dur": 7680, "label": "intro"},
                 {"tick": 7680, "dur": 7680, "label": "verse"}],
    "chords": [{"tick": 0, "dur": 3840, "label": "C"},
               {"tick": 3840, "dur": 3840, "label": "F"}],
    "tracks": [
        {"id": "vocal", "notes": [{"tick": 0, "dur": 960, "pitch": 60, "lyric": "晚"},
                                  {"tick": 960, "dur": 960, "pitch": 62, "lyric": "-"}]},
        {"id": "instrumental", "notes": []},
    ],
}


DEFAULT_OPTIONS = default_options()


class Char:
    """`asr.AlignedChar` 的最小替身（group_lyric_lines 只读这三个属性）。"""

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text, self.start, self.end = text, start, end


class GateRunner:
    """跑到中途卡住，等测试放行——用来观察"进行中"的状态与取消。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()

    def __call__(self, job, progress):
        progress.step("识别音乐", 0.06)
        progress.set(0.2, "窗口 1/3")
        self.entered.set()
        if not self.gate.wait(timeout=20):
            raise RuntimeError("测试没有放行")
        progress.check()
        write_artifacts(job.out_dir)
        payload = {"summary": {"notes": {}}, "decisions": {}, "project": {}}
        payload["highlights"] = build_highlights(payload)
        return {"detail": {}}


def fake_transcribe(job, progress) -> dict:
    progress.check()
    progress.step("加载模型", 0.0)
    progress.step("读取音频", 0.04)
    progress.step("识别音乐", 0.06)
    progress.set(0.42, "窗口 1/2 · 解码 128 tokens")
    write_artifacts(job.out_dir)
    progress.step("生成工程", 0.88)
    progress.step("整理产物", 0.98)
    payload = {
        "summary": {"duration_seconds": 12.5, "elapsed_seconds": 3.25, "device": "cpu",
                    "dtype": "fp32", "notes": {"melody": 7, "vocal": 5, "instrumental": 2},
                    "events": 9},
        "decisions": {"meter": "4/4", "pickup_seconds": 0.3, "bpm": 104.002,
                      "bpm_source": "grid", "bpm_confident": True, "tempo_constant": True,
                      "tempo_segments": 1, "keys": ["C:major"], "stable_tempos": [104.002]},
        "project": {"quantize": {"enabled": True, "grid_ticks": 240, "moved": 3, "notes": 7,
                                 "moved_ratio": 0.4286, "failed_tracks": 0},
                    "quantize_shift_seconds": {"max": 0.31},
                    "chords": [{"tick": 0, "dur": 1920, "label": "C"},
                               {"tick": 1920, "dur": 1920, "label": "F"}],
                    "sections": [{"tick": 0, "dur": 3840, "label": "intro"}]},
        "warnings": ["示例告警"],
    }
    with job.lock:
        job.highlights = payload["highlights"] = build_highlights(payload)
        job.decisions = payload["decisions"]
        job.summary = payload["summary"]
        job.warnings = list(payload["warnings"])
    progress.detail(meter="4/4", bpm=104.002, notes=payload["highlights"]["notes"])
    return {"detail": {"notes": payload["highlights"]["notes"], "warnings": ["示例告警"]}}


def fake_lyrics(job, progress) -> dict:
    progress.check()
    progress.step("读取工程", 0.02)
    if not (job.out_dir / "project.json").is_file():
        raise RuntimeError("还没有扒谱工程：请先跑「扒谱」")
    # 音源规则：有干声就用干声，没有就回退到混音原曲（真 runner 就是这么写的）
    stem = job.stems.get("vocal")
    source = "stem" if stem else "mix"
    source_file = str(stem["name"]) if stem else job.audio
    progress.log(f"歌词音源：{source_file}")
    progress.step("加载识别模型", 0.05)
    progress.step("语音识别", 0.10)
    # 逐段回传：先出 1 行，再出 2 行
    progress.lyrics(text="晚风停在", chars=FAKE_CHARS[:4], lines=group_lyric_lines(
        [Char(**item) for item in FAKE_CHARS[:4]]), slices=1, slices_total=2)
    progress.set(0.5, "已识别 1/2 段 · 4 字 / 2 行")
    progress.step("歌词对齐", 0.85)
    lines = group_lyric_lines([Char(**item) for item in FAKE_CHARS])
    progress.step("写入卷帘", 0.95)
    (job.out_dir / "lyrics.json").write_text(json.dumps({
        "text": "晚风停在旧站台", "lines": lines, "chars": FAKE_CHARS,
        "fill": {"assigned": 7, "melisma": 1, "dropped": 0, "absorbed": 0,
                 "unmatched_notes": 1},
    }, ensure_ascii=False), encoding="utf-8")
    progress.lyrics(text="晚风停在旧站台", chars=FAKE_CHARS, lines=lines,
                    slices=2, slices_total=2)
    detail = {"source": source, "source_file": source_file,
              "language": "自动", "language_used": "Chinese",
              "lines": len(lines), "chars": len(FAKE_CHARS), "assigned": 7, "melisma": 1}
    progress.detail(**detail)
    return {"detail": detail}


def fake_runners() -> dict:
    return {"transcribe": fake_transcribe, "lyrics": fake_lyrics}


def write_artifacts(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "project.json").write_text(json.dumps(PROJECT_STUB), encoding="utf-8")
    (out_dir / "report.json").write_text('{"decisions": {}}', encoding="utf-8")
    # `summary.json` 是完成门禁（G4）要求的三个关键产物之一：真的 pipeline 会写它，
    # 假 runner 也得写，否则"完成"就不再是一个可以随便宣布的状态。
    (out_dir / "summary.json").write_text(
        json.dumps({"duration_seconds": 8.0, "artifacts_ok": True, "notes": {}},
                   ensure_ascii=False), encoding="utf-8")
    (out_dir / "structure.lab").write_text("0.00\t4.00\tintro\n", encoding="utf-8")
    (out_dir / "melody.mid").write_bytes(b"MThd\x00\x00\x00\x06")
    (out_dir / "notation").mkdir(exist_ok=True)
    (out_dir / "notation" / "song_melody.mid").write_bytes(b"MThd\x00\x00\x00\x06")


def make_client(tmp_path: Path, runners, **kwargs) -> tuple[TestClient, JobManager]:
    # 默认关闭上传体检：测试里的假 WAV 字节 ffmpeg 解不出，会误伤几十条用例；
    # G10 的测试单独传 `probe_upload=True`。
    kwargs.setdefault("probe_upload", False)
    app = create_app(data_root=tmp_path / "data", runners=runners, **kwargs)
    return TestClient(app), app.state.manager


def job_dir(manager: JobManager, job: Mapping) -> Path:
    """工程目录 = `output/<歌名>/`（就是产物目录，和命令行一个形状）。"""
    return manager.root / project_folder_name(str(job["name"]))


def job_internal(manager: JobManager, job: Mapping) -> Path:
    """WebUI 自己的东西（音频 / 缓存 / 打包）都在工程目录的 `.webui/` 下。"""
    return job_dir(manager, job) / ".webui"


def wait_stage(manager: JobManager, job_id: str, phase: str,
               timeout: float = 20.0) -> str:
    """等某个阶段跑完（假 runner 是同步的，轮询够了）。

    **走快照**（`snapshot()` 拿 `job.lock`）：直接读 `job.stages[...].state` 会和收尾
    线程抢，可能看到 done 而 `job.json` / 终态事件还没落。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job is None:
            return ""
        state = job.snapshot()["stages"][phase]["state"]
        if state in {"done", "failed", "cancelled", "idle"}:
            return state
        time.sleep(0.02)
    return "timeout"


def wait_for_state(client: TestClient, job_id: str, states: set[str],
                   timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["state"] in states:
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务没有在 {timeout}s 内进入 {states}：{body['state']}")


def wait_for_stage(client: TestClient, job_id: str, phase: str, states: set[str],
                   timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["stages"][phase]["state"] in states:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{phase} 没进入 {states}：{body['stages'][phase]['state']}")


def upload(client: TestClient, name: str = "测试歌曲.wav", data: bytes = WAV_BYTES,
           plan: str | None = None):
    payload: dict = {"file": (name, io.BytesIO(data), "audio/wav")}
    if plan is not None:
        payload["plan"] = (None, plan)
    return client.post("/api/jobs", files=payload)


def collect_events(client: TestClient, url: str, limit: int = 500) -> list[dict]:
    events: list[dict] = []
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            events.append(json.loads(line[6:]))
            if len(events) >= limit:
                break
            if events[-1]["type"] in ("done", "error", "cancelled"):
                break
    return events


def real_wav(tmp_path: Path, seconds: float = 0.5, rate: int = 8000) -> bytes:
    """真波形：ffmpeg 解得出。用 soundfile 生成一段带包络的正弦。"""
    import numpy as np
    import soundfile as sf

    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    wave = (0.4 * np.sin(2 * np.pi * 440 * t) * np.linspace(1.0, 0.1, t.size)).astype("float32")
    path = tmp_path / "tone.wav"
    sf.write(path, wave, rate, subtype="PCM_16")
    return path.read_bytes()


def make_job(**options) -> Job:
    """一个不落盘的 Job，只为测读参数的纯函数。"""
    job = Job(id="x", name="x", audio="x.wav", root=Path("."), upload=Path("x.wav"),
              out_dir=Path("out"), created_at=0.0)
    job.options.update(options)
    return job


def stem_upload(client: TestClient, job_id: str, name: str = "vocal.wav",
                data: bytes = WAV_BYTES, kind: str = "vocal"):
    return client.post(f"/api/jobs/{job_id}/stems/{kind}",
                       files={"file": (name, io.BytesIO(data), "audio/wav")})
