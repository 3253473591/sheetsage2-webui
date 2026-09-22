"""把整合包目录打成 zip —— 用 Python 的 zipfile，而不是系统 tar.exe。

为什么要自己打
--------------
`tar.exe`（bsdtar）写 zip 时**不置 UTF-8 文件名标志位**（general purpose bit 11），
中文文件名按 Windows ANSI 代码页写进去。实测：

    zipfile 读回来的名字 = '╩╣╙├╦╡├≈.md'   ← 乱码
    flag_bits = 0x0008                        ← 没有 0x0800

后果是不同解压工具表现不一致（资源管理器通常没事，但 .NET 的 Expand-Archive /
某些 7-Zip 版本会变乱码）。Python 的 zipfile 在文件名非 ASCII 时会自动置
``0x800``，所以改用它，跨工具都稳。

用法::

    python packaging\\make_zip.py <stage_dir> <pkg_name> [--out 输出.zip]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, OSError):
    pass

CHUNK = 1 << 20

#: 这些目录名出现在**任意层级**都排除（纯缓存，Python 会自己重建）
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
#: 这些只在**包根目录**下才排除。早期版本对任意层级生效，结果把
#: `runtime/python/Lib/.../tmp/` 这类同名目录也误排除了 —— 虽然那次命中的
#: 全是 `__pycache__` 里的 .pyc（无害），但这种「按名字匹配」的规则太容易误伤。
TOP_LEVEL_RUNTIME_DIRS = {"output", "tmp"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}
EXCLUDE_NAMES = {"环境快照.txt"}


def is_runtime_junk(rel: Path) -> bool:
    """`rel` 是相对于包根目录的路径。"""
    parts = rel.parts
    if parts and parts[0] in TOP_LEVEL_RUNTIME_DIRS:
        return True
    if any(part in EXCLUDE_DIRS for part in parts[:-1]):
        return True
    if rel.name in EXCLUDE_NAMES:
        return True
    return rel.suffix.lower() in EXCLUDE_SUFFIXES


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="用 zipfile 打包整合包（UTF-8 文件名）")
    ap.add_argument("stage_dir", help="包含包目录的父目录")
    ap.add_argument("pkg_name", help="包目录名（会成为 zip 里的顶层目录）")
    ap.add_argument("--out", default=None, help="输出 zip 路径；默认放在 stage_dir 下")
    ap.add_argument("--level", type=int, default=6, help="deflate 级别 0-9（0=不压缩）")
    ap.add_argument("--keep-runtime", action="store_true",
                    help="连 output/ tmp/ *.log 一起打进去（默认排除，别用，除非你确定）")
    args = ap.parse_args()

    stage = Path(args.stage_dir).resolve()
    src = stage / args.pkg_name
    if not src.is_dir():
        print(f"[×] 包目录不存在：{src}")
        return 1
    out = Path(args.out) if args.out else stage / f"{args.pkg_name}.zip"
    if out.exists():
        out.unlink()

    all_files = sorted(p for p in src.rglob("*") if p.is_file())
    skip_set = (
        {p for p in all_files if is_runtime_junk(p.relative_to(src))}
        if not args.keep_runtime else set()
    )
    files = [p for p in all_files if p not in skip_set]
    total_bytes = sum(p.stat().st_size for p in files)
    print(f"打包 {src}")
    print(f"      {len(files)} 个文件 / {total_bytes / 1024**3:.2f} GB -> {out.name}")
    print(f"      deflate level {args.level}")
    if skip_set:
        skipped_bytes = sum(p.stat().st_size for p in skip_set)
        print(f"      已排除 {len(skip_set)} 个运行期产物（{skipped_bytes / 1024**2:.0f} MB）："
              f"output/ tmp/ *.log 等")

    compression = zipfile.ZIP_DEFLATED if args.level > 0 else zipfile.ZIP_STORED
    started = time.time()
    done_bytes = 0
    next_report = 0.0

    with zipfile.ZipFile(out, "w", compression, allowZip64=True) as zf:
        for i, path in enumerate(files, 1):
            arcname = f"{args.pkg_name}/{path.relative_to(src).as_posix()}"
            zf.write(path, arcname)
            done_bytes += path.stat().st_size
            now = time.time()
            if now >= next_report or i == len(files):
                next_report = now + 5.0
                pct = done_bytes / total_bytes * 100 if total_bytes else 100.0
                speed = done_bytes / max(now - started, 1e-6) / 1024**2
                print(f"      [{pct:5.1f}%] {done_bytes / 1024**3:.2f}/{total_bytes / 1024**3:.2f} GB"
                      f"  {speed:.0f} MB/s  ({i}/{len(files)} 文件)", flush=True)

    size = out.stat().st_size
    elapsed = time.time() - started
    print(f"\n完成：{out}")
    print(f"      {size / 1024**3:.2f} GB（压缩后）  用时 {elapsed / 60:.1f} 分钟")
    digest = sha256_file(out)
    print(f"      sha256 {digest}")
    side = out.with_name(out.name + ".sha256.txt")
    side.write_text(f"{digest}  {out.name}\n", encoding="ascii")
    print(f"      校验值写入 {side.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
