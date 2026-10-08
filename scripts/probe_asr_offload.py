r"""探针：Qwen3-ASR 能不能把**一部分权重放在内存（CPU）上**跑。

为什么问这个：`webui/runner.py` 默认在两个阶段之间**释放** SheetSage2，因为
0.5G（SheetSage2）+ 6.3G（ASR 1.7B）挤在 10G 卡上太危险，代价是下一首歌重跑扒谱要多花
约 15s 重新加载。如果 ASR 支持把一部分层留在内存里（accelerate 的 `device_map="auto"`
+ `max_memory`），就能把它的显存占用压到预算以内，两个模型同时常驻。

`qwen_asr.Qwen3ASRModel.from_pretrained(**kwargs)` 会把 kwargs 原样转给
`transformers.AutoModel.from_pretrained`，所以 accelerate 的那套参数应该都能用
（对齐器走 `forced_aligner_kwargs`）。这个脚本就是来验证"应该"的。

    .\.venv\Scripts\python.exe scripts\probe_asr_offload.py
    .\.venv\Scripts\python.exe scripts\probe_asr_offload.py --model models/Qwen3-ASR-1.7B
    .\.venv\Scripts\python.exe scripts\probe_asr_offload.py --budgets 3GiB,4GiB --with-cpu

每个变体都量：加载耗时、**峰值显存**、转写耗时、识别文本、逐字时间戳条数，并和"全 GPU"
那次的文本做逐字比对。判据很硬：**文本必须一致**，否则这个 offload 就不能用。
"""

from __future__ import annotations

import argparse
import gc
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from sheetsage2_webui.bootstrap import find_ffmpeg, prepare  # noqa: E402

CLIP_DIR = PROJECT / "tmp" / "webui-verify"
DEFAULT_AUDIO = PROJECT / "samples" / "凤凰传奇 - 光芒.flac"


def cut_clip(audio: Path, seconds: float) -> Path:
    target = CLIP_DIR / f"{audio.stem}-{seconds:g}s.flac"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        return target
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，无法切片")
    command = [ffmpeg, "-v", "error", "-y", "-t", f"{seconds:g}", "-i", str(audio),
               "-ac", "2", "-ar", "44100", str(target)]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"切片失败：{result.stderr.decode(errors='replace')[-300:]}")
    return target


def free_gpu() -> None:
    """把上一个变体彻底放掉，否则峰值显存会互相污染。"""
    for _ in range(3):
        gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
    except Exception:
        pass


class RssSampler:
    """采样本进程的常驻内存峰值。

    为什么不能只看"权重多大"：offload 到内存的权重**真的占系统内存**，再加上 torch 自己的
    运行时、CUDA 上下文、音频缓冲，16G 的机器到底够不够必须实测，不能拍脑袋。
    """

    def __init__(self, interval: float = 0.05) -> None:
        self.interval = interval
        self.peak = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _rss_mib() -> float:
        try:
            import psutil

            return psutil.Process().memory_info().rss / 1024**2
        except Exception:
            import resource

            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return value / 1024 if sys.platform != "darwin" else value / 1024**2

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self._rss_mib())
            self._stop.wait(self.interval)

    def __enter__(self) -> "RssSampler":
        self.peak = self._rss_mib()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self.peak = max(self.peak, self._rss_mib())


def device_map_summary(hf_device_map: dict[str, Any] | None) -> str:
    """`hf_device_map` 摊平成"设备 -> 多少层"，一眼看出到底 offload 了多少。"""
    if not hf_device_map:
        return "（没有 hf_device_map）"
    counts: dict[str, int] = {}
    for device in hf_device_map.values():
        key = str(device)
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    parts = [f"{device}: {count}（{count / total:.0%}）"
             for device, count in sorted(counts.items(), key=lambda item: -item[1])]
    return "层分布 " + " / ".join(parts)


