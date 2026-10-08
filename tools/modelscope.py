"""从 ModelScope 拉模型仓库（只用标准库）。

ModelScope 没有 HF 那种 `huggingface-cli`，但它的 HTTP 接口够用：

- 列文件：`GET /api/v1/models/{repo}/repo/files?Revision={rev}&Recursive=true`
  → `Data.Files[]`，每个条目有 `Path` / `Size` / `Sha256` / `Type`（`blob` 是文件、`tree` 是目录）；
- 下文件：`GET /models/{repo}/resolve/{rev}/{path}`（和 HF 的 `resolve` 一个形状，支持 Range 续传）。

大小/sha256 都来自列表接口本身，所以**每个文件都能校验**，不用另找清单。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from downloader import download, fetch_json, human_bytes

API = ("https://www.modelscope.cn/api/v1/models/{repo}/repo/files"
       "?Revision={revision}&Recursive=true")
RESOLVE = "https://www.modelscope.cn/models/{repo}/resolve/{revision}/{path}"


@dataclass(frozen=True)
class RepoFile:
    path: str
    size: int
    sha256: str

    @property
    def is_lfs(self) -> bool:                       # 大文件走 LFS，但 URL 一样
        return self.size > 10 * 1024 * 1024


def list_files(repo: str, revision: str = "master", *, timeout: int = 60) -> list[RepoFile]:
    """仓库里的**文件**（目录条目丢掉——它们没有大小也没有 sha256）。"""
    payload = fetch_json(API.format(repo=repo, revision=revision), timeout=timeout)
    entries = ((payload.get("Data") or {}).get("Files")) or []
    files = [
        RepoFile(path=str(item["Path"]), size=int(item.get("Size") or 0),
                 sha256=str(item.get("Sha256") or ""))
        for item in entries
        if item.get("Type") == "blob"
    ]
    if not files:
        raise RuntimeError(f"{repo}: 一个文件都没列出来（接口变了？）")
    return files


def file_url(repo: str, path: str, revision: str = "master") -> str:
    quoted = "/".join(urllib.parse.quote(part) for part in path.split("/"))
    return RESOLVE.format(repo=repo, revision=revision, path=quoted)


def repo_size(repo: str, revision: str = "master") -> int:
    return sum(item.size for item in list_files(repo, revision))


def download_repo(repo: str, target: str | Path, *, revision: str = "master",
                  dry_run: bool = False, progress: bool = True,
                  files: list[RepoFile] | None = None,
                  done_bytes_before: int = 0, grand_total: int | None = None) -> dict:
    """把一个仓库下到 `target/`，返回统计。

    - 已存在且**大小一致**的文件直接跳过（4.5 GB 的仓库每次都算 sha256 太慢）；
      大小不一致就重下——那说明上次没下完。
    - 进度是**两级**的：当前文件的百分比（`download()` 自己画）+ 这一行末尾的
      `[3/13 · 总 1.2/2.4 GB]`，让内测者知道"还有多少、大概还要多久"。
    """
    target = Path(target)
    entries = files if files is not None else list_files(repo, revision)
    total = sum(item.size for item in entries)
    grand_total = grand_total or total
    done_bytes = done_bytes_before
    skipped = downloaded = 0
    if progress:
        print(f"  {repo} -> {target}")
        print(f"    {len(entries)} 个文件，{human_bytes(total)}"
              f"（按 10 MB/s 大约 {total / 10 / 1024 / 1024 / 60:.0f} 分钟）")
    if dry_run:
        return {"repo": repo, "target": str(target), "files": len(entries),
                "bytes": total, "downloaded": 0, "skipped": 0, "dry_run": True}

    for index, item in enumerate(entries, start=1):
        destination = target / item.path
        extra = (f"[{index}/{len(entries)} · 总 {human_bytes(done_bytes)}"
                 f"/{human_bytes(grand_total)}]")
        if destination.is_file() and destination.stat().st_size == item.size:
            skipped += 1
            done_bytes += item.size
            if progress:
                print(f"    [{index}/{len(entries)}] {item.path} 已下过，跳过"
                      f"（{human_bytes(item.size)}）")
            continue
        if progress:
            print(f"    [{index}/{len(entries)}] {item.path}  {human_bytes(item.size)}")
        download(file_url(repo, item.path, revision), destination,
                 sha256=item.sha256 or None, size=item.size or None,
                 label=item.path, extra=extra, progress=progress)
        downloaded += 1
        done_bytes += item.size

    if progress:
        print(f"  {repo}：下好 {downloaded} 个、跳过 {skipped} 个；"
              f"累计 {human_bytes(done_bytes)}/{human_bytes(grand_total)}"
              f"（{done_bytes / grand_total:.0%}）")
    return {"repo": repo, "target": str(target), "files": len(entries), "bytes": total,
            "downloaded": downloaded, "skipped": skipped, "dry_run": False,
            "done_bytes": done_bytes}


def missing_files(repo: str, target: str | Path, *, revision: str = "master") -> list[str]:
    """还缺哪些文件（大小不一致也算缺）——自检与"要不要再下一趟"用。"""
    target = Path(target)
    missing: list[str] = []
    for item in list_files(repo, revision):
        destination = target / item.path
        if not destination.is_file() or destination.stat().st_size != item.size:
            missing.append(item.path)
    return missing
