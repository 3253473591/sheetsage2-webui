"""磁盘侧：工程恢复与只读导入、`job.json`、产物清单、原子写、缓存失效。

这一组关心"进程重启/包搬走后磁盘上还认不认得出这个工程"，以及"这一版产物到底是哪一版"。
"""

from __future__ import annotations

import io
import json
import time
import zipfile
import pytest
from pathlib import Path

from support_webui import (  # noqa: E402
    PROJECT_STUB,
    GateRunner,
    fake_lyrics,
    fake_runners,
    write_artifacts,
    make_client,
    job_dir,
    wait_stage,
    wait_for_state,
    upload,
    real_wav,
)
from sheetsage2_webui.webui.jobs import (  # noqa: E402
    ImportedProject,
    JobManager,
    project_folder_name,
    safe_name,
    scan_files,
)


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


def test_zombie_running_state_is_reset_on_restart(tmp_path):
    """meta 里写着 running、但阶段全已终态（进程死在一个已经判死的阶段之后）：

    恢复后必须变回 `draft`——否则界面永远显示「运行中」，`queue.running` 也一直指着它。
    """
    root = tmp_path / "output"
    first = JobManager(root, runners=fake_runners())
    try:
        job = first.create("僵尸.wav", io.BytesIO(b"x" * 1000), plan=["transcribe"])
        deadline = time.monotonic() + 20
        while job.state != "done" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert job.state == "done"
        meta = json.loads((job.root / "job.json").read_text(encoding="utf-8"))
        meta["state"] = "running"                  # 人为造出僵尸
        (job.root / "job.json").write_text(json.dumps(meta, ensure_ascii=False),
                                           encoding="utf-8")
        first.shutdown(timeout=2)
    finally:
        first.shutdown(timeout=2)

    again = JobManager(root, runners=fake_runners())
    try:
        restored = again.get(job.id)
        assert restored is not None
        assert restored.state == "draft"
        assert "中断" in restored.message
    finally:
        again.shutdown()


def test_hung_runner_marks_the_queue_stuck(tmp_path):
    """看门狗判死之后 runner 还不回来 = 线程回收不了（单 worker 停摆）。

    这时候界面必须能看到"队列停摆、请重启"，而不是停在 28% 装作在跑。
    """
    gate = GateRunner()
    manager = JobManager(tmp_path / "output",
                         runners={"transcribe": gate, "lyrics": fake_lyrics},
                         stall_timeout=0.3, hard_timeout=3600.0,
                         watchdog_interval=0.05, stuck_grace=0.3,
                         probe_upload=False)
    try:
        job = manager.create("卡死.wav", io.BytesIO(b"x" * 1000), plan=["transcribe"])
        assert gate.entered.wait(timeout=10)

        deadline = time.monotonic() + 15
        while manager._scheduler.stuck is None and time.monotonic() < deadline:
            time.sleep(0.05)
        stuck = manager._scheduler.stuck
        assert stuck is not None
        assert stuck["job_id"] == job.id and stuck["phase"] == "transcribe"
        assert manager.paused is True                     # 闸门关上，别再排队
        assert manager.env()["queue"]["stuck"]["phase"] == "transcribe"
    finally:
        gate.gate.set()
        manager.shutdown(timeout=1.0)


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


def test_atomic_write_never_leaves_a_part_file(tmp_path):
    """G5：JSON 落盘是"先写 .part 再 rename"，不会留下半截文件。"""
    from sheetsage2_webui.fsutil import write_text_atomic

    target = tmp_path / "project.json"
    write_text_atomic(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    assert list(tmp_path.glob("*.part")) == []
    assert not list(tmp_path.glob(".*.part"))


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
