"""批量：目录扫描与 LRC 配对、从路径建工程、批次轻列表、失败跳过与暂停。

约定：
- 这里测的是**批量这一层**的行为，不加载任何模型（`support_webui` 的假 runner）；
- 扫描是纯函数（`webui/batch_scan.py`），可以直接造文件夹喂它；
- 其余用例走 HTTP，看的是"外面看到的样子"。
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest

from support_webui import (WAV_BYTES, GateRunner, fake_lyrics, fake_runners, make_client,
                           upload, wait_for_state, write_artifacts)

from sheetsage2_webui.webui import batch_scan

#: 两行带时间戳的 LRC（utf-8）。
LRC_TEXT = "[00:01.00]晚风停在旧站台\n[00:05.00]一个人走回家\n"


def write(path: Path, data: bytes = b"RIFF0000WAVE") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------- 扫描
def test_scan_pairs_the_lrc_with_the_same_name(tmp_path):
    folder = tmp_path / "batch"
    write(folder / "a.wav")
    write(folder / "a.lrc", LRC_TEXT.encode("utf-8"))
    write(folder / "b.wav")
    write(folder / "c.mp3")
    write(folder / "c.LRC", LRC_TEXT.encode("gbk"))       # 扩展名大小写 + GBK 编码
    write(folder / "d.wav")
    write(folder / "d.txt", "第一行\n第二行\n".encode("utf-8"))
    write(folder / ".hidden.wav")                          # 隐藏文件不进批次
    write(folder / "cover.jpg")                            # 非音频不进批次

    result = batch_scan.scan_folder(folder)
    assert [item.name for item in result.items] == ["a", "b", "c", "d"]
    kinds = {item.name: item.lrc_kind for item in result.items}
    assert kinds == {"a": "lrc", "b": "none", "c": "lrc", "d": "txt"}
    assert result.items[0].lrc_lines == 2
    assert result.items[1].lrc is None
    assert result.problems == []


def test_scan_marks_japanese_and_korean_lrc(tmp_path):
    folder = tmp_path / "batch"
    write(folder / "ja.wav")
    write(folder / "ja.lrc", "[00:01.00]君が僕の心臓じゃないせいで\n".encode("utf-8"))
    write(folder / "ko.wav")
    write(folder / "ko.lrc", "[00:01.00]사랑은 늘 도망가\n".encode("utf-8"))

    result = batch_scan.scan_folder(folder)
    kinds = {item.name: item.lrc_kind for item in result.items}
    assert kinds == {"ja": "lrc-ja", "ko": "lrc-ko"}


def test_scan_warns_when_the_ja_aligner_is_missing(tmp_path):
    folder = tmp_path / "batch"
    write(folder / "ja.wav")
    write(folder / "ja.lrc", "[00:01.00]君が僕の心臓じゃないせいで\n".encode("utf-8"))

    result = batch_scan.scan_folder(folder, aligner_available=False)
    assert any("对齐器" in problem for problem in result.problems)


def test_scan_probes_duration_when_a_probe_is_given(tmp_path):
    folder = tmp_path / "batch"
    write(folder / "a.wav")
    result = batch_scan.scan_folder(
        folder, probe=lambda path: {"duration_seconds": 252.5, "ok": True})
    assert result.items[0].duration_seconds == 252.5


def test_scan_refuses_a_folder_inside_the_data_root(tmp_path):
    root = tmp_path / "data"
    (root / "批量").mkdir(parents=True)
    write(root / "批量" / "a.wav")
    with pytest.raises(ValueError, match="数据目录"):
        batch_scan.scan_folder(root / "批量", data_root=root)


def test_scan_refuses_a_missing_folder(tmp_path):
    with pytest.raises(ValueError, match="不是文件夹"):
        batch_scan.scan_folder(tmp_path / "没有这个")


def test_scan_reports_problems_without_dropping_items(tmp_path):
    folder = tmp_path / "batch"
    write(folder / "空.wav", b"")                           # 0 字节
    write(folder / "大.wav", b"x" * 400)                    # 超过 max_bytes
    write(folder / "重.wav")                                # 两首同名（不同后缀）
    write(folder / "重.mp3")
    write(folder / "无词.wav")
    write(folder / "无词.lrc", "[ti:标题]\n[ar:歌手]\n".encode("utf-8"))

    result = batch_scan.scan_folder(folder, max_bytes=200)
    text = "；".join(result.problems)
    assert "空.wav" in text and "空文件" in text
    assert "大.wav" in text and "太大" in text
    assert "同名" in text and "2 个工程" in text
    assert "无词.lrc" in text and "唱词" in text
    # 有问题也照样列出来（界面上标黄，不静默丢）
    assert {item.name for item in result.items} == {"空", "大", "重", "无词"}


def test_scan_caps_the_number_of_items(tmp_path):
    folder = tmp_path / "batch"
    for name in ("a", "b", "c"):
        write(folder / f"{name}.wav")
    with pytest.raises(ValueError, match="上限"):
        batch_scan.scan_folder(folder, max_items=2)


def test_scan_rejects_a_folder_without_audio(tmp_path):
    folder = tmp_path / "batch"
    write(folder / "readme.txt", b"nothing")
    with pytest.raises(ValueError, match="没有音频"):
        batch_scan.scan_folder(folder)


# --------------------------------------------------------- 从路径建工程 / 批次
def test_create_from_path_imports_the_audio_and_keeps_the_source(tmp_path):
    source = write(tmp_path / "我的歌" / "甲.wav", b"RIFF" + b"x" * 64)
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = manager.create_from_path(source)
        assert job.name == "甲"
        assert job.upload.is_file()
        assert job.upload.read_bytes() == source.read_bytes()
        assert source.is_file()                      # 源文件不动
        assert job.upload.resolve() != source.resolve()


def test_create_from_path_links_on_the_same_volume(tmp_path):
    source = write(tmp_path / "我的歌" / "甲.wav", b"RIFF" + b"x" * 64)
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = manager.create_from_path(source)
        # 硬链接：同一份数据，不占双倍空间
        assert os.stat(job.upload).st_ino == os.stat(source).st_ino


def test_create_from_path_copies_when_linking_is_not_possible(tmp_path, monkeypatch):
    source = write(tmp_path / "我的歌" / "甲.wav", b"RIFF" + b"x" * 64)

    def boom(*args, **kwargs):
        raise OSError("跨卷不能硬链接")

    monkeypatch.setattr(os, "link", boom)
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = manager.create_from_path(source)
        assert job.upload.read_bytes() == source.read_bytes()
        assert os.stat(job.upload).st_ino != os.stat(source).st_ino


def test_create_from_path_can_force_a_copy(tmp_path):
    source = write(tmp_path / "我的歌" / "甲.wav", b"RIFF" + b"x" * 64)
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = manager.create_from_path(source, link=False)
        assert os.stat(job.upload).st_ino != os.stat(source).st_ino


def test_create_carries_options_and_the_batch_tag(tmp_path):
    source = write(tmp_path / "我的歌" / "甲.wav")
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = manager.create_from_path(
            source, options={"dtype": "fp32"},
            batch={"id": "b-1", "title": "11-24 批量"})
        snapshot = job.snapshot()
        assert snapshot["options"]["dtype"] == "fp32"
        assert snapshot["batch"] == {"id": "b-1", "title": "11-24 批量"}
        # 没传 batch 的就是单曲（不能出现空壳 batch）
        plain = manager.create_from_path(write(tmp_path / "我的歌" / "乙.wav"))
        assert plain.snapshot()["batch"] is None


def test_batch_tag_survives_a_restart(tmp_path):
    source = write(tmp_path / "我的歌" / "甲.wav")
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        manager.create_from_path(source, batch={"id": "b-1", "title": "11-24 批量"})
    # 同一个数据根再起一个管理器（= 重启）
    client2, manager2 = make_client(tmp_path, fake_runners())
    with client2:
        restored = manager2.list()[0]
        assert restored.batch == {"id": "b-1", "title": "11-24 批量"}


# ------------------------------------------------------------------ 批量接口
def batch_folder(tmp_path: Path) -> Path:
    """两首歌，其中一首带 LRC（另一首走语音识别）。

    文件名用 ASCII 数字：排序不依赖中文的码点顺序（那是给界面看的，不是契约）。
    """
    folder = tmp_path / "专辑"
    write(folder / "01.flac")
    write(folder / "01.lrc", LRC_TEXT.encode("utf-8"))
    write(folder / "02.flac")
    return folder


def test_scan_endpoint_previews_the_folder(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        response = client.post("/api/batches/scan", json={"dir": str(batch_folder(tmp_path))})
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["name"] for item in body["items"]] == ["01", "02"]
        assert body["items"][0]["lrc_kind"] == "lrc"
        assert body["items"][1]["lrc_kind"] == "none"
        assert body["with_lyrics"] == 1


def test_scan_endpoint_maps_bad_input_to_400(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        missing = client.post("/api/batches/scan", json={"dir": str(tmp_path / "没有")})
        assert missing.status_code == 400
        assert "不是文件夹" in missing.json()["detail"]

        inside = tmp_path / "data" / "批量"
        write(inside / "a.wav")
        inside_root = client.post("/api/batches/scan", json={"dir": str(inside)})
        assert inside_root.status_code == 400
        assert "数据目录" in inside_root.json()["detail"]


def test_batch_create_builds_one_job_per_song_and_queues_them(tmp_path):
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        response = client.post("/api/batches", json={"dir": str(batch_folder(tmp_path))})
        assert response.status_code == 201, response.text
        body = response.json()
        assert len(body["jobs"]) == 2
        assert body["batch"]["id"]
        assert all(job["batch"]["id"] == body["batch"]["id"] for job in body["jobs"])

        # 每个工程都排了「扒谱 → 歌词」（串行自动接）
        for job in body["jobs"]:
            assert job["stages"]["transcribe"]["state"] in {"queued", "running", "done"}
        assert len(manager.list()) == 2


def test_batch_create_applies_options_and_the_lrc_text(tmp_path):
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        body = client.post("/api/batches", json={
            "dir": str(batch_folder(tmp_path)),
            "options": {"dtype": "fp32"},
        }).json()
        # 建批次返回的是**轻量行**（不带 options）——参数要去工程快照里看
        by_name = {job["name"]: job["id"] for job in body["jobs"]}
        first = client.get(f"/api/jobs/{by_name['01']}").json()
        second = client.get(f"/api/jobs/{by_name['02']}").json()
        assert first["options"]["dtype"] == "fp32"
        # LRC 以**文本**存进 options（和歌词抽屉一个口径：源目录以后被删也不影响）
        assert "晚风停在旧站台" in first["options"]["lyrics_lrc"]
        assert second["options"]["lyrics_lrc"] == ""


def test_batch_create_rejects_a_folder_inside_the_data_root(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        inside = tmp_path / "data" / "批量"
        write(inside / "a.wav")
        response = client.post("/api/batches", json={"dir": str(inside)})
        assert response.status_code == 400
        assert "数据目录" in response.json()["detail"]


def test_batches_are_listed_and_survive_by_derivation(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        created = client.post("/api/batches", json={"dir": str(batch_folder(tmp_path))}).json()
        listing = client.get("/api/batches").json()["batches"]
        assert [batch["id"] for batch in listing] == [created["batch"]["id"]]
        assert listing[0]["total"] == 2
        detail = client.get(f"/api/batches/{created['batch']['id']}").json()
        assert len(detail["rows"]) == 2
        assert {row["name"] for row in detail["rows"]} == {"01", "02"}


def test_unknown_batch_is_404(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        assert client.get("/api/batches/没有这个批次").status_code == 404


# ------------------------------------------------------- 浏览目录（原生选择器）
def test_browse_returns_the_picked_folder(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, fake_runners())
    monkeypatch.setattr(batch_scan, "pick_folder", lambda **kwargs: str(tmp_path))
    with client:
        response = client.post("/api/batches/browse")
        assert response.status_code == 200
        assert response.json()["dir"] == str(tmp_path)


def test_browse_reports_cancel_as_null(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, fake_runners())
    monkeypatch.setattr(batch_scan, "pick_folder", lambda **kwargs: None)
    with client:
        assert client.post("/api/batches/browse").json()["dir"] is None


def test_browse_falls_back_when_there_is_no_tkinter(tmp_path, monkeypatch):
    """服务进程没有 tkinter / 没有桌面 → 501 + 一句人话（界面据此提示"手动粘贴路径"）。"""
    def boom(**kwargs):
        raise batch_scan.FolderPickUnavailable("没有图形界面，没法弹选择器；请手动粘贴路径")

    client, _ = make_client(tmp_path, fake_runners())
    monkeypatch.setattr(batch_scan, "pick_folder", boom)
    with client:
        response = client.post("/api/batches/browse")
        assert response.status_code == 501
        assert "手动粘贴" in response.json()["detail"]


# ------------------------------------------------------- 拖文件夹：逐个上传建批次
def test_upload_endpoint_creates_a_batch_and_reuses_it(tmp_path):
    """拖文件夹走"上传"：第一次建批次，后续文件带上同一个 batch_id。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        first = client.post(
            "/api/batches/upload",
            files={"file": ("甲.wav", io.BytesIO(WAV_BYTES), "audio/wav")},
            data={"lyrics": LRC_TEXT, "title": "拖入的 4 首"},
        )
        assert first.status_code == 201, first.text
        body = first.json()
        batch_id = body["batch"]["id"]
        assert body["batch"]["title"] == "拖入的 4 首"
        job_id = body["jobs"][0]["id"]
        assert "晚风停在旧站台" in client.get(f"/api/jobs/{job_id}").json()["options"]["lyrics_lrc"]

        second = client.post(
            "/api/batches/upload",
            files={"file": ("乙.wav", io.BytesIO(WAV_BYTES), "audio/wav")},
            data={"batch_id": batch_id, "title": "不该覆盖"},
        )
        assert second.status_code == 201
        assert second.json()["batch"]["id"] == batch_id
        assert second.json()["batch"]["title"] == "拖入的 4 首"     # 复用首批次，不覆盖标题

        listing = client.get("/api/batches").json()["batches"]
        assert [batch["id"] for batch in listing] == [batch_id]
        assert listing[0]["total"] == 2


