r"""WebUI 的接线测试：两阶段生命周期、SSE、卷帘数据、波形与下载安全。

**不加载任何模型**——`create_app(runners=...)` 允许把两个阶段都换成假实现，于是 HTTP 层、
队列、事件流、打包、Range 播放全都能在 CI 里跑。真实端到端见 `scripts/verify_webui.py`。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.bootstrap import find_ffmpeg  # noqa: E402
from sheetsage2_webui.webui.app import create_app  # noqa: E402
from sheetsage2_webui.webui.jobs import (  # noqa: E402
    PARAM_OWNER,
    ImportedProject,
    Job,
    JobManager,
    UploadRejected,
    default_options,
    param_choices,
    project_folder_name,
    safe_name,
    scan_files,
)
from sheetsage2_webui.webui.media import clamp_buckets, compute_peaks, parse_range  # noqa: E402
from sheetsage2_webui.webui.runner import (  # noqa: E402
    _sections_for,
    asr_model_choices,
    build_highlights,
    grid_note_name,
    group_lyric_lines,
    transcribe_options,
)

#: 新建工程的默认参数（参数面板上默认高亮的那一项）。
DEFAULT_OPTIONS = default_options()

WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64

#: 假歌词：两行，中间隔 ~2.8s（> LINE_GAP），用来验证分行与"边识别边出"。
FAKE_CHARS = [
    {"text": "晚", "start": 1.0, "end": 1.3},
    {"text": "风", "start": 1.3, "end": 1.6},
    {"text": "停", "start": 1.6, "end": 1.9},
    {"text": "在", "start": 1.9, "end": 2.2},
    {"text": "旧", "start": 5.0, "end": 5.3},
    {"text": "站", "start": 5.3, "end": 5.6},
    {"text": "台", "start": 5.6, "end": 5.9},
]


class Char:
    """`asr.AlignedChar` 的最小替身（group_lyric_lines 只读这三个属性）。"""

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text, self.start, self.end = text, start, end


# --------------------------------------------------------------------- 桩
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


#: 一份像样的 tick 域工程：120 BPM、两个 4/4 小节（1920 tick = 0.5s）。
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
    """等某个阶段跑完（假 runner 是同步的，轮询够了）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job is None:
            return ""
        state = job.stages[phase].state
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


# --------------------------------------------------------------------- 纯函数
def test_grid_note_name_maps_ticks_to_note():
    # 命名跟 Synthesizer V 的 SNAP 菜单对齐；量化口径 = 一拍/96
    assert grid_note_name(20, 1920) == "128 分三连音"
    assert grid_note_name(60, 1920) == "128 分音符"
    assert grid_note_name(80, 1920) == "64 分三连音"      # = 1/24 个四分音符
    assert grid_note_name(120, 1920) == "64 分音符"
    assert grid_note_name(160, 1920) == "32 分三连音"      # = 1/12 个四分音符
    assert grid_note_name(240, 1920) == "32 分音符"
    assert grid_note_name(320, 1920) == "16 分三连音"      # = 1/6 个四分音符
    assert grid_note_name(480, 1920) == "16 分音符"
    assert grid_note_name(None) == "未量化"
    # 不是 PPQ 的整数分之一时给个中性名字，别硬叫成某个音符
    assert grid_note_name(180, 1920) == "自定义网格"


def make_job(**options) -> Job:
    """一个不落盘的 Job，只为测读参数的纯函数。"""
    job = Job(id="x", name="x", audio="x.wav", root=Path("."), upload=Path("x.wav"),
              out_dir=Path("out"), created_at=0.0)
    job.options.update(options)
    return job


def test_transcribe_options_read_the_range_and_quantize_params():
    assert transcribe_options(make_job()) == {"quantize": True}          # 默认整首 + 量化
    assert transcribe_options(make_job(range="30"))["max_seconds"] == 30.0
    assert transcribe_options(make_job(range="60"))["max_seconds"] == 60.0
    assert transcribe_options(make_job(range="full")) == {"quantize": True}
    assert transcribe_options(make_job(quantize="off")) == {"quantize": False}
    # 音高修正不走参数：`pipeline.run` 的默认值就是固定降一个八度（虚拟歌姬不按原调调音）
    assert "pitch_shift" not in transcribe_options(make_job())
    assert "pitch" not in PARAM_OWNER


def test_asr_model_choices_lists_the_sibling_weights(tmp_path):
    root = tmp_path / "models"
    for name in ("Qwen3-ASR-1.7B", "Qwen3-ASR-0.6B"):
        (root / name).mkdir(parents=True)
    choices = asr_model_choices(root / "Qwen3-ASR-1.7B")
    assert sorted(choices) == ["0.6B", "1.7B"]
    assert choices["0.6B"].endswith("Qwen3-ASR-0.6B")

    lonely = tmp_path / "solo" / "Qwen3-ASR-1.7B"
    lonely.mkdir(parents=True)
    assert asr_model_choices(lonely) == {"1.7B": str(lonely)}


def test_param_choices_follow_the_servers_asr_model():
    """服务端 `--asr-model` 换了模型时，「模型」那一栏的默认值要跟着走，否则会被静默覆盖。"""
    models = {"0.6B": "/m/0.6B", "1.7B": "/m/1.7B"}
    row = param_choices(models, "Qwen3-ASR-0.6B")["lyrics"][0]
    assert row["default"] == "0.6B"
    assert [item["value"] for item in row["options"]] == ["0.6B", "1.7B"]   # 默认排最前

    # 没有 session（测试里的假 runner）/ 没给默认值时退回静态表
    assert param_choices(None)["lyrics"][0]["default"] == "1.7B"
    assert param_choices(models, None)["lyrics"][0]["default"] == "1.7B"
    # 「语言」那一栏永远是下拉框，选项来自 asr.LANGUAGE_CHOICES
    language = next(row for row in param_choices(None)["lyrics"]
                    if row["key"] == "language")
    assert language["widget"] == "select" and len(language["options"]) == 6


def test_group_lyric_lines_breaks_on_gap_and_length():
    chars = [Char(**item) for item in FAKE_CHARS]
    lines = group_lyric_lines(chars)
    assert [line["text"] for line in lines] == ["晚风停在", "旧站台"]
    assert lines[0]["start"] == 1.0 and lines[1]["end"] == 5.9

    # 一行太长要断开（显示单位，别把整段糊成一行）
    many = [Char("字", index * 0.1, index * 0.1 + 0.1) for index in range(40)]
    assert max(len(line["text"]) for line in group_lyric_lines(many)) <= 18


def test_build_highlights_narrows_the_payload():
    payload = {
        "summary": {"duration_seconds": 12.5, "elapsed_seconds": 3.25, "device": "cuda",
                    "peak_gpu_mib": 3904.0, "notes": {"melody": 7, "vocal": 5, "instrumental": 2},
                    "events": 9},
        "decisions": {"meter": "6/8", "pickup_seconds": 0.3, "bpm": 100.001,
                      "bpm_source": "abc_q", "tempo_constant": False, "tempo_segments": 2,
                      "keys": ["F:major"], "halftime_hint": 200.0},
        "project": {"quantize": {"enabled": True, "grid_ticks": 20, "moved": 4, "notes": 7,
                                 "moved_ratio": 0.5714, "failed_tracks": 0},
                    "quantize_shift_seconds": {"max": 0.45},
                    "chords": [{"label": "C"}, {"label": "F"}, {"label": "C"}],
                    "sections": [{"label": "intro"}]},
    }
    data = build_highlights(payload)
    assert data["meter"] == "6/8"
    # 网格与拍号无关：6/8 也是 20 tick（一拍的 1/96）
    assert data["quantize"]["grid_note"] == "128 分三连音"
    assert data["quantize"]["max_shift_seconds"] == 0.45
    assert data["notes"] == {"melody": 7, "vocal": 5, "instrumental": 2, "events": 9}
    assert data["chord_count"] == 3 and data["chord_kinds"] == 2
    assert data["section_count"] == 1

    merged = dict(payload, project={"quantize": {"enabled": True, "grid_ticks": 180,
                                                 "shift_seconds": {"max": 0.2}}})
    assert build_highlights(merged)["quantize"]["max_shift_seconds"] == 0.2


