"""给 **AMD 卡（ROCm）** 准备一个能跑的环境——不改动现有 `.venv`。

## 为什么是"复制 + 换 torch"，不是从零装

同一条命令要在 N 卡（CUDA）和 A 卡（ROCm）上跑，而一个 venv 里塞不下两份 torch
（包名都叫 `torch`，各自带 `libtorch_cuda`/`libtorch_hip`）。所以：

1. **复制** `.venv` → `.venv-rocm`：transformers 4.57.6 / qwen-asr / accelerate 1.12.0 /
   numpy 2.5.3 这些**原样保留**（不用重下 3 GB，也不会被镜像解析成别的小版本）；
2. **卸掉 CUDA 版 torch/torchaudio**，按 AMD 官方 Index 装 ROCm 版（`--no-deps`：torch 的
   那些依赖（sympy/jinja2…）复制过来的环境里已经有了）；
3. 装 `rocm_sdk_core` / `rocm_sdk_libraries_custom`（AMD 运行时，不在 PyPI 上）；
4. 自检打印 `torch.version.hip`，通过后写 `.env-backend=rocm`——`00_start.bat` 照它挑解释器。

> 版本：Windows 侧 AMD 目前最高就是 **7.2.1 / torch 2.9.1**（2026-11 实测，
> `repo.radeon.com/rocm/windows/` 只有到 rocm-rel-7.2.1）。项目现在用的是 torch 2.10.0+cu130，
> 所以 A 卡那条路**要降一个小版本**；想不掉版本只能走 Linux。

用法（A 卡用户）::

    01_install.bat rocm                 和普通安装一样交互，只是换 ROCm 环境
    runtime\\python\\python.exe tools\\install_rocm.py --dry-run    只看要做的事

这个脚本只写 `.env-backend` 和 `.venv-rocm/`，**不碰** `.venv/`、`output/`、`models/`。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from downloader import download, human_bytes  # noqa: E402

from sheetsage2_webui import backend as gpu_backend  # noqa: E402

#: AMD 官方 Windows 轮子目录（不是 pip 索引：没有 PEP 503 的包目录，只能按文件 URL 装）。
ROCM_VERSION = "7.2.1"
INDEX_URL = f"https://repo.radeon.com/rocm/windows/rocm-rel-{ROCM_VERSION}/"

#: 需要的轮子：(文件名, 字节数)。size 用来判断"下完没下全"——AMD 不提供 sha256 旁路文件。
WHEELS: tuple[tuple[str, int], ...] = (
    ("rocm_sdk_core-7.2.1-py3-none-win_amd64.whl", 644793492),
    ("rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl", 489964648),
    ("torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl", 821065907),
    ("torchaudio-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl", 514708),
)

#: 先在旧环境里卸掉这些（CUDA 版），再装 ROCm 版。
REPLACE = ("torch", "torchaudio")
#: AMD 运行时包（必须先装，torch 的 `--no-deps` 不会替我们拉）。
RUNTIME = ("rocm_sdk_core", "rocm_sdk_libraries_custom")

#: 自检：打印版本 / HIP / 设备名。`is_available()` 为 False 时把结论交给调用方。
CHECK = (
    "import torch;"
    "print('torch', torch.__version__);"
    "print('hip', getattr(torch.version, 'hip', None));"
    "print('cuda', torch.version.cuda);"
    "print('available', torch.cuda.is_available());"
    "print('device', torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"
)

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def say(text: str = "") -> None:
    print(text, flush=True)


def title(text: str) -> None:
    say()
    say("=" * 66)
    say(f"  {text}")
    say("=" * 66)


def venv_python(venv: Path) -> Path:
    return venv / "Scripts" / "python.exe"


def wheel_path(name: str) -> Path:
    """本地文件名用**解码后**的 `+`：pip 要靠文件名认 wheel，`%2B` 认不出来。"""
    return ROOT / "wheels-rocm" / name.replace("%2B", "+")


def wheel_url(name: str) -> str:
    """URL 用 AMD 目录里原样的编码（`+` 必须写成 `%2B`，否则 404）。"""
    return INDEX_URL + name


def install_commands(venv: Path, local_wheels: list[Path]) -> list[list[str]]:
    """把"要跑哪几条 pip 命令"做成纯函数（测试里直接断言，不真装）。

    顺序很重要：**运行时包先装**（`--no-deps` 的 torch 不会替我们拉 rocm_sdk），
    然后才装 torch。全部走 `python -m pip`（复制过来的 `pip.exe` 里还写着老路径）。
    """
    python = str(venv_python(venv))
    runtime = [str(item) for item in local_wheels if item.name.startswith(RUNTIME)]
    torch = [str(item) for item in local_wheels if not item.name.startswith(RUNTIME)]
    return [
        [python, "-m", "pip", "uninstall", "-y", *REPLACE],
        [python, "-m", "pip", "install", "--no-deps", "--no-cache-dir", *runtime],
        [python, "-m", "pip", "install", "--no-deps", "--no-cache-dir", *torch],
    ]


def copy_venv(source: Path, target: Path) -> None:
    """复制整份环境。**跳过 `__pycache__`**：没用的几十万个文件，白等几分钟。"""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(source, target, ignore=ignore, dirs_exist_ok=True)


def run(command: list[str], *, dry_run: bool) -> int:
    say("    " + " ".join(command))
    if dry_run:
        return 0
    return subprocess.call(command)


def prepare(*, source: Path, target: Path, fresh: bool, dry_run: bool) -> None:
    title(f"[1/4] 准备 .venv-rocm（{'新建' if fresh else '从 .venv 复制'}）")
    if target.is_dir() and venv_python(target).is_file():
        say("    已经存在，跳过复制（想重来就先删掉 .venv-rocm）")
        return
    if dry_run:
        say(f"    [dry-run] {'新建' if fresh else '复制'} {target}（约 3.8 GB，几分钟）")
        return
    if fresh:
        base = ROOT / "runtime" / "python" / "python.exe"
        python = str(base) if base.is_file() else sys.executable
        say("    正在建空环境…")
        subprocess.check_call([python, "-m", "venv", str(target)])
        say("    正在装项目依赖（几分钟，pip 的输出会一直滚）…")
        code = subprocess.call([str(venv_python(target)), "-m", "pip", "install",
                                "-e", ".[web,svp]", "transformers==4.57.6", "qwen-asr",
                                "--index-url", MIRROR, "--no-cache-dir"])
        if code:
            raise SystemExit("    [x] 依赖装失败：先跑一次 01_install.bat 把环境弄好，"
                             "或者检查网络后重试")
        return
    if not venv_python(source).is_file():
        raise SystemExit("    没有 .venv：先跑一次 01_install.bat（N 卡那套），"
                         "再用 01_install.bat rocm 换 A 卡")
    say("    正在复制（约 3.8 GB，1~3 分钟，没有进度输出是正常的）…")
    copy_venv(source, target)
    say("    复制完成")


def download_wheels(*, dry_run: bool) -> list[Path]:
    title("[2/4] 下载 AMD 的轮子（约 2 GB，断线可续传）")
    total = sum(size for _, size in WHEELS)
    say(f"    一共 {human_bytes(total)}；装好之后 .venv / .venv-rocm 各留一份 torch "
        f"（互相独立，删掉 .venv-rocm 就是回退）")
    # 国内没有 repo.radeon.com 的镜像（清华/中科大都没镜像它；南大镜像的是 PyTorch 官方
    # 轮子，而 PyTorch 的 ROCm 轮子只有 Linux 版），所以这里把 URL 打出来：
    # 慢的话可以拿别的工具/代理下好，丢进 wheels-rocm\ 就会跳过。
    say("    下载太慢可以手动下（放进 wheels-rocm\\ 目录，脚本校验大小后会跳过）：")
    for name, size in WHEELS:
        say(f"      {wheel_url(name)}")
        say(f"        -> wheels-rocm\\{wheel_path(name).name}  （{human_bytes(size)}）")
    local: list[Path] = []
    for name, size in WHEELS:
        target = wheel_path(name)
        if dry_run:
            state = "本地已有" if target.is_file() else "要下载"
            say(f"    [dry-run] {name}：{state}（{human_bytes(size)}）")
            local.append(target)
            continue
        local.append(download(wheel_url(name), target, size=size, label=name,
                              progress=True))
    return local


def install(venv: Path, wheels: list[Path], *, dry_run: bool) -> None:
    title("[3/4] 换成 ROCm 版的 torch")
    for command in install_commands(venv, wheels):
        code = run(command, dry_run=dry_run)
        if code:
            raise SystemExit("    [x] 安装失败。常见原因：网络断 / 文件没下全（重跑本脚本会接着下）。"
                             "装坏了也不要紧：删掉 .venv-rocm 重来即可，.venv 没被动过。")


def selfcheck(venv: Path, *, dry_run: bool) -> bool:
    title("[4/4] 自检")
    if dry_run:
        say("    [dry-run] 然后跑：" + CHECK)
        return True
    code = subprocess.call([str(venv_python(venv)), "-c", CHECK])
    if code:
        say("    [x] 自检失败：这个环境里 torch 都 import 不了")
        return False
    # 单独再问一次"有没有可用设备"：`is_available()==False` 不一定是装坏，也可能是驱动没装。
    probe = subprocess.run(
        [str(venv_python(venv)), "-c",
         "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)"])
    if probe.returncode:
        say("    [!] torch 装上了，但没识别到显卡：先装 AMD 的 Adrenalin 驱动（和 ROCm 版本配套），")
        say("        再重启一次；还不行就把上面这几行输出发回去。")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给 AMD 卡装 ROCm 环境（不动 .venv）")
    parser.add_argument("--venv", default=str(ROOT / ".venv-rocm"),
                        help="目标环境（默认 .venv-rocm）")
    parser.add_argument("--source", default=str(ROOT / ".venv"),
                        help="从哪个环境复制（默认 .venv）")
    parser.add_argument("--fresh", action="store_true",
                        help="不复制，从空环境装（慢，但 .venv 不完整时用得上）")
    parser.add_argument("--dry-run", action="store_true", help="只打印要做的事")
    args = parser.parse_args(argv)

    venv = Path(args.venv)
    source = Path(args.source)
    say()
    say("  SheetSage2 Studio · AMD ROCm 环境")
    say(f"  ROCm {ROCM_VERSION} / torch 2.9.1（Windows 侧 AMD 目前最高的一版）")
    if args.dry_run:
        say("  [dry-run] 不会下载、不会安装、不会改动任何文件")

    started = time.monotonic()
    prepare(source=source, target=venv, fresh=args.fresh, dry_run=args.dry_run)
    wheels = download_wheels(dry_run=args.dry_run)
    install(venv, wheels, dry_run=args.dry_run)
    ok = selfcheck(venv, dry_run=args.dry_run)

    if ok and not args.dry_run:
        gpu_backend.write_backend_marker(ROOT, gpu_backend.BACKEND_ROCM)
        say()
        say("  完成：以后 00_start.bat 会自动用 .venv-rocm 启动（.env-backend = rocm）。")
        say("  想切回 N 卡：删掉 .env-backend，或用 SHEETSAGE_BACKEND=cuda 启动一次。")
    elif not ok:
        say()
        say("  没装完。重跑本脚本会接着来（轮子已经下好的会跳过）；")
        say("  实在不行删掉 .venv-rocm 重来——.venv（N 卡那套）一直没被动过。")
    say(f"  用时 {time.monotonic() - started:.0f} 秒")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\n  被 Ctrl+C 打断。重跑同一条命令会接着来（已下完的轮子不会重下）。")
        raise SystemExit(130)
