"""批量下载：把多个工程打成一个 zip（每首一个 `.svp`，或每首一个产物目录）。

**为什么不复用单个下载接口**：40 首点 40 次、落 40 个浏览器下载，那不是批量。这里只做
一件事——**给定一串 `Job`，产出一个 zip 和一份清单**。404/409 这类 HTTP 判断留在路由，
所以这一层不认识 FastAPI，单测可以直接喂 `Job`。

**清单（`_manifest.json`）是契约的一部分**：导出的（含音符数/轨数）与被跳过的（含原因）
都写进去。批量下载几乎不可能 100% 成功（还没扒谱的歌、没音符的工程都在所难免），
"少了三首"必须能在包里查到，而不是靠用户数文件。

SVP 的缓存语义与单首下载**共用** :func:`ensure_svp`：缓存键带 `artifacts_rev`（重跑过必然
重算），先写 `.part` 再改名（并发请求不会读到半截文件）。统计另存 `<key>.svp.json`，
这样第二次下载（命中缓存）时清单里仍然有音符数。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .job_files import safe_name, scan_files
from .job_models import Job
from ..svp import (DEFAULT_PARTS, SVP_PARTS, SVP_VERSION, build_svp, cache_key,
                   export_path)

#: 批量下载的两种口径。
KINDS: tuple[str, ...] = ("svp", "files")

#: 清单文件名：放在 zip 根目录，用户解压第一眼就能看到。
MANIFEST_NAME = "_manifest.json"


class BatchExportError(RuntimeError):
    """一条都没导出来（路由据此回 409，而不是发一个空包）。"""


def normalize_parts(raw: Iterable[str] | str) -> list[str]:
    """`"vocal,instrumental"` / `["instrumental"]` → 按 `SVP_PARTS` 顺序的合法子集。

    顺序要归一：`vocal,instrumental` 与 `instrumental,vocal` 必须命中**同一份**缓存。
    """
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    wanted = [str(item).strip() for item in items if str(item).strip()]
    unknown = [item for item in wanted if item not in SVP_PARTS]
    if unknown:
        raise ValueError(f"未知的导出内容 {unknown}；可选 {list(SVP_PARTS)}")
    chosen = [key for key in SVP_PARTS if key in set(wanted)]
    return chosen or list(DEFAULT_PARTS)


def ensure_svp(job: Job, parts: Sequence[str]) -> tuple[Path, Mapping[str, Any] | None]:
    """确保这个工程的 `.svp` 已生成，返回 `(路径, 统计)`；命中缓存时统计从旁文件读。"""
    options = dict(job.options)
    key = cache_key(parts, SVP_VERSION, options, rev=job.artifacts_rev)
    target = export_path(job.internal_dir, key)
    stats_path = target.with_name(target.name + ".json")
    if target.is_file():
        return target, _read_stats(stats_path)
    payload, stats = build_svp(job.out_dir, parts=list(parts), options=options,
                               version=SVP_VERSION)
    # 先写 .part 再改名：两个请求同时要同一份时不会读到半截文件
    temp = target.with_name(target.name + ".part")
    temp.write_bytes(payload)
    temp.replace(target)
    _write_stats(stats_path, stats)
    # 为了让 SVP 同轨不叠音而动过音符的话，日志里说一声（不然"怎么少了一个音"没法查）
    for warning in stats.get("warnings") or []:
        job.emit("log", phase=None, message=f"导出 SVP：{warning}")
    return target, stats


def export_batch(jobs: Sequence[Job], *, kind: str = "svp",
                 parts: Sequence[str] = DEFAULT_PARTS) -> tuple[Path, dict[str, Any]]:
    """把 `jobs` 打成一个 zip，返回 `(临时文件, 清单)`。

    `kind="svp"` → 每首一个 `<歌名>.svp`；`kind="files"` → 每首的产物按 `<歌名>/<相对路径>`。
    单个工程失败（没扒谱、没音符）只记进清单的 `skipped`，不拖累整批。
    一首都没导出来抛 :class:`BatchExportError`，调用方负责清掉临时目录。
    """
    if kind not in KINDS:
        raise ValueError(f"未知的下载内容 {kind!r}；可选 {list(KINDS)}")
    work = Path(tempfile.mkdtemp(prefix="sheetsage2-batch-export-"))
    target = work / "batch.zip"
    manifest: dict[str, Any] = {
        "kind": kind,
        "parts": list(parts),
        "created_at": time.time(),
        "exported": [],
        "skipped": [],
    }
    used: set[str] = set()
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as archive:
            for job in jobs:
                name = _unique_name(job.name, used)
                try:
                    if kind == "svp":
                        _add_svp(archive, job, name, parts, manifest)
                    else:
                        _add_files(archive, job, name, manifest)
                except (FileNotFoundError, ValueError) as exc:
                    manifest["skipped"].append({"id": job.id, "name": job.name,
                                                "reason": str(exc)})
            archive.writestr(MANIFEST_NAME,
                             json.dumps(manifest, ensure_ascii=False, indent=2))
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    if not manifest["exported"]:
        shutil.rmtree(work, ignore_errors=True)
        reason = manifest["skipped"][0]["reason"] if manifest["skipped"] else "没有可导出的工程"
        raise BatchExportError(f"这批没有可导出的内容：{reason}")
    return target, manifest


# --------------------------------------------------------------------- 内部
def _unique_name(base: str, used: set[str]) -> str:
    """压缩包里的名字：`同名.svp` / `同名-2.svp`，绝不覆盖。"""
    name = safe_name(base) or "audio"
    candidate = name
    index = 2
    while candidate.casefold() in used:
        candidate = f"{name}-{index}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _add_svp(archive: zipfile.ZipFile, job: Job, name: str, parts: Sequence[str],
             manifest: dict[str, Any]) -> None:
    """写一个 `.svp` 进包。**统计可以缺，导出的记录不能缺**——否则命中缓存又没有旁文件时，
    明明导出了却会被判成"这批没有可导出的内容"。"""
    path, stats = ensure_svp(job, parts)
    entry = f"{name}.svp"
    archive.write(path, entry)
    record: dict[str, Any] = {"id": job.id, "name": job.name, "file": entry}
    if stats is not None:
        record.update({
            "notes": int(stats.get("notes") or 0),
            "tracks": [track.get("title") for track in stats.get("tracks") or []],
            "language": stats.get("language"),
        })
    manifest["exported"].append(record)


def _add_files(archive: zipfile.ZipFile, job: Job, name: str,
               manifest: dict[str, Any]) -> None:
    files = _artifact_files(job)
    if not files:
        raise FileNotFoundError("还没有产物可导出")
    for relative, path in files:
        archive.write(path, f"{name}/{relative}")
    manifest["exported"].append({"id": job.id, "name": job.name, "files": len(files)})


def _artifact_files(job: Job) -> list[tuple[str, Path]]:
    """当前这一版的产物（口径与 `/files`、整包 zip 一致：只认清单，跳过内部文件）。"""
    listed = _listed_paths(job)
    out: list[tuple[str, Path]] = []
    for item in scan_files(job.out_dir):
        relative = str(item["path"])
        if listed is not None and relative not in listed:
            continue
        path = job.out_dir / relative
        if path.is_file():
            out.append((relative, path))
    return out


def _listed_paths(job: Job) -> set[str] | None:
    """读 `.webui/manifest.json` 里的产物清单；没有就是 `None`（老工程按目录扫描走）。"""
    try:
        data = json.loads(job.manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = data.get("files")
    return set(files) if isinstance(files, dict) else None


def _read_stats(path: Path) -> Mapping[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_stats(path: Path, stats: Mapping[str, Any]) -> None:
    """统计是锦上添花（清单里的音符数），写不进去不能把导出带崩。"""
    try:
        path.write_text(json.dumps(dict(stats), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
