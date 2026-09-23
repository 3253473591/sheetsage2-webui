"""扒谱任务 worker：作为**独立子进程**运行完整链路。

为什么要独立进程：任务文档 4.3/4.4 要求「停止」能强杀推理进程并清理显存。
只有在独立进程里跑，父进程才能用 ``taskkill /T /F`` 连带杀掉 torch 派生的一切。

进度协议（stdout 逐行 JSON，父进程按前缀过滤转发给 SSE）::

    @@P {"stage":"识别音乐","percent":42.0,"text":"窗口 1/3","detail":"..."}
    @@R {"...":"分析概览 + 诊断"}      # 成功，最后一行
    @@E {"message":"...","traceback":"..."}   # 失败

任务文档硬约束：**进度必须来自后端真实状态，禁止前端假推进**。本 worker 只在
真实阶段切换与真实窗口/解码计数推进时才写进度行。
"""

from __future__ import annotations

import argparse
import gc
import json
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# 允许 `python -m app.worker` 与直接运行两种方式
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.abcfilter import filter_abc  # noqa: E402
from app.overview import summarize  # noqa: E402
from app.rebuild import normalize_export_voices  # noqa: E402

PREFIX_PROGRESS = "@@P "
PREFIX_RESULT = "@@R "
PREFIX_ERROR = "@@E "


def _emit(prefix: str, payload: dict[str, Any]) -> None:
    sys.stdout.write(prefix + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _emit_progress(**payload: Any) -> None:
    _emit(PREFIX_PROGRESS, payload)


def _lerp(a: float, b: float, t: float) -> float:
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t


def _stage_percent(stage: str, t: float = 1.0) -> float:
    lo, hi = config.STAGE_RANGES[stage]
    return round(_lerp(lo, hi, t), 2)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SheetSage2 扒谱 worker")
    p.add_argument("--audio", required=True, help="输入音频路径")
    p.add_argument("--out", required=True, help="输出目录")
    p.add_argument("--preset", choices=("default", "paper"), default="default")
    p.add_argument("--dtype", choices=("bf16", "fp32"), default="bf16")
    p.add_argument("--max-seconds", type=float, default=None, help="只分析前 N 秒；缺省为整首")
    p.add_argument("--overlap", type=float, default=None)
    p.add_argument("--lookahead", type=float, default=None)
    p.add_argument(
        "--export-voices",
        default=None,
        help=(
            "要导出的内容，逗号分隔：vocal,ins,chords（默认 vocal）。"
            "对应前端 ③ 的三个勾选「人声主旋律 / 器乐旋律 / 和弦」——勾什么导什么。"
        ),
    )
    p.add_argument(
        "--no-only-melody",
        dest="only_melody",
        action="store_false",
        default=None,
        help="[旧参数] 取消「仅主旋律乐谱」：等价于 --export-voices vocal,ins,chords。",
    )
    p.add_argument(
        "--drop-intro-outro",
        action="store_true",
        default=False,
        help="额外删除 %% intro / %% outro 段落块（默认保留全部段落）。",
    )
    p.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cuda", "cpu"),
        help="推理设备；auto = 有 N 卡用 GPU，否则自动 CPU（整合包默认）。",
    )
    p.add_argument("--lyrics", default="la", help="一期统一歌词占位")
    p.add_argument(
        "--no-octave-fix",
        dest="fix_octave",
        action="store_false",
        default=True,
        help="关闭八度校正（默认开：模型的输出整体高一个八度，见交接文档 §14）。",
    )
    return p.parse_args(argv)


