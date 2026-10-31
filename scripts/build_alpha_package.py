r"""打内测包：把「解压就能跑」需要的东西拼成一个目录，再压成 zip。

放进包里的（都是实测过体积的）：

| 内容 | 说明 |
|---|---|
| `runtime/python/` | 便携 CPython 3.12（61 MB，自带 pip/venv，**同好机器不需要装 Python**） |
| `runtime/ffmpeg/bin/ffmpeg.exe` | 裁剪版静态 ffmpeg 7.1.1（2.2 MB，无 DLL）—— `bootstrap.find_ffmpeg()` 第一优先 |
| `src/` `scripts/` `tools/` `web/` | 应用代码 + 前端源码（前端还有已构建的 static，所以同好不需要 Node） |
| `downloads.json` | torch / 模型的直链与 sha256（`tools/bootstrap.py` 照着下） |
| `00_start.bat` / `01_install.bat` / `02_models.bat` | 三个双击入口：启动 / 安装 / 补模型 |
| `NOTICE` + `runtime/ffmpeg/LICENSE-ffmpeg.txt` | 许可（SheetSage2 权重 CC BY-NC 4.0；ffmpeg 的构建配置） |

**不放进包的**：`.venv/`（同好自己建）、`models/`（6~10.4 GB，首次运行再下）、
`output/` `tmp/` `dist/`、`web/node_modules/`、`samples/`（测试音频）、`reference/`（调研用）。

    .\.venv\Scripts\python.exe scripts\build_alpha_package.py            # 打包
    .\.venv\Scripts\python.exe scripts\build_alpha_package.py --no-zip   # 只拼目录
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

#: 便携 Python 从哪抄（uv 装的那份，自带 pip/ensurepip/venv）。
DEFAULT_PYTHON_RUNTIME = Path.home() / "AppData" / "Roaming" / "uv" / "python" / \
    "cpython-3.12.11-windows-x86_64-none"
#: 裁剪版静态 ffmpeg（参考项目里那份，实测四条用法全过）。
DEFAULT_FFMPEG = ROOT / "reference" / "Vocal2Midi" / "ffmpeg.exe"

#: TIFA 对齐权重。代码包里带一份 fp16，省得再从 GitHub Release 下（国内镜像也没有）。
TIFA_SOURCE = ROOT / "models" / "TIFA-1.0"

#: 7-Zip 可执行文件。有它就多打一份 `.7z`：实测比 zip 小 11 MB（98.9 -> 87.5），
#: 因为 LZMA2 对便携 Python 那种二进制更狠，而权重两者都压不动。
SEVEN_ZIP_CANDIDATES = (Path(r"D:\7-Zip\7z.exe"),
                        Path(r"C:\Program Files\7-Zip\7z.exe"),
                        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"))

#: 目录整体带走。
COPY_DIRS = ("src", "scripts", "tools", "web", "docs", "tests")
#: 单个文件带走。
COPY_FILES = ("pyproject.toml", "downloads.json", "NOTICE", "README.md",
              "00_start.bat", "01_install.bat", "02_models.bat", ".gitignore")
#: 目录里不要的东西。
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", "node_modules", "dist", ".smoke",
                 ".venv", "models", "output", "tmp", "wheels", "runtime"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip"}

FFMPEG_LICENSE = """FFmpeg — 裁剪版静态构建（本项目随包分发）
================================================

版本：7.1.1（Windows x86_64，静态链接，无外部 DLL，约 2.2 MB）
来源：参考项目 Vocal2Midi 随附的构建（见仓库 NOTICE）。

构建配置（原作者给出的裁剪参数）：

    --disable-everything --enable-small --extra-ldflags=-static
    --enable-protocol='file,pipe'
    --enable-demuxer='mov,mp3,ogg,flac,wav,aac,asf,matroska,aiff'
    --enable-decoder='aac,aac_latm,mp3,mp3float,flac,vorbis,opus,alac,wmav1,wmav2,wmapro,pcm_*'
    --enable-muxer='wav,ipod,pcm_f32le'
    --enable-filter='aresample,format,aformat,abuffer,abuffersink,buffer,buffersink'

