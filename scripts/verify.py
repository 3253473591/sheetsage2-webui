"""SheetSage2 集成验证脚本。

回答一个问题：这套集成能不能"稳定地调用并产出自洽的结果"。

验证项
------
1. 环境       —— ffmpeg、CUDA、依赖版本
2. 模型加载   —— 能加载到指定设备，且能重复加载
3. 全曲转写   —— 每首音频跑一次完整转写，产物齐全且自洽
4. 确定性     —— 同一段音频跑两次，事件与 MIDI 逐字节一致
5. 重新加载   —— 新进程内重新加载模型，结果与首次一致
6. 离线加载   —— local_files_only 可用（缓存完整，脱离网络也能跑）
7. CPU 路径   —— 无 GPU 时也能出结果

用法：
    python scripts/verify.py                 # 全部验证
    python scripts/verify.py --skip-slow     # 跳过 CPU / 离线 / 重新加载
    python scripts/verify.py --reuse         # 复用 output/ 下已有产物，不重跑全曲
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
import traceback
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

warnings.filterwarnings("ignore", message=".*pkg_resources is deprecated.*")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

import sheetsage2_webui as ss  # noqa: E402
from sheetsage2_webui.bootstrap import find_ffmpeg  # noqa: E402
from sheetsage2_webui.engine import EngineConfig, SheetSage2Engine, TranscriptionError  # noqa: E402
from sheetsage2_webui.result import inspect_output_dir, read_rows, summarize_result  # noqa: E402
from sheetsage2_webui.pipeline import run as run_pipeline  # noqa: E402
from sheetsage2_webui.service import transcribe_to_directory  # noqa: E402

AUDIO_SUFFIXES = {".flac", ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}
DETERMINISM_SECONDS = 30.0
CPU_CHECK_SECONDS = 8.0
ENTRY_CHECK_SECONDS = 15.0


# --------------------------------------------------------------------- 工具
@dataclass
class Check:
    """一条验证项的结果。"""

    id: str
    title: str
    passed: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return dict(id=self.id, title=self.title, passed=self.passed, detail=self.detail,
                    data=self.data, seconds=round(self.seconds, 2))


class Recorder:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def run(self, check_id: str, title: str, fn: Callable[[], tuple[bool, str, dict]]) -> Check:
        print(f"[验证] {title} ...", flush=True)
        started = time.monotonic()
        try:
            passed, detail, data = fn()
        except Exception as exc:
            passed = False
            detail = f"{type(exc).__name__}: {exc}"
            data = {"traceback": traceback.format_exc()[-2000:]}
        check = Check(check_id, title, passed, detail, data, time.monotonic() - started)
        self.checks.append(check)
        print(f"        -> {'通过' if passed else '失败'} ({check.seconds:.1f}s) {detail}", flush=True)
        return check

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)


def slug(path: Path) -> str:
    keep = [c if (c.isalnum() or c in "._-") else "_" for c in path.stem]
    return "".join(keep).strip("_") or "audio"


def discover_audio() -> list[Path]:
    found = [p for p in sorted(PROJECT_ROOT.iterdir())
             if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES]
    samples = PROJECT_ROOT / "samples"
    if samples.is_dir():
        found += [p for p in sorted(samples.iterdir())
                  if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES]
    return found


def canonical_hash(path: Path) -> str:
    """对 JSON 做规范化后再哈希，避免键序/空白差异造成假阴性。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:20]


def fingerprint(output_dir: Path) -> dict[str, str]:
    """一次转写的"指纹"：事件表 + 主 MIDI。"""
    result: dict[str, str] = {}
    for name in ("events.json", "transcription.mid", "melody_vocal.mid",
                 "melody_instrumental.mid", "chord.lab", "key.lab", "structure.lab"):
        path = output_dir / name
        if not path.is_file():
            continue
        result[name] = canonical_hash(path) if name.endswith(".json") else file_hash(path)
    return result


