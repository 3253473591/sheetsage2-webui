"""内测包首次运行引导：把一台干净机器变成能跑的机器。

内测者的体验应该是：**解压 → 双击 `00_start.bat` → 看着进度 → 浏览器打开**，全程不敲命令。
所以这个脚本自己带下载器（只用标准库，包里那份便携 Python 就能跑），并把每一步都做成
**幂等**的：已存在就跳过，跑到一半断了重跑能续上。

步骤（`--steps` 可以只跑其中几步）：

| 步骤 | 做什么 | 跳过条件 |
|---|---|---|
| `check` | 报环境：包根 / 便携 Python / 随包 ffmpeg / 磁盘 / 网络 | 永远跑（只看） |
| `venv` | 用包里 `runtime\\python` 建 `.venv`（自带 pip，不需要 uv） | `.venv\\Scripts\\python.exe` 在 |
| `torch` | torch + torchaudio 轮子：先看 `wheels/`，没有再按清单下（1.8 GB，可续传） | `.venv` 里 `import torch` 已经能过 |
| `pip` | `pip install -e ".[web,svp]" transformers==4.57.6 qwen-asr`（走清华镜像） | `sheetsage2_webui` + `fastapi` 都能 import |
| `models` | 模型：默认**最小集 4.3 GB**（对齐器已随包带）；1.7B 识别模型可选（+4.5 GB），日语对齐器要 `--with-ja`（+1.7 GB） | `models/<仓库名>/config.json` 等关键文件在 |
| `selfcheck` | `import torch` + CUDA 可用性 + ffmpeg 版本 + 模型齐不齐，写 `bootstrap-state.json` | 每次跑 |

    runtime\\python\\python.exe tools\\bootstrap.py                 # 全流程
    runtime\\python\\python.exe tools\\bootstrap.py --check         # 只体检，不下载
    runtime\\python\\python.exe tools\\bootstrap.py --with-1.7b     # 额外下 1.7B
    runtime\\python\\python.exe tools\\bootstrap.py --dry-run       # 只打印要下什么
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from downloader import download, fetch_json, free_space, human_bytes  # noqa: E402
from modelscope import download_repo, missing_files  # noqa: E402

MANIFEST = ROOT / "downloads.json"
WHEELS = ROOT / "wheels"
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "Scripts" / "python.exe"
STATE = ROOT / "bootstrap-state.json"
BUNDLED_PYTHON = ROOT / "runtime" / "python" / "python.exe"
#: 「这次先不下模型」的标记。`00_start.bat` 看到它就不会直接启动，而是再问一次四个选项。
MODELS_PENDING = ROOT / "models" / ".models-pending"


def mark_models_pending(pending: bool = True) -> None:
    """记下「模型还没下」。选 4 时写、真正下完时删。"""
    try:
        if pending:
            MODELS_PENDING.parent.mkdir(parents=True, exist_ok=True)
            MODELS_PENDING.write_text("这次先不下模型，下次启动要再问一次\n", encoding="utf-8")
        else:
            MODELS_PENDING.unlink(missing_ok=True)
    except OSError:
        pass

ALL_STEPS = ("check", "venv", "torch", "pip", "models", "selfcheck")


# --------------------------------------------------------------------- 工具
def use_utf8_stdio() -> None:
    """和 `scripts/launch.py` 一个口径：统一 UTF-8 + 行缓冲，别让转发出去的中文变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (ValueError, OSError):
            pass


def say(text: str = "") -> None:
    print(text, flush=True)


def title(text: str) -> None:
    say()
    say("  " + "-" * 62)
    say(f"  {text}")
    say("  " + "-" * 62)


def run(command: list[str], *, dry_run: bool = False) -> int:
    say("    $ " + " ".join(str(part) for part in command))
    if dry_run:
        return 0
    return subprocess.call([str(part) for part in command], cwd=ROOT)


