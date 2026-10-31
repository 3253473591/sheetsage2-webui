r"""P1 缺口的接线测试（G8-G17，除 G16 是前端外）。

**不加载任何模型**——继续沿用 `create_app(runners=...)` 的假 runner 装置。真实端到端见
`scripts/verify_webui.py`。

    .\.venv\Scripts\python.exe -m pytest tests/test_webui_p1.py -q
"""

from __future__ import annotations

import io
import json
import sys
import threading
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
    JobManager,
    StaleRevision,
    UploadRejected,
    _bytes_text,
    claim_project_dir,
    content_fingerprint,
)
from sheetsage2_webui.webui.media import probe_audio  # noqa: E402
from sheetsage2_webui.webui.preflight import preflight  # noqa: E402
from sheetsage2_webui.webui.runner import _fallback_since  # noqa: E402
from sheetsage2_webui.webui.security import (  # noqa: E402
    generate_pair_token,
    host_is_loopback,
    origin_is_loopback,
)

WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64
PROJECT_STUB = {"version": 1, "ppq": 1920, "audio": "t.wav", "duration_seconds": 8.0}


def write_artifacts(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "project.json").write_text(json.dumps(PROJECT_STUB), encoding="utf-8")
    (out_dir / "report.json").write_text('{"decisions": {}}', encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps({"duration_seconds": 8.0, "notes": {}}), encoding="utf-8")


def fake_transcribe(job, progress) -> dict:
    progress.check()
    progress.set(0.5, "识别音乐")
    write_artifacts(job.out_dir)
    return {"detail": {"notes": {}}}


def fake_lyrics(job, progress) -> dict:
    progress.check()
    if not (job.out_dir / "project.json").is_file():
        raise RuntimeError("还没有扒谱工程：请先跑「扒谱」")
    (job.out_dir / "lyrics.json").write_text('{"text": ""}', encoding="utf-8")
    return {"detail": {"lines": 0}}


def fake_runners() -> dict:
    return {"transcribe": fake_transcribe, "lyrics": fake_lyrics}


def make_client(tmp_path: Path, **kwargs) -> tuple[TestClient, JobManager]:
    kwargs.setdefault("probe_upload", False)
    app = create_app(data_root=tmp_path / "data", runners=fake_runners(), **kwargs)
    return TestClient(app), app.state.manager


def upload(client: TestClient, name: str = "测试.wav", data: bytes = WAV_BYTES,
           plan: str | None = None):
    payload: dict = {"file": (name, io.BytesIO(data), "audio/wav")}
    if plan is not None:
        payload["plan"] = (None, plan)
    return client.post("/api/jobs", files=payload)


def wait_stage(manager: JobManager, job_id: str, phase: str,
               timeout: float = 20.0) -> str:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job is None:
            return ""
        if job.stages[phase].state in {"done", "failed", "cancelled"}:
            return job.stages[phase].state
        time.sleep(0.02)
    return "timeout"


# --------------------------------------------------------------------- G8 乐观锁
def test_optimistic_lock_rejects_a_stale_revision(tmp_path):
    """两个标签页并发改参数：第二个基于旧 rev 的改动拿到 409 + 当前快照。"""
    client, manager = make_client(tmp_path)
    with client:
        job = upload(client).json()
        assert job["rev"] == 1
        first = client.patch(f"/api/jobs/{job['id']}/options",
                             json={"quantize": "off", "expected_rev": 1})
        assert first.status_code == 200
        assert first.json()["rev"] == 2
        # 还是拿 rev=1 改 → 冲突
        stale = client.patch(f"/api/jobs/{job['id']}/options",
                             json={"range": "30", "expected_rev": 1})
        assert stale.status_code == 409
        body = stale.json()
        assert "旧的" in body["detail"]["message"]
        assert body["detail"]["snapshot"]["rev"] == 2


