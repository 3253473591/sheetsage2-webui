"""做一个「代码覆盖包」：只含版本间**会变的那几 MB**，解压覆盖到整合包目录即可升级。

为什么需要这个
--------------
完整包 6.03 GB，但它的构成是：

    runtime\\（便携 Python + site-packages + ffmpeg）  3543.9 MB   ← 版本间基本不变
    models\\（两个权重）                              2631.1 MB   ← 永不变
    app\\ + web\\（真正的代码）                            2.81 MB   ← **每次迭代就变这些**
    其它（licenses / tools / 根散文件）                  ~0.15 MB

也就是说 **99.95% 的体积和"这一版改了什么"无关**。每出一版就让人重下 6 GB 是纯浪费，
网盘上传也一样。所以：**完整包只发一次，之后的迭代发这个几 MB 的覆盖包。**

覆盖包里有什么（**白名单，不是黑名单**）
----------------------------------------
只放这些，别的**一律不碰**：

    app\\  web\\  tools\\  licenses\\
    使用说明.md  ASK_AI.md  许可与来源.md  版本信息.json

**绝不放**：``runtime\\`` / ``models\\``（体积大且与代码无关）、
``settings.json``（那是使用者自己的端口/设备设置，覆盖掉会把他的配置冲掉）、
``output\\`` / ``tmp\\``（使用者的数据）。

压缩包内的路径是**相对包根**的（不套一层目录），所以使用者把它解压到整合包目录
、选「全部覆盖」就完成升级。

用法::

    # 全量代码覆盖包（~1 MB）
    python packaging\\make_patch.py dist\\1.1

    # 只含"相对上一版真正变了"的文件（通常只有几百 KB）
    python packaging\\make_patch.py dist\\1.1 --diff-from dist\\1.0

    # 指定输出
    python packaging\\make_patch.py dist\\1.1 --out dist\\patches\\p1.zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, OSError):
    pass

#: 覆盖包里**允许**出现的顶层项。白名单而不是黑名单 ——
#: 黑名单一旦漏写一项，就可能把 runtime/ 或使用者的 settings.json 塞进去。
ALLOW_TOP = {
    "app", "web", "tools", "licenses",
    "使用说明.md", "ASK_AI.md", "许可与来源.md", "版本信息.json",
}

#: 明确点出来的"绝不打包"项，纯粹为了在日志里说清楚（白名单本身已经排除了它们）。
NEVER = ["runtime", "models", "output", "tmp", "settings.json",
         "start.bat", "install.bat", "selfcheck.bat"]

#: 与 make_zip.py 同一套：纯缓存，Python 会自己重建
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}


def keep(rel: Path) -> bool:
    if not rel.parts or rel.parts[0] not in ALLOW_TOP:
        return False
    if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
        return False
    return rel.suffix.lower() not in EXCLUDE_SUFFIXES


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


README_NAME = "升级说明.txt"
README_TEXT = """这是「SheetSage2 扒谱」的代码覆盖包（不是完整包）。

怎么用
------
1. 把它解压到你的整合包目录（就是有 start.bat 的那一层）；
2. 提示"是否替换"时选**全部替换 / 覆盖**；
3. 重启 start.bat 即可。你的设置、已扒过的产物、模型、运行时都不会被动。

里面有什么
----------
只含会随版本变化的代码与文档：app\\  web\\  tools\\  licenses\\ 以及几个说明文件。

为什么这么小
------------
完整包 6 GB 里，runtime\\（便携 Python）与 models\\（权重）占 6.17 GB，
它们和"这一版改了什么"无关。所以完整包只需要下**一次**，
之后每版用这个几 MB 的覆盖包升级就行。

注意
----
* 覆盖包**不含** settings.json —— 你的端口/设备设置不会被冲掉；
* 覆盖包**不含** output\\ 与 tmp\\ —— 你的产物和上传文件都保留；
* 如果你还没装过完整包，这个包**不够用**，请先下完整包。
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="做代码覆盖包（只含版本间会变的几 MB）")
    ap.add_argument("pkg_dir", help="要打包的整合包目录，如 dist\\1.1")
    ap.add_argument("--diff-from", default=None,
                    help="上一版整合包目录；给了就只打「相对它变了」的文件")
    ap.add_argument("--out", default=None, help="输出 zip 路径")
    ap.add_argument("--version", default=None, help="覆盖用户名，默认读包里的 版本信息.json")
    args = ap.parse_args()

    pkg = Path(args.pkg_dir).resolve()
    if not (pkg / "app" / "server.py").is_file():
        print(f"[×] 不像一个整合包目录：{pkg}")
        return 1

    version = args.version
    if not version:
        mf = pkg / "版本信息.json"
        if mf.is_file():
            try:
                # 必须用 utf-8-sig：这个文件是 PowerShell 的 `-Encoding utf8` 写的，
                # **带 BOM**；用 utf-8 读会留一个 \ufeff 在开头，json.loads 直接报错。
                version = json.loads(mf.read_text(encoding="utf-8-sig")).get("version")
            except ValueError:
                version = None
    version = version or "unknown"

    all_files = sorted(p for p in pkg.rglob("*") if p.is_file() and keep(p.relative_to(pkg)))
    if not all_files:
        print("[×] 没有任何文件匹配白名单，检查 ALLOW_TOP")
        return 1

    base = Path(args.diff_from).resolve() if args.diff_from else None
    changed, same = [], 0
    if base:
        if not base.is_dir():
            print(f"[×] --diff-from 目录不存在：{base}")
            return 1
        base_hashes = {}
        for p in base.rglob("*"):
            rel = p.relative_to(base)
            if p.is_file() and keep(rel):
                base_hashes[rel.as_posix()] = sha256_file(p)
        for p in all_files:
            rel = p.relative_to(pkg).as_posix()
            if base_hashes.get(rel) != sha256_file(p):
                changed.append(p)
            else:
                same += 1
        files = changed
        print(f"与上一版对比：{len(files)} 个文件有变动，{same} 个相同（未变的不打进去）")
    else:
        files = all_files

    if not files:
        print("\n[✓] 两点之间没有任何代码变化 —— 不需要发覆盖包。")
        return 0

    out = Path(args.out) if args.out else pkg.parent / "patches" / f"SheetSage2-{version}-code-update.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    total = sum(p.stat().st_size for p in files)
    print(f"\n覆盖包：{len(files)} 个文件 / {total / 1024**2:.2f} MB（未压缩）")
    print("  含：" + "  ".join(sorted({p.relative_to(pkg).parts[0] for p in files})))
    print("  不含：" + "  ".join(NEVER))

    started = time.time()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        # 路径相对包根，不套目录：使用者在整合包目录里解压即可覆盖
        for p in files:
            zf.write(p, p.relative_to(pkg).as_posix())
        zf.writestr(README_NAME, README_TEXT)

    size = out.stat().st_size
    digest = sha256_file(out)
    print(f"\n完成：{out}")
    print(f"      {size / 1024:.0f} KB（压缩后）  用时 {time.time() - started:.1f}s")
    print(f"      含 {README_NAME}（把用法写在包里，免得收到的人不知道往哪解压）")
    print(f"      sha256 {digest}")
    side = out.with_name(out.name + ".sha256.txt")
    side.write_text(f"{digest}  {out.name}\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