def test_safe_name_strips_directories_and_illegal_chars():
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name(r"C:\songs\歌?.flac") == "歌_.flac"
    assert safe_name("") == "audio"


def test_scan_files_reports_size_and_kind(tmp_path):
    write_artifacts(tmp_path)
    files = {item["path"]: item for item in scan_files(tmp_path)}
    assert files["project.json"]["kind"] == "data"
    assert files["melody.mid"]["kind"] == "midi"
    assert files["notation/song_melody.mid"]["size"] == 8


def test_parse_range_handles_the_three_shapes():
    assert parse_range(None, 1000) is None
    assert parse_range("bytes=0-99", 1000) == (0, 99)
    assert parse_range("bytes=100-", 1000) == (100, 999)
    assert parse_range("bytes=-100", 1000) == (900, 999)
    # 越界要报错，让调用方回 416，而不是悄悄给全量
    with pytest.raises(ValueError):
        parse_range("bytes=5000-6000", 1000)
    with pytest.raises(ValueError):
        parse_range("items=0-10", 1000)


def test_clamp_buckets_keeps_the_request_sane():
    assert clamp_buckets(None) == 1200
    assert clamp_buckets(1) == 32
    assert clamp_buckets(10**9) == 8000


# --------------------------------------------------------------------- HTTP
def test_env_reports_both_models_and_phases(tmp_path):
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        body = client.get("/api/env").json()
    assert [item["key"] for item in body["phases"]] == ["transcribe", "lyrics"]
    assert [item["engine"] for item in body["phases"]] == ["SheetSage2", "Qwen3-ASR"]
    assert body["static_ready"] is True
    assert Path(body["data_root"]) == manager.root
    assert body["max_upload_mb"] == 512.0


def test_unknown_api_path_returns_a_speakable_404(tmp_path):
    """`/api/*` 下没匹配上的路径不能落到静态处理器（那个只认 GET，会给个光秃秃的 405）。"""
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        missing = client.delete("/api/jobs/")
        typo = client.delete("/api/jobz/abc")
    for response in (missing, typo):
        assert response.status_code == 404
        body = response.json()
        assert "没有这个接口" in body["detail"]
        assert body["method"] == "DELETE"
        assert body["path"].startswith("/api/")


def test_lyrics_needs_transcribe_first(tmp_path):
    """工作流是 扒谱 → 歌词：没扒谱就点歌词，后端回 409 + "请先完成扒谱"。"""
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})

        # 场景一：工程刚建、扒谱还没开始 -> 拒绝
        fresh = upload(client, name="没扒谱.wav", plan="transcribe").json()
        blocked = client.post(f"/api/jobs/{fresh['id']}/stages/lyrics")
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "请先完成扒谱"

        # 场景二：扒谱完成了但还没填词 -> 允许（这是正常状态）
        allowed = client.post(f"/api/jobs/{job['id']}/stages/lyrics")
        assert allowed.status_code == 200
        done = wait_for_state(client, job["id"], {"done"})
        assert done["stages"]["lyrics"]["state"] == "done"


def test_chain_queues_lyrics_while_transcribe_is_still_running(tmp_path):
    """「自动提取」故意在扒谱没跑完时就排歌词——它走 /chain，不受那道门限制。

    用 `GateRunner` 把扒谱**卡在中途**，否则假 runner 太快，"正在跑"这个前提会一闪而过。
    """
    runner = GateRunner()
    client, _ = make_client(tmp_path, {"transcribe": runner, "lyrics": fake_lyrics})
    with client:
        job = upload(client, plan="transcribe").json()
        assert runner.entered.wait(timeout=10)
        chained = client.post(f"/api/jobs/{job['id']}/chain")
        assert chained.status_code == 200
        snapshot = chained.json()
        assert snapshot["stages"]["transcribe"]["state"] in ("queued", "running")
        assert snapshot["stages"]["lyrics"]["state"] in ("queued", "idle")
        runner.gate.set()
        done = wait_for_state(client, job["id"], {"done"})
        assert done["stages"]["transcribe"]["state"] == "done"
        assert done["stages"]["lyrics"]["state"] == "done"


def test_upload_defaults_to_transcribe_only(tmp_path):
    """（已改名，见下）——门禁之后默认什么都不跑，这里保留显式 `plan` 的行为。

    显式 `plan=transcribe` 仍然是"只跑扒谱、歌词留到以后"，用来验证计划本身没变。
    """
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        assert job["stages"]["lyrics"]["state"] == "idle"
        done = wait_for_state(client, job["id"], {"done"})
        assert done["stages"]["transcribe"]["state"] == "done"
        assert done["stages"]["lyrics"]["state"] == "idle"


def test_upload_only_builds_a_job_until_you_start_it(tmp_path):
    """**门禁**：拖入音频不立刻扒谱。

    上传只建工程（状态 `draft`，两个阶段都 `idle`），参数面板这时才露出来；
    用户点「开始扒谱」（`POST /stages/transcribe`）之后才真的排队。
    """
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client).json()
        assert job["state"] == "draft"
        assert job["stages"]["transcribe"]["state"] == "idle"
        assert job["stages"]["lyrics"]["state"] == "idle"
        assert job["options"] == DEFAULT_OPTIONS

        # 没排队：等一会儿也不会自己开跑
        time.sleep(0.4)
        assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "draft"
        assert manager.env()["queue"]["pending"] == 0

        # 用户点了才开始
        started = client.post(f"/api/jobs/{job['id']}/stages/transcribe").json()
        assert started["state"] in ("queued", "running")
        done = wait_for_state(client, job["id"], {"done"})
        assert done["stages"]["transcribe"]["state"] == "done"
        assert done["stages"]["lyrics"]["state"] == "idle"


def test_env_exposes_the_param_panel(tmp_path):
    """`/api/env` 把参数表发给前端：两组的行、默认值、以及"是否锁定"。"""
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        body = client.get("/api/env").json()
    params = body["params"]
    assert [item["key"] for item in params["transcribe"]] == \
        ["dtype", "device", "range", "quantize"]
    assert [item["key"] for item in params["lyrics"]] == \
        ["asr_model", "source", "skip_instrumental", "language"]
    assert [item["label"] for item in params["transcribe"][0]["options"]] == \
        ["BF16 快速", "FP32 精确"]
    # 音高修正是**固定**的（一律降一个八度），故意不给用户选
    assert "音高" not in [item["label"] for item in params["transcribe"]]
    # 「段落」：默认只跑唱段（跳过前奏/间奏/尾奏），两个取值都带悬停说明
    section_row = params["lyrics"][2]
    assert section_row["default"] == "vocal"
    assert [item["value"] for item in section_row["options"]] == ["vocal", "all"]
    assert all(item.get("hint") for item in section_row["options"])
    # 语言保留本项目的下拉框（原型是标签按钮），取值来自 asr.LANGUAGE_CHOICES
    language = params["lyrics"][3]
    assert language["widget"] == "select"
    assert [item["label"] for item in language["options"]][:3] == ["自动", "中文", "英语"]
    assert body["params_locked"] is False


def test_language_option_and_invalid_value(tmp_path):
    """识别语言：默认自动（空串），可选规范语言名（大小写也认），乱填 400。"""
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        assert job["options"]["language"] == ""            # 默认自动
        wait_for_state(client, job["id"], {"done"})

        changed = client.patch(f"/api/jobs/{job['id']}/options",
                               json={"language": "japanese"})
        assert changed.status_code == 200
        assert changed.json()["options"]["language"] == "Japanese"
        assert client.get("/api/env").json()["languages"][0]["label"] == "自动"

        bad = client.patch(f"/api/jobs/{job['id']}/options", json={"language": "Klingon"})
        assert bad.status_code == 400

        # 改语言会让已识别的歌词作废（换了识别口径）
        client.post(f"/api/jobs/{job['id']}/stages/lyrics")
        wait_for_stage(client, job["id"], "lyrics", {"done"})
        reset = client.patch(f"/api/jobs/{job['id']}/options", json={"language": ""}).json()
        assert reset["stages"]["lyrics"]["state"] == "idle"