def test_rev_bumps_on_any_result_changing_edit(tmp_path):
    """G8：rev 的语义 = 任何影响结果的变更都 +1（改参数 / 换音源 / 重跑）。"""
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        job = manager.create("歌.wav", io.BytesIO(WAV_BYTES))
        assert job.rev == 1
        manager.set_options(job.id, quantize="off")
        assert job.rev == 2
        manager.attach_stem(job.id, "vocal", "vocal.wav", io.BytesIO(WAV_BYTES))
        assert job.rev == 3
        manager.run_stage(job.id, "transcribe")
        assert job.rev == 4
        with pytest.raises(StaleRevision):
            manager.set_options(job.id, expected_rev=1, range="30")
    finally:
        manager.shutdown()


# --------------------------------------------------------------------- G9 目录竞态
def test_concurrent_creates_get_distinct_directories(tmp_path):
    """并发上传同名文件：`mkdir(exist_ok=False)` 让 10 个请求各得各的目录。"""
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    barrier = threading.Barrier(10)
    roots: list[Path] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            job = manager.create("同名.wav", io.BytesIO(WAV_BYTES))
            roots.append(job.root)
        except BaseException as exc:                     # noqa: BLE001 - 收集后断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    manager.shutdown()
    assert not errors
    assert len(roots) == len(set(roots)) == 10            # 10 个不同目录
    assert all(root.is_dir() for root in roots)


def test_claim_project_dir_is_atomic(tmp_path):
    first = claim_project_dir(tmp_path, "同名")
    second = claim_project_dir(tmp_path, "同名")
    assert first.name == "同名"
    assert second.name == "同名-2"
    assert first != second


# --------------------------------------------------------------------- G14 重复入队
def test_run_stage_deduplicates_queued_and_running(tmp_path):
    """连点两次「开始」只入队一次，不会跑两遍、也不会把取消抹掉。"""
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        job = manager.create("歌.wav", io.BytesIO(WAV_BYTES))
        manager.run_stage(job.id, "transcribe")
        manager.run_stage(job.id, "transcribe")          # 第二次应该被去重
        assert job.plan.count("transcribe") == 1
        assert wait_stage(manager, job.id, "transcribe") == "done"
        done_events = [event for event in job.events
                       if event.type == "phase_done" and event.data.get("phase") == "transcribe"]
        assert len(done_events) == 1
    finally:
        manager.shutdown()


def test_cancel_tombstone_blocks_immediate_restart(tmp_path):
    """取消后 5 秒内的「开始」当手抖连点拒绝，别把取消复活。"""
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        job = manager.create("歌.wav", io.BytesIO(WAV_BYTES))
        manager.cancel(job.id)
        with pytest.raises(RuntimeError) as exc:
            manager.run_stage(job.id, "transcribe")
        assert "刚刚取消" in str(exc.value)
    finally:
        manager.shutdown()


# --------------------------------------------------------------------- G10 上传体检
@pytest.mark.skipif(find_ffmpeg() is None, reason="本机没有 ffmpeg")
def test_probe_audio_rejects_a_renamed_document(tmp_path):
    text = tmp_path / "doc.mp3"
    text.write_bytes(b"this is not audio at all " * 100)
    result = probe_audio(text)
    assert result is not None and result["ok"] is False
    assert result["code"] == "E_AUDIO_UNREADABLE"