许可：FFmpeg 是 LGPL 2.1+/GPL 的混合体，具体取决于构建时开了哪些组件。
本内测包**只把它当成可执行程序随包附带**（未修改二进制、未与项目代码链接），
用于解码用户上传的音频。完整的许可文本与构建信息见 https://ffmpeg.org/legal.html 。
如需商用或再分发，请自行确认该构建的许可条款，或换成官方构建。
"""


def human(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def should_skip(path: Path, *, keep_pycache: bool = False) -> bool:
    """要不要跳过这个文件。

    `keep_pycache=True` 只给**便携 Python 自己**用：留着它的 `__pycache__`
    首次启动快一点，也免得只读目录里写不出缓存。
    """
    for part in path.parts:
        if part not in EXCLUDE_NAMES:
            continue
        if part == "__pycache__" and keep_pycache:
            continue
        return True
    suffix = path.suffix.lower()
    if keep_pycache and suffix in {".pyc", ".pyo"}:
        return False
    return suffix in EXCLUDE_SUFFIXES


def copy_tree(source: Path, target: Path, *, label: str,
              log: list[tuple[str, int, int]], keep_pycache: bool = False) -> None:
    """把 source 拷到 target（跳过排除项），记一条清单。"""
    count = 0
    total = 0
    for item in source.rglob("*"):
        if not item.is_file() or should_skip(item.relative_to(source),
                                             keep_pycache=keep_pycache):
            continue
        destination = target / item.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        count += 1
        total += item.stat().st_size
    log.append((label, count, total))


def half_checkpoint(source: Path, target: Path) -> bool:
    """把 fp32 的 `model.pt` 转成 fp16 写到 `target`。返回是否命中了缓存。

    转一次要读 151MB、写 76MB（约 30s），而打包经常因为改一行文档就要重来。
    所以结果缓存在 `models/TIFA-1.0/.fp16-cache/`：**源文件没变就直接复制**，
    变了（mtime 更新）才重新转。
    """
    cache_dir = source.parent / ".fp16-cache"
    cached = cache_dir / "model.pt"
    if cached.is_file() and cached.stat().st_mtime >= source.stat().st_mtime:
        shutil.copy2(cached, target)
        return True

    import torch

    state = torch.load(source, map_location="cpu", weights_only=False)
    inner = state.get("state_dict", state) if isinstance(state, dict) else state
    if isinstance(inner, dict):
        inner = {key: (value.half() if torch.is_tensor(value) and value.is_floating_point()
                       else value) for key, value in inner.items()}
        if isinstance(state, dict) and "state_dict" in state:
            state["state_dict"] = inner
        else:
            state = inner
    cache_dir.mkdir(parents=True, exist_ok=True)
    torch.save(state, cached)
    shutil.copy2(cached, target)
    return False


def include_tifa(package: Path, log: list[tuple[str, int, int]]) -> None:
    """把 TIFA 权重按 fp16 打进包（模型 CC BY-NC-SA 4.0，和代码的许可分开）。

    fp32 151MB，fp16 76MB（实测边界差 ≤10ms）。缺权重时只记一条日志，不拦打包。
    """
    source = TIFA_SOURCE
    checkpoint = source / "model.pt"
    if not checkpoint.is_file():
        log.append(("models/TIFA-1.0（缺失，跳过）", 0, 0))
        return
    target = package / "models" / "TIFA-1.0"
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        if not item.is_file() or item.name == "model.pt":
            continue
        if ".fp16-cache" in item.parts:
            continue
        destination = target / item.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
    cached = half_checkpoint(checkpoint, target / "model.pt")
    (target / "LICENSE-model.txt").write_text(
        "TIFA-1.0 权重\n"
        "==============\n"
        "来源：https://github.com/openvpi/TIFA （release v1.0.0）\n"
        "许可：CC BY-NC-SA 4.0（模型文件本身；代码是 MIT）\n"
        "改动：仅把 fp32 权重转成 fp16 以控制包体，未做训练/微调/蒸馏。\n"
        "用途：本项目的歌词强制对齐后端。非商用；再分发请保留本文件。\n",
        encoding="utf-8")
    files = sum(1 for item in target.rglob("*") if item.is_file())
    size = sum(item.stat().st_size for item in target.rglob("*") if item.is_file())
    log.append((f"models/TIFA-1.0（fp16 权重{'，缓存' if cached else '，已转换'}）",
                files, size))


def build(package: Path, *, python_runtime: Path, ffmpeg: Path) -> list[tuple[str, int, int]]:
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    log: list[tuple[str, int, int]] = []

    # 1) 便携 Python
    if not (python_runtime / "python.exe").is_file():
        raise SystemExit(f"没有便携 Python：{python_runtime}")
    copy_tree(python_runtime, package / "runtime" / "python",
              label="runtime/python（便携 CPython）", log=log, keep_pycache=True)

    # 2) ffmpeg（就放在 bootstrap.find_ffmpeg() 的第一优先候选里）
    if not ffmpeg.is_file():
        raise SystemExit(f"没有 ffmpeg：{ffmpeg}")
    ffmpeg_dir = package / "runtime" / "ffmpeg" / "bin"
    ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ffmpeg, ffmpeg_dir / "ffmpeg.exe")
    (ffmpeg_dir.parent / "LICENSE-ffmpeg.txt").write_text(FFMPEG_LICENSE, encoding="utf-8")
    log.append(("runtime/ffmpeg（裁剪版静态 ffmpeg）", 2, ffmpeg.stat().st_size))

    # 3) 代码与文档
    for name in COPY_DIRS:
        source = ROOT / name
        if source.is_dir():
            copy_tree(source, package / name, label=f"{name}/", log=log)
    for name in COPY_FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, package / name)
            log.append((name, 1, source.stat().st_size))

    # 4) TIFA 对齐权重（fp16）。放离线包里，别指望国内能下到 GitHub Release。
    include_tifa(package, log)


    # 6) 空目录占位，让内测者知道以后东西放哪
    for name in ("models", "output"):
        (package / name).mkdir(exist_ok=True)
        (package / name / "放这里.txt").write_text(
            "其它模型由内测包自己填（首次运行 / 跑歌之后）；TIFA 权重已随包放在 models/TIFA-1.0。\n",
            encoding="utf-8")
    return log


def find_seven_zip() -> Path | None:
    for candidate in SEVEN_ZIP_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("7z")
    return Path(found) if found else None


def seven_zip_package(package: Path, archive: Path, seven_zip: Path) -> int:
    """用 7-Zip 的 LZMA2 打一份 .7z（最高压缩）。返回字节数。"""
    if archive.exists():
        archive.unlink()
    result = subprocess.run([str(seven_zip), "a", "-t7z", "-mx=9", "-mmt=on",
                             str(archive), package.name],
                            cwd=str(package.parent), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"7-Zip 打包失败：{(result.stderr or result.stdout)[-300:]}")
    return archive.stat().st_size


def zip_package(package: Path, archive: Path) -> tuple[int, int]:
    if archive.exists():
        archive.unlink()
    files = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for item in sorted(package.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(package.parent))
                files += 1
    return files, archive.stat().st_size


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打 SheetSage2 内测包")
    parser.add_argument("--name", default="SheetSage2-Studio-alpha3", help="包名")
    parser.add_argument("--python-runtime", type=Path, default=DEFAULT_PYTHON_RUNTIME,
                        help="便携 CPython 目录（默认取 uv 装的那份）")
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG,
                        help="随包的 ffmpeg.exe（默认参考项目里那份裁剪版）")
    parser.add_argument("--out", type=Path, default=DIST, help="输出目录")
    parser.add_argument("--no-zip", action="store_true", help="只拼目录，不压缩")
    parser.add_argument("--with-zip", action="store_true",
                        help="除了 .7z 再多出一份 .zip（默认只出 .7z）")
    args = parser.parse_args(argv)

    package = args.out / args.name
    log = build(package, python_runtime=args.python_runtime, ffmpeg=args.ffmpeg)

    print(f"  目录：{package}")
    total_files = total_bytes = 0
    for label, count, size in log:
        total_files += count
        total_bytes += size
        print(f"    {label:<34} {count:>5} 个文件  {human(size):>10}")
    print(f"    {'合计':<34} {total_files:>5} 个文件  {human(total_bytes):>10}")

    if args.no_zip:
        return 0

    lines: list[str] = []
    seven_zip = find_seven_zip()
    zip_size: int | None = None
    if args.with_zip or seven_zip is None:
        if seven_zip is None:
            print("    （没找到 7-Zip，只能出 zip）")
        archive = args.out / f"{args.name}.zip"
        files, zip_size = zip_package(package, archive)
        digest = sha256_of(archive)
        lines.append(f"{digest}  {archive.name}\n")
        print(f"\n  {archive}")
        print(f"    {files} 个文件，{human(zip_size)}（sha256 {digest[:16]}…）")

    if seven_zip is not None:
        target = args.out / f"{args.name}.7z"
        size7 = seven_zip_package(package, target, seven_zip)
        digest7 = sha256_of(target)
        lines.append(f"{digest7}  {target.name}\n")
        extra = (f"（比 zip 小 {human(zip_size - size7)}，"
                 f"-{(zip_size - size7) / zip_size * 100:.1f}%）" if zip_size else "")
        print(f"\n  {target}")
        print(f"    {human(size7)}{extra}（sha256 {digest7[:16]}…）")

    print(f"    解压后 {human(total_bytes)}；内测者还要下 torch+模型（见 downloads.json）")
    (args.out / f"{args.name}.sha256").write_text("".join(lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