def test_transcribe_params_invalidate_the_score_and_the_lyrics(tmp_path):
    """改扒谱参数（精度/设备/范围/量化/音高）= 换口径：谱面作废，歌词也作废。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        wait_for_state(client, job["id"], {"done"})

        changed = client.patch(f"/api/jobs/{job['id']}/options",
                               json={"quantize": "off", "dtype": "fp32"})
        assert changed.status_code == 200
        body = changed.json()
        assert body["options"]["quantize"] == "off"
        assert body["options"]["dtype"] == "fp32"
        assert body["stages"]["transcribe"]["state"] == "idle"
        assert body["stages"]["lyrics"]["state"] == "idle"

        # 事件流里记了一句"为什么作废"，别让人以为结果凭空消失了
        messages = [event.data.get("message", "") for event in manager.get(job["id"]).events]
        assert any("量化" in text and "作废" in text for text in messages)


def test_lyrics_params_only_invalidate_the_lyrics(tmp_path):
    """改歌词参数（模型/音源/段落）只作废歌词，谱面留着——扒谱那趟的口径没变。"""
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        wait_for_state(client, job["id"], {"done"})

        changed = client.patch(f"/api/jobs/{job['id']}/options",
                               json={"asr_model": "0.6B", "source": "mix",
                                     "skip_instrumental": "all"})
        assert changed.status_code == 200
        body = changed.json()
        assert body["options"]["skip_instrumental"] == "all"
        assert body["stages"]["transcribe"]["state"] == "done"      # 谱面不动
        assert body["stages"]["lyrics"]["state"] == "idle"          # 歌词作废


def test_skip_instrumental_param_is_validated(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client).json()
        assert job["options"]["skip_instrumental"] == "vocal"       # 默认跳过器乐段
        assert client.patch(f"/api/jobs/{job['id']}/options",
                            json={"skip_instrumental": "all"}).status_code == 200
        assert client.patch(f"/api/jobs/{job['id']}/options",
                            json={"skip_instrumental": "yes"}).status_code == 400


def test_sections_are_sliced_merged_and_translated(tmp_path):
    """歌词识别按**识别出来的段落**切片：相邻同名合并、标签翻中文、跳过器乐段。

    用假 runner 直接调 `_sections_for`——不加载模型也能验这个口径。
    """
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = client.post("/api/jobs", files={
            "file": ("段落.wav", io.BytesIO(WAV_BYTES), "audio/wav")}).json()
    record = manager.get(job["id"])
    record.out_dir.mkdir(parents=True, exist_ok=True)
    (record.out_dir / "structure.lab").write_text(
        "0\t20\tintro\n20\t30\tintro\n30\t50\tverse\n50\t70\tverse\n"
        "70\t90\tchorus\n90\t100\tinterlude\n100\t120\tchorus\n", encoding="utf-8")

    sections, skipped = _sections_for(record, {"duration_seconds": 120.0},
                                      max_seconds=30.0, skip_instrumental=True)
    assert [row.label for row in skipped] == ["前奏", "间奏"]
    assert all(row.seconds <= 30.0 for row in sections)
    # 跳过 0-30（前奏）与 90-100（间奏）之后，唱段是 30-90 与 100-120
    assert sections[0].start == 30.0
    assert sections[-1].end == 120.0

    every, none_skipped = _sections_for(record, {"duration_seconds": 120.0},
                                        max_seconds=30.0, skip_instrumental=False)
    assert none_skipped == []
    assert every[0].start == 0.0

    # 整首都是器乐段时回退到全部段落，别让任务死在"没有可用区间"上
    (record.out_dir / "structure.lab").write_text("0\t60\tinterlude\n",
                                                  encoding="utf-8")
    fallback, _ = _sections_for(record, {"duration_seconds": 60.0},
                                max_seconds=30.0, skip_instrumental=True)
    assert fallback and fallback[0].start == 0.0


def test_unknown_param_is_rejected(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client).json()
        assert client.patch(f"/api/jobs/{job['id']}/options",
                            json={"reverb": "on"}).status_code == 400
        assert client.patch(f"/api/jobs/{job['id']}/options",
                            json={"range": "整首"}).status_code == 400


def test_params_cannot_change_while_the_stage_runs(tmp_path):
    """扒谱参数在扒谱跑着的时候改不了：那一趟用的是改动前的值，改了界面会和结果对不上。"""
    runner = GateRunner()
    client, _ = make_client(tmp_path, {"transcribe": runner, "lyrics": fake_lyrics})
    with client:
        job = upload(client, plan="transcribe").json()
        assert runner.entered.wait(timeout=10)
        blocked = client.patch(f"/api/jobs/{job['id']}/options", json={"dtype": "fp32"})
        assert blocked.status_code == 409
        assert "扒谱正在跑" in blocked.json()["detail"]
        # 歌词参数不受影响（歌词阶段没在跑）
        assert client.patch(f"/api/jobs/{job['id']}/options",
                            json={"language": "English"}).status_code == 200
        runner.gate.set()
        wait_for_state(client, job["id"], {"done"})


def test_locked_params_are_read_only(tmp_path):
    """4G 特供：服务端把参数锁死，环境里报出来，改参数一律 409。"""
    client, _ = make_client(tmp_path, fake_runners(), lock_params=True)
    with client:
        assert client.get("/api/env").json()["params_locked"] is True
        job = upload(client).json()
        assert job["options"] == DEFAULT_OPTIONS
        blocked = client.patch(f"/api/jobs/{job['id']}/options", json={"dtype": "fp32"})
        assert blocked.status_code == 409
        assert "锁定" in blocked.json()["detail"]


def test_lyrics_stage_records_the_language_it_used(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        done = wait_for_state(client, job["id"], {"done"})
        detail = done["stages"]["lyrics"]["detail"]
        assert detail["language"] == "自动"
        assert "language_used" in detail


def test_upload_runs_both_stages_when_explicitly_planned(tmp_path):
    """显式要求两个阶段（一条龙）时，两个都跑完，产物齐全。"""
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        assert job["state"] in ("queued", "running")
        done = wait_for_state(client, job["id"], {"done"})

    stages = done["stages"]
    assert stages["transcribe"]["state"] == "done"
    assert stages["lyrics"]["state"] == "done"
    assert stages["lyrics"]["detail"]["lines"] == 2
    assert stages["lyrics"]["detail"]["assigned"] == 7
    assert stages["transcribe"]["detail"]["meter"] == "4/4"
    assert done["highlights"]["notes"]["vocal"] == 5
    assert [item["path"] for item in done["files"]] == [
        "lyrics.json", "melody.mid", "notation/song_melody.mid", "project.json",
        "report.json", "structure.lab", "summary.json"]


def test_plan_can_run_transcribe_only_then_lyrics_separately(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        settled = wait_for_state(client, job["id"], {"done"})
        assert settled["stages"]["transcribe"]["state"] == "done"
        assert settled["stages"]["lyrics"]["state"] == "idle"     # 没排进计划

        started = client.post(f"/api/jobs/{job['id']}/stages/lyrics").json()
        assert started["stages"]["lyrics"]["state"] == "queued"
        done = wait_for_state(client, job["id"], {"done"})
        assert done["stages"]["lyrics"]["state"] == "done"

        unknown = client.post(f"/api/jobs/{job['id']}/stages/drums")
        assert unknown.status_code == 400


def test_rerunning_transcribe_invalidates_lyrics(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        again = client.post(f"/api/jobs/{job['id']}/stages/transcribe").json()
        # 重新扒谱会让音符位置变，旧歌词必须作废，不能留在界面上骗人
        assert again["stages"]["lyrics"]["state"] in ("idle", "queued")
        assert again["rev"] == 2
        done = wait_for_state(client, job["id"], {"done"})
        assert done["stages"]["transcribe"]["state"] == "done"


def test_lyrics_stage_fails_clearly_without_a_project(tmp_path):
    def angry_lyrics(job, progress):
        progress.step("读取工程", 0.02)
        raise RuntimeError("还没有扒谱工程：请先跑「扒谱」")

    client, _ = make_client(tmp_path, {"transcribe": fake_transcribe,
                                       "lyrics": angry_lyrics})
    with client:
        job = upload(client, plan="lyrics").json()
        failed = wait_for_state(client, job["id"], {"failed"})
        assert "先跑「扒谱」" in failed["stages"]["lyrics"]["error"]


def test_upload_rejects_bad_extension_and_empty_file(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        bad = upload(client, name="歌词.txt", data=b"hello")
        assert bad.status_code == 400
        assert "不支持的文件类型" in bad.json()["detail"]

        empty = upload(client, name="空.wav", data=b"")
        assert empty.status_code == 400
        assert "空文件" in empty.json()["detail"]

        assert client.get("/api/jobs").json()["jobs"] == []


def test_upload_rejects_oversize(tmp_path):
    client, _ = make_client(tmp_path, fake_runners(), max_upload_mb=0.001)
    with client:
        response = upload(client, data=b"x" * 4096)
        assert response.status_code == 400
        assert "上限" in response.json()["detail"]


def test_unknown_job_is_404(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        for path in ("/api/jobs/nope", "/api/jobs/nope/project", "/api/jobs/nope/lyrics",
                     "/api/jobs/nope/peaks", "/api/jobs/nope/audio",
                     "/api/jobs/nope/download"):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/jobs/nope/cancel").status_code == 404
        assert client.delete("/api/jobs/nope").status_code == 404
        assert client.get("/api/jobs/nope/events").status_code == 404


def test_sse_streams_phase_step_lyrics_then_terminal(tmp_path):
    runner = GateRunner()
    client, _ = make_client(tmp_path, {"transcribe": runner, "lyrics": fake_lyrics})
    with client:
        job = upload(client, plan="transcribe").json()
        assert runner.entered.wait(timeout=10)

        collected: list[dict] = []

        def consume() -> None:
            collected.extend(collect_events(client, f"/api/jobs/{job['id']}/events"))

        thread = threading.Thread(target=consume)
        thread.start()
        time.sleep(0.3)
        runner.gate.set()
        thread.join(timeout=20)
        assert not thread.is_alive(), "SSE 流没有结束"

    types = [event["type"] for event in collected]
    assert "phase" in types and "step" in types and "progress" in types
    assert types[-1] == "done"
    assert all(event["data"].get("phase") == "transcribe"
               for event in collected if event["type"] in ("step", "progress"))
    sequences = [event["seq"] for event in collected]
    assert sequences == sorted(set(sequences))


def test_sse_carries_streamed_lyrics(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        # 两个阶段都要跑：歌词阶段是从扒谱产物里读工程的
        job = upload(client, plan="transcribe,lyrics").json()
        wait_for_state(client, job["id"], {"done"})
        events = collect_events(client, f"/api/jobs/{job['id']}/events")
    lyric_events = [event for event in events if event["type"] == "lyrics"]
    assert lyric_events, "歌词阶段没有推送实时歌词"
    first, last = lyric_events[0], lyric_events[-1]
    assert [line["text"] for line in first["data"]["lines"]] == ["晚风停在"]   # 一行行长出来
    assert last["data"]["text"] == "晚风停在旧站台"
    assert last["data"]["lines"][1]["text"] == "旧站台"
    assert last["data"]["phase"] == "lyrics"


def test_sse_replays_from_since(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        everything = collect_events(client, f"/api/jobs/{job['id']}/events")
        tail = collect_events(client,
                             f"/api/jobs/{job['id']}/events?since={everything[-2]['seq']}")
        assert [event["seq"] for event in tail] == [everything[-1]["seq"]]


def test_failed_stage_surfaces_the_error(tmp_path):
    def boom(job, progress):
        progress.step("识别音乐", 0.06)
        raise ValueError("显存不够了")

    client, _ = make_client(tmp_path, {"transcribe": boom, "lyrics": fake_lyrics})
    with client:
        job = upload(client, plan="transcribe").json()
        failed = wait_for_state(client, job["id"], {"failed"})
        assert "显存不够了" in (failed["stages"]["transcribe"]["error"] or "")
        events = collect_events(client, f"/api/jobs/{job['id']}/events")
        assert events[-1]["type"] == "error"


def test_project_and_lyrics_endpoints(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        wait_for_state(client, job["id"], {"done"})
        project = client.get(f"/api/jobs/{job['id']}/project").json()
        assert len(project["timeline"]["measures"]) == 2
        assert project["tracks"][0]["id"] == "vocal"
        body = client.get(f"/api/jobs/{job['id']}/lyrics").json()
        assert body["lines"][0] == {"text": "晚风停在", "start": 1.0, "end": 2.2}

        # 产物还没写出来时是 404（或正在写入时也是 404），不是 500
        thin = client.post("/api/jobs", files={
            "file": ("b.wav", io.BytesIO(WAV_BYTES), "audio/wav")}).json()
        assert client.get(f"/api/jobs/{thin['id']}/project").status_code in (404, 200)


def test_lanes_converts_ticks_to_seconds(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        wait_for_state(client, job["id"], {"done"})
        lanes = client.get(f"/api/jobs/{job['id']}/lanes").json()

    assert lanes["duration_seconds"] == 8.0
    assert [(row["label"], row["start"], row["end"]) for row in lanes["sections"]] == \
        [("intro", 0.0, 4.0), ("verse", 4.0, 8.0)]
    assert [(row["label"], row["start"], row["end"]) for row in lanes["chords"]] == \
        [("C", 0.0, 2.0), ("F", 2.0, 4.0)]
    assert len(lanes["measures"]) == 2 and lanes["measures"][0]["bpm"] == 120.0
    # 人声音符：转音标记不占"字"，但要保留音符
    assert [(note["start"], note["lyric"]) for note in lanes["vocal_notes"]] == \
        [(0.0, "晚"), (0.5, "")]
    assert lanes["keys"] == ["C:major"]
    assert lanes["lyrics_ready"] is True

    # 没有工程时是 404
    empty = client.post("/api/jobs", files={
        "file": ("c.wav", io.BytesIO(WAV_BYTES), "audio/wav"), "plan": (None, "transcribe")})
    assert client.get(f"/api/jobs/{empty.json()['id']}/lanes").status_code in (404, 200)


def test_download_single_file_and_bundle(tmp_path):
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        wait_for_state(client, job["id"], {"done"})

        single = client.get(f"/api/jobs/{job['id']}/files/report.json")
        assert single.status_code == 200
        assert single.content == b'{"decisions": {}}'
        assert client.get(
            f"/api/jobs/{job['id']}/files/notation/song_melody.mid").status_code == 200

        bundle = client.get(f"/api/jobs/{job['id']}/download")
        assert bundle.status_code == 200
        assert bundle.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            names = archive.namelist()
            assert archive.testzip() is None
        assert "测试歌曲/report.json" in names
        assert "测试歌曲/lyrics.json" in names

        # 第二次是缓存：不重新打包
        path = job_internal(manager, job) / "bundle.zip"
        stamp = path.stat().st_mtime_ns
        assert client.get(f"/api/jobs/{job['id']}/download").content == bundle.content
        assert path.stat().st_mtime_ns == stamp


# --------------------------------------------------------------------- 导出 SVP
def test_exports_lists_what_can_be_exported(tmp_path):
    """下载弹窗的数据源：能勾哪几组、各多少音、默认只勾人声。"""
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client).json()
        assert client.get(f"/api/jobs/{job['id']}/exports").status_code == 404  # 还没扒谱

        client.post(f"/api/jobs/{job['id']}/stages/transcribe")
        wait_for_state(client, job["id"], {"done"})
        body = client.get(f"/api/jobs/{job['id']}/exports").json()

    assert [row["key"] for row in body["parts"]] == ["vocal", "instrumental", "chords"]
    assert body["defaults"] == ["vocal"]                    # 默认勾人声、关乐器、关和弦
    by_key = {row["key"]: row for row in body["parts"]}
    assert by_key["vocal"]["notes"] == 2 and by_key["vocal"]["default"] is True
    assert by_key["instrumental"]["default"] is False
    assert by_key["chords"]["available"] is False            # 假工程没有 chords.mid
    assert body["svp"]["available"] is True
    assert body["svp"]["version"] == "1.9"


def test_download_svp_default_vocal_only(tmp_path):
    pytest.importorskip("libresvip", reason="可选依赖：pip install -e .[svp]")
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})

        response = client.get(f"/api/jobs/{job['id']}/download?kind=svp")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert ".svp" in response.headers["content-disposition"]
        assert response.content.startswith(b"{")            # SVP 是 JSON（1.x 末尾有个 \x00）
        assert b'"tracks"' in response.content
        # 缓存落在工程根目录的 export/ 下，重复点不重新生成
        cached = list((job_internal(manager, job) / "export").glob("*.svp"))
        assert len(cached) == 1
        stamp = cached[0].stat().st_mtime_ns
        assert client.get(f"/api/jobs/{job['id']}/download?kind=svp").content \
            == response.content
        assert cached[0].stat().st_mtime_ns == stamp


def test_download_svp_rejects_unknown_parts(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        bad = client.get(f"/api/jobs/{job['id']}/download?kind=svp&parts=vocal,drums")
        assert bad.status_code == 400
        assert "drums" in bad.json()["detail"]
        # 没扒谱时是 404（没有 project.json）
        fresh = upload(client, name="空工程.wav").json()
        missing = client.get(f"/api/jobs/{fresh['id']}/download?kind=svp")
        assert missing.status_code == 404


def test_download_rejects_path_traversal(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
    (tmp_path / "secret.txt").write_text("别看我", encoding="utf-8")
    response = client.get(f"/api/jobs/{job['id']}/files/..%2F..%2F..%2Fsecret.txt")
    assert response.status_code == 404


def test_cancel_queued_job_and_refuse_deleting_running_one(tmp_path):
    runner = GateRunner()
    client, manager = make_client(tmp_path, {"transcribe": runner, "lyrics": fake_lyrics})
    with client:
        first = upload(client, name="第一首.wav", plan="transcribe").json()
        assert runner.entered.wait(timeout=10)
        second = upload(client, name="第二首.wav", plan="transcribe").json()
        assert second["state"] == "queued"

        cancelled = client.post(f"/api/jobs/{second['id']}/cancel").json()
        assert cancelled["state"] == "cancelled"
        assert client.get(f"/api/jobs/{second['id']}").json()["state"] == "cancelled"

        # 正在跑的任务不能删（否则会把它脚下的产物目录抽走）
        assert client.delete(f"/api/jobs/{first['id']}").status_code == 409

        runner.gate.set()
        wait_for_state(client, first["id"], {"done"})

        assert client.delete(f"/api/jobs/{first['id']}").status_code == 200
        assert client.get(f"/api/jobs/{first['id']}").status_code == 404
        assert not job_dir(manager, first).exists()


def test_cancel_running_stage_stops_at_the_next_checkpoint(tmp_path):
    runner = GateRunner()
    client, _ = make_client(tmp_path, {"transcribe": runner, "lyrics": fake_lyrics})
    with client:
        job = upload(client, plan="transcribe").json()
        assert runner.entered.wait(timeout=10)
        assert client.post(f"/api/jobs/{job['id']}/cancel").json()["state"] == "running"
        runner.gate.set()
        stopped = wait_for_state(client, job["id"], {"cancelled"})
        assert stopped["stages"]["transcribe"]["state"] == "cancelled"


def test_job_metadata_is_written_next_to_the_artifacts(tmp_path):
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
    meta = json.loads((job_dir(manager, job) / "job.json").read_text(encoding="utf-8"))
    assert meta["state"] == "done"
    assert meta["audio"] == "测试歌曲.wav"
    assert meta["stages"]["transcribe"]["state"] == "done"
    assert "output_dir" in meta


def test_manager_create_writes_and_cleans_up_rejected_upload(tmp_path):
    manager = JobManager(tmp_path / "data", runners=fake_runners(), max_upload_mb=0.001)
    try:
        with pytest.raises(UploadRejected):
            manager.create("big.wav", io.BytesIO(b"x" * 100_000))
        # 半截工程目录要清干净（工程目录 = output/<歌名>/）
        assert list((tmp_path / "data").iterdir()) == []
    finally:
        manager.shutdown()


# --------------------------------------------------------------------- 工程列表
def test_project_folder_name_matches_the_cli_slug():
    """WebUI 建的目录必须和命令行建的**同名同形状**，两边才会互相认。"""
    from sheetsage2_webui.cli import _slug

    for stem in ("凤凰传奇 - 光芒", "门尼 - 巴拉莱卡（The Rod）", "song 01", "…"):
        assert project_folder_name(stem) == _slug(Path(stem))
    assert project_folder_name("") == "audio"


def test_project_folder_is_the_output_dir_and_internals_are_hidden(tmp_path):
    """工程目录 = `output/<歌名>/`：产物直接躺里面，WebUI 自己的东西在 `.webui/`。"""
    manager = JobManager(tmp_path / "output", runners=fake_runners())
    try:
        job = manager.create("晚风.wav", io.BytesIO(b"x" * 1000))
        root = tmp_path / "output" / "晚风"
        assert job.root == root and job.out_dir == root
        assert (root / "job.json").is_file()
        # 输入文件带 uuid 前缀（G9）：展示名仍存 job.audio，落盘名 = `<job_id>-<原名>`。
        input_files = list((root / ".webui" / "input").iterdir())
        assert len(input_files) == 1 and input_files[0].name.endswith("-晚风.wav")

        write_artifacts(root)
        listing = {item["path"] for item in scan_files(root)}
        assert "project.json" in listing
        assert "job.json" not in listing                    # 元数据不算产物
        assert not any(item.startswith(".webui") for item in listing)
    finally:
        manager.shutdown()


def test_projects_survive_a_restart(tmp_path):
    """**重启不该丢工程**：新的 JobManager 从 output/ 里把 job.json 读回来。"""
    root = tmp_path / "output"
    first = JobManager(root, runners=fake_runners())
    try:
        job = first.create("晚风.wav", io.BytesIO(b"x" * 1000))
        first.set_options(job.id, quantize="off")           # 先改参数，再跑（改参数会作废结果）
        first.run_stage(job.id, "transcribe")
        assert wait_stage(first, job.id, "transcribe") == "done"
        snapshot = first.get(job.id).snapshot()
    finally:
        first.shutdown()

    again = JobManager(root, runners=fake_runners())
    try:
        jobs = again.list()
        assert [item.id for item in jobs] == [job.id]
        restored = jobs[0]
        assert restored.name == snapshot["name"]
        assert restored.options["quantize"] == "off"        # 参数也跟着回来了
        assert restored.stages["transcribe"].state == "done"
        assert {item["path"] for item in restored.files} >= {"project.json", "report.json"}
        assert restored.highlights is not None or restored.imported is False
    finally:
        again.shutdown()


def test_interrupted_stage_is_not_reported_as_running(tmp_path):
    """上次跑到一半就退出：恢复后要说"被中断"，不能装成还在跑。"""
    root = tmp_path / "output"
    gate = GateRunner()
    first = JobManager(root, runners={"transcribe": gate, "lyrics": fake_lyrics})
    try:
        job = first.create("卡住.wav", io.BytesIO(b"x" * 1000), plan=["transcribe"])
        assert gate.entered.wait(timeout=20)
        # 这时阶段是 running，直接把管理器丢掉（模拟"关掉程序"）
        meta_before = json.loads((job.root / "job.json").read_text(encoding="utf-8"))
        assert meta_before["stages"]["transcribe"]["state"] in {"queued", "running"}
        first.shutdown(timeout=0.1)
        gate.gate.set()
    finally:
        gate.gate.set()

    again = JobManager(root, runners=fake_runners())
    try:
        restored = again.get(job.id)
        assert restored is not None
        assert restored.stages["transcribe"].state == "idle"
        assert "中断" in restored.stages["transcribe"].message
        assert restored.state == "draft"
    finally:
        again.shutdown()


def test_output_folders_created_elsewhere_show_up_readonly(tmp_path):
    """`output/` 里本来就有的产物（命令行跑的）要进列表，但**只读**。"""
    root = tmp_path / "output"
    folder = root / "凤凰传奇_-_光芒"
    folder.mkdir(parents=True)
    (folder / "project.json").write_text(json.dumps(PROJECT_STUB), encoding="utf-8")
    (folder / "report.json").write_text('{"decisions": {}}', encoding="utf-8")
    (folder / "summary.json").write_text('{"duration_seconds": 8.0, "notes": {"vocal": 2}}',
                                         encoding="utf-8")
    (root / "空目录").mkdir()                               # 什么都没有 -> 不该进列表

    manager = JobManager(root, runners=fake_runners())
    try:
        assert [job.name for job in manager.list()] == ["凤凰传奇_-_光芒"]
        job = manager.get("凤凰传奇_-_光芒")
        assert job is not None and job.imported
        assert job.stages["transcribe"].state == "done"      # 有 project.json
        assert job.stages["lyrics"].state == "idle"          # 没有 lyrics.json
        assert job.highlights is not None                    # 从产物里补出来的
        assert "project.json" in {item["path"] for item in job.files}

        # 只读：重跑 / 改参数 / 删 全都不行
        for action in (lambda: manager.run_stage(job.id, "transcribe"),
                       lambda: manager.run_chain(job.id),
                       lambda: manager.set_options(job.id, quantize="off"),
                       lambda: manager.delete(job.id)):
            with pytest.raises(ImportedProject):
                action()
        # 而且**不许**往里写 job.json（那就把别人的产物变成 WebUI 工程了）
        assert not (folder / "job.json").exists()
    finally:
        manager.shutdown()


def test_imported_project_endpoints_are_read_only(tmp_path):
    """只读工程在 API 层：列表里有、能看工程、不能重跑、没有原音频。"""
    root = tmp_path / "data"
    folder = root / "命令行跑的"
    folder.mkdir(parents=True)
    (folder / "project.json").write_text(json.dumps(PROJECT_STUB), encoding="utf-8")

    client, manager = make_client(tmp_path, fake_runners())
    with client:
        listing = client.get("/api/jobs").json()["jobs"]
        assert [item["name"] for item in listing] == ["命令行跑的"]
        assert listing[0]["imported"] is True

        detail = client.get("/api/jobs/命令行跑的").json()
        assert detail["imported"] is True
        assert detail["stages"]["transcribe"]["state"] == "done"
        assert client.get("/api/jobs/命令行跑的/project").status_code == 200

        assert client.post("/api/jobs/命令行跑的/stages/transcribe").status_code == 409
        assert client.post("/api/jobs/命令行跑的/chain").status_code == 409
        assert client.patch("/api/jobs/命令行跑的/options",
                            json={"quantize": "off"}).status_code == 409
        assert client.delete("/api/jobs/命令行跑的").status_code == 409
        assert client.get("/api/jobs/命令行跑的/audio").status_code == 404
        assert client.get("/api/jobs/命令行跑的/files/job.json").status_code == 404
        # 没有原音频时波形是 404，不是 500（以前会一路炸到 ffmpeg 解码）
        peaks = client.get("/api/jobs/命令行跑的/peaks?buckets=64")
        assert peaks.status_code == 404, peaks.text
        assert "原音频" in peaks.json()["detail"]
    manager.shutdown()


def test_favicon_does_not_404(tmp_path):
    """浏览器总来要一次图标，回 204 就行（不然日志里天天一行 404）。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        assert client.get("/favicon.ico").status_code == 204
    manager.shutdown()



