r"""内测包自检：把「每出一版都要跑一遍」的那套检查固化成脚本。

检查项（都是前两版手工跑过的）：

1. **结构**：关键文件齐不齐、zip 与目录文件数一致、`zip.testzip()` 通过、
   zip 里的路径都带包名前缀（解压不会散一地）；
2. **中文编码**：包内所有文本文件都能按 UTF-8 读出来、没有 mojibake
   （历史上 Windows 控制台按 GBK 写文件踩过这个坑）；
3. **版本串一致**：`downloads.json` / `tools/downloader.py` / `scripts/build_alpha_package.py` /
   `tests/test_packaging.py` / `README-internal-test.md` 五处说的是同一个版本；
4. **本次改动确实进包**：`octave.py`、`svp.py` 的延音规则等；
5. **随包的 ffmpeg**：跑得起来、该有的解码器都在（flac/mp3/aac/vorbis/opus/alac/wmav2）；
6. **便携 Python**：跑得起来、版本对；
7. **`tools/bootstrap.py --dry-run`**：报版本、认出随包 ffmpeg、ModelScope 可达（要联网）。

    .\.venv\Scripts\python.exe scripts\verify_package.py                    # 默认查 dist 里最新那个
    .\.venv\Scripts\python.exe scripts\verify_package.py dist\xxx-alpha3    # 指定目录
    .\.venv\Scripts\python.exe scripts\verify_package.py dist\xxx-alpha3.zip  # 指定 zip（跳过 4/5/6）
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

#: 包里必须有的东西（相对包根）。
REQUIRED = (
    "webui.bat", "setup.bat", "downloads.json", "NOTICE", "pyproject.toml",
    "README-internal-test.md", "内测说明.md",
    "runtime/python/python.exe",
    "runtime/ffmpeg/bin/ffmpeg.exe",
    "runtime/ffmpeg/LICENSE-ffmpeg.txt",
    "src/sheetsage2_webui/__init__.py",
    "src/sheetsage2_webui/pipeline.py",
    "src/sheetsage2_webui/octave.py",
    "src/sheetsage2_webui/svp.py",
    "src/sheetsage2_webui/webui/app.py",
    "src/sheetsage2_webui/webui/static/index.html",
    "tools/bootstrap.py",
    "tools/downloader.py",
    "scripts/build_alpha_package.py",
    "tests/test_packaging.py",
    "models/放这里.txt",
    "output/放这里.txt",
)
#: 文本文件的扩展名（做编码检查用）。
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".bat", ".toml", ".js", ".mjs",
                 ".css", ".html", ".vue", ".cfg", ".ini", ".yml", ".yaml",
                 ".ts", ".map", ".gitignore", ".gitattributes"}
#: 典型的 mojibake 痕迹（UTF-8 字节被当成 GBK/Latin-1 读出来）。
MOJIBAKE = ("锟斤拷", "鐜嬫湞", "鏂囦欢", "ä¸", "Ã¤", "â€", "ï¿½", "å¤", "æ–‡")
#: 随包 ffmpeg 必须带的解码器。
DECODERS = ("flac", "mp3", "aac", "vorbis", "opus", "alac", "wmav2", "pcm_f32le")

ok_count = 0
bad: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    global ok_count
    if condition:
        ok_count += 1
        print(f"  [OK] {label}" + (f"  {detail}" if detail else ""))
    else:
        bad.append(label)
        print(f"  [XX] {label}" + (f"  {detail}" if detail else ""))
    return condition


def run_capture(cmd: list[str], *, timeout: int = 120,
                cwd: Path | None = None) -> tuple[int, str]:
    """跑一个子进程并把输出收回来。

    **不用管道**：沙箱/受控环境下 piped stdio 会被拒（EPERM），所以走临时文件。
    `cwd` 一定要给成**包目录**：`tools/bootstrap.py` 是按自己的 `__file__` 找包根的，
    从项目根跑它会去找项目的 `runtime/`（那里没有随包 ffmpeg）。
    """
    with tempfile.TemporaryFile() as sink:
        try:
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"     # 别在包里留 __pycache__
            proc = subprocess.run(cmd, stdout=sink, stderr=subprocess.STDOUT,
                                  timeout=timeout, cwd=str(cwd) if cwd else None,
                                  env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, f"{type(exc).__name__}: {exc}"
        sink.seek(0)
        return proc.returncode, sink.read().decode("utf-8", "replace")


def find_package(target: Path | None) -> Path:
    if target is not None:
        return target
    candidates = [p for p in DIST.glob("SheetSage2-Studio-alpha*") if p.is_dir()]
    if not candidates:
        raise SystemExit(f"{DIST} 里没有包目录；先跑 scripts/build_alpha_package.py")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def file_count(package: Path) -> int:
    """包里**该有的**文件数（不含目录、也不含"跑起来才会生成"的东西）。"""
    return sum(1 for item in package.rglob("*")
               if item.is_file() and not is_runtime_artifact(item, package))


def is_runtime_artifact(path: Path, package: Path) -> bool:
    """跑起来才会出现的东西：`__pycache__` / `wheels/` / `.venv/` / 模型 / 产物。

    两个坑：
    - 它们**不该进 zip**，但本机跑过自检或真跑过一次之后会出现在目录里，
      所以比"目录 vs zip"时要把它们排除掉（zip 里有没有它们是另一条断言）；
    - **`runtime/python/` 下的 `__pycache__` 是故意随包的**（首次启动快一点、
      只读目录里写不出缓存），不能当污染排除掉——那是 766 个文件。
    """
    relative = path.relative_to(package).parts
    if is_runtime_entry(relative):
        return True
    return False


def is_runtime_entry(relative: tuple[str, ...] | list[str]) -> bool:
    """按**包内相对路径**判断是不是运行时产物（zip 那边的同名判断共用这一份）。"""
    relative = list(relative)
    shipped_pycache = relative[:2] == ["runtime", "python"]   # 便携 Python 自带的缓存
    if "__pycache__" in relative or Path(relative[-1]).suffix.lower() in {".pyc", ".pyo"}:
        return not shipped_pycache
    if set(relative) & {"wheels", ".venv", ".pytest_cache"}:
        return True
    if relative[-1] == "bootstrap-state.json":
        return True
    if len(relative) >= 2 and relative[-2] in ("models", "output") \
            and relative[-1] != "放这里.txt":
        return True
    return False


def check_version_strings(package: Path, version: str) -> None:
    print("\n[3] 版本串一致")
    manifest = (package / "downloads.json").read_text(encoding="utf-8")
    check("downloads.json version", f'"version": "{version}"' in manifest, version)
    downloader = (package / "tools/downloader.py").read_text(encoding="utf-8")
    check("downloader USER_AGENT",
          f'USER_AGENT = "sheetsage2-webui-{version}"' in downloader)
    builder = (package / "scripts/build_alpha_package.py").read_text(encoding="utf-8")
    check("build_alpha_package --name 默认值",
          f'default="SheetSage2-Studio-{version}"' in builder)
    tests = (package / "tests/test_packaging.py").read_text(encoding="utf-8")
    check("test_packaging 清单断言", f'manifest["version"] == "{version}"' in tests)
    readme = (package / "README-internal-test.md").read_text(encoding="utf-8")
    alpha = version.replace("alpha", "alpha ")
    check("内测说明标题", readme.splitlines()[0].startswith(f"# SheetSage2 Studio · {alpha}"),
          readme.splitlines()[0])


def check_this_rounds_changes(package: Path) -> None:
    print("\n[4] 本次改动在包里")
    octave = package / "src/sheetsage2_webui/octave.py"
    check("octave.py 在", octave.is_file())
    if octave.is_file():
        text = octave.read_text(encoding="utf-8")
        check("octave.py 是八度一致性校正",
              "重复段" in text and "CONTINUATION" not in text)
    svp = (package / "src/sheetsage2_webui/svp.py").read_text(encoding="utf-8")
    check("svp.py 有延音规则（CONTINUATION_MAX_GAP）",
          "CONTINUATION_MAX_GAP" in svp and "占位音节自己也算锚点" in svp)
    check("svp.py EXPORT_REVISION 已升到 5", "EXPORT_REVISION = 5" in svp)
    pitch = (package / "src/sheetsage2_webui/pitch.py").read_text(encoding="utf-8")
    check("pitch.py 接了八度校正", "_octave_plan_from_labs" in pitch)
    pipeline = (package / "src/sheetsage2_webui/pipeline.py").read_text(encoding="utf-8")
    check("pipeline.py 有 --no-octave-fix", "no-octave-fix" in pipeline)


def check_ffmpeg(package: Path) -> None:
    print("\n[5] 随包 ffmpeg")
    exe = package / "runtime/ffmpeg/bin/ffmpeg.exe"
    code, out = run_capture([str(exe), "-version"])
    check("ffmpeg -version 能跑", code == 0)
    first = out.splitlines()[0] if out else ""
    check("版本号 7.1.1", "7.1.1" in first, first)
    code, out = run_capture([str(exe), "-decoders"])
    if check("ffmpeg -decoders 能跑", code == 0):
        missing = [name for name in DECODERS if not re.search(rf"\b{re.escape(name)}\b", out)]
        check("解码器齐全", not missing, f"缺 {missing}" if missing else "/".join(DECODERS))


def check_python(package: Path) -> None:
    print("\n[6] 便携 Python")
    exe = package / "runtime/python/python.exe"
    code, out = run_capture([str(exe), "--version"])
    check("python --version 能跑", code == 0, out.strip())
    check("版本 3.12", "3.12" in out, out.strip())


def check_bootstrap(package: Path) -> None:
    print("\n[7] tools/bootstrap.py --dry-run")
    code, out = run_capture(
        [str(package / "runtime/python/python.exe"), "tools/bootstrap.py", "--dry-run"],
        timeout=180, cwd=package)
    check("bootstrap --dry-run 能跑", code == 0, f"exit {code}")
    check("报出了版本号", "alpha" in out, next((l for l in out.splitlines()
                                                if "alpha" in l), "").strip())
    line = next((l for l in out.splitlines() if "随包 ffmpeg" in l), "")
    check("认出随包 ffmpeg", "随包 ffmpeg runtime" in line and "[!]" not in line,
          line.strip())
    if code != 0:
        print("    ---- bootstrap 输出 ----")
        for line in out.splitlines()[-15:]:
            print("    " + line)


def check_text_encoding(package: Path) -> None:
    print("\n[2] 中文编码")
    non_utf8: list[str] = []
    mojibake: list[str] = []
    scanned = 0
    for item in package.rglob("*"):
        if not item.is_file():
            continue
        if item.name == Path(__file__).name:
            continue        # 这个文件里就写着下面那些 mojibake 标记本身
        suffix = item.suffix.lower()
        if suffix not in TEXT_SUFFIXES and item.name not in (".gitignore", ".gitattributes"):
            continue
        scanned += 1
        try:
            text = item.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            non_utf8.append(str(item.relative_to(package)))
            continue
        for marker in MOJIBAKE:
            if marker in text:
                mojibake.append(f"{item.relative_to(package)} ~ {marker}")
                break
    check("文本文件都是 UTF-8", not non_utf8,
          f"{scanned} 个文本；坏 {non_utf8[:5]}" if non_utf8 else f"{scanned} 个文本")
    check("0 处 mojibake", not mojibake, str(mojibake[:5]) if mojibake else "")


def check_structure(package: Path, archive: Path | None) -> None:
    print("\n[1] 结构")
    missing = [name for name in REQUIRED if not (package / name).is_file()]
    check("关键文件齐全", not missing, f"缺 {missing}" if missing else f"{len(REQUIRED)} 个")
    if archive is None or not archive.is_file():
        print("  -- 没有 zip，跳过 zip 检查")
        return
    with zipfile.ZipFile(archive) as zf:
        check("zip testzip() 通过", zf.testzip() is None)
        names = [n for n in zf.namelist() if not n.endswith("/")]
        prefix = package.name + "/"
        check("zip 里都带包名前缀", all(n.startswith(prefix) for n in names),
              f"不带的 {[n for n in names if not n.startswith(prefix)][:3]}")
        check("zip 与目录（去掉运行时产物）文件数一致",
              len(names) == file_count(package),
              f"zip {len(names)} / 目录 {file_count(package)}")
        dirty = [n for n in names if is_runtime_entry(n.split("/")[1:])]
        check("zip 里没有运行时产物", not dirty, str(dirty[:3]) if dirty else "")

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="内测包自检")
    parser.add_argument("target", nargs="?", type=Path,
                        help="包目录或 zip（默认 dist 里最新的那个目录）")
    args = parser.parse_args(argv)

    target = find_package(args.target)
    is_zip = target.is_file() and target.suffix == ".zip"
    package = target if target.is_dir() else target.with_suffix("")
    archive = target if is_zip else target.with_suffix(".zip")
    if not package.is_dir():
        raise SystemExit(f"找不到包目录：{package}")

    match = re.search(r"(alpha\d+)", package.name)
    version = match.group(1) if match else "?"
    print(f"自检对象：{package}")
    print(f"版本串  ：{version}")

    check_structure(package, archive)
    check_text_encoding(package)
    check_version_strings(package, version)
    check_this_rounds_changes(package)
    if is_zip:
        print("\n[5/6/7] 指定的是 zip，跳过 ffmpeg / Python / bootstrap 实跑")
    else:
        check_ffmpeg(package)
        check_python(package)
        check_bootstrap(package)

    print(f"\n合计：{ok_count} 项通过，{len(bad)} 项失败")
    for label in bad:
        print(f"  失败：{label}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
