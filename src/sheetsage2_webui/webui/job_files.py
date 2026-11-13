"""文件与上传边界：文件名收敛、工程目录原子认领、产物扫描、内容指纹、流落盘。

这一层只认路径与字节流，不认识 Job / 队列 / 线程——所以它既给调度器用，也给存储层用。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from .job_errors import UploadRejected

#: 允许上传的音频后缀。其余一律挡在上传阶段，别等 ffmpeg 报错。
AUDIO_SUFFIXES = frozenset(
    {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".oga",
     ".aif", ".aiff", ".wma", ".mp4", ".wv", ".ape", ".alac", ".caf"}
)

#: 可单独上传的音源（stem）。目前只有歌词用的 `vocal`，将来要加伴奏/鼓就直接加 key。
STEM_KINDS = ("vocal",)
STEM_LABELS = {"vocal": "干声"}

#: 产物文件后缀 -> 给前端看的类别。
FILE_KINDS = {
    ".json": "data", ".lab": "label", ".tsv": "label", ".txt": "text",
    ".mid": "midi", ".midi": "midi", ".abc": "score", ".wav": "audio",
}

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


#: WebUI 自己的东西放在工程目录的哪个子目录里（点开头 = 不是产物，列表与打包都会跳过）。
INTERNAL_DIRNAME = ".webui"


def project_folder_name(stem: str) -> str:
    """音频名 -> 工程目录名。

    **必须和 `cli._slug()` 一个规则**：命令行跑出来的 `output/<歌名>/` 和 WebUI 建出来的
    长得一样，两边才能互相认（`tests/test_webui.py` 里有一条断言盯着这件事）。
    """
    keep = [c if (c.isalnum() or c in "._-") else "_" for c in str(stem)]
    return "".join(keep).strip("._") or "audio"


def unique_project_dir(root: Path, name: str) -> Path:
    """`<root>/<name>`；重名就 `-2`、`-3`……（**绝不覆盖已有工程**）。"""
    candidate = root / name
    index = 2
    while candidate.exists():
        candidate = root / f"{name}-{index}"
        index += 1
    return candidate


def claim_project_dir(root: Path, name: str, max_tries: int = 50) -> Path:
    """原子地认领一个工程目录（G9）：**创建目录本身就是互斥**。

    `unique_project_dir` 是"先看名字有没有被占，再返回"，判断与创建之间有窗口——两个并发
    上传同名文件会同时选到同一个目录，随后 `mkdir(exist_ok=True)` 让它们写进同一个地方。
    这里反过来：直接 `mkdir(exist_ok=False)`，失败（目录已经存在）就换 `-2`、`-3` 再试，
    谁创建成功谁就独占这个名字，**没有竞态**。
    """
    candidate = root / name
    for index in range(2, max_tries + 2):
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            candidate = root / f"{name}-{index}"
    raise OSError(f"同名工程太多，目录 {root / name} 认领失败（试了 {max_tries} 个名字）")


def is_internal(relative: str) -> bool:
    """产物清单里要不要跳过这一项（`job.json` 与 `.webui/` 是 WebUI 自己的东西）。"""
    text = str(relative).replace("\\", "/")
    if text == "job.json":
        return True
    return any(part.startswith(".") for part in text.split("/") if part)


def safe_name(name: str) -> str:
    """把用户给的文件名收敛成一个能安全落盘的 basename。"""
    base = Path(str(name).replace("\\", "/")).name
    cleaned = _UNSAFE.sub("_", base).strip().strip(".")
    return cleaned or "audio"


def unique_suffix(name: str) -> str:
    """取后缀，全小写；没有后缀就当空。"""
    return Path(safe_name(name)).suffix.lower()


def _kind_of(path: str) -> str:
    return FILE_KINDS.get(Path(path).suffix.lower(), "other")


def _check_audio(name: str) -> str:
    """校验文件名后缀，返回安全的 basename。"""
    audio = safe_name(name)
    suffix = unique_suffix(audio)
    if suffix not in AUDIO_SUFFIXES:
        raise UploadRejected(
            f"不支持的文件类型 {suffix or '(无后缀)'}；"
            f"请上传 {'/'.join(sorted(s.lstrip('.') for s in AUDIO_SUFFIXES))} 之一")
    return audio


def content_fingerprint(path: Path, head_bytes: int = 1024 * 1024) -> str:
    """内容指纹 = 文件大小 + 前 1MB 的 sha1（G10 去重：同一首歌传两次能被认出来）。"""
    import hashlib

    try:
        size = path.stat().st_size
    except OSError:
        return ""
    digest = hashlib.sha1()
    try:
        with path.open("rb") as handle:
            digest.update(handle.read(head_bytes))
    except OSError:
        return ""
    return f"{size}:{digest.hexdigest()}"


def _write_stream(target: Path, stream: Any, limit: int, limit_mb: float) -> int:
    """把上传流写到 `target`，超限立刻中断。返回字节数（0 表示空文件）。"""
    written = 0
    with target.open("wb") as handle:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise UploadRejected(f"文件超过 {limit_mb:g} MB 上限")
            handle.write(chunk)
    return written


def scan_files(root: Path) -> list[dict[str, Any]]:
    """产物清单：相对路径 + 字节数 + 类别（给前端决定图标和排序）。

    **跳过 WebUI 自己的东西**（`job.json` 和 `.webui/`）：工程目录就是"产物目录"，
    和命令行跑出来的 `output/<歌名>/` 完全同构，产物清单里不该混进元数据与缓存。
    """
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(root)).replace("\\", "/")
        if is_internal(relative):
            continue
        out.append({"path": relative, "size": path.stat().st_size, "kind": _kind_of(relative)})
    return out


def iter_chunks(stream: Iterable[bytes], size: int = 1024 * 1024) -> Iterator[bytes]:
    """给测试用的固定分块读取（生产路径是 `JobManager.create` 直接读流）。"""
    buffer = bytearray()
    for chunk in stream:
        buffer.extend(chunk)
        while len(buffer) >= size:
            yield bytes(buffer[:size])
            del buffer[:size]
    if buffer:
        yield bytes(buffer)