@pytest.mark.skipif(find_ffmpeg() is None, reason="本机没有 ffmpeg")
def test_probe_audio_accepts_a_real_tone(tmp_path):
    import numpy as np
    import soundfile as sf

    rate = 16000
    t = np.linspace(0, 1.0, rate, endpoint=False)
    wave = (0.3 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    path = tmp_path / "tone.wav"
    sf.write(path, wave, rate, subtype="PCM_16")
    result = probe_audio(path)
    assert result is not None and result["ok"] is True
    assert result["silent"] is False


def test_content_fingerprint_changes_with_bytes(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)
    assert content_fingerprint(a) != content_fingerprint(b)
    assert content_fingerprint(a) == content_fingerprint(a)


@pytest.mark.skipif(find_ffmpeg() is None, reason="本机没有 ffmpeg")
def test_probe_upload_rejects_bad_content(tmp_path):
    client, _ = make_client(tmp_path, probe_upload=True)
    with client:
        response = upload(client, name="文档.mp3", data=b"not audio " * 100)
        assert response.status_code == 400
        assert "音频" in response.json()["detail"]


# --------------------------------------------------------------------- G11 磁盘 / 清理
def test_env_reports_disk_and_cleanup_frees_bytes(tmp_path):
    client, manager = make_client(tmp_path)
    with client:
        job = upload(client, plan="transcribe").json()
        _ = wait_stage(manager, job["id"], "transcribe")
        env = client.get("/api/env").json()
        assert set(env["disk"]) >= {"free", "total", "used"}
        # 下载一次会生成 bundle 缓存，清理之后能释放
        _ = client.get(f"/api/jobs/{job['id']}/download")
        internal = manager.get(job["id"]).internal_dir
        assert (internal / "bundle.zip").is_file()
        cleaned = client.post(f"/api/jobs/{job['id']}/cleanup")
        assert cleaned.status_code == 200
        assert cleaned.json()["freed"] > 0
        assert not (internal / "bundle.zip").exists()


def test_bytes_text_is_human_readable():
    assert _bytes_text(0) == "0 B"
    assert _bytes_text(1024 * 1024 * 1024) == "1.0 GB"
    assert "MB" in _bytes_text(50 * 1024 * 1024)


# --------------------------------------------------------------------- G15 预检
def test_preflight_returns_the_expected_checks(tmp_path):
    checks = preflight(tmp_path)
    keys = {item["key"] for item in checks}
    assert {"ffmpeg", "writable", "disk", "cuda"} <= keys
    assert all(item["level"] in {"ok", "warn", "error", "info"} for item in checks)


def test_preflight_counts_a_minimal_asr_install(tmp_path):
    """最小集只装 0.6B 也算"识别模型就绪"（曾经写死 1.7B，笔记本上误报未安装）。"""
    from sheetsage2_webui.webui import preflight as module

    models = tmp_path / "models"
    for name in ("Qwen3-ASR-0.6B", "TIFA-1.0"):
        (models / name).mkdir(parents=True)
        (models / name / "config.json").write_text("{}", encoding="utf-8")
    original = module.PROJECT_ROOT
    module.PROJECT_ROOT = tmp_path
    try:
        assert [path.name for path in module.asr_model_dirs()] == ["Qwen3-ASR-0.6B"]
    finally:
        module.PROJECT_ROOT = original


def test_default_asr_config_falls_back_to_the_installed_model(tmp_path):
    """最小集只有 0.6B：默认配置不能死认 1.7B，也不能硬塞一个没装的 Qwen 对齐器。"""
    from sheetsage2_webui.webui.runner import default_asr_config, pick_asr_model

    models = tmp_path / "models"
    (models / "Qwen3-ASR-0.6B").mkdir(parents=True)
    (models / "Qwen3-ASR-0.6B" / "config.json").write_text("{}", encoding="utf-8")

    assert pick_asr_model(models).name == "Qwen3-ASR-0.6B"
    config = default_asr_config(models)
    assert Path(str(config.asr_model)).name == "Qwen3-ASR-0.6B"
    assert config.aligner_model is None          # 没装就不传，默认走随包的 TIFA

    (models / "Qwen3-ASR-1.7B").mkdir()
    (models / "Qwen3-ASR-1.7B" / "config.json").write_text("{}", encoding="utf-8")
    assert pick_asr_model(models).name == "Qwen3-ASR-1.7B"      # 装了 1.7B 还是优先它


def test_preflight_accepts_the_bundled_tifa_layout(tmp_path, monkeypatch):
    """随包 TIFA 是 `config.yaml` + `model.pt`（**没有 config.json**），不能判成没装。

    真出过这个 bug：内测者按 1 装完，横幅还一直喊「逐字对齐器缺了，跑一次
    01_install.bat 把模型下齐」，其实对齐器就在包里。
    """
    from sheetsage2_webui.webui import preflight as module

    models = tmp_path / "models"
    asr = models / "Qwen3-ASR-0.6B"
    asr.mkdir(parents=True)
    (asr / "config.json").write_text("{}", encoding="utf-8")
    tifa = models / "TIFA-1.0"
    tifa.mkdir()
    (tifa / "model.pt").write_text("x", encoding="utf-8")
    (tifa / "config.yaml").write_text("x", encoding="utf-8")

    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "SHEETSAGE_MODEL_DIR", models / "SheetSage2")
    monkeypatch.setattr(module, "TIFA_MODEL_DIR", tifa)
    monkeypatch.setattr(module, "ALIGNER_MODEL_DIR", models / "Qwen3-ForcedAligner-0.6B")

    checks = {item["key"]: item for item in module.preflight(tmp_path / "data")}
    assert checks["lyrics_model"]["level"] == "ok", checks["lyrics_model"]

    # 把随包的对齐器拿掉之后，提示要说「解压不完整」，不能说「还没下模型」
    (tifa / "model.pt").unlink()
    checks = {item["key"]: item for item in module.preflight(tmp_path / "data")}
    assert checks["lyrics_model"]["level"] == "warn"
    assert "解压" in checks["lyrics_model"]["detail"]