# --------------------------------------------------------------------- 音频
@pytest.mark.skipif(not find_ffmpeg(), reason="需要 ffmpeg 解码")
def test_peaks_and_audio_range(tmp_path):
    wave = real_wav(tmp_path)
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, name="tone.wav", data=wave, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})

        peaks = client.get(f"/api/jobs/{job['id']}/peaks?buckets=64").json()
        assert peaks["buckets"] == 64
        assert len(peaks["peaks"]) == 64
        assert max(peaks["peaks"]) > 0.1 and max(peaks["peaks"]) <= 1.0
        # 生成的是渐弱正弦：结尾必须比开头小
        assert peaks["peaks"][-1] < peaks["peaks"][0]
        assert (job_internal(manager, job) / "peaks-mix-64.json").is_file()

        whole = client.get(f"/api/jobs/{job['id']}/audio")
        assert whole.status_code == 200
        assert whole.headers["accept-ranges"] == "bytes"
        assert whole.content == wave

        part = client.get(f"/api/jobs/{job['id']}/audio", headers={"Range": "bytes=0-99"})
        assert part.status_code == 206
        assert part.headers["content-range"] == f"bytes 0-99/{len(wave)}"
        assert part.content == wave[:100]

        bad = client.get(f"/api/jobs/{job['id']}/audio",
                         headers={"Range": f"bytes={len(wave) + 10}-"})
        assert bad.status_code == 416
        assert bad.headers["content-range"] == f"bytes */{len(wave)}"


