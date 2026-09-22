"""ABC 乐谱过滤（对应任务文档 3.6.4）。

SheetSage2 产出的 ABC 形如：

    X:1
    T:
    M:4/4
    L:1/16
    Q:1/4=75
    V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
    V: Ins clef=treble name="Ins Melody" snm="Inst."
    K:Eb
    % intro
    V: Vocal
    Z|"Cm"z8"G#"z8|
    V: Ins
    z8f2g2b2c'2|
    % verse
    V: Vocal
    "Cm"e3dd2ee-|

要点（与任务文档 3.6.4 的对应关系）：

1. ``V: `` 有两种：**声部声明**（带 ``clef=``/``name=`` 等属性，出现在文件头）和
   **声部切换**（只有 ``V: <名字>``，出现在正文中）。两者语义不同，必须分开处理。
2. ``% intro`` / ``% verse`` / ``% chorus`` 等是**段落注释行**，其后紧跟该段落的音乐。
   文档规则 4/5 字面「删除 % intro 及其后全部内容」会删光整首曲子，因此删除动作
   按**段落块**进行：从该注释行删到下一个 ``% <段落>`` 注释行或文件尾。
3. 默认（``only_melody=True``）只保留 ``V: Vocal``，即删除 ``V: Ins`` 的声明、
   切换行与其后音符；**默认不删除 intro/outro 段落**（经确认为独立开关）。
4. 行内和弦标记 ``"Cmaj7"`` / ``"Fm(maj7)"`` 一律保留，不做删除。
"""

from __future__ import annotations

import re
from typing import Sequence

__all__ = ["filter_abc", "AbcFilterReport"]

# 段落注释行：行首 % 后跟一个段落名（词/连字符，可含空格），例如 `% pre-chorus`
_SECTION_RE = re.compile(r"^\s*%\s*([A-Za-z][\w \-]*?)\s*$")

# ABC 信息字段（header field），形如 `X:`、`K:`、`Q:`；单字母 + 冒号
_INFO_FIELD_RE = re.compile(r"^\s*[A-Za-z]:")

# 声部声明：`V: <名> <属性...>`，属性含 `=`，如 clef=/name=/snm=
_VOICE_DECL_RE = re.compile(r"^\s*V:\s*(?P<name>[^\s=]+)(?P<attrs>.*)$")

# 声部切换：`V: <名>` 后无 `=` 属性
_VOICE_SWITCH_RE = re.compile(r"^\s*V:\s*(?P<name>[^\s=%]+)\s*$")

# 需要整体删除的段落名（小写比较）
_DROP_SECTIONS = {"intro", "outro"}

#: 声部名 → 三个可选内容 token 之一（与 ``app.rebuild.EXPORT_VOICE_TOKENS`` 对齐）
_VOICE_KIND = {
    "vocal": "vocal", "melody": "vocal", "lead": "vocal", "voice": "vocal",
    "ins": "ins", "instrumental": "ins", "accompaniment": "ins",
}


def _voice_kind(name: str) -> str | None:
    return _VOICE_KIND.get((name or "").strip().lower())


class AbcFilterReport:
    """过滤过程的可观测记录，用于「诊断信息」页。"""

    __slots__ = ("has_voices", "kept_voices", "dropped_voices", "dropped_sections", "lines_in", "lines_out")

    def __init__(self) -> None:
        self.has_voices: bool = False
        self.kept_voices: list[str] = []
        self.dropped_voices: list[str] = []
        self.dropped_sections: list[str] = []
        self.lines_in: int = 0
        self.lines_out: int = 0

    def as_dict(self) -> dict:
        return {
            "has_voices": self.has_voices,
            "kept_voices": self.kept_voices,
            "dropped_voices": self.dropped_voices,
            "dropped_sections": self.dropped_sections,
            "lines_in": self.lines_in,
            "lines_out": self.lines_out,
        }


