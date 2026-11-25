"""扫描一个文件夹，把「歌曲 + 同名 LRC」配成一张表（批量导入的预览）。

**纯函数**：只读目录、不建工程、不动源文件、不认识 HTTP。扫描结果就是界面上的预览表，
也是 `POST /api/batches` 建工程时的输入——所以两处口径必须一模一样，都走这里。

配对规则（界面上要让用户一眼看懂，所以规则要简单）：
- 以音频的 **stem** 为准（`casefold()`），同目录下找同 stem 的 `.lrc` / `.txt`；
- `.lrc` 带时间戳 → 逐行精确对齐（`lrc`）；
- 无时间戳（`.txt`，或没有 `[mm:ss]` 的 `.lrc`）→ 仍要跑语音识别（`txt`），只是字面用官方词；
- 日/韩 LRC 走 Qwen 强制对齐器（TIFA 没有 ja/ko G2P）：有对齐器是 `lrc-ja` / `lrc-ko`，
  没有就照旧标出来 + 在 `problems` 里说清楚（不在这里决定"退回识别"，那是 runner 的事）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..asr import parse_lyric_text, read_lrc
from ..tifa_align import guess_language
from .job_files import AUDIO_SUFFIXES

#: 歌词文件的候选后缀（`.lrc` 优先）。
LRC_SUFFIXES: tuple[str, ...] = (".lrc", ".txt")

#: 单批最多几首。**扫描与拖拽上传共用这一个数**（前端从 `/api/env.batch_max` 读）。
#: 100 是"一晚上跑得完"和"一次别错太多"之间的折中：一首 4 分钟的歌 GPU 上约 2~4 分钟，
#: 100 首 ≈ 5~7 小时；再多就该拆成几批、分几个晚上跑。
MAX_BATCH = 100


@dataclass(frozen=True)
class ScanItem:
    """预览表里的一行。"""

    audio: Path
    name: str
    size: int
    lrc: Path | None = None
    #: `lrc`（TIFA 逐行）| `lrc-ja` / `lrc-ko`（Qwen 对齐器）| `txt`（无时间戳）| `none`
    lrc_kind: str = "none"
    lrc_lines: int = 0
    duration_seconds: float | None = None
    #: 这一行自己的问题（界面上标黄）；不阻断建工程。
    problem: str | None = None

    @property
    def duration_known(self) -> bool:
        return self.duration_seconds is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "audio": str(self.audio),
            "size": self.size,
            "duration_seconds": self.duration_seconds,
            "lrc": str(self.lrc) if self.lrc is not None else None,
            "lrc_kind": self.lrc_kind,
            "lrc_lines": self.lrc_lines,
            "problem": self.problem,
        }


@dataclass(frozen=True)
class ScanResult:
    """一次扫描：预览行 + 整批级的问题（人话，界面直接列出来）。"""

    folder: Path
    items: list[ScanItem] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: 有没有任何一首带歌词源（界面据此提示"这批都会走语音识别"）。
    with_lyrics: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "folder": str(self.folder),
            "items": [item.as_dict() for item in self.items],
            "problems": list(self.problems),
            "with_lyrics": self.with_lyrics,
            # 总时长（能做预估的才有值）
            "total_seconds": round(sum(item.duration_seconds or 0.0
                                       for item in self.items), 2),
        }


class FolderPickUnavailable(RuntimeError):
    """没法弹目录选择器（没有 tkinter / 没有桌面）——界面据此提示"手动粘贴路径"。"""


def pick_folder(*, title: str = "选择要批量导入的文件夹") -> str | None:
    """在**服务进程**里弹一个原生目录选择器（本机工具才有的便利）。

    Web 页面拿不到服务器的目录，只能让用户粘路径；这里是唯一能"浏览"的地方。
    没有 tkinter / 没有桌面（无头机器）就抛 :class:`FolderPickUnavailable`，
    让调用方回 501 并提示手填——**不要**假装打开了一个窗口。
    """
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as exc:                    # noqa: BLE001 - 导入失败的原因不重要
        raise FolderPickUnavailable(
            "这个环境没有 tkinter，弹不了目录选择器；请手动粘贴文件夹路径") from exc
    try:
        root = tkinter.Tk()
    except Exception as exc:                    # noqa: BLE001 - 没有显示器 / 没有桌面会话
        raise FolderPickUnavailable(
            "这个环境没有图形界面，弹不了目录选择器；请手动粘贴文件夹路径") from exc
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)   # 否则窗口可能藏在浏览器后面
        except Exception:                       # noqa: BLE001 - 有的 Tk 版本没有这个属性
            pass
        chosen = filedialog.askdirectory(title=title, mustexist=True)
    finally:
        try:
            root.destroy()
        except Exception:                       # noqa: BLE001 - 关不掉也不该影响返回
            pass
    return str(chosen) if chosen else None


def scan_folder(
    folder: str | Path,
    *,
    data_root: str | Path | None = None,
    max_items: int = MAX_BATCH,
    max_bytes: int | None = 512 * 1024 * 1024,
    probe: Callable[[Path], Mapping[str, Any] | None] | None = None,
    aligner_available: bool | None = None,
) -> ScanResult:
    """扫一个文件夹。**只读**：不回写、不移动、不删源文件。

    - `data_root` 只用来拒绝"把数据目录本身/它的子目录当输入"（那里的东西是工程产物，
      再扫一遍会把自己收编成工程）；
    - `probe` 是时长探测（生产传 `media.probe_audio`，测试传假的），传 `None` 就不探；
    - `aligner_available`：日/韩 LRC 要 Qwen 对齐器，`False` 时在 `problems` 里说清楚。
    """
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise ValueError(f"{root} 不是文件夹（或者不存在）")
    root = root.resolve()
    if data_root is not None:
        data = Path(data_root).expanduser().resolve()
        if root == data or data in root.parents:
            raise ValueError("不能扫数据目录（或它的子目录）：那里是工程产物，会被再收编一次")

    files = sorted((path for path in root.iterdir()
                    if path.is_file() and not path.name.startswith(".")),
                   key=lambda path: path.name.casefold())
    audio = [path for path in files if path.suffix.lower() in AUDIO_SUFFIXES]
    if not audio:
        supported = "/".join(sorted(suffix.lstrip(".") for suffix in AUDIO_SUFFIXES))
        raise ValueError(f"{root} 里没有音频。支持 {supported}")
    if len(audio) > max_items:
        raise ValueError(f"这个文件夹里有 {len(audio)} 首，超过一次 {max_items} 首的上限；"
                         f"请分两次导入")

    # 同 stem 的歌词（大小写不敏感）。`.lrc` 优先于 `.txt`。
    lyrics: dict[str, list[Path]] = {}
    for path in files:
        if path.suffix.lower() in LRC_SUFFIXES:
            lyrics.setdefault(path.stem.casefold(), []).append(path)

    # 先按 stem 归组：同名音频要提示"会建成两个工程"。
    by_stem: dict[str, list[Path]] = {}
    for path in audio:
        by_stem.setdefault(path.stem.casefold(), []).append(path)

    items: list[ScanItem] = []
    problems: list[str] = []
    for path in audio:
        stem = path.stem
        key = stem.casefold()
        size = path.stat().st_size
        item_problems: list[str] = []
        if size == 0:
            item_problems.append(f"{path.name} 是空文件")
        elif max_bytes is not None and size > max_bytes:
            item_problems.append(
                f"{path.name} 太大（{_mb(size)} MB，上限 {_mb(max_bytes)} MB）")
        twins = by_stem[key]
        if len(twins) > 1:
            item_problems.append(
                f"「{stem}」有 {len(twins)} 首同名（{' / '.join(p.name for p in twins)}）："
                f"会建成 {len(twins)} 个工程")

        lrc, kind, lines, lrc_problem = _pair_lyrics(stem, lyrics.get(key) or [],
                                                     aligner_available)
        if lrc_problem:
            item_problems.append(lrc_problem)

        duration = None
        if probe is not None:
            info = probe(path) or {}
            try:
                duration = float(info.get("duration_seconds") or 0.0) or None
            except (TypeError, ValueError):
                duration = None

        problems.extend(item_problems)
        items.append(ScanItem(
            audio=path, name=stem, size=size, lrc=lrc, lrc_kind=kind,
            lrc_lines=lines, duration_seconds=duration,
            problem="；".join(item_problems) or None,
        ))

    return ScanResult(
        folder=root,
        items=items,
        problems=problems,
        with_lyrics=sum(1 for item in items if item.lrc_kind != "none"),
    )


# --------------------------------------------------------------------- 内部
def _pair_lyrics(stem: str, candidates: list[Path],
                 aligner_available: bool | None) -> tuple[Path | None, str, int, str | None]:
    """挑一个同名歌词并判断口径，返回 `(路径, kind, 行数, 问题)`。"""
    if not candidates:
        return None, "none", 0, None
    candidates = sorted(candidates, key=lambda path: (path.suffix.lower() != ".lrc",
                                                      path.name.casefold()))
    chosen = candidates[0]
    note = (f"「{stem}」有 {len(candidates)} 个同名歌词，用了 {chosen.name}"
            if len(candidates) > 1 else None)
    try:
        text = read_lrc(chosen)
    except OSError as exc:
        return None, "none", 0, f"{chosen.name} 读不出来：{exc}"
    lines = parse_lyric_text(text)
    if not lines:
        return None, "none", 0, f"{chosen.name} 里没有解析出唱词行，这首会走语音识别"

    if lines[0].start < 0:
        return chosen, "txt", len(lines), note

    script = guess_language(text)
    if script in ("ja", "ko"):
        kind = f"lrc-{script}"
        label = "日语" if script == "ja" else "韩语"
        if aligner_available is False:
            return chosen, kind, len(lines), (
                f"{chosen.name} 是{label} LRC，需要{label}对齐器（Qwen3-ForcedAligner）；"
                f"本机没装，识别歌词时会失败")
        return chosen, kind, len(lines), note
    return chosen, "lrc", len(lines), note


def _mb(size: int) -> str:
    """大小 -> 人话（上限很小时显示 KB，别写成 "0.0 MB"）。"""
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"