def pick_device(requested: str, requested_dtype: str) -> tuple[str, str, str]:
    """决定推理设备与计算精度，返回 ``(device, dtype, note)``。

    整合包里测试者的显卡千奇百怪（没有 N 卡、驱动老、显存被别人占着），
    所以这里的原则是 **永远选一个能跑的组合**，而不是拒绝开工：

    * ``auto``：CUDA 可用就走 GPU，否则 CPU；
    * 显式写了 ``cuda`` 但当前环境用不了 CUDA —— 也降级 CPU，并把原因写进 note；
    * CPU 上把 ``bf16`` 换成 ``fp32``：CPU 的 bf16 算子既没加速又容易踩未实现路径。
    """
    note = ""
    cuda_ok = False
    try:
        import torch

        cuda_ok = bool(torch.cuda.is_available())
    except Exception as exc:  # noqa: BLE001 - torch 缺失/损坏都按 CPU 处理
        note = f"无法导入 torch（{type(exc).__name__}: {exc}），按 CPU 处理"

    device = requested
    if device == "auto":
        device = "cuda" if cuda_ok else "cpu"
    if device == "cuda" and not cuda_ok:
        device = "cpu"
        note = (note + "；" if note else "") + "请求了 GPU 但当前环境用不了 CUDA，已改用 CPU"

    dtype = requested_dtype
    if device == "cpu" and dtype == "bf16":
        dtype = "fp32"
        note = (note + "；" if note else "") + "CPU 上 bf16 无加速，已改用 fp32"

    return device, dtype, note


#: `is_gpu_failure` 用到的错误特征（按小写匹配）。只列**设备侧**的失败。
_GPU_FAILURE_HINTS = (
    "cuda", "no kernel image", "device-side assert", "cublas", "cudnn",
    "cufft", "out of memory", "nvidia", "nvml", "driver",
)


def is_gpu_failure(exc: BaseException) -> bool:
    """该异常是否表示「这台机器的 GPU 这条路走不通」。

    为什么需要单独判一次：**GPU 能不能用，光看 ``torch.cuda.is_available()``
    是判断不出来的**。以整合包默认的 cu130 wheel 为例，它只编译了 Turing 及以上；
    插着更老的卡时 ``is_available()`` 仍是 True，加载权重也正常（``.to(device)``
    不执行 kernel），要等**第一颗 kernel 真正执行**才抛
    ``no kernel image is available for execution on the device``。
    显存不足同理：通常炸在推理中途，而不是加载权重时。

    只对这类错误退回 CPU 重跑。代码/数据的逻辑错误（例如 overlap 前缀塞满上下文）
    在 CPU 上一样会失败，重跑只会白白再等一遍。
    """
    try:
        import torch
    except Exception:  # noqa: BLE001 - torch 都没了，那就不是 GPU 的问题
        return False

    if isinstance(exc, getattr(torch.cuda, "OutOfMemoryError", ())):
        return True
    if type(exc).__name__ in ("OutOfMemoryError", "AcceleratorError", "CudaError"):
        return True

    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in _GPU_FAILURE_HINTS)


def load_model(device: str, on_stage):
    """加载 SheetSage2（含 MERT-v2-FullSong 主干）。

    权重与推理代码都在本地目录，用 ``local_files_only=True`` 避免任何联网行为。
    """
    on_stage("start")
    import os

    import torch
    from transformers import AutoModel

    if device == "cpu":
        # CPU 推理就是靠核数堆出来的，别再自我限制成 4 线程
        torch.set_num_threads(max(1, os.cpu_count() or 4))
    else:
        torch.set_num_threads(min(4, torch.get_num_threads()))
    model = AutoModel.from_pretrained(
        str(config.SHEETSAGE2_DIR),
        trust_remote_code=True,
        local_files_only=True,
        base_model_path=str(config.MERT_FULLSONG_DIR),
    )
    model = model.eval().to(device)
    on_stage("done")
    return model


