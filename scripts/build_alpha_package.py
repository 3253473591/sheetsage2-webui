r"""打内测包：把「解压就能跑」需要的东西拼成一个目录，再压成 zip。

放进包里的（都是实测过体积的）：

| 内容 | 说明 |
|---|---|
| `runtime/python/` | 便携 CPython 3.12（61 MB，自带 pip/venv，**同好机器不需要装 Python**） |
| `runtime/ffmpeg/bin/ffmpeg.exe` | 裁剪版静态 ffmpeg 7.1.1（2.2 MB，无 DLL）—— `bootstrap.find_ffmpeg()` 第一优先 |
| `src/` `scripts/` `tools/` `web/` | 应用代码 + 前端源码（前端还有已构建的 static，所以同好不需要 Node） |
| `downloads.json` | torch / 模型的直链与 sha256（`tools/bootstrap.py` 照着下） |
| `webui.bat` / `setup.bat` | 双击入口（首次运行自动引导） |
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

#: 目录整体带走。
COPY_DIRS = ("src", "scripts", "tools", "web", "docs", "tests")
#: 单个文件带走。
COPY_FILES = ("pyproject.toml", "downloads.json", "NOTICE", "README.md",
              "README-internal-test.md", "webui.bat", "setup.bat", ".gitignore")
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

    # 4) 内测说明放在最显眼的位置
    readme = ROOT / "README-internal-test.md"
    if readme.is_file():
        shutil.copy2(readme, package / "内测说明.md")
        log.append(("内测说明.md", 1, readme.stat().st_size))

    # 5) 空目录占位，让内测者知道以后东西放哪
    for name in ("models", "output"):
        (package / name).mkdir(exist_ok=True)
        (package / name / "放这里.txt").write_text(
            "这个目录会由内测包自己填（首次运行 / 跑歌之后）。\n", encoding="utf-8")
    return log


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
    parser.add_argument("--name", default="SheetSage2-Studio-alpha2", help="包名")
    parser.add_argument("--python-runtime", type=Path, default=DEFAULT_PYTHON_RUNTIME,
                        help="便携 CPython 目录（默认取 uv 装的那份）")
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG,
                        help="随包的 ffmpeg.exe（默认参考项目里那份裁剪版）")
    parser.add_argument("--out", type=Path, default=DIST, help="输出目录")
    parser.add_argument("--no-zip", action="store_true", help="只拼目录，不压缩")
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
    archive = args.out / f"{args.name}.zip"
    files, size = zip_package(package, archive)
    digest = sha256_of(archive)
    print(f"\n  {archive}")
    print(f"    {files} 个文件，{human(size)}（sha256 {digest[:16]}…）")
    print(f"    解压后 {human(total_bytes)}；内测者还要下 torch+模型（见 downloads.json）")
    (args.out / f"{args.name}.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