@pytest.mark.skipif(not find_ffmpeg(), reason="需要 ffmpeg 解码")
def test_compute_peaks_on_a_real_file(tmp_path):
    path = tmp_path / "tone.wav"
    path.write_bytes(real_wav(tmp_path))
    data = compute_peaks(path, 32)
    assert data["buckets"] == 32 and data["duration"] == pytest.approx(0.5, abs=0.05)


# --------------------------------------------------------------------- 干声（音源）
def stem_upload(client: TestClient, job_id: str, name: str = "vocal.wav",
                data: bytes = WAV_BYTES, kind: str = "vocal"):
    return client.post(f"/api/jobs/{job_id}/stems/{kind}",
                       files={"file": (name, io.BytesIO(data), "audio/wav")})


def test_lyrics_uses_the_stem_when_uploaded_and_falls_back_without_it(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        # 工作流是 扒谱 → 歌词：等扒谱完成再点歌词（界面上那个按钮此时才放开）
        wait_for_state(client, job["id"], {"done"})
        # 没有干声：自动回退到混音原曲
        assert client.post(f"/api/jobs/{job['id']}/stages/lyrics").status_code == 200
        mixed = wait_for_stage(client, job["id"], "lyrics", {"done"})
        assert mixed["stages"]["lyrics"]["detail"]["source"] == "mix"
        assert mixed["stages"]["lyrics"]["detail"]["source_file"] == job["audio"]
        assert mixed["stems"] == {}

        # 传一条干声：歌词阶段改用它（内容不校验，传混音原曲也允许）
        uploaded = stem_upload(client, job["id"], name="我的干声.flac")
        assert uploaded.status_code == 201
        snapshot = uploaded.json()
        assert snapshot["stems"]["vocal"]["name"] == "我的干声.flac"
        assert snapshot["stages"]["lyrics"]["state"] == "idle"      # 音源变了，旧结果作废

        client.post(f"/api/jobs/{job['id']}/stages/lyrics")
        with_stem = wait_for_stage(client, job["id"], "lyrics", {"done"})
        assert with_stem["stages"]["lyrics"]["detail"]["source"] == "stem"
        assert with_stem["stages"]["lyrics"]["detail"]["source_file"] == "我的干声.flac"

        # 移除干声：又回退到混音原曲
        removed = client.delete(f"/api/jobs/{job['id']}/stems/vocal").json()
        assert removed["stems"] == {}
        assert removed["stages"]["lyrics"]["state"] == "idle"


def test_stem_upload_rejects_bad_input_and_running_job(tmp_path):
    gate = GateRunner()

    def lyrics(job, progress):
        # 只有"跑着的"那首卡在歌词阶段，用来验证这时候不许动音源
        if job.name == "跑着的":
            progress.step("语音识别", 0.10)
            gate.entered.set()
            if not gate.gate.wait(timeout=20):
                raise RuntimeError("测试没有放行")
        return fake_lyrics(job, progress)

    client, _ = make_client(tmp_path, {"transcribe": fake_transcribe, "lyrics": lyrics})
    with client:
        # 后缀不对 / 空文件 / 未知音源
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        assert stem_upload(client, job["id"], name="歌词.txt", data=b"x").status_code == 400
        assert stem_upload(client, job["id"], name="空.wav", data=b"").status_code == 400
        assert client.post(f"/api/jobs/{job['id']}/stems/drums",
                           files={"file": ("a.wav", io.BytesIO(WAV_BYTES), "audio/wav")}
                           ).status_code == 400
        # 扒谱在跑（或已跑完）时换音源是允许的：干声跟扒谱无关
        assert stem_upload(client, job["id"], name="先传一条.wav").status_code == 201

        # 歌词识别在跑的时候不许动（那条音频正被切片读着）
        busy = upload(client, name="跑着的.wav", plan="transcribe,lyrics").json()
        assert gate.entered.wait(timeout=10)
        assert busy["stages"]["lyrics"]["state"] in ("queued", "running")
        assert stem_upload(client, busy["id"]).status_code == 409
        assert client.delete(f"/api/jobs/{busy['id']}/stems/vocal").status_code == 409
        gate.gate.set()
        wait_for_state(client, busy["id"], {"done"})


def test_stem_audio_supports_range_and_its_own_peaks(tmp_path):
    wave = real_wav(tmp_path)
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_stage(client, job["id"], "transcribe", {"done"})
        assert client.get(f"/api/jobs/{job['id']}/stems/vocal/audio").status_code == 404
        assert client.get(f"/api/jobs/{job['id']}/peaks?source=stem").status_code == 404

        assert stem_upload(client, job["id"], name="vocal.wav", data=wave).status_code == 201
        whole = client.get(f"/api/jobs/{job['id']}/stems/vocal/audio")
        assert whole.status_code == 200 and whole.content == wave
        part = client.get(f"/api/jobs/{job['id']}/stems/vocal/audio",
                          headers={"Range": "bytes=10-19"})
        assert part.status_code == 206 and part.content == wave[10:20]

        if find_ffmpeg():
            peaks = client.get(f"/api/jobs/{job['id']}/peaks?source=stem&buckets=32").json()
            assert peaks["buckets"] == 32 and max(peaks["peaks"]) > 0.1


# --------------------------------------------------------------------- 健壮性（P0）
# 这一节对应 `docs/健壮性保障评估.md` 的 G1~G7（第一期）。每条缺口一条测试，
# 全部走假 runner，不加载任何模型。
def test_delete_goes_to_the_trash_and_can_be_restored(tmp_path):
    """G2：删除是**软删除**——进回收站，能恢复，产物和参数都回来。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        root = job_dir(manager, job)
        assert (root / "project.json").is_file()

        assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
        assert client.get(f"/api/jobs/{job['id']}").status_code == 404
        assert not root.exists()                            # 目录已经挪走

        entries = client.get("/api/trash").json()["entries"]
        assert len(entries) == 1
        assert entries[0]["name"] == job["name"]
        assert entries[0]["files"] > 0

        restored = client.post(f"/api/trash/{entries[0]['id']}/restore")
        assert restored.status_code == 200
        assert restored.json()["id"] == job["id"]
        assert job_dir(manager, job).is_dir()
        assert client.get(f"/api/jobs/{job['id']}/files/project.json").status_code == 200
        assert client.get("/api/trash").json()["entries"] == []


def test_delete_with_permanent_flag_skips_the_trash(tmp_path):
    """G2：用户确认"彻底删除"时才是真删（不进回收站）。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        response = client.delete(f"/api/jobs/{job['id']}", params={"permanent": "true"})
        assert response.status_code == 200
        assert client.get("/api/trash").json()["entries"] == []
        assert not job_dir(manager, job).exists()


# --------------------------------------------------- 回收站的保留策略（7 天 / 2GB）
def _trash_entry(manager, name: str) -> Path:
    """按工程名找到它在 `.trash/` 里的目录（条目名带工程名和 id）。"""
    for entry in manager.trash_root.iterdir():
        if name in entry.name:
            return entry
    raise AssertionError(f"回收站里没有 {name}")


def _age(path: Path, days: float) -> None:
    """把目录的修改时间往前拨——`_purge_trash` 判断过期用的就是它。"""
    stamp = time.time() - days * 86400.0
    os.utime(path, (stamp, stamp))


def test_trash_purge_keeps_entries_inside_the_age_window(tmp_path):
    """6 天前删的还在 7 天窗口里，不能被清掉。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, name="六天前.wav", plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        client.delete(f"/api/jobs/{job['id']}")
        _age(_trash_entry(manager, "六天前"), 6)
        manager._purge_trash()                      # 等价于"重启一次服务"
        assert len(client.get("/api/trash").json()["entries"]) == 1


def test_trash_purge_drops_entries_older_than_seven_days(tmp_path):
    """8 天前删的会被真删——不用等七天，把 mtime 拨回去就行。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, name="八天前.wav", plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        client.delete(f"/api/jobs/{job['id']}")
        entry = _trash_entry(manager, "八天前")
        _age(entry, 8)
        manager._purge_trash()
        assert not entry.exists()
        assert client.get("/api/trash").json()["entries"] == []


def test_trash_purge_evicts_the_oldest_when_over_the_size_cap(tmp_path):
    """总量超上限就砍最旧的——把上限调小即可验，不用真堆 2GB。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        for name in ("旧的.wav", "新的.wav"):
            job = upload(client, name=name, plan="transcribe").json()
            wait_for_state(client, job["id"], {"done"})
            client.delete(f"/api/jobs/{job['id']}")
        old_entry = _trash_entry(manager, "旧的")
        new_entry = _trash_entry(manager, "新的")
        _age(old_entry, 1)
        _age(new_entry, 0)

        def size_of(path: Path) -> int:
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

        # 上限卡在"只装得下新的那一份"，于是砍掉旧的之后刚好不超
        manager.trash_max_bytes = size_of(new_entry) + 1
        manager._purge_trash()
        assert not old_entry.exists()               # 最旧的先被砍
        assert new_entry.exists()                   # 剩下的那份还在窗口里
        assert [row["name"] for row in client.get("/api/trash").json()["entries"]] == [
            "新的"]


def test_trash_purge_also_runs_on_a_timer(tmp_path):
    """不重启、也不再删东西：后台定时器到点就会清掉过期条目。

    没有这条定时的话，「7 天自动清」只在启动和删除时生效——服务连开一周而用户
    一个工程都没删，过期条目会一直躺在 `.trash/` 里。
    """
    client, manager = make_client(tmp_path, fake_runners(), trash_purge_interval=0.05)
    with client:
        job = upload(client, name="定时清理.wav", plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        client.delete(f"/api/jobs/{job['id']}")
        entry = _trash_entry(manager, "定时清理")
        _age(entry, 8)                              # 已经过期，但没有触发任何删除
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and entry.exists():
            time.sleep(0.05)
        assert not entry.exists()


def test_rerunning_rebuilds_the_bundle_and_bumps_the_artifact_rev(tmp_path):
    """G3：重跑之后下载到的**一定是新结果**（缓存键带产物版本戳）。"""
    marker = {"tag": "v1"}

    def transcribe(job, progress):
        write_artifacts(job.out_dir)
        (job.out_dir / "report.json").write_text(
            json.dumps({"decisions": {}, "tag": marker["tag"]}), encoding="utf-8")
        progress.step("整理产物", 0.98)
        return {"detail": {}}

    client, _ = make_client(tmp_path, {"transcribe": transcribe, "lyrics": fake_lyrics})
    with client:
        job = upload(client, name="版本.wav", plan="transcribe").json()
        first = wait_for_state(client, job["id"], {"done"})
        assert first["artifacts_rev"] == 1
        zip_one = client.get(f"/api/jobs/{job['id']}/download")
        assert zip_one.headers["x-artifact-rev"] == "1"

        marker["tag"] = "v2"
        assert client.post(f"/api/jobs/{job['id']}/stages/transcribe").status_code == 200
        again = wait_for_state(client, job["id"], {"done"})
        assert again["artifacts_rev"] == 2
        zip_two = client.get(f"/api/jobs/{job['id']}/download")
        assert zip_two.headers["x-artifact-rev"] == "2"

    with zipfile.ZipFile(io.BytesIO(zip_one.content)) as archive:
        assert b'"v1"' in archive.read("版本/report.json")
    with zipfile.ZipFile(io.BytesIO(zip_two.content)) as archive:
        assert b'"v2"' in archive.read("版本/report.json")


def test_svp_cache_key_follows_the_artifact_rev():
    """G3：SVP 的缓存键里带产物版本戳，重跑后不会发旧文件。"""
    from sheetsage2_webui.svp import cache_key

    base = cache_key(["vocal"], "1.9", {"language": ""})
    bumped = cache_key(["vocal"], "1.9", {"language": ""}, rev=3)
    assert base != bumped
    assert bumped == cache_key(["vocal"], "1.9", {"language": ""}, rev=3)


@pytest.mark.skipif(not find_ffmpeg(), reason="需要 ffmpeg 解码")
def test_peaks_cache_is_invalidated_when_the_audio_changes(tmp_path):
    """G3：同一条音源换了内容（换干声）之后，波形缓存必须跟着失效。"""
    from sheetsage2_webui.webui.media import peaks_for

    audio = tmp_path / "tone.wav"
    audio.write_bytes(real_wav(tmp_path))
    first = peaks_for(tmp_path, audio, 32)
    assert first["buckets"] == 32

    time.sleep(0.02)                                   # 让 mtime 变得可分辨
    audio.write_bytes(real_wav(tmp_path, seconds=0.9))
    second = peaks_for(tmp_path, audio, 32)
    assert second["source"] != first["source"]         # 指纹变了 = 真的重算了
    assert second["duration"] == pytest.approx(0.9, abs=0.05)


def test_incomplete_artifacts_are_not_reported_as_done(tmp_path):
    """G4：半成品不许标成完成（缺产物 → 阶段 failed，并说缺什么）。"""
    def thin(job, progress):
        progress.step("整理产物", 0.98)
        (job.out_dir / "project.json").write_text(json.dumps(PROJECT_STUB), encoding="utf-8")
        return {"detail": {}}

    client, _ = make_client(tmp_path, {"transcribe": thin, "lyrics": fake_lyrics})
    with client:
        job = upload(client, plan="transcribe").json()
        failed = wait_for_state(client, job["id"], {"failed"})
        stage = failed["stages"]["transcribe"]
        assert stage["state"] == "failed"
        assert stage["error_code"] == "E_ARTIFACTS_INCOMPLETE"
        assert "产物不完整" in stage["error"]
        assert "report.json" in stage["error"]


def test_the_runners_own_artifact_report_blocks_completion(tmp_path):
    """G4：runner 的体检（`detail.artifacts.ok=False`）同样能拦住"完成"。"""
    def degraded(job, progress):
        write_artifacts(job.out_dir)
        return {"detail": {"artifacts": {"ok": False, "missing": ["chords.mid"],
                                         "problems": ["缺少 1 个产物文件"]}}}

    client, _ = make_client(tmp_path, {"transcribe": degraded, "lyrics": fake_lyrics})
    with client:
        job = upload(client, plan="transcribe").json()
        failed = wait_for_state(client, job["id"], {"failed"})
        stage = failed["stages"]["transcribe"]
        assert stage["error_code"] == "E_ARTIFACTS_INCOMPLETE"
        assert "chords.mid" in stage["error"]


def test_atomic_write_never_leaves_a_part_file(tmp_path):
    """G5：JSON 落盘是"先写 .part 再 rename"，不会留下半截文件。"""
    from sheetsage2_webui.fsutil import write_text_atomic

    target = tmp_path / "project.json"
    write_text_atomic(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    assert list(tmp_path.glob("*.part")) == []
    assert not list(tmp_path.glob(".*.part"))


def test_half_written_project_json_is_404_not_500(tmp_path):
    """G5：产物被写了一半时，接口给"还没准备好"，不是 500。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        (job_dir(manager, job) / "project.json").write_text('{"timeline":', encoding="utf-8")
        assert client.get(f"/api/jobs/{job['id']}/project").status_code == 404


def test_failed_rerun_does_not_leave_the_old_lyrics_in_output(tmp_path):
    """G5：重跑扒谱失败在中途时，旧歌词不能和这一轮的新产物混在一起。"""
    boom = {"on": False}

    def transcribe(job, progress):
        if boom["on"]:
            raise RuntimeError("磁盘满了，写不进去")
        return fake_transcribe(job, progress)

    client, manager = make_client(tmp_path, {"transcribe": transcribe, "lyrics": fake_lyrics})
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        wait_for_state(client, job["id"], {"done"})
        assert (job_dir(manager, job) / "lyrics.json").is_file()

        boom["on"] = True
        assert client.post(f"/api/jobs/{job['id']}/stages/transcribe").status_code == 200
        wait_for_state(client, job["id"], {"failed"})
        # 旧歌词被挪进了 `.webui/prev/`：磁盘上还留着，但不属于"当前这一版"
        assert not (job_dir(manager, job) / "lyrics.json").exists()
        assert list((job_internal(manager, job) / "prev").rglob("lyrics.json"))


def test_a_stalled_stage_is_failed_by_the_watchdog(tmp_path):
    """G6：一个回调都不来 = 卡死；看门狗判失败并给人话，不是永远转圈。"""
    def stuck(job, progress):
        progress.step("识别音乐", 0.06)
        time.sleep(2.0)                                # 故意不给任何进展
        return {"detail": {}}

    client, _ = make_client(tmp_path, {"transcribe": stuck, "lyrics": fake_lyrics},
                            stall_timeout=0.3, watchdog_interval=0.05)
    with client:
        job = upload(client, plan="transcribe").json()
        failed = wait_for_state(client, job["id"], {"failed"}, timeout=15)
        stage = failed["stages"]["transcribe"]
        assert stage["error_code"] == "E_STALLED"
        assert "卡住" in stage["error"]
        assert stage["error_action"]


def test_a_stage_that_exceeds_the_hard_timeout_is_failed(tmp_path):
    """G6：总时长超过上限也判失败（有进展但永远跑不完的僵尸任务）。"""
    def slow(job, progress):
        for _ in range(60):
            progress.set(min(0.9, progress.run.progress + 0.01), "还在跑")
            time.sleep(0.05)
        return {"detail": {}}

    client, _ = make_client(tmp_path, {"transcribe": slow, "lyrics": fake_lyrics},
                            stall_timeout=30.0, hard_timeout=0.3,
                            watchdog_interval=0.05)
    with client:
        job = upload(client, plan="transcribe").json()
        failed = wait_for_state(client, job["id"], {"failed"}, timeout=15)
        stage = failed["stages"]["transcribe"]
        assert stage["error_code"] == "E_TIMEOUT"
        assert "超过上限" in stage["error"]


def test_errors_are_translated_into_human_text_with_a_code(tmp_path):
    """G7：失败要带上"错误码 + 人话 + 下一步 + 技术原文"，人话里不出现异常类名。"""
    def no_space(job, progress):
        raise OSError(28, "No space left on device")

    client, _ = make_client(tmp_path, {"transcribe": no_space, "lyrics": fake_lyrics})
    with client:
        job = upload(client, plan="transcribe").json()
        failed = wait_for_state(client, job["id"], {"failed"})
        stage = failed["stages"]["transcribe"]
        assert stage["error_code"] == "E_DISK_FULL"
        assert "磁盘满" in stage["error"]
        assert "OSError" not in stage["error"]
        assert stage["error_action"]
        assert "No space left" in stage["error_detail"]
        events = collect_events(client, f"/api/jobs/{job['id']}/events")
        assert events[-1]["type"] == "error"


def test_error_table_maps_common_failures():
    """G7：错误码表的判定（不加载模型，纯函数）。"""
    from sheetsage2_webui.webui.errors import describe

    assert describe(OSError(28, "No space left on device")).code == "E_DISK_FULL"
    assert describe(RuntimeError("CUDA out of memory")).code == "E_VRAM_OOM"
    assert describe(RuntimeError("没有拿到任何逐字时间戳，填词无法进行")).code == "E_ASR_EMPTY"
    assert describe(RuntimeError("解码音频失败：Invalid data found")).code == "E_AUDIO_UNREADABLE"

    weird = describe(KeyError("boom"))
    assert weird.code.startswith("E_INTERNAL-")
    assert weird.code == describe(KeyError("boom")).code        # 同一个 bug 同一个码
    assert weird.code != describe(KeyError("bang")).code
    assert "错误码" in weird.message
