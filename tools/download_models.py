"""下载 SheetSage2 + MERT-v2-FullSong 权重到 ``models/``。

为什么权重不直接进仓库
----------------------
1. **许可**：两个仓库的权重都是 **CC BY-NC 4.0（仅限非商业）**，与代码的 MIT
   不是一回事，混在一个仓库里容易让人误以为整个项目可以商用。
2. **体积**：SheetSage2 约 218 MB，MERT-v2-FullSong 约 2.4 GB，其中
   ``model.safetensors`` 单文件远超 GitHub 的 100 MB 限制。

所以仓库只带代码，权重由本脚本从 Hugging Face 现取。

用法
----
::

    # 1) 先登录（两个仓库都是 gated，需要在页面上点过同意）
    .venv\\Scripts\\python.exe -m pip install -U huggingface_hub
    .venv\\Scripts\\huggingface-cli.exe login

    # 2) 再下载
    .venv\\Scripts\\python.exe tools\\download_models.py

    # 只下某一个 / 换个目录
    .venv\\Scripts\\python.exe tools\\download_models.py --only mert
    .venv\\Scripts\\python.exe tools\\download_models.py --models-dir D:\\models

也可以用环境变量 ``HF_TOKEN`` 代替 ``huggingface-cli login``。

下载完成后目录结构应为::

    models/
        SheetSage2/          config.json  modeling_*.py  model.safetensors ...
        MERT-v2-FullSong/    ...

``app/config.py`` 会优先读 ``models/``，所以下完直接 ``start.bat`` 即可。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

#: repo id → models/ 下的子目录名
REPOS = {
    "sheetsage2": ("m-a-p/SheetSage2", "SheetSage2"),
    "mert": ("m-a-p/MERT-v2-FullSong", "MERT-v2-FullSong"),
}

#: 判定「这个仓库已经下好了」的标志文件
REQUIRED = {
    "sheetsage2": "model.safetensors",
    "mert": "model.safetensors",
}

ROOT = Path(__file__).resolve().parent.parent


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def already_there(target: Path, marker: str) -> bool:
    return (target / marker).is_file()


def download_one(key: str, models_dir: Path, token: str | None, force: bool) -> bool:
    from huggingface_hub import snapshot_download

    repo_id, subdir = REPOS[key]
    target = models_dir / subdir
    marker = REQUIRED[key]

    if not force and already_there(target, marker):
        print(f"[skip] {repo_id} 已存在（{_human(dir_size(target))}）-> {target}")
        return True

    print(f"[download] {repo_id}")
    print(f"           目标目录 {target}")
    print("           2.4 GB 级别的仓库，首次可能要十几分钟；断了重跑本脚本会续传")

    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(target),
            token=token,
            # 保留 .gitattributes / README 等元数据，方便核对版本
            ignore_patterns=["*.h5", "*.msgpack", "*.onnx"],
        )
    except Exception as exc:  # noqa: BLE001 - 要把 HF 的报错原样讲给用户
        text = str(exc)
        print(f"\n[FAILED] {repo_id}: {text}\n", file=sys.stderr)
        low = text.lower()
        if "401" in text or "403" in text or "gated" in low or "authenticated" in low:
            print(
                "看起来是权限问题。两个仓库都是 gated，需要：\n"
                "  1. 浏览器登录 Hugging Face，打开\n"
                "     https://huggingface.co/m-a-p/SheetSage2\n"
                "     https://huggingface.co/m-a-p/MERT-v2-FullSong\n"
                "     两个页面各点一次同意（Agree and access repository）；\n"
                "  2. 本机执行  huggingface-cli login  （或设 HF_TOKEN 环境变量）；\n"
                "  3. 重跑本脚本。",
                file=sys.stderr,
            )
        elif "connection" in low or "timeout" in low or "resolve" in low:
            print(
                "看起来是网络问题。国内可先设镜像：\n"
                "  set HF_ENDPOINT=https://hf-mirror.com\n"
                "再重跑本脚本。",
                file=sys.stderr,
            )
        return False

    size = dir_size(target)
    if not already_there(target, marker):
        print(f"[WARN] {repo_id} 下载结束但没找到 {marker}，请检查 {target}", file=sys.stderr)
        return False
    print(f"[ok] {repo_id} -> {target}  ({_human(size)})")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="下载 SheetSage2 / MERT-v2-FullSong 权重到 models/",
    )
    parser.add_argument(
        "--only",
        choices=sorted(REPOS),
        action="append",
        help="只下指定的仓库，可重复；默认两个都下",
    )
    parser.add_argument(
        "--models-dir",
        default=str(ROOT / "models"),
        help="权重落地目录（默认 <项目根>/models）",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HF_TOKEN") or None,
        help="Hugging Face token；默认读环境变量 HF_TOKEN，都没有则用本机已登录的凭据",
    )
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = parser.parse_args(argv)

    models_dir = Path(args.models_dir).expanduser().resolve()
    models_dir.mkdir(parents=True, exist_ok=True)

    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print(
            "缺少依赖 huggingface_hub。先装：\n"
            f'  "{sys.executable}" -m pip install -U huggingface_hub',
            file=sys.stderr,
        )
        return 2

    keys = args.only or list(REPOS)
    print(f"落地目录：{models_dir}")
    print(f"要下载的：{', '.join(REPOS[k][0] for k in keys)}\n")

    results = {k: download_one(k, models_dir, args.token, args.force) for k in keys}

    print("\n===== 结果 =====")
    for k, ok in results.items():
        print(f"  {'OK  ' if ok else 'FAIL'}  {REPOS[k][0]}")

    if all(results.values()):
        print("\n全部就绪。现在可以双击 start.bat 了。")
        return 0
    print("\n有仓库没下好，按上面的提示处理后重跑本脚本即可（已下好的会自动跳过）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