# --------------------------------------------------------------------- 绘图
def render_roll(output_dir: Path, summary: dict, target: Path) -> None:
    """把 melody_full.lab 画成钢琴卷帘预览图，方便肉眼核对。"""
    import matplotlib

    matplotlib.use("Agg")
    # 标题里有中文歌名，默认的 DejaVu Sans 没有 CJK 字形。
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    import matplotlib.pyplot as plt

    rows = read_rows(output_dir / "melody_full.lab")
    if not rows:
        return
    vocal, ins = [], []
    for row in rows:
        try:
            start, end, pitch, track = float(row[0]), float(row[1]), int(float(row[2])), int(float(row[3]))
        except (ValueError, IndexError):
            continue
        (vocal if track == 0 else ins).append((start, end, pitch))

    fig, ax = plt.subplots(figsize=(14, 5), dpi=110)
    for notes, color in ((vocal, "#e8524f"), (ins, "#3a86ff")):
        for start, end, pitch in notes:
            ax.add_patch(plt.Rectangle((start, pitch - 0.35), max(end - start, 0.02), 0.7,
                                       color=color, alpha=0.85))
    ax.set_xlim(0, summary["duration_seconds"])
    allp = [p for _, _, p in vocal + ins]
    if allp:
        ax.set_ylim(min(allp) - 3, max(allp) + 3)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("MIDI pitch")
    ax.set_title(f"{summary['audio']}  |  key={','.join(summary['keys'])}  "
                 f"{summary.get('meter') or ''} BPM≈{summary.get('bpm')}  "
                 f"notes={summary['notes']['melody']}")
    ax.grid(alpha=0.25, linewidth=0.5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in ("#e8524f", "#3a86ff")]
    ax.legend(handles, ["Vocal", "Ins"], loc="upper right", fontsize=8)
    fig.tight_layout()
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target)
    plt.close(fig)