def run_variant(name: str, config, audio: Path, *, budget: str | None,
                max_ram: str, device: str) -> dict[str, Any]:
    """跑一个变体：加载 -> 转写 -> 量峰值显存。"""
    import torch
    from qwen_asr import Qwen3ASRModel

    free_gpu()
    baseline = torch.cuda.memory_allocated() / 1024**2 if torch.cuda.is_available() else 0.0

    model_kwargs: dict[str, Any] = {"dtype": torch.bfloat16,
                                    "max_inference_batch_size": 1}
    aligner_kwargs: dict[str, Any] = {"dtype": torch.bfloat16}
    if budget is None:
        model_kwargs["device_map"] = device
        aligner_kwargs["device_map"] = "cuda:0" if device.startswith("cuda") else "cpu"
    else:
        # accelerate：先按预算往卡上放，放不下的留在内存里
        memory = {0: budget, "cpu": max_ram}
        model_kwargs["device_map"] = "auto"
        model_kwargs["max_memory"] = memory
        aligner_kwargs["device_map"] = "auto"
        aligner_kwargs["max_memory"] = memory
    if config.aligner:
        model_kwargs["forced_aligner"] = str(config.aligner)
        model_kwargs["forced_aligner_kwargs"] = aligner_kwargs

    print(f"\n--- {name} ---")
    print(f"  参数 device_map={model_kwargs['device_map']!r}"
          + (f" max_memory={model_kwargs.get('max_memory')}" if budget else ""))
    started = time.monotonic()
    with RssSampler() as rss:
        model = Qwen3ASRModel.from_pretrained(str(config.model), **model_kwargs)
        load_seconds = time.monotonic() - started
        after_load = torch.cuda.memory_allocated() / 1024**2 if torch.cuda.is_available() else 0.0
        print(f"  加载 {load_seconds:.1f}s，加载后显存 {after_load:.0f}MiB"
              f"（基线 {baseline:.0f}MiB）")

        inner = getattr(model, "model", None)
        print("  " + device_map_summary(getattr(inner, "hf_device_map", None)))
        aligner = getattr(model, "forced_aligner", None)
        if aligner is not None:
            aligner_inner = getattr(aligner, "model", None)
            print("  对齐器 " + device_map_summary(getattr(aligner_inner, "hf_device_map", None)))

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()
        try:
            try:
                results = model.transcribe(audio=str(audio), language="Chinese",
                                           return_time_stamps=bool(config.aligner),
                                           max_new_tokens=1024)
            except TypeError:
                # 有些版本不接受 max_new_tokens（`asr.py` 里也是这么兜的）
                results = model.transcribe(audio=str(audio), language="Chinese",
                                           return_time_stamps=bool(config.aligner))
        except Exception as exc:                  # 参数不被接受 / OOM 都要如实报
            print(f"  转写失败：{type(exc).__name__}: {exc}")
            del model
            free_gpu()
            return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "peak_rss_mib": round(rss.peak, 1)}
        seconds = time.monotonic() - started
        peak = torch.cuda.max_memory_allocated() / 1024**2 if torch.cuda.is_available() else 0.0
        total = (torch.cuda.get_device_properties(0).total_memory / 1024**2
                 if torch.cuda.is_available() else 0.0)

        result = results[0] if isinstance(results, (list, tuple)) else results
        text = (getattr(result, "text", "") or "").strip()
        stamps = getattr(result, "time_stamps", None) or []
    print(f"  转写 {seconds:.1f}s   峰值显存 {peak:.0f}MiB / 总 {total:.0f}MiB"
          f"（余量 {total - peak:.0f}MiB）")
    print(f"  峰值内存（RSS） {rss.peak:.0f}MiB")
    print(f"  文本 {len(text)} 字：{text[:60]}…")
    print(f"  逐字时间戳 {len(stamps)} 条")
    del model
    free_gpu()
    return {"name": name, "ok": True, "load_seconds": round(load_seconds, 1),
            "peak_mib": round(peak, 1), "total_mib": round(total, 1),
            "peak_rss_mib": round(rss.peak, 1), "group": "asr",
            "elapsed": round(seconds, 1), "text": text, "stamps": len(stamps)}