def load_manifest() -> dict:
    if not MANIFEST.is_file():
        raise SystemExit(f"缺少清单 {MANIFEST}（内测包解压不完整？）")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- 各步
def step_check(manifest: dict) -> dict:
    title("[1/6] 环境体检")
    info: dict[str, object] = {"root": str(ROOT)}
    say(f"    包目录      {ROOT}")
    say(f"    系统        {platform.system()} {platform.release()} / Python {platform.python_version()}")

    if BUNDLED_PYTHON.is_file():
        say(f"    便携解释器  {BUNDLED_PYTHON.relative_to(ROOT)}")
        info["bundled_python"] = str(BUNDLED_PYTHON)
    else:
        say("    [!] 没有找到 runtime/python（内测包解压不完整？）")

    ffmpeg = ROOT / manifest["bundled"]["ffmpeg"]["path"]
    if ffmpeg.is_file():
        say(f"    随包 ffmpeg {ffmpeg.relative_to(ROOT)}（{human_bytes(ffmpeg.stat().st_size)}）")
        info["ffmpeg"] = str(ffmpeg)
    else:
        say("    [!] 随包 ffmpeg 不见了，扒谱会失败")

    free = free_space(ROOT)
    say(f"    磁盘可用    {human_bytes(free)}")
    if free < 20 * 1024 ** 3:
        say("    [!] 建议留 20 GB 以上（模型 6~10.4 GB + torch 1.8 GB + 产物）")
    info["free_bytes"] = free

    try:
        fetch_json("https://www.modelscope.cn/api/v1/models/m-a-p/SheetSage2",
                   timeout=20)
        say("    网络        ModelScope 可达")
        info["network"] = True
    except Exception as exc:                       # noqa: BLE001
        say(f"    [!] 网络        连不上 ModelScope（{type(exc).__name__}）——下载会失败")
        info["network"] = False
    return info


def step_venv(*, dry_run: bool) -> None:
    title("[2/6] 建 Python 环境（用包里那份，不用 uv、不用联网装 Python）")
    if VENV_PYTHON.is_file():
        say(f"    {VENV.relative_to(ROOT)} 已存在，跳过")
        return
    if not BUNDLED_PYTHON.is_file():
        raise SystemExit("    没有 runtime/python/python.exe，没法建环境")
    say("    正在建 .venv（10~30 秒，这一步**没有进度输出是正常的**）…")
    run([BUNDLED_PYTHON, "-m", "venv", str(VENV)], dry_run=dry_run)
    if dry_run:
        return
    code = run([VENV_PYTHON, "-c", "import pip, sys; print('pip', pip.__version__)"])
    if code != 0:                                  # venv 自带的 pip 坏了才补
        run([VENV_PYTHON, "-m", "ensurepip", "--upgrade"])
    say("    环境就绪")


