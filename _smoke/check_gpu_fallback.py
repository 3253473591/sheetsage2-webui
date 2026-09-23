"""GPU→CPU 兜底：推理中途的 CUDA 失败必须整段退回 CPU 重跑。

为什么要有这个用例
------------------
``torch.cuda.is_available()`` 只说明「有卡、驱动能起来」，**不保证这份 torch 构建
带了它的内核**。整合包默认的 cu130 wheel 只编译 Turing 及以上：插着更老的卡时
``is_available()`` 仍是 True，加载权重也正常（``.to(device)`` 不执行 kernel），要等
**第一颗 kernel 真正执行**才抛 ``no kernel image is available ...``。显存不足同理，
通常炸在推理中途而不是加载权重时。

旧实现只把 ``load_model`` 包在 try 里（``app/worker.py``），上面两类失败都会让整个
任务判死——外层 ``except BaseException`` 只上报错误，不会退 CPU。

本脚本把 ``load_model`` / ``pick_device`` / ``_preflight_ffmpeg`` / ``filter_abc`` /
``summarize`` / ``rebuild_exports`` 全换成桩，用假模型制造失败，
**不需要显卡、不需要 ffmpeg、不需要模型权重**，直接跑::

    .venv\\Scripts\\python.exe _smoke\\check_gpu_fallback.py
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import rebuild, worker  # noqa: E402

#: 草稿目录放项目自己的 ``tmp/`` 下（与 ``tmp/_midi_dbg`` 等既有先例一致），
#: 跑完就删；不写系统 temp。
WORK = ROOT / "tmp" / "_gpu_fallback_check"

FAILURES: list[str] = []

#: 假模型在 cuda 上的默认失败（真实的 cu130 + 老卡就是这个）
NO_KERNEL_IMAGE = RuntimeError(
    "CUDA error: no kernel image is available for execution on the device"
)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


# --------------------------------------------------------------------------
# 桩：把整条链路换成假的，只留 run() 的设备/兜底逻辑是真的
# --------------------------------------------------------------------------

class FakeModel:
    """假模型：``boom`` 非空就抛它；否则 cuda 上抛「没内核」，cpu 上正常返回。"""

    def __init__(self, device: str, boom: BaseException | None = None) -> None:
        self.device = device
        self.max_output_seq_len = 128
        self.seen_dtypes: list[str | None] = []
        self.boom = boom

    def transcribe(self, path: str, **options: Any) -> dict[str, Any]:
        self.seen_dtypes.append(options.get("dtype"))
        progress = options.get("progress")
        if progress:
            progress({"stage": "audio"})
            progress({"stage": "encoding", "window": 1, "windows": 1, "start": 0.0})
            progress({"stage": "decoding", "window": 1, "windows": 1, "tokens": 7})
        if self.boom is not None:
            raise self.boom
        if self.device == "cuda":
            raise NO_KERNEL_IMAGE
        return {"abc": "X:1\nT:stub\nK:C\nC D E F|", "num_events": 4}


class FakeReport:
    lines_in = 3
    lines_out = 2
    kept_voices = ["V:1"]
    dropped_voices: list[str] = []
    dropped_sections: list[str] = []

    def as_dict(self) -> dict[str, Any]:
        return {"lines_in": self.lines_in, "lines_out": self.lines_out}


def make_load_stub(calls: dict[str, Any]) -> Any:
    def fake_load_model(device: str, on_stage: Any) -> FakeModel:
        calls["load"].append(device)
        on_stage("start")  # 真 load_model 也这么调，顺便验证 on_reload 不让阶段倒退
        model = FakeModel(device, boom=calls.get("boom"))
        calls["models"].append(model)
        on_stage("done")
        return model

    return fake_load_model


def fake_filter_abc(raw: str, **kwargs: Any) -> tuple[str, FakeReport]:
    return raw, FakeReport()


def fake_summarize(out: Path, **kwargs: Any) -> dict[str, Any]:
    return {
        "bpm": 120.0,
        "score_bpm": 120.0,
        "diagnostics": list(kwargs.get("extra_diagnostics") or []),
    }


def fake_rebuild_exports(out: Path, abc: str, **kwargs: Any) -> dict[str, Any]:
    return {"exports": [], "bpm": kwargs.get("bpm"), "warnings": []}


def run_worker(
    argv: list[str],
    calls: dict[str, Any],
    pick: Any,
) -> tuple[int, list, dict | None, dict | None]:
    """打桩跑一次 ``worker.run()``，返回 (返回码, 进度行, 结果, 错误)。"""
    saved = (
        worker.pick_device,
        worker.load_model,
        worker._preflight_ffmpeg,
        worker.filter_abc,
        worker.summarize,
        rebuild.rebuild_exports,
    )
    worker.pick_device = pick
    worker.load_model = make_load_stub(calls)
    worker._preflight_ffmpeg = lambda: "ffmpeg(stub)"
    worker.filter_abc = fake_filter_abc
    worker.summarize = fake_summarize
    rebuild.rebuild_exports = fake_rebuild_exports
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = worker.run(argv)
    finally:
        (
            worker.pick_device,
            worker.load_model,
            worker._preflight_ffmpeg,
            worker.filter_abc,
            worker.summarize,
            rebuild.rebuild_exports,
        ) = saved

    progress: list[dict] = []
    result = error = None
    for line in buf.getvalue().splitlines():
        if line.startswith(worker.PREFIX_PROGRESS):
            progress.append(json.loads(line[len(worker.PREFIX_PROGRESS):]))
        elif line.startswith(worker.PREFIX_RESULT):
            result = json.loads(line[len(worker.PREFIX_RESULT):])
        elif line.startswith(worker.PREFIX_ERROR):
            error = json.loads(line[len(worker.PREFIX_ERROR):])
    return rc, progress, result, error


def make_inputs(tmp: Path, tag: str) -> tuple[str, str]:
    audio = tmp / f"{tag}.mp3"
    audio.write_bytes(b"\x00")  # run() 只检查 is_file()，不解码
    return str(audio), str(tmp / f"out_{tag}")


def base_argv(audio: str, out: str, device: str = "cuda") -> list[str]:
    return [
        "--audio", audio, "--out", out, "--device", device, "--dtype", "bf16",
        "--export-voices", "vocal", "--no-octave-fix",
    ]


def pick_cuda(requested: str, requested_dtype: str) -> tuple[str, str, str]:
    return "cuda", "bf16", ""


def pick_cpu(requested: str, requested_dtype: str) -> tuple[str, str, str]:
    return "cpu", "fp32", "请求了 GPU 但当前环境用不了 CUDA，已改用 CPU"


# --------------------------------------------------------------------------
# 1. 分类器：什么才算「GPU 走不通」
# --------------------------------------------------------------------------

def test_classifier() -> None:
    print("\n--- 1. is_gpu_failure 分类 ---")
    gpu_like = [
        NO_KERNEL_IMAGE,
        RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB (GPU 0; 12.00 GiB)"),
        RuntimeError("CUDA error: device-side assert triggered"),
        RuntimeError("CUDA error: an illegal memory access was encountered"),
        RuntimeError("cuBLAS error 8 when calling cublasCreate(handle)"),
        RuntimeError("Could not load library cudnn_ops_infer64_8.dll"),
    ]
    logic_like = [
        ValueError("Overlap prefix fills the context; reduce overlap_seconds"),
        RuntimeError("Melody-only ABC unavailable: No ABC score was produced"),
        KeyError("abc"),
        FileNotFoundError("melody_vocal.lab"),
        TypeError("unsupported operand type(s) for +: 'NoneType' and 'float'"),
    ]
    for exc in gpu_like:
        check(f"GPU 类判真：{type(exc).__name__}: {str(exc)[:40]}", worker.is_gpu_failure(exc))
    for exc in logic_like:
        check(f"逻辑类判假：{type(exc).__name__}: {str(exc)[:40]}", not worker.is_gpu_failure(exc))


# --------------------------------------------------------------------------
# 2. 推理中途 CUDA 失败 → 自动 CPU 重跑
# --------------------------------------------------------------------------

def test_fallback(tmp: Path) -> None:
    print("\n--- 2. 推理中途 CUDA 失败 → 自动退回 CPU 重跑 ---")
    audio, out = make_inputs(tmp, "fallback")
    calls: dict[str, Any] = {"load": [], "models": []}  # boom=None → cuda 抛「没内核」
    rc, progress, result, error = run_worker(base_argv(audio, out), calls, pick_cuda)

    check("任务未判死（返回码 0、有 @@R、无 @@E）",
          rc == 0 and result is not None and error is None,
          f"rc={rc} error={error}")
    check("先 cuda 再 cpu 各加载一次", calls["load"] == ["cuda", "cpu"], str(calls["load"]))
    check("CUDA 那一轮确实跑到了推理（不是加载就炸）",
          len(calls["models"]) >= 1 and calls["models"][0].seen_dtypes == ["bf16"],
          str([m.seen_dtypes for m in calls["models"]]))
    check("CPU 重跑用的是 fp32（options['dtype'] 已同步）",
          len(calls["models"]) == 2 and calls["models"][1].seen_dtypes == ["fp32"],
          str([m.seen_dtypes for m in calls["models"]]))

    if result:
        check("summary.device 记为 cpu", result.get("device") == "cpu", str(result.get("device")))
        check("summary.dtype 记为 fp32", result.get("dtype") == "fp32", str(result.get("dtype")))
        check("device_note 说明了退避原因",
              "GPU 推理失败" in str(result.get("device_note", "")),
              str(result.get("device_note", ""))[:70])
        check("进度单调不减（文档 3.6.3 硬指标）", result.get("progress_monotonic") is True)
        check("设备选择写进了 diagnostics",
              any("设备选择" in d for d in result.get("diagnostics", [])))
    else:
        check("拿到了 @@R 结果", False, str(error))

    stages = [p["stage"] for p in progress]
    if "识别音乐" in stages:
        first = stages.index("识别音乐")
        check("阶段顺序不回退（识别音乐之后不再出现加载模型）",
              "加载模型" not in stages[first:], " → ".join(stages))
    else:
        check("出现了识别音乐阶段", False, " → ".join(stages))
    check("兜底在进度里可见（前端能看到为什么变慢）",
          any("GPU 推理失败" in str(p.get("detail", "")) for p in progress))


# --------------------------------------------------------------------------
# 3. 逻辑错误不许误触发 CPU 重跑
# --------------------------------------------------------------------------

def test_no_retry_on_logic_error(tmp: Path) -> None:
    print("\n--- 3. 逻辑错误不误触发重跑（否则白白再等一遍） ---")
    audio, out = make_inputs(tmp, "logic")
    calls: dict[str, Any] = {
        "load": [], "models": [],
        "boom": ValueError("Overlap prefix fills the context; reduce overlap_seconds"),
    }
    rc, progress, result, error = run_worker(base_argv(audio, out), calls, pick_cuda)

    check("任务如实失败（返回码 1 + @@E）", rc == 1 and error is not None and result is None)
    check("错误原样上报，没被兜底吞掉",
          error is not None and "Overlap prefix" in error.get("message", ""),
          str(error.get("message"))[:70] if error else "")
    check("没有多余的 CPU 重跑（只加载一次）", calls["load"] == ["cuda"], str(calls["load"]))


# --------------------------------------------------------------------------
# 4. 已经在 CPU 上跑时，绝不重跑（防死循环 / 防白白重来）
# --------------------------------------------------------------------------

def test_no_retry_when_already_cpu(tmp: Path) -> None:
    print("\n--- 4. 已经在 CPU 上：即使报 CUDA 错也不重跑 ---")
    audio, out = make_inputs(tmp, "cpu")
    calls: dict[str, Any] = {
        "load": [], "models": [],
        "boom": RuntimeError("CUDA error: no kernel image is available"),
    }
    rc, progress, result, error = run_worker(base_argv(audio, out, device="cpu"), calls, pick_cpu)

    check("只加载一次，没有二次重跑", calls["load"] == ["cpu"], str(calls["load"]))
    check("任务如实失败", rc == 1 and error is not None)


def main() -> int:
    print("=" * 66)
    print("  GPU→CPU 兜底回归（不需要显卡 / ffmpeg / 权重）")
    print("=" * 66)
    test_classifier()
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        test_fallback(WORK)
        test_no_retry_on_logic_error(WORK)
        test_no_retry_when_already_cpu(WORK)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)

    print()
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
