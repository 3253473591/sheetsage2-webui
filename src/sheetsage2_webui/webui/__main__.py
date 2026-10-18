"""起服务：``python -m sheetsage2_webui.webui``。

零参数即可用（模型自动选设备、数据目录默认 `tmp/webui`）。这里保留的开关都是**这一台机器
的基准**（设备、模型、显存预算），页面上的参数面板是**每个工程**的（精度 / 设备 / 范围 /
量化 / 识别模型 / 音源 / 语言），叠在基准之上。`--lock-params` 把参数面板锁死——
「4G 特供」模式就是靠它。

    python -m sheetsage2_webui.webui                       # http://127.0.0.1:8000
    python -m sheetsage2_webui.webui --port 8080 --device cpu
    python -m sheetsage2_webui.webui --engine-weights bf16 --asr-max-vram 1GiB \\
        --asr-max-slice 30 --lock-params                   # 4G 显存特供
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from ..asr import AsrConfig
from ..bootstrap import PROJECT_ROOT
from ..engine import EngineConfig
from .app import create_app, data_root_from_env
from .logs import attach_uvicorn, install_crash_hook, setup_logging
from .runner import RunnerSession
from .security import generate_pair_token
from .singleton import AlreadyRunning, InstanceLock

DEFAULT_ASR_MODEL = "models/Qwen3-ASR-1.7B"
DEFAULT_ALIGNER = "models/Qwen3-ForcedAligner-0.6B"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sheetsage2_webui.webui",
        description="SheetSage2 Studio 本地服务（扒谱 → 歌词识别 → 下载产物）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只对本机开放）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-root", default=None,
                        help="上传与产物的根目录（默认 <项目根>/tmp/webui，可用 SHEETSAGE_WEBUI_DATA 覆盖）")
    parser.add_argument("--model", default=EngineConfig.model_id, help="扒谱模型 id 或本地目录")
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--dtype", default="bf16", choices=("bf16", "fp32"))
    parser.add_argument("--preset", default="default", choices=("default", "paper"))
    parser.add_argument("--local-files-only", action="store_true", help="只用本地权重缓存")
    parser.add_argument("--asr-model", default=DEFAULT_ASR_MODEL, help="歌词识别模型（默认本地 1.7B）")
    parser.add_argument("--aligner", default=DEFAULT_ALIGNER, help="逐字对齐模型")
    parser.add_argument("--asr-device", default="cuda:0", help="歌词识别设备")
    parser.add_argument("--asr-max-vram", default=None,
                        help="给歌词识别模型一个显存预算（如 5GiB）：放不下的层留在内存里，"
                             "前向时临时搬上卡。用来在显存紧张时让两个模型同时常驻——"
                             "实测 1.7B 预算 3GiB 时显存 5996->5039MiB、转写 5.6->10.5s，"
                             "识别文本逐字一致（见 scripts/probe_asr_offload.py）")
    parser.add_argument("--asr-max-slice", type=float, default=None,
                        help="歌词识别单个切片的上限秒数（默认 90）。显存峰值随切片长度线性涨，"
                             "小显存卡（4G）建议 30")
    parser.add_argument("--engine-weights", default="fp32", choices=("fp32", "bf16", "fp16"),
                        help="扒谱模型的权重精度（默认 fp32，= 一直以来的行为）。"
                             "SheetSage2 不支持 offload（只接受单设备），小显存卡只能靠 bf16 "
                             "把权重减半：实测扒谱阶段峰值 3379MiB -> 见 scripts/probe_asr_offload.py")

    parser.add_argument("--language", default="",
                        help="歌词识别语言的**默认值**（空 = 自动让模型判）。"
                             "每个工程还能在页面上单独选")
    parser.add_argument("--lock-params", action="store_true",
                        help="锁死页面上的参数面板（PATCH /options 一律 409）。"
                             "「4G 特供」模式用它：参数固定成服务端认定的安全组合")
    parser.add_argument("--keep-models", action="store_true",
                        help="切换阶段时不释放上一个模型（显存够大时可以省掉重新加载）")
    parser.add_argument("--max-upload-mb", type=float, default=512.0)
    parser.add_argument("--workers", type=int, default=1,
                        help="并行的推理工作线程数（GPU 只有一块就保持 1）")
    parser.add_argument("--log-level", default="info",
                        choices=("critical", "error", "warning", "info", "debug"))
    parser.add_argument("--no-singleton", action="store_true",
                        help="允许同一个数据目录上跑多个服务实例（默认禁止：双击两次只会开一个）")
    return parser


def resolve(path: str) -> str:
    """相对路径按项目根解析；`models/Qwen3-ASR-1.7B` 这种写法才点得着。"""
    candidate = Path(path)
    if candidate.is_absolute() or candidate.exists():
        return str(candidate)
    rooted = PROJECT_ROOT / candidate
    return str(rooted) if rooted.exists() else path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = Path(args.data_root) if args.data_root else data_root_from_env()

    # **单实例锁**（G1）：同一个数据目录只许一个服务。拿不到锁的那一方不是报错退出，
    # 而是告诉用户"已经有一个在跑了，地址是这个"——双击两次的第二次就走到这里。
    lock = InstanceLock(data_root)
    if not args.no_singleton and os.environ.get("SHEETSAGE_ALLOW_MULTI") != "1":
        display = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        try:
            lock.acquire(port=args.port, url=f"http://{display}:{args.port}")
        except AlreadyRunning as exc:
            info = exc.info
            print("已经有一个 SheetSage2 Studio 在跑了，这次不再启动第二个。")
            if info.get("url"):
                print(f"  正在跑的那个：{info['url']}"
                      + (f"（PID {info.get('pid')}）" if info.get("pid") else ""))
            print(f"  锁文件：{lock.path}")
            print("  想重启就先关掉那个窗口（或结束那个进程），再双击一次。")
            return 0
    else:
        print("  注意：本次跳过了单实例检查（--no-singleton / SHEETSAGE_ALLOW_MULTI）")

    try:
        asr_options: dict[str, Any] = {}
        if args.asr_max_slice:
            asr_options["max_slice_seconds"] = args.asr_max_slice
        session = RunnerSession(
            EngineConfig(model_id=args.model, device=args.device, dtype=args.dtype,
                         preset=args.preset, local_files_only=args.local_files_only,
                         weights_dtype=args.engine_weights),
            AsrConfig(asr_model=resolve(args.asr_model), aligner_model=resolve(args.aligner),
                      language=args.language, device=args.asr_device,
                      max_vram=args.asr_max_vram, **asr_options),
            free_between_stages=not args.keep_models,
        )
        # 落盘日志（G12）：崩溃之后现场还在，能导出诊断包。
        log_path = setup_logging(data_root)
        attach_uvicorn(log_path, args.log_level)
        install_crash_hook(data_root)

        # 安全（G13）：回环监听做 Origin/Host 校验；非回环监听强制要求 token。
        loopback_hosts = ("127.0.0.1", "localhost", "::1")
        loopback = args.host in loopback_hosts
        require_token = not loopback
        pair_token = generate_pair_token() if require_token else None

        app = create_app(data_root=data_root, session=session,
                         max_upload_mb=args.max_upload_mb,
                         workers=args.workers, lock_params=args.lock_params,
                         loopback_guard=True, require_token=require_token,
                         pair_token=pair_token)

        try:
            import uvicorn
        except ImportError:
            print("缺少 uvicorn：pip install -e .[web]", file=sys.stderr)
            return 2

        display = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        print(f"SheetSage2 Studio -> http://{display}:{args.port}")
        print(f"  数据目录 {app.state.manager.root}")
        print(f"  日志目录 {log_path.parent}")
        print(f"  扒谱 {args.model}（{args.device}） · 歌词 {args.asr_model}（{args.asr_device}）"
              + (f"，显存预算 {args.asr_max_vram}" if args.asr_max_vram else ""))
        print(f"  上传上限 {args.max_upload_mb:g} MB"
              + ("，阶段间保留模型" if args.keep_models else "，阶段间释放模型以省显存"))
        print("  参数面板：" + ("已锁定（4G 特供）" if args.lock_params
                                else "可调（精度 / 设备 / 范围 / 量化 / 模型 / 音源 / 语言）"))
        lock.update(port=args.port, url=f"http://{display}:{args.port}")
        if not loopback:
            print(f"  ⚠ 监听 {args.host}：同一网络的其他人也能访问这个服务（能上传/删除/导出），"
                  "只在可信网络里这么用")
            print(f"  🔑 已启用访问凭证（pair token）：http://{display}:{args.port}/?token={pair_token}")
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
