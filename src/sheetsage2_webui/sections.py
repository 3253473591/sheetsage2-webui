"""段落标签：模型给的英文结构名 → 中文，并把**连续相同**的段合并。

模型的 `structure.lab` 是**按乐句**给的（一首歌十几到三十行），相邻行经常是同一个标签
（`verse verse verse` / `chorus chorus`），直接用有两个毛病：

1. 界面上出现 "verse、verse、chorus、chorus" 这种重复块；
2. 歌词识别的切片会把本该连成一段的切成好几刀。

这里只做两件事：**规范化标签**（英文 → 中文）与**合并相邻同名段**。纯函数、不依赖模型
也不依赖时间轴——秒域（`asr.sections_from_lab`）和 tick 域（`pipeline` 的工程段落）都调它。

`structure.lab` 本身**不动**：它是模型的原始产物（自洽性检查逐行比），中文只出现在我们
工程化之后的 `project.json` / 卷帘 / 日志里。

实测 `output/` 里 10 个真实工程用到的标签一共 7 种，正好是下面这张表覆盖的那些。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

#: 模型可能给的结构名（小写）-> 界面上用的中文名。加别名只改这里。
SECTION_LABELS: dict[str, str] = {
    # 前奏
    "intro": "前奏", "introduction": "前奏", "opening": "前奏",
    # 主歌
    "verse": "主歌", "verses": "主歌", "strophe": "主歌",
    # 预副歌
    "pre-chorus": "预副歌", "prechorus": "预副歌", "pre chorus": "预副歌",
    "pre_chorus": "预副歌", "pre-chorus ": "预副歌", "build": "预副歌", "build-up": "预副歌",
    # 副歌
    "chorus": "副歌", "refrain": "副歌", "hook": "副歌", "drop": "副歌",
    # 桥段
    "bridge": "桥段", "middle eight": "桥段", "middle-eight": "桥段", "middle8": "桥段",
    # 间奏（器乐）
    "interlude": "间奏", "instrumental": "间奏", "inst": "间奏", "solo": "间奏",
    "breakdown": "间奏", "break": "间奏", "riff": "间奏",
    # 尾奏
    "outro": "尾奏", "ending": "尾奏", "end": "尾奏", "coda": "尾奏",
    "outro/ending": "尾奏", "fadeout": "尾奏", "fade out": "尾奏", "fade-out": "尾奏",
}

#: 一般不唱的段落（纯器乐）——歌词识别可以跳过它们。
INSTRUMENTAL_LABELS = frozenset({"前奏", "间奏", "尾奏"})

#: 界面上认得的七个中文段落名（`docs/WebUI规格.md` 的对照表）。
CHINESE_SECTIONS: tuple[str, ...] = ("前奏", "主歌", "预副歌", "副歌", "间奏", "桥段", "尾奏")


def canonical_label(raw: str | None) -> str | None:
    """模型的标签 → 中文。**认不出来就原样返回**（别乱猜），空值返回 `None`。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return SECTION_LABELS.get(text.lower().strip(), text)


def is_instrumental(label: str | None) -> bool:
    """前奏 / 间奏 / 尾奏（一般不唱，跳过它们不会丢歌词——除非模型标错）。"""
    return canonical_label(label) in INSTRUMENTAL_LABELS


def merge_consecutive(
    spans: Sequence[tuple[float, float, Any]],
    *,
    gap: float = 1e-6,
) -> list[tuple[float, float, Any]]:
    """把**相邻且同标签**的段并成一段（只并"连着"的）。

    `[(0,1,'前奏'), (1,2,'前奏'), (2,3,'主歌'), (5,6,'前奏')]`
      → `[(0,2,'前奏'), (2,3,'主歌'), (5,6,'前奏')]`

    只认"贴着"的段：副歌 — 主歌 — 副歌 仍然是三段，不会并成一个副歌。
    """
    merged: list[tuple[float, float, Any]] = []
    for start, end, label in spans:
        if float(end) <= float(start):
            continue
        if merged and merged[-1][2] == label and float(start) - float(merged[-1][1]) <= gap:
            merged[-1] = (merged[-1][0], end, label)
        else:
            merged.append((start, end, label))
    return merged


def canonical_labels(labels: Iterable[str | None]) -> list[str]:
    """去重**保序**地翻译一组标签（给 `summary` / 报告用）。"""
    out: list[str] = []
    for label in labels:
        text = canonical_label(label)
        if text and text not in out:
            out.append(text)
    return out