def venv_imports(module: str) -> bool:
    if not VENV_PYTHON.is_file():
        return False
    return subprocess.call([str(VENV_PYTHON), "-c", f"import {module}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def step_torch(manifest: dict, *, dry_run: bool) -> None:
    title("[3/6] torch / torchaudio（约 1.8 GB，断线可续传）")
    if venv_imports("torch"):
        say("    已经装好了，跳过")
        return
    total = sum(item.get("bytes") or 0 for item in manifest["torch"])
    say(f"    要下 {human_bytes(total)}（按 10 MB/s 约 {total / 10 / 1024 / 1024 / 60:.0f} 分钟）；"
        f"下完会校验 sha256（那几秒也没进度条，正常）")
    # `--dry-run` 必须在这一行之后**立刻**停住：`download()` 不认识 dry_run，
    # 放任它跑下去就是真下 1.8 GB（曾经就这么漏过一次，把包目录塞满了轮子）。
    if dry_run:
        for item in manifest["torch"]:
            target = WHEELS / item["name"]
            state = "本地已有（会重算 sha256）" if target.is_file() else "要下载"
            say(f"    [dry-run] {item['name']}：{state}（{human_bytes(item.get('bytes') or 0)}）")
        say("    [dry-run] 然后 pip install 上面这些轮子（跳过）")
        return
    WHEELS.mkdir(exist_ok=True)
    local: list[Path] = []
    for item in manifest["torch"]:
        # 一律走 `download()`：本地已有的轮子也会**校验 sha256**（1.8 GB 的包，
        # 拿错版本/半截文件在这里当场发现，比 pip 报一堆看不懂的错强）。
        local.append(download(item["url"], WHEELS / item["name"], sha256=item["sha256"],
                              size=item.get("bytes"), label=item["name"], progress=True))
    say("    装进 .venv（pip 自己会打进度）…")
    code = run([VENV_PYTHON, "-m", "pip", "install", *local,
                "--index-url", manifest["pypi"]["index_url"],
                "--extra-index-url", manifest["pypi"]["extra_index_url"],
                "--no-cache-dir"],
               dry_run=dry_run)
    if code and not dry_run:
        raise SystemExit(
            "    [x] torch 装失败。常见原因：网络断了 / pip 缓存里有个坏文件（已加 --no-cache-dir）。"
            "重跑 01_install.bat 即可，已下好的轮子还在 wheels\\ 里。")


def step_pip(manifest: dict, *, dry_run: bool) -> None:
    title("[4/6] 项目依赖（走国内镜像）")
    if venv_imports("fastapi") and venv_imports("sheetsage2_webui"):
        say("    已经装好了，跳过")
        return
    if not dry_run and not venv_imports("torch"):
        # 先装 torch 再装依赖是有顺序的：反过来 pip 会去 PyPI 拖 CPU 版 torch（2.5 GB，没 CUDA）
        raise SystemExit("    [x] 还没装 torch。先跑 `01_install.bat`（会先下 torch 再装依赖），"
                         "或者 `01_install.bat --steps torch,pip`。")
    say("    正在装依赖（pip 的输出会一直滚，几分钟很正常）…")
    code = run([VENV_PYTHON, "-m", "pip", "install", "-e", ".[web,svp]",
                *manifest["pypi"]["packages"],
                "--index-url", manifest["pypi"]["index_url"],
                "--no-cache-dir"],
               dry_run=dry_run)
    if code and not dry_run:
        # **必须响亮地失败**：这里吞掉返回码的话，环境是半成品，用户却在最后才发现
        # "No module named 'sheetsage2_webui'"——实测踩过。
        raise SystemExit(
            "    [x] 依赖装失败（pip 返回 {code}）。看着上面的报错：多半是网络断在中途，"
            "重跑 01_install.bat 即可；一直失败就把整段输出发我。".format(code=code))


def chosen_repos(manifest: dict, mode: str, with_17b: bool,
                 with_ja: bool = False) -> list[str]:
    """按档位给出要下的仓库。

    **对齐器不再默认下**：TIFA 随包带，中/粤/英都用它；只有日语还需要
    Qwen3-ForcedAligner（TIFA 1.0 没有日语的离线 G2P），所以它挪进了可选，
    用 `--with-ja` 才下。
    """
    models = manifest["models"]
    repositories = models["repositories"]
    if mode == "skip":
        return []
    if mode == "full":
        return list(models["full"])
    repos = list(models["minimal"])                 # 默认最小集
    optional = models.get("optional") or {}
    asr_17b = optional.get("asr_1.7b")
    ja_aligner = optional.get("ja_aligner")
    if with_17b and asr_17b:
        repos.append(asr_17b)
    if with_ja and ja_aligner:
        repos.append(ja_aligner)
    return [repo for repo in dict.fromkeys(repos) if repo in repositories]


def step_models(manifest: dict, *, mode: str, with_17b: bool, with_ja: bool,
                dry_run: bool) -> list[dict]:
    title("[5/6] 模型")
    models = manifest["models"]
    repositories = models["repositories"]
    if mode == "skip":
        say("    这次先不下模型（下次启动还会再问）")
        if not dry_run:
            mark_models_pending()
        return []

    repos = chosen_repos(manifest, mode, with_17b, with_ja)
    optional = models.get("optional") or {}
    if mode == "minimal":
        if "asr_1.7b" in optional and not with_17b:
            size = repositories[optional["asr_1.7b"]]["bytes"]
            say(f"    （1.7B 识别模型是可选项，{human_bytes(size)}，在 02_models.bat 里选 3）")
        if "ja_aligner" in optional and not with_ja:
            size = repositories[optional["ja_aligner"]]["bytes"]
            say(f"    （中文/粤语/英语用随包的 TIFA 对齐器，不用额外下；"
                f"要唱日语在 02_models.bat 里选 2，{human_bytes(size)}）")

    plan = sum(repositories[repo]["bytes"] for repo in repos)
    say(f"    档位 {mode}：{len(repos)} 个仓库，合计 {human_bytes(plan)}"
        f"（按 10 MB/s 约 {plan / 10 / 1024 / 1024 / 60:.0f} 分钟；"
        f"进度条一行一个文件，末尾会写'总 x/y'）")
    for repo in repos:
        say(f"      · {repo:<30} {human_bytes(repositories[repo]['bytes'])}"
            f" -> {repositories[repo]['target']}")
    if dry_run:
        return [{"repo": repo, "dry_run": True} for repo in repos]

    results = []
    done_before = 0
    for repo in repos:
        info = repositories[repo]
        result = download_repo(repo, ROOT / info["target"],
                               revision=info.get("revision", "master"),
                               done_bytes_before=done_before, grand_total=plan,
                               progress=True)
        done_before += info["bytes"]
        results.append(result)
    say(f"    模型下完：累计 {human_bytes(min(done_before, plan))}/{human_bytes(plan)}")
    mark_models_pending(False)
    return results


def step_selfcheck(manifest: dict, model_results: list[dict], *,
                   dry_run: bool) -> dict:
    title("[6/6] 自检")
    report: dict[str, object] = {"when": time.strftime("%Y-%m-%d %H:%M:%S")}
    if dry_run:
        say("    --dry-run：跳过")
        return report

    code = (
        "import json, torch;"
        "print(json.dumps({'torch': torch.__version__,"
        " 'cuda': torch.cuda.is_available(),"
        " 'cuda_version': torch.version.cuda,"
        " 'gpu': (torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)}))"
    )
    say("    正在 import torch（第一次要几秒，没输出正常）…")
    result = subprocess.run([str(VENV_PYTHON), "-c", code], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode == 0:
        torch_info = json.loads(result.stdout.strip().splitlines()[-1])
        report.update(torch_info)
        say(f"    torch {torch_info['torch']}（CUDA {torch_info['cuda_version']}）")
        if torch_info["cuda"]:
            say(f"    GPU  {torch_info['gpu']}")
        else:
            say("    [!] CUDA 不可用：能跑，但全曲扒谱会非常慢（多半是显卡驱动太旧）")
    else:
        say("    [!] torch 自检失败：")
        say((result.stderr or "").strip()[-500:])
        report["torch_error"] = (result.stderr or "").strip()[-500:]

    ffmpeg = ROOT / manifest["bundled"]["ffmpeg"]["path"]
    if ffmpeg.is_file():
        probe = subprocess.run([str(ffmpeg), "-version"], capture_output=True, text=True)
        first = (probe.stdout or "").splitlines()[0] if probe.stdout else "?"
        report["ffmpeg"] = first
        say(f"    ffmpeg {first}")
    else:
        say("    [!] 随包 ffmpeg 不见了")
        report["ffmpeg"] = None

    for info in model_results:
        if info.get("dry_run"):
            continue
        repo, target = info["repo"], Path(info["target"])
        if not target.is_dir():
            continue
        try:
            missing = missing_files(repo, target, revision="master")
        except Exception:                          # noqa: BLE001 - 断网时别炸
            missing = []
        report.setdefault("models", {})[repo] = {"missing": len(missing)}
        flag = "齐" if not missing else f"缺 {len(missing)} 个文件"
        say(f"    {repo:<30} {flag}")

    say()
    say("  下一步：双击 00_start.bat 启动；以后想补模型就双击 02_models.bat")
    return report


# --------------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SheetSage2 内测包首次运行引导")
    parser.add_argument("--steps", default=",".join(ALL_STEPS),
                        help=f"只跑这几步，逗号分隔（默认全部）：{','.join(ALL_STEPS)}")
    parser.add_argument("--models", default=None, choices=("minimal", "full", "skip"),
                        help="模型档位：minimal 最小集（默认）/ full 全量 / skip 跳过")
    parser.add_argument("--with-1.7b", dest="with_17b", action="store_true",
                        help="在最小集之外**额外**下 1.7B 识别模型（+4.5 GB，专有名词更准）")
    parser.add_argument("--with-ja", dest="with_ja", action="store_true",
                        help="额外下 Qwen 对齐器（+1.7 GB）：只在唱**日语**歌时需要，"
                             "中/粤/英用随包的 TIFA")
    parser.add_argument("--check", action="store_true", help="只体检，什么都不下")
    parser.add_argument("--dry-run", action="store_true", help="只打印要做的事")
    args = parser.parse_args(argv)

    use_utf8_stdio()
    manifest = load_manifest()
    steps = [item.strip() for item in args.steps.split(",") if item.strip()]
    unknown = [item for item in steps if item not in ALL_STEPS]
    if unknown:
        raise SystemExit(f"不认识的步骤 {unknown}；可选 {list(ALL_STEPS)}")
    if args.check:
        steps = ["check"]

    mode = args.models
    with_17b = args.with_17b
    with_ja = args.with_ja
    if mode is None:
        if "models" not in steps:
            mode = "skip"
        elif args.dry_run or not sys.stdin.isatty():
            mode = "minimal"                        # 非交互/预演：默认最小集
        else:
            say()
            say("  这次要下载哪一套？（说明见 README）")
            say("    1  默认（中/粤/英）                  约 4.3 GB  —— 默认")
            say("    2  日语特供                                +1.7 GB")
            say("    3  提高歌词识别精度（适合大显存）          +4.5 GB")
            say("    4  暂不下载（下次启动还会再问）")
            answer = input("  选一个 1/2/3/4（回车 = 1）: ").strip() or "1"
            if answer == "2":
                mode, with_ja = "minimal", True
            elif answer == "3":
                mode, with_17b = "minimal", True
            elif answer == "4":
                mode = "skip"
            else:
                mode = "minimal"

    say()
    say("  SheetSage2 Studio · 内测包首次运行")
    say(f"  版本 {manifest['version']}    步骤 {'/'.join(steps)}")

    started = time.monotonic()
    report: dict[str, object] = {"version": manifest["version"], "steps": steps}
    model_results: list[dict] = []

    if "check" in steps:
        report["check"] = step_check(manifest)
    if "venv" in steps:
        step_venv(dry_run=args.dry_run)
    if "torch" in steps:
        step_torch(manifest, dry_run=args.dry_run)
    if "pip" in steps:
        step_pip(manifest, dry_run=args.dry_run)
    if "models" in steps:
        model_results = step_models(manifest, mode=mode, with_17b=with_17b,
                                    with_ja=with_ja, dry_run=args.dry_run)
    if "selfcheck" in steps:
        report["selfcheck"] = step_selfcheck(manifest, model_results, dry_run=args.dry_run)

    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    if not args.dry_run:
        STATE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
        say(f"  （记录写到 {STATE.relative_to(ROOT)}）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\n  被 Ctrl+C 打断。重跑同一个命令会**接着下**（已下完的文件不会重下）。")
        raise SystemExit(130) from None