def run_engine_variant(audio: Path, device: str = "auto",
                       weights: str = "fp32") -> dict[str, Any]:
    """只跑**扒谱阶段**（SheetSage2），量它单独占多少显存/内存。

    这是"最低配"的关键：两个阶段是**串行**的（默认阶段之间释放模型），所以卡的下限
    取两者中较大的那个，而不是相加——但如果扒谱本身就超了 4G，那 ASR 再怎么 offload 也没用。

    注意：**扒谱阶段没有 offload 这条路**——SheetSage2 自己的 `from_pretrained` 只接受单个
    设备（`device_map="auto"` 会被它强转成 `cuda`）。唯一的杠杆是把权重精度降到 bf16。
    """
    import tempfile

    from sheetsage2_webui.engine import EngineConfig, SheetSage2Engine

    free_gpu()
    name = f"扒谱阶段（SheetSage2 权重 {weights}）"
    print(f"\n--- {name} ---")
    engine_config = EngineConfig(device=device, weights_dtype=weights)
    started = time.monotonic()
    with RssSampler() as rss:
        engine = SheetSage2Engine(engine_config)
        engine.load()
        load_seconds = time.monotonic() - started
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        out = Path(tempfile.mkdtemp(prefix="probe-engine-"))
        started = time.monotonic()
        try:
            result = engine.transcribe(audio, out)
        except Exception as exc:
            print(f"  转写失败：{type(exc).__name__}: {exc}")
            return {"name": name, "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "group": "engine", "peak_rss_mib": round(rss.peak, 1)}
        # 出谱的一致性信号：音符数 / 拍数 / ABC 长度 / 调性。降精度到底改不改谱面，
        # 光看显存是看不出来的，得比这个。
        labs = result.get("labs") or {}
        signature = "|".join(str(value) for value in (
            result.get("melody_notes"), result.get("vocal_notes"),
            result.get("instrumental_notes"),
            len((labs.get("beat") or "").splitlines()),
            len((labs.get("melody_full") or "").splitlines()),
            len(result.get("abc") or ""),
        ))
        seconds = time.monotonic() - started
        peak = torch.cuda.max_memory_allocated() / 1024**2 if torch.cuda.is_available() else 0.0
        total = (torch.cuda.get_device_properties(0).total_memory / 1024**2
                 if torch.cuda.is_available() else 0.0)
    print(f"  加载 {load_seconds:.1f}s  转写 {seconds:.1f}s  "
          f"峰值显存 {peak:.0f}MiB / 总 {total:.0f}MiB（余量 {total - peak:.0f}MiB）")
    print(f"  峰值内存（RSS） {rss.peak:.0f}MiB")
    print(f"  出谱签名（音/唱/器/拍/旋律行/ABC）：{signature}")
    del engine
    free_gpu()
    return {"name": name, "ok": True, "group": "engine",
            "load_seconds": round(load_seconds, 1), "peak_mib": round(peak, 1),
            "total_mib": round(total, 1), "peak_rss_mib": round(rss.peak, 1),
            "elapsed": round(seconds, 1), "text": signature, "stamps": 0}


def parse_size(text: str | None) -> float | None:
    """`4GiB` / `16GB` / `4096MiB` -> MiB。"""
    if not text:
        return None
    value = text.strip().lower().replace(" ", "")
    for suffix, factor in (("gib", 1024.0), ("gb", 1000.0), ("mib", 1.0), ("mb", 0.9537)):
        if value.endswith(suffix):
            try:
                return float(value[: -len(suffix)]) * factor
            except ValueError:
                return None
    try:
        return float(value)
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qwen3-ASR 部分权重 offload 到内存的探针")
    parser.add_argument("--model", default="models/Qwen3-ASR-0.6B", help="ASR 模型")
    parser.add_argument("--aligner", default="models/Qwen3-ForcedAligner-0.6B",
                        help="对齐器（传空字符串则不用）")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--budgets", default="1GiB,2GiB,4GiB",
                        help="逗号分隔的显存预算（accelerate max_memory 的 GPU 部分）。"
                             "预算必须比权重小才看得到 offload——默认从最紧的开始试")
    parser.add_argument("--max-ram", default="96GiB", help="留给内存的上限")
    parser.add_argument("--device", default="cuda:0", help="全 GPU 变体用的设备")
    parser.add_argument("--with-cpu", action="store_true", help="额外跑一次纯 CPU（很慢）")
    parser.add_argument("--engine-budget", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--engine-weights", default="fp32",
                        help="扒谱模型的权重精度，逗号分隔（如 fp32,bf16）。"
                             "SheetSage2 不支持 offload，小显存卡只能靠 bf16 把权重减半")
    parser.add_argument("--with-engine", action="store_true",
                        help="额外量一次扒谱阶段（SheetSage2）单独占多少")
    parser.add_argument("--target-vram", default=None,
                        help="目标显卡显存（如 4GiB）：算一下这些变体放不放得下")
    parser.add_argument("--target-ram", default=None,
                        help="目标机器内存（如 16GiB）：算一下峰值 RSS 放不放得下")
    args = parser.parse_args(argv)

    prepare()
    import torch

    if not torch.cuda.is_available():
        print("没有 CUDA，探针只能跑 CPU 变体", file=sys.stderr)
        return 2

    class Config:
        pass

    config = Config()
    config.model = args.model if Path(args.model).exists() else PROJECT / args.model
    config.aligner = (args.aligner if Path(args.aligner).exists()
                      else PROJECT / args.aligner) if args.aligner else None

    audio = cut_clip(args.audio, args.seconds) if args.audio.is_file() else None
    if audio is None:
        print(f"找不到音频 {args.audio}", file=sys.stderr)
        return 2

    total = torch.cuda.get_device_properties(0).total_memory / 1024**2
    print(f"GPU {torch.cuda.get_device_name(0)}  总显存 {total:.0f}MiB")
    print(f"模型 {config.model}")
    print(f"对齐器 {config.aligner}")
    print(f"音频 {audio.name}  {args.seconds:g}s")
    print("注意：`max_memory` 是**每个模型各自**的上限（ASR 和对齐器分别加载、分别算），")
    print("      所以预算必须小于单个模型的权重大小才会真的 offload，否则它全部放卡上。")

    results = [run_variant("全 GPU（现在线上就是这么跑的）", config, audio, budget=None,
                           max_ram=args.max_ram, device=args.device)]
    for budget in [item.strip() for item in args.budgets.split(",") if item.strip()]:
        results.append(run_variant(f"offload 到内存（GPU 预算 {budget}）", config, audio,
                                   budget=budget, max_ram=args.max_ram, device=args.device))
    if args.with_cpu:
        results.append(run_variant("纯 CPU", config, audio, budget=None,
                                   max_ram=args.max_ram, device="cpu"))
    if args.with_engine:
        for weights in [item.strip() for item in args.engine_weights.split(",") if item.strip()]:
            results.append(run_engine_variant(audio, args.device, weights))

    reference = next((item for item in results
                      if item["ok"] and item["name"].startswith("全 GPU")), None)
    engine_reference = next((item for item in results
                             if item["ok"] and item.get("group") == "engine"), None)
    print("\n" + "=" * 88)
    print(f"{'变体':30s} {'加载':>6s} {'峰值显存':>10s} {'余量':>9s} {'峰值内存':>9s} "
          f"{'转写':>7s}  结果一致")
    print("-" * 88)
    failed = []
    for item in results:
        if not item["ok"]:
            print(f"{item['name'][:30]:30s} {'—':>6s} {'—':>10s} {'—':>9s} "
                  f"{item.get('peak_rss_mib', 0):8.0f}M {'—':>7s}  失败：{item['error'][:40]}")
            failed.append(item["name"])
            continue
        same = "—"
        if item.get("group") == "engine":
            if engine_reference is not None and item is not engine_reference:
                same = "是" if item["text"] == engine_reference["text"] else "**否**"
        elif reference is not None and item is not reference:
            same = "是" if item["text"] == reference["text"] else "**否**"
        if same == "**否**":
            failed.append(item["name"])
        margin = item["total_mib"] - item["peak_mib"]
        print(f"{item['name'][:30]:30s} {item['load_seconds']:6.1f}s "
              f"{item['peak_mib']:8.0f}MiB {margin:7.0f}MiB "
              f"{item['peak_rss_mib']:8.0f}MiB {item['elapsed']:6.1f}s  {same}")

    target_vram = parse_size(args.target_vram)
    target_ram = parse_size(args.target_ram)
    if target_vram or target_ram:
        #: 显存里除了 torch 分配到的张量，还有 CUDA 上下文和桌面合成器占的；
        #: Windows 上保守留 800MiB，Linux 桌面留 500MiB。
        reserve = 800.0 if os.name == "nt" else 500.0
        print("\n" + "=" * 88)
        print(f"目标机器：显存 {args.target_vram or '—'} · 内存 {args.target_ram or '—'}"
              f"（本机实测，显存预留 {reserve:.0f}MiB 给 CUDA 上下文/桌面）")
        usable = (target_vram - reserve) if target_vram else None
        for item in results:
            if not item["ok"]:
                continue
            notes = []
            if usable is not None:
                ok = item["peak_mib"] <= usable
                notes.append(f"显存 {'够' if ok else '**不够**'}"
                             f"（峰值 {item['peak_mib']:.0f} vs 可用 {usable:.0f}MiB）")
            if target_ram:
                ok_ram = item["peak_rss_mib"] <= target_ram
                notes.append(f"内存 {'够' if ok_ram else '**不够**'}"
                             f"（峰值 {item['peak_rss_mib']:.0f} vs {target_ram:.0f}MiB）")
            print(f"  {item['name'][:34]:34s} " + " / ".join(notes))
        print("\n  注意：两个阶段是**串行**的（默认阶段之间释放模型），所以卡的下限取两者较大的那个；")
        print("        如果要用 --keep-models 让两个模型同时常驻，就必须按**相加**算。")

    if failed:
        print(f"\n有问题的变体：{failed}")
        return 1
    print("\n全部变体文本一致：部分权重放内存不影响识别结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
