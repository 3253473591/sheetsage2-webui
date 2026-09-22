"""轻量包首次安装：装依赖 + 下模型。

由 ``install.bat`` 在准备好便携 Python 之后调用::

    runtime\\python\\python.exe tools\\install_deps.py [--device auto|cpu|gpu] [--yes]

做四件事，每步都可重入（已经装好的会跳过，不会重复下载）：

1. 安装 torch / torchaudio —— **有 N 卡装 cu130，没 N 卡装 CPU 版**（必须最先装）
2. ``--no-deps`` 安装 ``requirements-base.txt`` 里的 69 个纯 PyPI 依赖（约 300 MB）
3. 用 huggingface_hub 拉 ``m-a-p/SheetSage2`` 与 ``m-a-p/MERT-v2-FullSong``
4. 校验权重文件（有 sha256 清单就核对），最后在子进程里 import 一次验收

装依赖优先用**随包的 uv**（``runtime\\uv.exe``，快两个数量级），没有才退回 ``python -m pip``。

网络这块默认走官方源，失败自动换国内镜像重试（pip 换清华、torch 换阿里/上交、
HuggingFace 换 hf-mirror）。想直接指定用 ``--pypi-mirror`` / ``--torch-index`` /
``--hf-endpoint``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, OSError):
    pass

MODELS_DIR = ROOT / "models"
REQ_BASE = ROOT / "requirements-base.txt"

#: 权重仓库 → 包内落地目录
REPOS = (
    ("m-a-p/SheetSage2", MODELS_DIR / "SheetSage2"),
    ("m-a-p/MERT-v2-FullSong", MODELS_DIR / "MERT-v2-FullSong"),
)

PYPI_OFFICIAL = "https://pypi.org/simple"
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

#: 默认（也是随包构建、端到端验收用）的 torch 版本
DEFAULT_TORCH_VERSION = "2.10.0"
#: **声明的最低 torch 版本**。三个来源取最大值：
#:   * transformers 4.57.6 声明 `torch>=2.2`
#:   * accelerate 1.15.0 声明 `torch>=2.0.0`
#:   * 便携运行时是 CPython 3.12，而 torch 从 2.2.0 才提供 cp312 的 win_amd64 wheel
#: 另外 numpy 钉在 2.5.3，torch<2.3 与 numpy 2.x 有已知的 C-API 不兼容，
#: 所以实际建议 >=2.3；实测跑通的是 2.6.0 与 2.10.0（见 packaging\README-打包说明.md）。
MIN_TORCH = (2, 2)

TORCH_INDEX = {
    "gpu": "https://download.pytorch.org/whl/cu130",
    "cpu": "https://download.pytorch.org/whl/cpu",
}
TORCH_INDEX_FALLBACK = {
    "gpu": [
        "https://mirrors.aliyun.com/pytorch-wheels/cu130",
        "https://mirror.sjtu.edu.cn/pytorch-wheels/cu130",
    ],
    "cpu": [
        "https://mirrors.aliyun.com/pytorch-wheels/cpu",
    ],
}
TORCH_PINS = {
    "gpu": (f"torch=={DEFAULT_TORCH_VERSION}+cu130", f"torchaudio=={DEFAULT_TORCH_VERSION}+cu130"),
    "cpu": (f"torch=={DEFAULT_TORCH_VERSION}+cpu", f"torchaudio=={DEFAULT_TORCH_VERSION}+cpu"),
}

HF_OFFICIAL = "https://huggingface.co"
HF_MIRROR = "https://hf-mirror.com"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def step(title: str) -> None:
    log()
    log("=" * 66)
    log(f"  {title}")
    log("=" * 66)


def run(cmd: list[str], **kwargs) -> int:
    """回显并执行命令，返回退出码。"""
    log("$ " + " ".join(cmd))
    try:
        return subprocess.call(cmd, **kwargs)
    except OSError as exc:
        log(f"[!] 无法执行：{exc}")
        return 1


# --------------------------------------------------------------------------
# 设备选择
# --------------------------------------------------------------------------
def detect_gpu() -> tuple[bool, str]:
    """返回 (是否有 NVIDIA 显卡, 说明)。只依赖 nvidia-smi，不 import torch。"""
    smi = shutil.which("nvidia-smi") or r"C:\WINDOWS\system32\nvidia-smi.exe"
    if not Path(smi).exists():
        return False, "没有找到 nvidia-smi"
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"nvidia-smi 执行失败：{exc}"
    line = (out.stdout or "").strip().splitlines()
    if not line:
        return False, "nvidia-smi 没有输出"
    return True, line[0].strip()


def choose_device(requested: str, ask: bool) -> str:
    has_gpu, info = detect_gpu()
    if requested != "auto":
        log(f"  按参数指定：{requested}")
        return requested
    if has_gpu:
        log(f"  检测到 NVIDIA 显卡：{info}")
        if ask:
            # 注意：stdin 是 tty 也可能读不到（被脚本/钩子接管时会直接 EOF），
            # 所以这里必须吞掉 EOFError，而不是让整个安装崩在这一行。
            try:
                answer = input("  要装 GPU 版（cu130，约 2.7 GB）吗？[Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                log("  （读不到键盘输入，按默认继续）")
                answer = ""
            if answer in ("n", "no"):
                log("  好，改装 CPU 版。")
                return "cpu"
        return "gpu"
    log(f"  没检测到可用的 NVIDIA 显卡（{info}）。")
    log("  → 装 CPU 版 torch（体积小很多，但扒谱速度会慢十几倍）。")
    return "cpu"


# --------------------------------------------------------------------------
# pip / uv
# --------------------------------------------------------------------------
def _uv_exe() -> str | None:
    """优先用随包的 uv：解析依赖比 pip 快两个数量级，而且不用等 pip 回溯。"""
    cand = ROOT / "runtime" / "uv.exe"
    if cand.is_file():
        return str(cand)
    return shutil.which("uv")


def _install(args: list[str], index_url: str | None, no_deps: bool = False) -> int:
    uv = _uv_exe()
    if uv:
        cmd = [uv, "pip", "install", "--python", sys.executable, *args]
        if index_url:
            cmd += ["--index-url", index_url]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]
        if index_url:
            cmd += ["--index-url", index_url]
    if no_deps:
        cmd.append("--no-deps")
    return run(cmd)


def _local_tag(index_url: str | None) -> str:
    """从 PyTorch 索引 URL 的末段推出本地版本标签。

    `https://download.pytorch.org/whl/cpu` → ``cpu``；
    `https://mirrors.aliyun.com/pytorch-wheels/cu124` → ``cu124``。
    推不出来时返回空串（那就装不带本地标签的包，例如从 PyPI 装）。
    """
    import re

    tail = (index_url or "").rstrip("/").rsplit("/", 1)[-1].lower()
    if tail == "cpu" or re.fullmatch(r"cu\d{2,3}", tail):
        return tail
    return ""


def resolve_torch_specs(
    device: str,
    torch_spec: str | None,
    torch_version: str | None,
    index_url: str | None,
) -> list[str]:
    """决定这一步到底装哪两个包。

    优先级：``--torch-spec``（完全自定义） > ``--torch-version``（带自动本地标签）
    > 内置默认引脚。本地标签按**当前正在试的那个源**推导，所以换镜像时也不会装错。
    """
    if torch_spec:
        return [s for s in torch_spec.replace(",", " ").split() if s]
    if torch_version:
        tag = _local_tag(index_url)
        suffix = f"+{tag}" if tag else ""
        return [f"torch=={torch_version}{suffix}", f"torchaudio=={torch_version}{suffix}"]
    return list(TORCH_PINS[device])


def install_torch(
    device: str,
    torch_index: str | None,
    torch_version: str | None = None,
    torch_spec: str | None = None,
) -> bool:
    """**先装 torch**。

    顺序很关键：``accelerate`` 的元数据里依赖 ``torch>=2.0``，如果先装基础依赖，
    解析器会从 PyPI 拖一个我们不要的 torch（实测拉了 2.14.0，2.5 GB）。
    先把正确的 torch 装好，后面基础依赖再做 ``--no-deps``，就没人能动了。

    ``--torch-version`` / ``--torch-spec`` 可以把版本换成别的：
    程序里**没有**任何 torch 版本断言，换版本只是为了适配别人的显卡驱动或已有环境。
    """
    step(f"第 1/4 步：安装 PyTorch（{device} 版）")
    indexes = [torch_index] if torch_index else [TORCH_INDEX[device], *TORCH_INDEX_FALLBACK[device]]
    for url in indexes:
        if not url:
            continue
        specs = resolve_torch_specs(device, torch_spec, torch_version, url)
        log(f"  源：{url}")
        log(f"  装：{'  '.join(specs)}")
        if _install(specs, url) == 0:
            return True
        log(f"[!] {url} 安装失败，换下一个源…")
    return False


def install_base(pypi_mirror: str) -> bool:
    """装纯 PyPI 依赖。

    ``requirements-base.txt`` 是 ``uv pip freeze`` 出来的**完整闭包**（69 个包），
    所以这里用 ``--no-deps``：既省一次全图解析，也避免任何包把 torch 换掉。
    """
    step("第 2/4 步：安装基础依赖（约 300 MB）")
    if not REQ_BASE.is_file():
        log(f"[!] 找不到 {REQ_BASE}")
        return False
    args = ["-r", str(REQ_BASE)]
    if _install(args, PYPI_OFFICIAL, no_deps=True) == 0:
        return True
    log()
    log("[!] 官方 PyPI 失败，改用国内镜像重试…")
    fallback = pypi_mirror or PYPI_MIRROR
    return _install(args, fallback, no_deps=True) == 0


# --------------------------------------------------------------------------
# 模型权重
# --------------------------------------------------------------------------
def download_models(hf_endpoint: str | None) -> bool:
    step("第 3/4 步：下载模型权重（约 2.6 GB，最慢的一步）")
    endpoints = [hf_endpoint] if hf_endpoint else [HF_OFFICIAL, HF_MIRROR]
    for endpoint in endpoints:
        if not endpoint:
            continue
        os.environ["HF_ENDPOINT"] = endpoint
        log(f"  源：{endpoint}")
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            log("[!] 没有 huggingface_hub —— 第 1 步没成功？")
            return False
        ok = True
        for repo, target in REPOS:
            if (target / "model.safetensors").is_file():
                log(f"  [跳过] {repo} 已存在：{target}")
                continue
            target.mkdir(parents=True, exist_ok=True)
            log(f"  下载 {repo} → {target}")
            try:
                snapshot_download(
                    repo_id=repo,
                    local_dir=str(target),
                    # 只下运行必需的文件，别把 .gitattributes/示例音频一起拖下来
                    ignore_patterns=["*.png", "*.jpg", "*.jpeg", "*.gif", "*.mp4", "*.wav", "*.flac", "*.mp3"],
                )
            except Exception as exc:  # noqa: BLE001 - 网络问题要能重试
                log(f"  [!] {repo} 下载失败：{type(exc).__name__}: {exc}")
                ok = False
                break
        if ok:
            return True
        log("[!] 换下一个 HuggingFace 源重试…")
    return False


def _sha256(path: Path, chunk: int = 1 << 22) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def verify(expect_models: bool = True) -> bool:
    step("第 4/4 步：验收")
    ok = True

    # ---- 权重文件 ----
    required = {
        MODELS_DIR / "SheetSage2" / "model.safetensors": None,
        MODELS_DIR / "SheetSage2" / "config.json": None,
        MODELS_DIR / "MERT-v2-FullSong" / "model.safetensors": None,
    }
    manifest = MODELS_DIR / "MERT-v2-FullSong" / "weights_manifest.json"
    if manifest.is_file():
        try:
            required[MODELS_DIR / "MERT-v2-FullSong" / "model.safetensors"] = (
                json.loads(manifest.read_text(encoding="utf-8")).get("sha256")
            )
        except (OSError, ValueError):
            pass

    if not expect_models:
        # --skip-models：本来就没打算下权重，缺了不是错误，只提示一句
        missing = [p for p in required if not p.is_file()]
        log(f"  [跳过] 权重检查（--skip-models）；当前缺 {len(missing)} 个文件")
        log("         真要开始扒谱前，用不带 --skip-models 的命令再跑一次。")
        required = {}

    for path, expected in required.items():
        if not path.is_file():
            log(f"  [缺失] {path}")
            ok = False
            continue
        size_mb = path.stat().st_size / 1024**2
        log(f"  [存在] {path.relative_to(ROOT)}  {size_mb:.1f} MB")
        if expected and size_mb > 50:
            log("         正在核对 sha256（大文件，要等十几秒）…")
            actual = _sha256(path)
            if actual.lower() == str(expected).lower():
                log("         sha256 一致 ✅")
            else:
                log(f"  [!] sha256 不一致！\n         期望 {expected}\n         实际 {actual}")
                log("         多半是下载中断，删掉这个文件重跑安装即可。")
                ok = False

    # ---- 子进程 import 验收 ----
    log("\n  正在子进程里 import torch / transformers（首次要十几秒）…")
    probe = (
        "import json,sys;"
        "r={};"
        "import torch;r['torch']=torch.__version__;r['cuda']=bool(torch.cuda.is_available());"
        "r['cuda_version']=torch.version.cuda;"
        "import transformers;r['transformers']=transformers.__version__;"
        "print('@@'+json.dumps(r))"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        line = next((l for l in reversed((out.stdout or "").splitlines()) if l.startswith("@@")), None)
        if line:
            info = json.loads(line[2:])
            log(f"  torch {info['torch']} / transformers {info['transformers']}")
            # 版本下限：transformers 4.57.6 要 torch>=2.2（accelerate 要 >=2.0），
            # 加上 numpy 2.x 与 torch<2.3 的 C-API 不兼容，低于声明值就直接点出来。
            try:
                parts = tuple(int(x) for x in info["torch"].split("+")[0].split(".")[:2])
            except (KeyError, ValueError):
                parts = ()
            if parts and parts < MIN_TORCH:
                log(f"  [!] torch {info['torch']} 低于最低要求 {MIN_TORCH[0]}.{MIN_TORCH[1]}，"
                    "大概率跑不起来；建议回去用默认版本")
                ok = False
            elif info["cuda"]:
                log(f"  CUDA 可用（{info['cuda_version']}）✅ 会用显卡跑")
            else:
                log("  CUDA 不可用 → 会用 CPU 跑（慢，但能出结果）")
        else:
            log("  [!] 探测子进程没有正常输出：")
            log((out.stdout or "")[-1500:])
            log((out.stderr or "")[-1500:])
            ok = False
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  [!] 探测失败：{exc}")
        ok = False

    return ok


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="SheetSage2 轻量包安装器")
    parser.add_argument("--device", choices=("auto", "gpu", "cpu"), default="auto")
    parser.add_argument("--yes", action="store_true", help="不询问，直接按检测结果装")
    parser.add_argument("--pypi-mirror", default=None)
    parser.add_argument("--torch-index", default=None, help="换 PyTorch 下载源（如 .../whl/cu124）")
    parser.add_argument("--torch-version", default=None,
                        help=f"换 torch 版本（如 2.6.0）。默认 {DEFAULT_TORCH_VERSION}；"
                             "本地标签 +cpu/+cuXXX 会按 --torch-index 自动推导。"
                             f"程序要求 torch>={MIN_TORCH[0]}.{MIN_TORCH[1]}，与 torchaudio 同版本")
    parser.add_argument("--torch-spec", default=None,
                        help="完全自定义要装的 torch 包，逗号分隔，"
                             "如 'torch==2.6.0+cpu,torchaudio==2.6.0+cpu'（会覆盖 --torch-version）")
    parser.add_argument("--hf-endpoint", default=None)
    parser.add_argument("--skip-deps", action="store_true", help="只下模型")
    parser.add_argument("--skip-models", action="store_true", help="只装依赖")
    args = parser.parse_args()

    started = time.time()
    log("SheetSage2 扒谱 · 轻量包首次安装")
    log(f"目标目录：{ROOT}")

    step("第 0/4 步：选择要安装的 PyTorch 版本")
    device = choose_device(args.device, ask=(not args.yes and sys.stdin.isatty()))
    if args.torch_version and not args.torch_spec:
        log(f"  指定版本：torch {args.torch_version}"
            + (f"（源 {args.torch_index}）" if args.torch_index else "（源按设备自动选）"))
        if not args.torch_index and device == "gpu":
            log(f"  [提示] cu130 源只有 torch>=2.9；装 {args.torch_version} 需要配一个老一点的源，例如：")
            log(f"         --torch-index https://download.pytorch.org/whl/cu124")
    if args.torch_spec:
        log(f"  自定义安装项：{args.torch_spec}")

    if not args.skip_deps:
        if not install_torch(device, args.torch_index, args.torch_version, args.torch_spec):
            log("\n[×] PyTorch 安装失败。可以试试指定镜像或版本：")
            log("    runtime\\python\\python.exe tools\\install_deps.py "
                "--torch-index https://mirrors.aliyun.com/pytorch-wheels/cu130")
            log("    runtime\\python\\python.exe tools\\install_deps.py "
                "--torch-version 2.6.0 --torch-index https://download.pytorch.org/whl/cpu")
            return 1
        if not install_base(args.pypi_mirror):
            log("\n[×] 基础依赖安装失败。网络恢复后重新双击 install.bat 即可续传。")
            return 1

    if not args.skip_models and not download_models(args.hf_endpoint):
        log("\n[×] 模型下载失败。国内网络建议挂梯子，或改用镜像：")
        log("    runtime\\python\\python.exe tools\\install_deps.py --hf-endpoint https://hf-mirror.com")
        return 1

    good = verify(expect_models=not args.skip_models)
    minutes = (time.time() - started) / 60
    log()
    if good:
        log(f"✅ 安装完成，用时 {minutes:.1f} 分钟。双击 start.bat 开始扒谱。")
        return 0
    log(f"⚠️ 安装跑完了（{minutes:.1f} 分钟）但验收有异常，请看上面标 [!] 的行。")
    log("   还搞不定就双击 selfcheck.bat，把生成的环境快照发给 AI。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