def filter_abc(
    abc: str,
    *,
    export_voices: Sequence[str] | None = None,
    only_melody: bool | None = None,
    drop_intro_outro: bool = False,
) -> tuple[str, AbcFilterReport]:
    """过滤 ABC 文本。

    Args:
        abc: 原始 ABC 文本。
        export_voices: **要保留的内容**，``"vocal"`` / ``"ins"`` 的子集
            （``"chords"`` 不是 ABC 声部，由 ``rebuild`` 从 ``chords.mid`` 单独取）。
            例：``["vocal"]`` 只留人声主旋律；``["vocal", "ins"]`` 两条都留。
        only_melody: 旧参数，向后兼容 —— 没给 ``export_voices`` 时生效：
            ``True`` 只留人声主旋律，``False`` 两条都留。
        drop_intro_outro: True 时额外删除 ``% intro`` / ``% outro`` 整个段落块。

    Returns:
        ``(过滤后的 ABC 文本, 过滤报告)``。当输入没有 ``V:`` 声部标记时不过滤声部
        （文档规则 7），仅按需做段落过滤。
    """
    report = AbcFilterReport()
    if not abc:
        return abc or "", report

    # 「想保留哪些声部」——认不出来的声部一律保留，免得把不认识的声部误删
    if export_voices is not None:
        wanted = {str(t).strip().lower() for t in export_voices}
    elif only_melody is not None:
        wanted = {"vocal"} if only_melody else {"vocal", "ins"}
    else:
        wanted = {"vocal"}          # 默认只留人声主旋律（与前端默认勾选一致）

    def _keep_voice(name: str) -> bool:
        kind = _voice_kind(name)
        return True if kind is None else (kind in wanted)

    lines = abc.splitlines()
    report.lines_in = len(lines)

    report.has_voices = any(_VOICE_SWITCH_RE.match(ln) or _VOICE_DECL_RE.match(ln) for ln in lines)
    # 两条声部都要 == 没有任何声部需要删；此时不做声部过滤（认不出的声部同理保留）
    do_voice_filter = report.has_voices and not ({"vocal", "ins"} <= wanted)

    out: list[str] = []
    current_voice: str | None = None
    skip_section = False  # 命中要删除的段落时置位，直到下一个段落注释行

    seen_kept: set[str] = set()
    seen_dropped: set[str] = set()

    for line in lines:
        # --- 段落注释行：决定是否进入「跳过」状态 ---
        sec = _SECTION_RE.match(line)
        if sec:
            name = sec.group(1).strip()
            if drop_intro_outro and name.lower() in _DROP_SECTIONS:
                skip_section = True
                report.dropped_sections.append(name)
                continue
            skip_section = False
            out.append(line)
            continue

        # --- 非段落注释的普通注释：透传（但若处于跳过状态则丢弃）---
        if line.lstrip().startswith("%"):
            if not skip_section:
                out.append(line)
            continue

        if skip_section:
            continue

        # --- 声部声明（带属性，文件头）---
        decl = _VOICE_DECL_RE.match(line)
        if decl and "=" in decl.group("attrs"):
            name = decl.group("name")
            if do_voice_filter and not _keep_voice(name):
                seen_dropped.add(name)
                continue
            seen_kept.add(name)
            out.append(line)
            continue

        # --- 声部切换 ---
        sw = _VOICE_SWITCH_RE.match(line)
        if sw:
            name = sw.group("name")
            current_voice = name
            if do_voice_filter and not _keep_voice(name):
                seen_dropped.add(name)
                continue  # 删除该切换行
            seen_kept.add(name)
            out.append(line)
            continue

        # --- 其它信息字段（X:/T:/M:/L:/Q:/K: 等）---
        if _INFO_FIELD_RE.match(line) and current_voice is None:
            out.append(line)
            continue

        # --- 音符行 ---
        if do_voice_filter and current_voice is not None and not _keep_voice(current_voice):
            continue  # 属于被删除声部
        out.append(line)

    report.kept_voices = sorted(seen_kept, key=str.lower)
    report.dropped_voices = sorted(seen_dropped, key=str.lower)
    report.lines_out = len(out)

    text = "\n".join(out)
    if abc.endswith("\n"):
        text += "\n"
    return text, report