def test_env_exposes_preflight(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        env = client.get("/api/env").json()
        assert isinstance(env["preflight"], list)
        assert any(item["key"] == "writable" for item in env["preflight"])


# --------------------------------------------------------------------- G17 OOM 告知
def test_fallback_since_reads_only_the_new_records():
    class Record:
        def __init__(self, reason=None):
            self.fallback_reason = reason

    class Engine:
        records = [Record(), Record("old fallback")]

    engine = Engine()
    engine.records.append(Record("CUDA OOM 退回 CPU"))
    assert _fallback_since(engine, before=2) == "CUDA OOM 退回 CPU"
    assert _fallback_since(engine, before=3) is None      # 只看增量，别翻旧账


# --------------------------------------------------------------------- G13 CSRF/Token
def test_loopback_guard_rejects_cross_origin_post(tmp_path):
    client, _ = make_client(tmp_path, loopback_guard=True, allowed_hosts=("testserver",))
    with client:
        job = upload(client).json()
        evil = client.post(f"/api/jobs/{job['id']}/cancel",
                           headers={"Origin": "http://evil.example"})
        assert evil.status_code == 403
        # 无 Origin（本机工具）与回环 Origin 都放行
        ok = client.post(f"/api/jobs/{job['id']}/cancel")
        assert ok.status_code == 200


def test_require_token_blocks_requests_without_the_pair_token(tmp_path):
    client, manager = make_client(tmp_path, require_token=True)
    token = client.app.state.pair_token
    with client:
        assert client.get("/api/jobs").status_code == 403
        assert client.get("/api/jobs", headers={"X-Pair-Token": token}).status_code == 200
        assert client.get(f"/api/jobs?token={token}").status_code == 200


def test_security_helpers():
    assert host_is_loopback("127.0.0.1:8000")
    assert host_is_loopback("[::1]:8000")
    assert not host_is_loopback("evil.example")
    assert origin_is_loopback("http://127.0.0.1:8000")
    assert not origin_is_loopback("http://evil.example")
    assert len(generate_pair_token()) == 32


# --------------------------------------------------------------------- G12 诊断包
def test_diagnose_bundle_contains_env_and_metadata(tmp_path):
    client, manager = make_client(tmp_path)
    with client:
        job = upload(client, plan="transcribe").json()
        _ = wait_stage(manager, job["id"], "transcribe")
        response = client.get("/api/diagnose")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = zf.namelist()
            assert "env.json" in names
            assert any(name.endswith("job.json") for name in names)


def test_setup_logging_writes_a_log_file(tmp_path):
    from sheetsage2_webui.webui.logs import setup_logging

    path = setup_logging(tmp_path / "data")
    assert path.parent.is_dir()
    import logging

    logging.getLogger("sheetsage2").info("P1 诊断日志测试")
    assert path.is_file()
