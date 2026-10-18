r"""P2 缺口的接线测试（G18-G22）。

P2 里三项是纯前端（G18 进度单调、G19 请求超时、G20 的界面那半），它们的单测在
`web/test/progress.test.ts` 与 `web/test/api.test.ts`；这个文件管**后端那半**：

| 缺口 | 这里断言什么 |
|---|---|
| G20 耗时预估 | `eta.estimate` 的口径与区间、快照里的 `stages.*.eta`、CPU/显卡系数、跑完就不猜 |
| G22 备份/迁移 | 包里有 `backup.json` + 产物 + 原音频，**没有**可重算的缓存；导回来工程能用；坏包/越界路径被拒 |

G21（断点续跑）没有实现，原因写在 `docs/健壮性保障评估.md` §6.G21：窗口循环在模型自己的
`transcribe()` 里，我们这一层只拿得到回调，硬做等于改上游代码。

    .\.venv\Scripts\python.exe -m pytest tests/test_webui_p2.py -q
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.webui.app import create_app  # noqa: E402
from sheetsage2_webui.webui.backup import (  # noqa: E402
    BackupRejected,
    project_entries,
    read_manifest,
)
from sheetsage2_webui.webui.eta import (  # noqa: E402
    estimate,
    humanize,
    range_seconds,
    remaining,
)
from sheetsage2_webui.webui.jobs import JobManager  # noqa: E402

WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64
PROJECT_STUB = {"version": 1, "ppq": 1920, "audio": "t.wav", "duration_seconds": 8.0}


# --------------------------------------------------------------------- 桩
def write_artifacts(out_dir: Path, *, duration: float = 8.0) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "project.json").write_text(
        json.dumps(dict(PROJECT_STUB, duration_seconds=duration)), encoding="utf-8")
    (out_dir / "report.json").write_text('{"decisions": {}}', encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps({"duration_seconds": duration, "notes": {}}), encoding="utf-8")


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


def upload(client: TestClient, name: str = "测试.wav", plan: str | None = None):
    payload: dict = {"file": (name, io.BytesIO(WAV_BYTES), "audio/wav")}
    if plan is not None:
        payload["plan"] = (None, plan)
    return client.post("/api/jobs", files=payload)


def wait_stage(manager: JobManager, job_id: str, phase: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job is None:
            return ""
        if job.stages[phase].state in {"done", "failed", "cancelled"}:
            return job.stages[phase].state
        time.sleep(0.02)
    return "timeout"


# --------------------------------------------------------------------- G20 预估
def test_eta_uses_gpu_and_cpu_factors():
    """显卡 vs CPU 是十几倍的差距——这两个系数绝对不能写反。"""
    gpu = estimate("transcribe", 240.0, "cuda")
    cpu = estimate("transcribe", 240.0, "cpu")
    assert gpu and cpu
    assert gpu["device"] == "cuda" and cpu["device"] == "cpu"
    assert cpu["seconds"] > gpu["seconds"] * 8          # 0.6 vs 8.0 的量级
    assert gpu["low"] < gpu["seconds"] < gpu["high"]
    assert gpu["label"].startswith("预计")


def test_eta_clamps_to_the_selected_range():
    """选了「前 30 秒」就只跑 30 秒，预估也得跟着缩。"""
    full = estimate("transcribe", 240.0, "cuda", {"range": "full"})
    partial = estimate("transcribe", 240.0, "cuda", {"range": "30"})
    assert full and partial
    assert partial["audio_seconds"] == 30.0
    assert partial["seconds"] < full["seconds"] / 2
    assert range_seconds({"range": "60"}) == 60.0
    assert range_seconds({"range": "full"}) is None


def test_eta_needs_a_known_duration():
    """不知道音频多长就别猜：界面不显示，比给个错数字强。"""
    assert estimate("transcribe", None, "cuda") is None
    assert estimate("transcribe", 0.0, "cuda") is None
    assert estimate("transcribe", 1.0, "cuda") is None   # 太短，估了也没意义
    assert estimate("未知阶段", 240.0, "cuda") is None


def test_eta_humanize_and_remaining():
    assert humanize(40, 60) == "40~60 秒"
    assert humanize(130, 220) == "2~4 分钟"
    assert humanize(7000, 8000).endswith("小时")
    # 还剩多久：估 180 秒、已经跑了 60 秒 -> 120 秒；跑超了就归零（别出现负数）
    assert remaining({"seconds": 180.0}, 60) == 120.0
    assert remaining({"seconds": 180.0}, 999) == 0.0
    assert remaining(None, 10) is None


def test_snapshot_carries_eta_for_unfinished_stages(tmp_path):
    """快照里 `stages.*.eta` 只给还没跑完的阶段——跑完了就不用猜了。"""
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        job = manager.create("歌.wav", io.BytesIO(WAV_BYTES))
        job.audio_info = {"duration_seconds": 240.0}
        manager.set_device_hint("cuda")
        eta = job.snapshot()["stages"]["transcribe"]["eta"]
        assert eta and eta["device"] == "cuda"
        assert eta["audio_seconds"] == 240.0
        assert eta["label"].startswith("预计")

        manager.run_stage(job.id, "transcribe")
        assert wait_stage(manager, job.id, "transcribe") == "done"
        assert job.snapshot()["stages"]["transcribe"]["eta"] is None
    finally:
        manager.shutdown()


def test_eta_follows_the_device_option(tmp_path):
    """工程自己的「设备」优先：面板选了 CPU，就按 CPU 的系数估。"""
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        job = manager.create("歌.wav", io.BytesIO(WAV_BYTES))
        job.audio_info = {"duration_seconds": 120.0}
        manager.set_device_hint("cuda")
        assert manager.eta_for(job, "transcribe")["device"] == "cuda"
        manager.set_options(job.id, device="cpu")
        assert manager.eta_for(job, "transcribe")["device"] == "cpu"
        # 设备还不知道时按显卡估（有卡的机器远多于没卡的）
        manager.set_device_hint(None)
        manager.set_options(job.id, device="auto")
        assert manager.eta_for(job, "transcribe")["device"] == "cuda"
    finally:
        manager.shutdown()


def test_snapshot_eta_is_null_without_duration(tmp_path):
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        job = manager.create("歌.wav", io.BytesIO(WAV_BYTES))
        assert job.snapshot()["stages"]["transcribe"]["eta"] is None
        assert job.snapshot()["eta"] is None
    finally:
        manager.shutdown()


# --------------------------------------------------------------------- G22 备份
def test_backup_contains_manifest_and_artifacts(tmp_path):
    """备份包 = 清单 + 产物 + 原音频 + job.json；**不带**能重算的缓存。"""
    client, manager = make_client(tmp_path)
    with client:
        job = upload(client, plan="transcribe").json()
        assert wait_stage(manager, job["id"], "transcribe") == "done"
        # 造一份"可重算的缓存"：它不该进备份包
        internal = manager.get(job["id"]).internal_dir
        (internal / "bundle.zip").write_bytes(b"cache")
        (internal / "peaks-mix-1200.json").write_text("{}", encoding="utf-8")

        response = client.get("/api/backup")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["x-backup-projects"] == "1"
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read("backup.json"))
        joined = "\n".join(names)
        assert manifest["kind"] == "sheetsage2-studio-backup"
        assert manifest["version"] >= 1
        assert manifest["projects"][0]["name"] == "测试"
        assert "project.json" in joined and "job.json" in joined
        assert "/.webui/input/" in joined            # 原音频：不带就没法重跑
        assert "bundle.zip" not in joined             # 缓存不进包
        assert "peaks-" not in joined


def test_backup_skips_caches_by_default_and_can_include_them(tmp_path):
    project = tmp_path / "歌"
    (project / ".webui" / "input").mkdir(parents=True)
    (project / "project.json").write_text("{}", encoding="utf-8")
    (project / ".webui" / "input" / "a.wav").write_bytes(WAV_BYTES)
    (project / ".webui" / "bundle.zip").write_bytes(b"x")
    (project / ".webui" / "peaks-mix-1200.json").write_text("{}", encoding="utf-8")
    (project / ".webui" / "export").mkdir()
    (project / ".webui" / "export" / "a.svp").write_bytes(b"y")

    plain = {relative for _, relative in project_entries(project)}
    assert plain == {"project.json", ".webui/input/a.wav"}

    full = {relative for _, relative in project_entries(project, include_cache=True)}
    assert ".webui/bundle.zip" in full and ".webui/export/a.svp" in full
    # 不带原音频时，`input/` 与 `stems/` 一起跳过
    no_audio = {relative for _, relative in project_entries(project, include_audio=False)}
    assert no_audio == {"project.json"}


def test_restore_brings_projects_into_a_fresh_data_root(tmp_path):
    """换电脑的完整链路：A 备份 → B 导入 → 产物、原音频、参数都还在。"""
    client, manager = make_client(tmp_path)
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        assert wait_stage(manager, job["id"], "lyrics") == "done"
        archive = client.get("/api/backup").content

    fresh = JobManager(tmp_path / "data2", runners=fake_runners())
    try:
        source = tmp_path / "backup.zip"
        source.write_bytes(archive)
        result = fresh.restore_backup(source)
        assert result["count"] == 1
        assert not result["skipped"]
        restored = fresh.list()
        assert len(restored) == 1
        item = restored[0]
        assert item.name == "测试"
        assert item.out_dir.is_dir()
        assert (item.out_dir / "project.json").is_file()
        assert (item.out_dir / "lyrics.json").is_file()
        assert item.upload.is_file()                  # 原音频按布局重算过路径
        assert item.options == job["options"]         # 参数跟着回来
        assert item.artifacts_rev == manager.get(job["id"]).artifacts_rev
    finally:
        fresh.shutdown()


def test_restore_never_overwrites_an_existing_project(tmp_path):
    """同一个包导两次：第二次落成 `-2`、id 也不撞（删一个不会连坐另一个）。"""
    client, manager = make_client(tmp_path)
    with client:
        upload(client, plan="transcribe")
        archive = client.get("/api/backup").content
        restored = client.post("/api/backup/restore",
                               files={"file": ("b.zip", io.BytesIO(archive), "application/zip")})
        assert restored.status_code == 201
        body = restored.json()
        assert body["count"] == 1
        jobs = client.get("/api/jobs").json()["jobs"]
        assert len(jobs) == 2
        assert jobs[0]["id"] != jobs[1]["id"]
        folders = [item for item in (tmp_path / "data").iterdir()
                   if item.is_dir() and not item.name.startswith(".")]
        assert len(folders) == 2


def test_restore_rejects_a_foreign_archive(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        assert client.post("/api/backup/restore",
                           files={"file": ("x.zip", io.BytesIO(b"not a zip"),
                                           "application/zip")}).status_code == 400
        # 合法的 zip，但没有备份清单
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("projects/歌/project.json", "{}")
        response = client.post("/api/backup/restore",
                               files={"file": ("x.zip", io.BytesIO(buffer.getvalue()),
                                               "application/zip")})
        assert response.status_code == 400
        assert "备份清单" in response.json()["detail"]


def test_restore_rejects_path_traversal_entries(tmp_path):
    """zip-slip：包里写 `..` 想跳到数据目录外面，整包拒绝。"""
    buffer = io.BytesIO()
    manifest = {"kind": "sheetsage2-studio-backup", "version": 1, "projects": []}
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("backup.json", json.dumps(manifest))
        zf.writestr("projects/歌/../../evil.txt", "x")
    archive = tmp_path / "evil.zip"
    archive.write_bytes(buffer.getvalue())
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        with pytest.raises(BackupRejected) as exc:
            manager.restore_backup(archive)
        assert "可疑路径" in str(exc.value)
        assert not (tmp_path / "evil.txt").exists()
    finally:
        manager.shutdown()


def test_read_manifest_rejects_unknown_versions(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("backup.json", json.dumps(
            {"kind": "sheetsage2-studio-backup", "version": 99, "projects": []}))
    archive = tmp_path / "future.zip"
    archive.write_bytes(buffer.getvalue())
    with pytest.raises(BackupRejected) as exc:
        read_manifest(archive)
    assert "认不出来" in str(exc.value)