def _preflight_ffmpeg() -> str:
    """确认**推理链路**能调到 ffmpeg，返回实际使用的路径。

    为什么不能只靠 ``envcheck``：SheetSage2 的 ``audio_sheetsage2.py::load_audio``
    里写死了 ``shutil.which("ffmpeg")`` 和 ``subprocess.run(["ffmpeg", ...])``，
    只看 PATH。``config`` 在 import 期已经把随包 ffmpeg 接上 PATH
    （见 ``config.ensure_ffmpeg_on_path``），这里再确认一次并给出**能照着做**的提示，
    替代 vendored 代码那句 "install it and add it to PATH"（测试者看了不知道装什么）。
    """
    config.ensure_ffmpeg_on_path()
    found = shutil.which("ffmpeg")
    if found:
        return found

    resolved = config.ffmpeg_path()
    hint = (
        f"已解析到 {resolved}，但它不在 PATH 上" if resolved
        else "既没在包内找到 runtime\\ffmpeg\\bin\\ffmpeg.exe，PATH 上也没有 ffmpeg"
    )
    raise RuntimeError(
        "找不到 ffmpeg，无法解码音频（mp3/flac 都需要它）。"
        f"{hint}。"
        "全量包请确认 runtime\\ffmpeg\\bin\\ffmpeg.exe 还在（解压不完整就重新解压）；"
        "也可以设环境变量 FFMPEG_BIN 直接指向 ffmpeg.exe。"
        "拿不准就跑一次 selfcheck.bat，把环境快照发给 AI。"
    )


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    audio = Path(args.audio)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if not audio.is_file():
        _emit(PREFIX_ERROR, {"message": f"音频不存在：{audio}"})
        return 1

    started = time.time()
    timeline: list[dict[str, Any]] = []
    # 「要导出哪些内容」：用户勾了什么就导什么（人声主旋律 / 器乐旋律 / 和弦）
    export_voices = normalize_export_voices(args.export_voices, args.only_melody)
    # 任务文档 3.6.3 硬指标：百分比**单调不减**，仅新任务重置。
    # 模型对每个窗口都会重新从低值发 encoding 回调（实测出现 50% → 32% 倒退），
    # 因此这里统一做高水位钳制，保证前端拿到的 percent 永不下降。
    high_water = {"percent": 0.0}

    def mark(stage: str, percent: float, text: str, **extra: Any) -> None:
        percent = max(float(percent), high_water["percent"])
        high_water["percent"] = percent
        elapsed = round(time.time() - started, 2)
        timeline.append({"stage": stage, "percent": percent, "at": elapsed})
        _emit_progress(stage=stage, percent=percent, text=text, elapsed=elapsed, **extra)

    try:
        # ---------------- 前置检查：ffmpeg ----------------
        # 必须排在加载模型之前：模型要 2.4 GB、加载几十秒，等加载完才发现解不了音频太亏。
        ffmpeg_used = _preflight_ffmpeg()

        # ---------------- 加载模型 0–2% ----------------
        load_started = time.time()

        device, dtype, device_note = pick_device(args.device, args.dtype)

        def on_load(phase: str) -> None:
            if phase == "done":
                return
            detail = f"{device} · {dtype}" + (f"（{device_note}）" if device_note else "")
            mark("加载模型", _stage_percent("加载模型", 0.1), "加载模型…", detail=detail)

        mark(
            "加载模型",
            _stage_percent("加载模型", 0.0),
            "加载模型…",
            detail=f"{device} · {dtype}" + (f"（{device_note}）" if device_note else ""),
        )
        try:
            model = load_model(device, on_load)
        except Exception as exc:  # noqa: BLE001
            # GPU 路径在测试者机器上可能因为驱动过旧 / 显存不足 / 算子不支持而失败。
            # 与其让整个任务挂掉，不如自动退回 CPU 再试一次（慢，但能出结果）。
            if device != "cuda":
                raise
            device, dtype = "cpu", "fp32"
            device_note = (
                f"GPU 初始化失败（{type(exc).__name__}: {exc}），已自动改用 CPU 推理"
            )
            mark("加载模型", _stage_percent("加载模型", 0.15), "GPU 不可用，改用 CPU 重新加载…",
                 detail=device_note)
            try:
                import torch as _torch

                _torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001 - 清理失败不影响重试
                pass
            model = load_model(device, on_load)

        load_seconds = time.time() - load_started
        mark(
            "加载模型",
            _stage_percent("加载模型", 1.0),
            "模型加载完成",
            detail=f"{device} · {dtype}",
        )

        # ---------------- 读取音频 2–5% ----------------
        mark("读取音频", _stage_percent("读取音频", 0.0), "读取并重采样音频…")

        # ---------------- 识别音乐 30–70% ----------------
        recognize_started = time.time()
        # 「生成乐谱」阶段稍后统一推进，见下方 notation 分支的说明
        notation_pending = {"seen": False}

        def progress_cb(info: dict[str, Any]) -> None:
            stage = info.get("stage")
            if stage == "audio":
                mark("读取音频", _stage_percent("读取音频", 1.0), "读取并重采样音频…")
            elif stage == "encoding":
                window, windows = info.get("window", 1), info.get("windows", 1) or 1
                mark(
                    "识别音乐",
                    _stage_percent("识别音乐", 0.05),
                    f"编码音频窗口 {window}/{windows}",
                    detail=f"窗口起点 {info.get('start', 0):.0f}s",
                )
            elif stage == "decoding":
                window, windows = info.get("window", 1), info.get("windows", 1) or 1
                tokens = info.get("tokens", 0)
                maximum = getattr(model, "max_output_seq_len", None) or 0
                inner = (tokens / maximum) if maximum else 0.0
                # 解码阶段占总识别区间的绝大部分，用真实 token 数线性推进
                t = _lerp(0.05, 0.95, ((window - 1) + inner) / max(1, windows))
                mark(
                    "识别音乐",
                    _stage_percent("识别音乐", t),
                    f"识别旋律/和弦/节拍 · 窗口 {window}/{windows}",
                    detail=f"{tokens} tokens",
                )
            elif stage == "window_complete":
                window, windows = info.get("window", 1), info.get("windows", 1) or 1
                t = _lerp(0.05, 0.95, window / max(1, windows))
                mark("识别音乐", _stage_percent("识别音乐", t), f"窗口 {window}/{windows} 完成")
            elif stage == "notation":
                # 该回调发生在 transcribe() 内部、返回之前。若此处直接推进到
                # 「生成乐谱」，阶段顺序会变成「生成乐谱 → 识别音乐完成」，
                # 与文档的阶段清单顺序矛盾。因此只记录，等 transcribe()
                # 返回、识别阶段收尾后再推进。
                notation_pending["seen"] = True

        options: dict[str, Any] = {
            "preset": args.preset,
            "dtype": dtype,
            "output_dir": str(out),
            "progress": progress_cb,
        }
        if args.max_seconds and args.max_seconds > 0:
            options["max_seconds"] = float(args.max_seconds)
        if args.overlap is not None:
            options["overlap_seconds"] = float(args.overlap)
        if args.lookahead is not None:
            options["lookahead_seconds"] = float(args.lookahead)

        try:
            result = model.transcribe(str(audio), **options)
        except Exception as exc:  # noqa: BLE001
            # 关键兜底：GPU 的失败点通常在**推理中途**——算力不在这份 torch 的 arch
            # 列表里、显存不够、驱动 / cuBLAS 起不来——而不是加载权重的时候。
            # 上面那个 try 只包住 load_model，救不了这一类；再往外一层只会上报失败。
            # 所以识别也必须纳入兜底：换 CPU 重新加载模型，整段重跑一遍。
            if device != "cuda" or not is_gpu_failure(exc):
                raise
            device, dtype = "cpu", "fp32"
            options["dtype"] = dtype  # 必须同步：transcribe 用的是 options 里的精度
            device_note = (
                f"GPU 推理失败（{type(exc).__name__}: {exc}）"[:200]
                + "，已自动改用 CPU 重新加载并重跑"
            )
            mark(
                "识别音乐",
                _stage_percent("识别音乐", 0.0),
                "GPU 推理失败，改用 CPU 重新加载并重跑…",
                detail=device_note,
            )

            def on_reload(phase: str) -> None:
                # 重跑仍挂在「识别音乐」阶段。若沿用上面的 on_load，进度会从
                # 「识别音乐」倒回「加载模型」，破坏文档规定的阶段顺序。
                if phase == "done":
                    return
                mark(
                    "识别音乐",
                    _stage_percent("识别音乐", 0.0),
                    "正在用 CPU 重新加载模型…",
                    detail=f"cpu · fp32（{device_note}）",
                )

            model = None  # 先松开 CUDA 上的权重，再重新装 CPU 版
            gc.collect()
            try:
                import torch as _torch

                _torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001 - 清理失败不影响重试
                pass

            model = load_model(device, on_reload)
            result = model.transcribe(str(audio), **options)
        recognize_seconds = time.time() - recognize_started
        mark("识别音乐", _stage_percent("识别音乐", 1.0), "识别完成")

        # ---------------- 生成乐谱 / MIDI 后处理 / SVP 70–95% ----------------
        mark("生成乐谱", _stage_percent("生成乐谱", 0.15), "整理乐谱与产物…")

        raw_abc = result.get("abc") or ""

        # ---------------- 八度校正 ----------------
        # SheetSage2 对**所有**素材的输出都高一个八度（实测：全混音 / 干净人声 stem /
        # 两首不同的歌，音频基频都在 f/2；用户耳判确认）。见交接文档 §14。
        # 这里**不写死 -12**：用音频投票判断，判不出来就不动——否则以后模型修好了，
        # 写死会把本来正确的输出改坏。
        octave_info: dict[str, Any] = {"shift": 0, "reason": "未检测（--no-octave-fix）"}
        if args.fix_octave:
            try:
                from app.octave import (
                    estimate_octave_shift, read_lab_notes, shift_abc_octaves, shift_lab_pitches,
                )

                lab_notes = read_lab_notes(out / "melody_vocal.lab")
                octave_info = estimate_octave_shift(audio, lab_notes)
                shift = int(octave_info.get("shift") or 0)
                if shift and raw_abc:
                    raw_abc = shift_abc_octaves(raw_abc, shift // 12)
                    result["abc"] = raw_abc
                    (out / "score.abc").write_text(raw_abc, encoding="utf-8")
                    for name in ("melody_vocal.lab", "melody_instrumental.lab", "melody_full.lab"):
                        shift_lab_pitches(out / name, shift)
                    octave_info["applied"] = True
                else:
                    octave_info["applied"] = False
            except Exception as exc:  # noqa: BLE001 - 校正失败不能拖垮扒谱
                octave_info = {"shift": 0, "reason": f"校正失败：{type(exc).__name__}: {exc}"}

        filtered_abc, report = filter_abc(
            raw_abc,
            export_voices=export_voices,
            drop_intro_outro=args.drop_intro_outro,
        )

        filtered_path = out / "score.melody.abc"
        if filtered_abc:
            filtered_path.write_text(filtered_abc, encoding="utf-8")

        mark("生成乐谱", _stage_percent("生成乐谱", 0.45), "汇总分析概览…")

        overview = summarize(
            out,
            audio=audio,
            extra_diagnostics=[
                f"导出内容={export_voices}；"
                f"保留声部={report.kept_voices or ['(无 V: 标记)']}，"
                f"删除声部={report.dropped_voices or ['(无)']}",
                f"删除 %% intro/%% outro 段落：{'开' if args.drop_intro_outro else '关'}"
                + (f"；已删除段落={report.dropped_sections}" if report.dropped_sections else ""),
                f"ABC 过滤：{report.lines_in} 行 → {report.lines_out} 行",
                (
                    f"八度校正：{'已应用 ' + str(octave_info.get('shift')) + ' 半音'
                     if octave_info.get('shift') else '未改动'}"
                    f"（{octave_info.get('reason', '')}）"
                ),
                f"模型加载 {load_seconds:.2f}s；识别 {recognize_seconds:.2f}s",
                f"ffmpeg {ffmpeg_used}",
                f"推理设备 {device} · 精度 {dtype}"
                + (f"（{device_note}）" if device_note else ""),
            ],
        )

        # ---- 导出产物（SVP / MIDI）----
        # 设计收口：SVP 与 MIDI **一律由 ABC 生成**（app.rebuild），不再另从
        # playback.json 生成一份。原因：
        #   1. 页面「乐谱编辑」以 ABC 为可编辑源，导出必须与它同源；
        #   2. 两套来源会产生分歧——实测遇到过 playback.json 里没有 Vocal 轨时，
        #      notes_from_playback 静默回退到 tracks[0]，把**器乐轨当成「主人声」**
        #      写进 SVP，属于拿错数据当交付物。
        mark("生成乐谱", _stage_percent("生成乐谱", 0.7), "生成 SVP / MIDI…")

        mark("生成乐谱", _stage_percent("生成乐谱", 0.9), "写出导出产物…")

        # ---- 统一导出目录 export/：本次推理的**初始**产物 ----
        # 之后用户在 ③ 钢琴卷帘上的改动走 POST /roll（roll.json → 同一个 rebuild_exports），
        # 所以这里的产物会在用户第一次保存卷帘时被覆盖成编辑后的版本。
        # 不再产出 .abc：ABC 只是模型的输出格式，用户侧的可编辑真相是 roll.json。
        from app.rebuild import rebuild_exports

        export_info = rebuild_exports(
            out,
            filtered_abc or raw_abc,
            bpm=(overview.get("bpm") or overview.get("score_bpm") or 120.0),
            export_voices=export_voices,
            lyrics=args.lyrics,
            project_name=audio.stem,
        )

        # svp_info 由导出结果派生，保证 summary["svp"] 与实际交付的 SVP 一致
        _svp_export = next((e for e in export_info.get("exports", []) if e.get("kind") == "svp"), None)
        if _svp_export:
            svp_info: dict[str, Any] = {
                "written": True,
                "path": _svp_export["path"],
                "version": _svp_export["version"],
                "tracks": _svp_export.get("tracks"),
                "notes": _svp_export.get("notes"),
                "bpm": export_info.get("bpm"),
            }
        else:
            svp_info = {
                "written": False,
                "reason": export_info.get("error") or "未生成 SVP",
                "detail": export_info.get("warnings"),
            }

        # ---------------- 完成 100% ----------------
        # 必须在构造 summary 之前记录：summary["timeline"] 与写盘的 summary.json
        # 都要包含最终阶段，否则持久化的 timeline 会缺「完成」、
        # progress_monotonic 也覆盖不到 100%。
        mark("完成", _stage_percent("完成", 1.0), "完成")

        summary = dict(overview)
        summary["abc_filtered"] = filtered_abc
        summary["abc_filter"] = report.as_dict()
        summary["svp"] = svp_info
        summary["export"] = export_info
        # 速度表（变速曲）：写进 summary 供前端「分析概览」显示。单速度时长度为 1。
        tempo_map = export_info.get("tempo_map") or []
        if len(tempo_map) > 1:
            chain = " → ".join(f"{item['bpm']:.0f}" for item in tempo_map)
            summary["tempo_map"] = tempo_map
            summary.setdefault("diagnostics", []).append(
                f"变速曲：速度表 {len(tempo_map)} 段（{chain} BPM），"
                "SVP 的 time.tempo 与 MIDI 的 set_tempo 已按它写出"
            )
        summary["audio_name"] = audio.name
        summary["export_voices"] = export_voices
        # 旧字段保留，避免前端/脚本读不到而误判
        summary["only_melody"] = export_voices == ["vocal"]
        summary["octave_fix"] = octave_info
        summary["drop_intro_outro"] = args.drop_intro_outro
        summary["preset"] = args.preset
        summary["dtype"] = dtype
        summary["dtype_requested"] = args.dtype
        summary["max_seconds"] = args.max_seconds
        summary["device"] = device
        summary["device_requested"] = args.device
        if device_note:
            summary["device_note"] = device_note
            summary.setdefault("diagnostics", []).append(f"设备选择：{device_note}")
        summary["elapsed_total"] = round(time.time() - started, 2)
        summary["timeline"] = timeline
        # 自校验：进度必须单调不减（文档 3.6.3 / 7 验收标准「进度条不倒退」）
        _percents = [t["percent"] for t in timeline]
        summary["progress_monotonic"] = all(b >= a for a, b in zip(_percents, _percents[1:]))
        # 概览数据可能因产物增加而变化，重新取一次文件清单
        summary["files"] = [
            {
                "name": p.name,
                "rel": str(p.relative_to(out)).replace("\\", "/"),
                "size": p.stat().st_size,
                "path": str(p),
            }
            for p in sorted(out.rglob("*"))
            if p.is_file()
        ]

        summary_file = out / "summary.json"
        summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        _emit(PREFIX_RESULT, summary)
        return 0

    except BaseException as exc:  # noqa: BLE001 - 需要把一切失败都报给前端
        _emit(
            PREFIX_ERROR,
            {
                "message": f"{type(exc).__name__}: {exc}"[:500],
                "traceback": traceback.format_exc()[-4000:],
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