def test_upload_endpoint_rejects_bad_input(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        not_audio = client.post(
            "/api/batches/upload",
            files={"file": ("歌词.txt", io.BytesIO(b"x"), "text/plain")})
        assert not_audio.status_code == 400

        bad_lyrics = client.post(
            "/api/batches/upload",
            files={"file": ("甲.wav", io.BytesIO(WAV_BYTES), "audio/wav")},
            data={"lyrics": "[ti:标题]\n[ar:歌手]\n"})
        assert bad_lyrics.status_code == 400
        assert "唱词" in bad_lyrics.json()["detail"]


def test_batch_rows_keep_the_folder_order(tmp_path):
    """批次内的顺序 = 文件夹里的顺序。

    不能只靠 `created_at` 排序：Windows 的时钟粒度约 15ms，一批工程几毫秒就建完了，
    时间戳会并列——并列时顺序会退化成"最近建的在前"，和扫描表对不上。
    """
    folder = tmp_path / "专辑"
    for name in ("01", "02", "03"):
        write(folder / f"{name}.flac")
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        created = client.post("/api/batches", json={"dir": str(folder)}).json()
        detail = client.get(f"/api/batches/{created['batch']['id']}").json()
        assert [row["name"] for row in detail["rows"]] == ["01", "02", "03"]
        # 列表里也是这个顺序（导出时按它打包）
        listing = client.get("/api/batches").json()["batches"][0]
        assert listing["job_ids"] == [row["id"] for row in detail["rows"]]


def test_rows_view_is_light_and_carries_the_batch(tmp_path):
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        created = client.post("/api/batches", json={"dir": str(batch_folder(tmp_path))}).json()
        rows = client.get("/api/jobs", params={"view": "rows"}).json()["jobs"]
        assert len(rows) == 2
        row = rows[0]
        assert row["batch"]["id"] == created["batch"]["id"]
        assert set(row) >= {"id", "name", "state", "stages", "batch", "audio_seconds"}
        # 轻列表不带重东西（否则 40 首轮询会到 MB 级）
        assert "files" not in row
        assert "options" not in row
        assert "highlights" not in row


# ------------------------------------------------------- 调度：失败跳过 / 暂停
def test_failed_transcribe_skips_the_lyrics(tmp_path):
    """扒谱失败后别再跑歌词：否则 40 首的批量就是 80 次失败，还可能拿旧谱面填新词。"""
    calls: list[str] = []

    def boom(job, progress):
        calls.append("transcribe")
        raise RuntimeError("模型炸了")

    def lyrics(job, progress):
        calls.append("lyrics")
        return {"detail": {}}

    client, _ = make_client(tmp_path, {"transcribe": boom, "lyrics": lyrics})
    with client:
        job = upload(client, plan="transcribe,lyrics").json()
        body = wait_for_state(client, job["id"], {"failed"})
        assert body["stages"]["transcribe"]["state"] == "failed"
        assert body["stages"]["lyrics"]["state"] == "skipped"
        assert calls == ["transcribe"]


def test_pause_stops_dispatch_and_resume_continues(tmp_path):
    runs: list[str] = []

    def slow(job, progress):
        runs.append(job.name)
        time.sleep(0.05)
        write_artifacts(job.out_dir)
        return {"detail": {}}

    client, _ = make_client(tmp_path, {"transcribe": slow, "lyrics": fake_lyrics})
    with client:
        assert client.post("/api/batches/pause").json()["paused"] is True
        first = upload(client, name="a.wav", plan="transcribe").json()
        upload(client, name="b.wav", plan="transcribe")
        time.sleep(0.3)
        assert runs == []                       # 暂停期间一个都不派发

        assert client.post("/api/batches/resume").json()["paused"] is False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(runs) < 2:
            time.sleep(0.05)
        assert len(runs) == 2                   # 恢复后照旧串行接上
        assert client.get(f"/api/jobs/{first['id']}").json()["state"] == "done"


def test_pause_does_not_interrupt_the_running_stage(tmp_path):
    gate = GateRunner()
    client, _ = make_client(tmp_path, {"transcribe": gate, "lyrics": fake_lyrics},
                            probe_upload=False)
    with client:
        job = upload(client, plan="transcribe").json()
        assert gate.entered.wait(timeout=5)     # 已经在跑了
        client.post("/api/batches/pause")
        gate.gate.set()                          # 放行：暂停不该打断它
        body = wait_for_state(client, job["id"], {"done"})
        assert body["stages"]["transcribe"]["state"] == "done"


# --------------------------------------------- LRC 模式下不加载识别模型（批量提速）
def test_lrc_with_tifa_does_not_load_the_asr_model(tmp_path):
    """中/英/粤的 LRC 只要 TIFA（40M）；日/韩要对齐器（在 ASR 模型里），照常加载。"""
    from sheetsage2_webui.asr import AsrConfig, LyricLine
    from sheetsage2_webui.tifa_align import default_model_path
    from sheetsage2_webui.webui.runner import RunnerSession

    if not default_model_path().is_file():
        pytest.skip("没装 TIFA（可选权重），这条优化不适用")
    config = AsrConfig(asr_model=tmp_path)
    chinese = [LyricLine(1.0, "晚风停在旧站台")]
    japanese = [LyricLine(1.0, "君が僕の心臓じゃないせいで")]

    spare = RunnerSession._align_only_engine(chinese, config, "Chinese")
    assert spare is not None and spare.loaded is False      # 拿到手的是"没加载"的引擎
    assert RunnerSession._align_only_engine(japanese, config, None) is None
    assert RunnerSession._align_only_engine(None, config, "Chinese") is None