# --------------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SheetSage2 集成验证")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--report-dir", type=Path, default=PROJECT_ROOT / "reports")
    parser.add_argument("--tmp-root", type=Path, default=PROJECT_ROOT / "tmp" / "verify")
    parser.add_argument("--skip-slow", action="store_true", help="跳过重新加载 / 离线 / CPU 验证")
    parser.add_argument("--reuse", action="store_true", help="复用已有全曲产物，不重跑")
    parser.add_argument("--model", default=EngineConfig.model_id)
    args = parser.parse_args(argv)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    args.tmp_root.mkdir(parents=True, exist_ok=True)

    audio_files = discover_audio()
    rec = Recorder()
    context: dict[str, Any] = {}

    # ---------------------------------------------------------------- 1 环境
    def check_env():
        import torch
        import transformers

        ffmpeg = find_ffmpeg()
        info = {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "cuda_available": torch.cuda.is_available(),
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "ffmpeg": ffmpeg,
            "audio_files": [p.name for p in audio_files],
        }
        context["env"] = info
        problems = []
        if not torch.cuda.is_available():
            problems.append("CUDA 不可用")
        if not ffmpeg:
            problems.append("找不到 ffmpeg")
        if not audio_files:
            problems.append("工作区内没有音频")
        return (not problems), "；".join(problems) or f"{info['gpu']} / torch {info['torch']}", info

    rec.run("env", "运行环境", check_env)
    if not context.get("env", {}).get("cuda_available"):
        print("CUDA 不可用，后续验证意义有限，提前结束。", file=sys.stderr)
        return 1

    # ---------------------------------------------------------------- 2 加载
    engine = SheetSage2Engine(EngineConfig(model_id=args.model, device="cuda", dtype="bf16"))

    def check_load():
        started = time.monotonic()
        engine.load()
        elapsed = time.monotonic() - started
        ok = engine.loaded and engine.device == "cuda"
        context["load_seconds"] = elapsed
        return ok, f"device={engine.device}，耗时 {elapsed:.1f}s", {
            "device": engine.device, "seconds": round(elapsed, 2)}

    if not rec.run("model_load", "模型加载", check_load).passed:
        write_reports(rec, args, context)
        return 1

    # ---------------------------------------------------------------- 3 全曲
    for index, audio in enumerate(audio_files):
        out_dir = args.output_root / slug(audio)

        def check_full(audio=audio, out_dir=out_dir):
            if args.reuse and (out_dir / "summary.json").is_file():
                summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
                duration = summary["duration_seconds"]
                note = "复用已有产物"
            else:
                started = time.monotonic()
                result = engine.transcribe(audio, out_dir)
                summary = summarize_result(result, engine.device, "bf16").as_dict()
                duration = summary["duration_seconds"]
                summary["wall_seconds"] = round(time.monotonic() - started, 1)
                (out_dir / "summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                note = f"实跑 {summary['wall_seconds']}s"

            report = inspect_output_dir(out_dir, duration)
            render_roll(out_dir, summary, args.report_dir / "rolls" / f"{slug(audio)}.png")
            detail = (f"{note}；时长 {duration:.0f}s，音符 {summary['notes']['melody']}，"
                      f"调性 {','.join(summary['keys'])}，"
                      f"{summary.get('meter') or ''} BPM≈{summary.get('bpm')}"
                      f"（朴素法 {summary.get('bpm_median_interval')}），"
                      f"{'产物自洽' if report.ok else '产物问题:' + str(report.problems)}")
            return report.ok, detail, {"summary": summary, "artifacts": report.as_dict()}

        rec.run(f"transcribe_{index}", f"全曲转写：{audio.name}", check_full)

    # ---------------------------------------------------------------- 4 确定性
    if audio_files:
        probe = audio_files[0]

        def check_determinism():
            dirs = [args.tmp_root / f"det{run}" for run in (1, 2)]
            prints = []
            for out_dir in dirs:
                shutil.rmtree(out_dir, ignore_errors=True)
                engine.transcribe(probe, out_dir, max_seconds=DETERMINISM_SECONDS)
                prints.append(fingerprint(out_dir))

            differing = [k for k in prints[0] if prints[0][k] != prints[1].get(k)]
            data = {"run1": prints[0], "run2": prints[1], "differing": differing,
                    "probe": probe.name, "seconds": DETERMINISM_SECONDS}
            if differing:
                return False, f"两次转写不一致的产物：{differing}", data
            return True, f"{len(prints[0])} 个产物哈希两次完全一致", data

        rec.run("determinism", f"确定性（{DETERMINISM_SECONDS:.0f}s 片段跑两次）", check_determinism)

    # ------------------------------------------------------- 4b/4c 对外入口
    if audio_files:
        probe = audio_files[0]

        def check_service_entry():
            out_dir = args.tmp_root / "service"
            shutil.rmtree(out_dir, ignore_errors=True)
            payload = transcribe_to_directory(probe, out_dir, engine=engine,
                                              max_seconds=ENTRY_CHECK_SECONDS)
            summary = payload["summary"]
            ok = payload["artifacts"]["ok"] and summary["notes"]["melody"] > 0 and bool(payload["abc"])
            detail = (f"{len(payload['files'])} 个产物，音符 {summary['notes']['melody']}，"
                      f"拍号 {summary['meter']}，BPM≈{summary['bpm']}，"
                      f"{'产物自洽' if payload['artifacts']['ok'] else str(payload['artifacts']['problems'])}")
            return ok, detail, {"summary": summary, "artifacts": payload["artifacts"],
                                "files": len(payload["files"])}

        rec.run("service_entry", "service 入口（transcribe_to_directory）", check_service_entry)

        def check_cli_entry():
            out_dir = args.tmp_root / "cli"
            shutil.rmtree(out_dir, ignore_errors=True)
            proc = subprocess.run(
                [sys.executable, "-m", "sheetsage2_webui.cli", str(probe),
                 "--max-seconds", str(ENTRY_CHECK_SECONDS), "--out-root", str(out_dir)],
                cwd=PROJECT_ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=900,
            )
            produced = list(out_dir.rglob("events.json"))
            ok = proc.returncode == 0 and bool(produced)
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            detail = (f"退出码 {proc.returncode}，产出 {len(produced)} 份 events.json"
                      + ("" if ok else f"；末尾输出：{' / '.join(tail)}"))
            return ok, detail, {"returncode": proc.returncode,
                                "stdout_tail": (proc.stdout or "").strip().splitlines()[-8:]}

        rec.run("cli_entry", "CLI 入口（python -m sheetsage2_webui.cli）", check_cli_entry)

        # ------------------------------------------------------------ 4d 一键流水线
        def check_pipeline():
            out_dir = args.tmp_root / "pipeline"
            shutil.rmtree(out_dir, ignore_errors=True)
            payload = run_pipeline(probe, out_dir, engine=engine,
                                   max_seconds=ENTRY_CHECK_SECONDS)
            decisions = payload["decisions"]
            project = payload["project"]

            problems = []
            if decisions["round_trip_error"] >= 1e-6:
                problems.append(f"秒↔tick 往返误差过大：{decisions['round_trip_error']:.2e}s")
            if not decisions["notes"]:
                problems.append("tick 域没有音符")
            if not decisions["meter"]:
                problems.append("没有识别出拍号")
            if not project["timeline"]["tick_length"]:
                problems.append("tick 总长为 0")
            if not project["timeline"]["tempo_map"]:
                problems.append("tempo_map 为空")
            for name in ("project.json", "report.json", "summary.json"):
                if not (out_dir / name).is_file():
                    problems.append(f"缺少 {name}")

            # 量化：所有音符边界必须落在网格上（这一步以前是"完全没有"）
            quantize = project.get("quantize") or {}
            if not quantize.get("enabled"):
                problems.append("量化没有开启")
            grid = quantize.get("grid_ticks") or 0
            if grid <= 0:
                problems.append("量化网格步长无效")
            else:
                off_grid = [n for t in project["tracks"] for n in t["notes"]
                            if n["tick"] % grid or n["dur"] % grid or n["dur"] <= 0]
                if off_grid:
                    problems.append(f"{len(off_grid)} 个音符没有吸到 {grid} tick 网格")
            shift = project.get("quantize_shift_seconds") or {}
            if quantize.get("enabled") and "max" not in shift:
                problems.append("缺少量化位移统计")

            data = {"decisions": decisions,
                    "tick_length": project["timeline"]["tick_length"],
                    "tempo_map": len(project["timeline"]["tempo_map"]),
                    "quantize": {k: quantize.get(k) for k in
                                 ("enabled", "grid_ticks", "notes", "moved",
                                  "failed_tracks")},
                    "quantize_shift_seconds": shift,
                    "tracks": {t["id"]: len(t["notes"]) for t in project["tracks"]}}
            detail = (f"拍号 {decisions['meter']}，弱起 {decisions['pickup_seconds']}s，"
                      f"速度 {decisions['bpm']}（{decisions['bpm_source']}，"
                      f"恒速 {decisions['tempo_constant']}），"
                      f"音符 {decisions['notes']}，往返误差 "
                      f"{decisions['round_trip_error']:.1e}s，"
                      f"tempo_map {len(project['timeline']['tempo_map'])} 条，"
                      f"量化到 {grid} tick 网格（移动 {quantize.get('moved')}"
                      + (f"，最大位移 {shift['max']}s" if "max" in shift else "")
                      + "）"
                      + ("；问题：" + str(problems) if problems else ""))
            return not problems, detail, data

        rec.run("pipeline", "一键流水线（零参数 → tick 域工程 + 报告）", check_pipeline)

    if not args.skip_slow and audio_files:
        probe = audio_files[0]

        # ------------------------------------------------------------ 5 重新加载
        def check_reload():
            fresh = SheetSage2Engine(EngineConfig(model_id=args.model, device="cuda", dtype="bf16"))
            fresh.load()
            out_dir = args.tmp_root / "reload"
            shutil.rmtree(out_dir, ignore_errors=True)
            fresh.transcribe(probe, out_dir, max_seconds=DETERMINISM_SECONDS)
            new_print = fingerprint(out_dir)

            baseline = args.tmp_root / "det1"
            if not (baseline / "events.json").is_file():
                return False, "缺少基线产物，无法比对", {}
            base_print = fingerprint(baseline)

            differing = [k for k in base_print if base_print[k] != new_print.get(k)]
            data = {"baseline": base_print, "reloaded": new_print, "differing": differing}
            if differing:
                return False, f"重新加载后结果不同：{differing}", data
            return True, "新实例加载后逐字节复现首次结果", data

        rec.run("reload", "重新加载模型（同进程第二个实例）", check_reload)

        # ------------------------------------------------------------ 6 离线加载
        def check_offline():
            offline = SheetSage2Engine(EngineConfig(model_id=args.model, device="cpu",
                                                    dtype="fp32", local_files_only=True))
            started = time.monotonic()
            offline.load()
            return True, f"local_files_only 加载成功（{time.monotonic() - started:.1f}s，device=cpu）", {
                "device": offline.device}

        rec.run("offline_load", "离线加载（local_files_only，缓存完整性）", check_offline)

        # ------------------------------------------------------------ 7 CPU 路径
        def check_cpu():
            cpu = SheetSage2Engine(EngineConfig(model_id=args.model, device="cpu", dtype="fp32"))
            cpu.load()
            out_dir = args.tmp_root / "cpu"
            shutil.rmtree(out_dir, ignore_errors=True)
            result = cpu.transcribe(probe, out_dir, max_seconds=CPU_CHECK_SECONDS)
            summary = summarize_result(result, cpu.device, "fp32").as_dict()
            report = inspect_output_dir(out_dir, summary["duration_seconds"])
            data = {"summary": summary, "artifacts": report.as_dict()}
            ok = report.ok and summary["notes"]["melody"] > 0
            detail = (f"CPU 出 {summary['notes']['melody']} 个音符，"
                      f"{'产物自洽' if report.ok else str(report.problems)}")
            return ok, detail, data

        rec.run("cpu_path", f"CPU 路径（{CPU_CHECK_SECONDS:.0f}s 片段）", check_cpu)

    write_reports(rec, args, context)
    return 0 if rec.ok else 1


# --------------------------------------------------------------------- 报告
def write_reports(rec: Recorder, args, context: dict[str, Any]) -> None:
    report_dir = args.report_dir
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": rec.ok,
        "environment": context.get("env"),
        "checks": [c.as_dict() for c in rec.checks],
    }
    (report_dir / "verification.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# SheetSage2 集成验证报告", "",
             f"- 生成时间：{payload['generated_at']}",
             f"- 总体结论：**{'全部通过' if rec.ok else '存在失败项'}**", ""]
    env = context.get("env") or {}
    if env:
        lines += ["## 运行环境", "",
                  f"- Python {env.get('python')} / torch {env.get('torch')} / transformers {env.get('transformers')}",
                  f"- GPU：{env.get('gpu')}",
                  f"- ffmpeg：`{env.get('ffmpeg')}`",
                  f"- 验证音频：{'、'.join(env.get('audio_files') or [])}", ""]
    lines += ["## 验证项", "", "| # | 验证项 | 结果 | 耗时 | 说明 |", "|---|---|---|---|---|"]
    for i, check in enumerate(rec.checks, 1):
        mark = "✅" if check.passed else "❌"
        detail = check.detail.replace("|", "\\|")
        lines.append(f"| {i} | {check.title} | {mark} | {check.seconds:.1f}s | {detail} |")

    lines += ["", "## 全曲结果", ""]
    for check in rec.checks:
        if not check.id.startswith("transcribe_"):
            continue
        summary = (check.data or {}).get("summary") or {}
        lines += [
            f"### {summary.get('audio')}", "",
            f"- 时长 {summary.get('duration_seconds')}s，转写耗时 {summary.get('wall_seconds') or summary.get('elapsed_seconds')}s，"
            f"GPU 峰值 {summary.get('peak_gpu_mib')} MiB",
            f"- 音符：旋律 {summary.get('notes', {}).get('melody')} "
            f"（人声 {summary.get('notes', {}).get('vocal')} / 器乐 {summary.get('notes', {}).get('instrumental')}）",
            f"- 调性 {','.join(summary.get('keys') or [])}；拍号 {summary.get('meter')}；"
            f"BPM≈{summary.get('bpm')}（四分音符；该拍号下每拍 {summary.get('beat_unit_bpm')} BPM）；"
            f"段落 {','.join(summary.get('structure') or [])}",
            f"- 速度估计：鲁棒（最小二乘 + 稳定段）{summary.get('bpm')}，"
            f"朴素中位间隔法 {summary.get('bpm_median_interval')}；"
            f"稳定段覆盖 {(summary.get('tempo_coverage') or 0):.1%}，"
            f"残差 max {summary.get('tempo_residual_max')}s，"
            f"可视为恒速：{'是' if summary.get('tempo_constant') else '否'}"
            + ("" if summary.get("tempo_constant")
               else f"（局部速度 {summary.get('tempo_range', {}).get('min')}–"
                    f"{summary.get('tempo_range', {}).get('max')} BPM，"
                    f"任何单一 BPM 都只是摘要）"),
            f"- 模型自带 Q 值：{summary.get('abc_bpm') if summary.get('abc_bpm') is not None else '无（未生成 score.abc）'}"
            + (f"，与拟合值差 {summary.get('bpm_delta'):+}，"
               f"{'一致' if summary.get('bpm_agreement') else '**不一致**'}，"
               f"采用 {summary.get('bpm_chosen')}（{summary.get('bpm_source')}）"
               if summary.get("abc_bpm") is not None else ""),
            f"- 拍号：beat.lab {summary.get('meter')} vs score.abc "
            f"M: {summary.get('abc_meter')}"
            + (f"（{'一致' if summary.get('abc_meter') == summary.get('meter') else '**不一致**'}）"
               if summary.get("abc_meter") else "（无 score.abc）")
            + (f"；弱起小节 {summary['pickup']['meter']}，{summary['pickup']['beats']} 拍，"
               f"{summary['pickup']['start']}–{summary['pickup']['end']}s"
               if summary.get("pickup") else "；无弱起")
            + ("" if summary.get("meter_consistent", True) else "；**中途有转拍号**"),
            f"- 和弦：{', '.join(summary.get('chords') or [])}",
            f"- ABC 小节数 {summary.get('abc_measures')}"
            + (f"（ABC 不可用：{summary.get('abc_error')}）" if summary.get("abc_error") else ""),
            f"- 卷帘预览：`reports/rolls/{slug(Path(str(summary.get('audio'))))}.png`",
            "",
        ]

    (report_dir / "验证报告.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告：{report_dir / 'verification.json'}")
    print(f"报告：{report_dir / '验证报告.md'}")
    print(f"总体：{'全部通过' if rec.ok else '存在失败项'}")


if __name__ == "__main__":
    raise SystemExit(main())
