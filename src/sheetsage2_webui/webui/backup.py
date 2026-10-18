"""数据备份 / 迁移（G22）：换电脑、重装系统、把工程交给别人——不用懂 `output/` 在哪。

**为什么需要它**：数据目录可以用 `SHEETSAGE_WEBUI_DATA` 改，但小白根本不会用；换台电脑
就是"把整个 `output/` 目录拷过去"，而 `job.json` 里存的是**绝对路径**（虽然
`_job_from_meta` 会按布局重算，但没人指望用户知道这件事）。这里给两个按钮：

| 按钮 | 做什么 | 产物 |
|---|---|---|
| 「备份」 | 把所有工程打成一个 zip | `sheetsage2-backup-<时间>.zip`（含 `backup.json` 清单） |
| 「导入备份」 | 把 zip 里的工程收进来 | 每个工程落成 `output/<歌名>/`，重名自动 `-2` |

**包里放什么**：产物 + `job.json` + `.webui/input` 里的原音频 + `manifest.json`。
**不放什么**：`bundle.zip` / `peaks-*.json` / `export/*.svp` / `prev/` —— 这些全都能从产物
重算，带上只是让包变大一倍（`include_cache=True` 才带，留给"要一模一样复原"的场合）。

**安全**：导入是不可信的输入（用户可能手滑传了个别的东西），所以

1. 拒绝绝对路径、`..`、盘符、符号链接条目（zip-slip）；
2. 条目数与解压总量都有上限（zip 炸弹）；
3. 必须有 `backup.json` 且版本认得出——不认识的包直接说"这不是本程序的备份包"；
4. 落到 `output/` 时**绝不覆盖已有工程**（`claim_project_dir` 的 `-2` 兜着）。
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

#: 包里的清单文件名（放在 zip 根）。
MANIFEST_NAME = "backup.json"
#: 清单版本：以后加字段时靠它判断"这个包我还认得"。
BACKUP_VERSION = 1
#: 工程在包里的前缀。
PROJECTS_PREFIX = "projects/"

#: 导入时的硬上限（防 zip 炸弹 / 手滑传错文件）。
MAX_ENTRIES = 20000
MAX_UNCOMPRESSED_BYTES = 16 * 1024 ** 3

#: 导出时跳过的缓存（都能重算）。相对工程目录。
CACHE_FILES = ("bundle.zip", "bundle.meta.json")
CACHE_GLOBS = ("peaks-*.json",)

#: `.webui/` 里**要带**的东西：原音频与干声（不带就没法重跑）、以及产物清单。
KEEP_INTERNAL = ("input", "stems")
KEEP_INTERNAL_FILES = ("manifest.json",)


class BackupRejected(ValueError):
    """这个包不能导入（不是本程序的备份 / 结构坏了 / 太大）。API 翻成 400 + 人话。"""


# --------------------------------------------------------------------- 导出
def project_entries(root: Path, *, include_audio: bool = True,
                    include_cache: bool = False) -> list[tuple[Path, str]]:
    """一个工程目录里**值得进备份包**的文件：`[(绝对路径, 包内相对路径)]`。"""
    entries: list[tuple[Path, str]] = []
    if not root.is_dir():
        return entries
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if not _keep(relative, include_audio=include_audio, include_cache=include_cache):
            continue
        entries.append((path, relative))
    return entries


def _keep(relative: str, *, include_audio: bool, include_cache: bool) -> bool:
    parts = PurePosixPath(relative).parts
    if not parts:
        return False
    name = parts[-1]
    # 原音频 / 干声是不是要带，先按 include_audio 定（和"带不带缓存"是两件事）
    if parts[0] == ".webui" and len(parts) >= 3 and parts[1] in ("input", "stems"):
        return include_audio
    if include_cache:
        return True
    if name in CACHE_FILES:
        return False
    if any(PurePosixPath(name).match(pattern) for pattern in CACHE_GLOBS):
        return False
    if parts[0] != ".webui":
        return True
    # `.webui/...`：默认只带产物清单，其余（缓存 / prev / export）都是能重算的
    if len(parts) == 2 and parts[1] in KEEP_INTERNAL_FILES:
        return True
    return False


def build_manifest(projects: Sequence[Mapping[str, Any]], *,
                   data_root: str | Path, include_audio: bool,
                   include_cache: bool) -> dict[str, Any]:
    """备份包的清单：谁、什么时候、装了什么。导入时用它认包 + 给用户看摘要。"""
    return {
        "kind": "sheetsage2-studio-backup",
        "version": BACKUP_VERSION,
        "created_at": time.time(),
        "app_version": _app_version(),
        "data_root": str(data_root),
        "include_audio": bool(include_audio),
        "include_cache": bool(include_cache),
        "projects": [dict(item) for item in projects],
    }


def _app_version() -> str:
    try:
        from .. import __version__

        return str(__version__)
    except Exception:                                # noqa: BLE001 - 版本号拿不到不影响备份
        return "unknown"


def write_backup(target: Path, projects: Iterable[tuple[str, Path]], manifest: Mapping[str, Any],
                 *, include_audio: bool = True, include_cache: bool = False) -> dict[str, Any]:
    """写备份 zip。`projects` 是 `[(包内目录名, 工程目录)]`。

    先写 `.part` 再改名：写一半被杀掉时不会留下一个**看起来能用**的坏包。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    files = 0
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(dict(manifest), ensure_ascii=False, indent=2))
            for folder, root in projects:
                base = f"{PROJECTS_PREFIX}{folder}/"
                for path, relative in project_entries(root, include_audio=include_audio,
                                                     include_cache=include_cache):
                    zf.write(path, arcname=base + relative)
                    files += 1
        temp.replace(target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return {"file": str(target), "files": files, "bytes": target.stat().st_size,
            "created_at": manifest.get("created_at"), "projects": len(manifest.get("projects") or [])}


# --------------------------------------------------------------------- 导入
def _safe_entry(name: str) -> bool:
    """这条 zip 条目能不能落盘（zip-slip / 绝对路径 / 盘符 / `..` 全部拒绝）。"""
    text = str(name).replace("\\", "/")
    if not text or text.startswith("/") or text.startswith("../"):
        return False
    if len(text) > 1 and text[1] == ":":             # `C:/...`
        return False
    parts = PurePosixPath(text).parts
    if not parts or any(part in ("..", "") for part in parts):
        return False
    return text == MANIFEST_NAME or text.startswith(PROJECTS_PREFIX)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """zip 里能塞符号链接（unix mode 里有 S_IFLNK）：解压成一个指到哪都不知道的链接。"""
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000


def read_manifest(archive: Path) -> dict[str, Any]:
    """只读清单（不落地任何文件）：先认包再决定要不要解压。"""
    try:
        with zipfile.ZipFile(archive) as zf:
            raw = zf.read(MANIFEST_NAME)
    except KeyError as exc:
        raise BackupRejected("这个压缩包里没有备份清单（backup.json）："
                             "它可能不是本程序导出的备份") from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupRejected(f"这个文件不是有效的备份包（{type(exc).__name__}）") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupRejected("备份清单读不出来（可能已经损坏）") from exc
    if not isinstance(manifest, dict) or manifest.get("kind") != "sheetsage2-studio-backup":
        raise BackupRejected("这个包不是 SheetSage2 Studio 的备份")
    try:
        version = int(manifest.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version <= 0 or version > BACKUP_VERSION:
        raise BackupRejected(
            f"备份包版本是 {manifest.get('version')!r}，这个版本的程序认不出来；"
            f"请用导出它的那个版本恢复")
    return manifest


def extract_backup(archive: Path, staging: Path) -> tuple[dict[str, Any], list[Path]]:
    """把备份包解到 `staging`，返回 `(清单, 工程目录列表)`。

    条目逐个校验后再写盘：宁可整包拒绝，也不要往数据目录里塞进来路不明的路径。
    """
    manifest = read_manifest(archive)
    staging.mkdir(parents=True, exist_ok=True)
    total = 0
    count = 0
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                count += 1
                if count > MAX_ENTRIES:
                    raise BackupRejected(f"备份包里的文件太多（超过 {MAX_ENTRIES} 个）")
                if _is_symlink(info):
                    raise BackupRejected(f"备份包里有一条符号链接（{info.filename}），拒绝导入")
                if not _safe_entry(info.filename):
                    raise BackupRejected(f"备份包里有一条可疑路径（{info.filename}），拒绝导入")
                total += max(0, info.file_size)
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise BackupRejected("备份包解压后太大（超过 "
                                         f"{MAX_UNCOMPRESSED_BYTES // 1024 ** 3} GB）")
                if info.filename == MANIFEST_NAME:
                    continue
                target = staging / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, target.open("wb") as sink:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        sink.write(block)
    except zipfile.BadZipFile as exc:
        raise BackupRejected("这个压缩包已经损坏，读不出来") from exc
    projects_root = staging / PROJECTS_PREFIX.rstrip("/")
    projects = sorted([item for item in projects_root.iterdir() if item.is_dir()]) \
        if projects_root.is_dir() else []
    return manifest, projects
