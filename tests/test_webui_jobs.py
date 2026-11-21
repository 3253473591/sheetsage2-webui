"""任务域：状态机、队列、阶段生命周期、参数与作废、完成门禁、看门狗。

这些都是"作业本身"的规则，和 HTTP 形状无关——所以它们用 `JobManager` 直接验，
不经过路由（路由的契约在 `test_webui_routes.py`）。
"""

from __future__ import annotations

import io
import json
import time
import pytest

from support_webui import (  # noqa: E402
    WAV_BYTES,
    FAKE_CHARS,
    PROJECT_STUB,
    DEFAULT_OPTIONS,
    Char,
    GateRunner,
    fake_transcribe,
    fake_lyrics,
    fake_runners,
    write_artifacts,
    make_client,
    job_dir,
    job_internal,
    wait_for_state,
    wait_for_stage,
    upload,
    collect_events,
    make_job,
)
from sheetsage2_webui.webui.runner import (  # noqa: E402
    _sections_for,
    asr_model_choices,
    build_highlights,
    grid_note_name,
    group_lyric_lines,
    transcribe_options,
)
from sheetsage2_webui.webui.jobs import (  # noqa: E402
    PARAM_OWNER,
    JobManager,
    UploadRejected,
    param_choices,
)
from sheetsage2_webui.webui.job_options import (  # noqa: E402
    describe_change,
    validate_options,
)

LRC_TEXT = "[00:01.00]晚风停在旧站台\n[00:05.00]旧站台"


def test_jobs_facade_keeps_the_old_public_exports():
    """`webui.jobs` 是**兼容门面**：以前能从这里 import 的名字现在还得能 import。

    计划 §3.2 的“保留兼容入口”不是口号：外部脚本/插件只认这条路径，拆模块时漏一个
    名字就是一次 `ImportError`。
    """
    import sheetsage2_webui.webui.jobs as facade

    for name in ("option_values", "ASR_MODEL_PREFIX", "validate_options", "PARAM_SPECS",
                 "safe_name", "scan_files", "project_folder_name", "is_internal",
                 "claim_project_dir", "content_fingerprint"):
        assert hasattr(facade, name), name
    # 私有的也曾经能 import（有人在调试时用过），一并保留
    for name in ("_per_phase", "_option_label", "_UNSAFE", "_PhaseWatch"):
        assert hasattr(facade, name), name


def test_manager_timeouts_stay_writable_attributes(tmp_path):
    """阈值以前是普通属性（`manager.stall_timeout = {...}` 直接改）；现在由调度器持有，
    门面必须保留可写语义——否则运维脚本热调阈值会静默失效。"""
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        manager.stall_timeout = {"transcribe": 5.0, "lyrics": 6.0}
        assert manager.stall_timeout == {"transcribe": 5.0, "lyrics": 6.0}
        manager.stall_timeout = 7.0                       # 一个数 = 两个阶段都用它
        assert manager.stall_timeout == {"transcribe": 7.0, "lyrics": 7.0}
        manager.hard_timeout = 8.0
        assert manager.hard_timeout["lyrics"] == 8.0
        manager.watchdog_interval = 0.5
        assert manager.watchdog_interval == 0.5
        manager.trash_purge_interval = 0.25
        assert manager.trash_purge_interval == 0.25
    finally:
        manager.shutdown()


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


def test_validate_options_accepts_lrc_text():
    """歌词源是自由文本，不是枚举值：粘贴 LRC 不能去查 `options` 白名单。"""
    assert validate_options({"lyrics_lrc": LRC_TEXT}) == {"lyrics_lrc": LRC_TEXT}


def test_validate_options_accepts_empty_lrc_to_clear():
    assert validate_options({"lyrics_lrc": ""}) == {"lyrics_lrc": ""}
    assert validate_options({"lyrics_lrc": None}) == {"lyrics_lrc": ""}


def test_validate_options_rejects_text_without_lyrics():
    with pytest.raises(ValueError, match="唱词"):
        validate_options({"lyrics_lrc": "[ti:标题]\n[ar:歌手]\n[by:某人]"})


def test_validate_options_rejects_oversized_lrc():
    with pytest.raises(ValueError, match="太长"):
        validate_options({"lyrics_lrc": "啊" * 200_001})


def test_describe_change_summarizes_lrc_text():
    """日志/作废提示里不能塞整首 LRC；只报行数和字数。"""
    why = describe_change({"lyrics_lrc": ""}, {"lyrics_lrc": LRC_TEXT})
    assert "晚风" not in why
    assert "2 行" in why


def test_group_lyric_lines_breaks_on_gap_and_length():
    chars = [Char(**item) for item in FAKE_CHARS]
    lines = group_lyric_lines(chars)
    assert [line["text"] for line in lines] == ["晚风停在", "旧站台"]
    assert lines[0]["start"] == 1.0 and lines[1]["end"] == 5.9


def test_metadata_counts_the_terminal_event(tmp_path):
    """`job.json` 的 `events` 计数必须包含终态事件（诊断包对不上就是白写）。

    收尾把"发终态事件"和"落盘"都放在同一把 `job.lock` 里；顺序是**先发后写**，
    所以落盘的快照不会少算最后那条事件。
    """
    import threading

    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        job = manager.create("歌.wav", io.BytesIO(WAV_BYTES))
        manager.run_stage(job.id, "transcribe")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and job.snapshot()["state"] != "done":
            time.sleep(0.02)
        assert job.snapshot()["state"] == "done"
        meta = json.loads((job.root / "job.json").read_text(encoding="utf-8"))
        assert meta["events"] == len(job.events_since(0))
        assert meta["state"] == "done"
    finally:
        manager.shutdown()

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


def test_env_exposes_the_param_panel(tmp_path):
    """`/api/env` 把参数表发给前端：两组的行、默认值、以及"是否锁定"。"""
    client, _ = make_client(tmp_path, fake_runners())
    with client:
        body = client.get("/api/env").json()
    params = body["params"]
    assert [item["key"] for item in params["transcribe"]] == \
        ["dtype", "device", "range", "quantize"]
    assert [item["key"] for item in params["lyrics"]] == \
        ["asr_model", "source", "skip_instrumental", "language", "lyrics_lrc"]
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
        "lyrics.json", "melody.mid", "meta.json", "notation/song_melody.mid",
        "project.json", "report.json", "structure.lab", "summary.json"]


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


def test_manager_create_writes_and_cleans_up_rejected_upload(tmp_path):
    manager = JobManager(tmp_path / "data", runners=fake_runners(), max_upload_mb=0.001)
    try:
        with pytest.raises(UploadRejected):
            manager.create("big.wav", io.BytesIO(b"x" * 100_000))
        # 半截工程目录要清干净（工程目录 = output/<歌名>/）
        assert list((tmp_path / "data").iterdir()) == []
    finally:
        manager.shutdown()


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
