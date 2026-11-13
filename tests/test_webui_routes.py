"""HTTP 契约：路由的状态码、JSON 形状、SSE、下载与 Range。

这一组只盯着"外面看到的样子"：改内部结构时它不该需要改（除非契约真的变了）。
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
import pytest
from pathlib import Path

from support_webui import (  # noqa: E402
    WAV_BYTES,
    DEFAULT_OPTIONS,
    GateRunner,
    fake_transcribe,
    fake_lyrics,
    fake_runners,
    make_client,
    job_dir,
    job_internal,
    wait_for_state,
    wait_for_stage,
    upload,
    collect_events,
    real_wav,
    stem_upload,
)
from sheetsage2_webui.webui.media import (  # noqa: E402
    clamp_buckets,
    compute_peaks,
    parse_range,
)
from sheetsage2_webui.bootstrap import (  # noqa: E402
    find_ffmpeg,
)


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


def test_favicon_does_not_404(tmp_path):
    """浏览器总来要一次图标，回 204 就行（不然日志里天天一行 404）。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        assert client.get("/favicon.ico").status_code == 204
    manager.shutdown()


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


def test_compute_peaks_on_a_real_file(tmp_path):
    path = tmp_path / "tone.wav"
    path.write_bytes(real_wav(tmp_path))
    data = compute_peaks(path, 32)
    assert data["buckets"] == 32 and data["duration"] == pytest.approx(0.5, abs=0.05)


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


def test_half_written_project_json_is_404_not_500(tmp_path):
    """G5：产物被写了一半时，接口给"还没准备好"，不是 500。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        (job_dir(manager, job) / "project.json").write_text('{"timeline":', encoding="utf-8")
        assert client.get(f"/api/jobs/{job['id']}/project").status_code == 404


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
