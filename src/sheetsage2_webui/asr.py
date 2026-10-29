"""歌词识别：Qwen3-ASR + Qwen3-ForcedAligner，按 SheetSage2 的区间切片跑。

为什么要切片
------------
1. **省显存。** 实测峰值随切片长度线性增长（约 10MB/s）：30s → 3.41GB、120s → 4.08GB、
   195s → 5.09GB（模型常驻 3.18GB）。切片后一开始就把上限钉死。
2. **对齐器有长度上限。** 官方文档写"up to 5 minutes"，而样例里 `2020原唱` 是 392s。
   切片是安全路径。
3. **限制最坏延迟。** 同一套参数下有的切片 14s、有的 46s，长音频的方差会放大。

切片的边界直接用 SheetSage2 的 `structure.lab`——段落本来就是音乐上的自然切点。
段落标签会被规范化成中文，相邻同名段先合并（模型的标签是按乐句给的，`verse verse` 到处都是，
见 `sections.py`）；**前奏 / 间奏 / 尾奏一般不唱，可以整段跳过**（`skip_instrumental=True`），
既省时间也省显存。

环境要求
--------
`qwen-asr` 依赖 `transformers==4.57.6`，**和 SheetSage2 要求的版本完全一致**，
所以两个模型可以共存于同一个环境。安装：`pip install qwen-asr`。
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .bootstrap import find_ffmpeg
from .sections import canonical_label, is_instrumental, merge_consecutive

#: 单个切片的上限。对齐器官方支持到 5 分钟，但显存和延迟都随长度线性涨，所以卡在 90s。
DEFAULT_MAX_SLICE_SECONDS = 90.0
#: 每秒音频预留的生成 token 数。给太少会截断歌词，给太多会在纯器乐段空转（实测 2048 会让
#: 一段 100s 的切片从 41s 变成 176s）。
DEFAULT_TOKENS_PER_SECOND = 12.0
#: 相邻两字之间允许的最大空白（秒）。超过就**报告**（不判定），让人核对。
#: 慢歌的长句之间本来就有 15s+ 的停顿（实测《相思》19s），所以没法用统一阈值判决。
MAX_CHAR_GAP_SECONDS = 15.0


def _max_char_gap(chars: Sequence["AlignedChar"]) -> tuple[float, int]:
    """相邻字之间的最大空白（秒）及其位置下标（空白起点）。

    返回 `(gap, index)`；`index` 是空白**之后**那个字的下标。`index == 0` 表示空白在
    第一个字之前（前奏），`index == len(chars)` 表示在最后一个字之后（尾奏）——两者都
    无害。只有夹在唱词中间的大空白才说明对齐器漏了音频。
    """
    worst, worst_at = 0.0, 0
    for index, (previous, current) in enumerate(zip(chars, chars[1:]), start=1):
        gap = current.start - previous.end
        if gap > worst:
            worst, worst_at = gap, index
    return worst, worst_at


@dataclass
class Section:
    """一个切片区间（秒）。"""

    start: float
    end: float
    label: str | None = None

    @property
    def seconds(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict[str, Any]:
        return dict(start=self.start, end=self.end, label=self.label,
                    seconds=round(self.seconds, 3))


@dataclass
class AlignedChar:
    """一个字（或英文词）及其时间。时间是**整首曲**的时间轴。"""

    text: str
    start: float
    end: float

    def as_dict(self) -> dict[str, Any]:
        return dict(text=self.text, start=round(self.start, 3), end=round(self.end, 3))


#: 零长度字插值：一个字最多摊多宽（秒）。空档再大也不把一个字拉成十几秒。
MAX_REPAIR_CHAR_SECONDS = 1.5

#: 切边区间的边界最多为了一段贴边的人声往外挪多少秒（避免为了一个尾音的「啊」
#: 把整段间奏都拉进识别范围）。
MAX_VOCAL_EXTEND_SECONDS = 12.0

#: 相邻两个音之间超过这么久，就算人声断了，不并成同一句。
VOCAL_PHRASE_GAP_SECONDS = 1.5


def interpolate_degenerate(chars: Sequence["AlignedChar"], *,
                           max_char_seconds: float = MAX_REPAIR_CHAR_SECONDS,
                           ) -> tuple[list["AlignedChar"], int]:
    """把对齐器给不出时间的字（`start == end`）摊到前后两个字之间的空档里。

    对齐器给零长度 = **它没测到这个字的边界**。原来的做法是把它当一个「瞬间」，
    填词时再用 ±容差去窗口里找音——等于把「不知道」编码成一个假位置。实测全语料
    17% 的字是零长度，其中不少会被贪心挂到几秒之后才开始的音上。

    这里改成：连续的一段零长度字，均分在「前一个有长度的字的 end」到「后一个有长度
    的字的 start」之间。它仍然是**估计**，但比「所有不知道的字都堆在一个时刻」更接近
    真实唱法，而且保证了单调、不重叠。

    `max_char_seconds` 是单字上限：空档远大于字数时（实测最长 35.7s 只有一个字），
    锚在左边按上限排，剩下的留作停顿——那种空档多半是间奏，不是这个字拖了 35 秒。

    **只动零长度的字**：有 start/end 的字原样保留（返回的是新列表，不改入参）。
    返回 `(新列表, 修补的字数)`。
    """
    out = [AlignedChar(char.text, char.start, char.end) for char in chars]
    repaired = 0
    index = 0
    while index < len(out):
        if out[index].end > out[index].start + 1e-9:
            index += 1
            continue
        first = index
        while index < len(out) and out[index].end <= out[index].start + 1e-9:
            index += 1
        count = index - first
        left = out[first - 1].end if first else None
        right = out[index].start if index < len(out) else None
        if left is None and right is None:
            # 整首都没有一个有长度的字：只能从第一个字的起点顺排
            left = out[first].start
            right = left + max_char_seconds * count
        elif left is None:
            left = right - max_char_seconds * count
        elif right is None:
            right = left + max_char_seconds * count
        if right <= left:
            # 前后两个有长度的字本身就重叠/相接（不该出现）：给一段最小的槽位
            right = left + 0.1 * count
        step = min((right - left) / count, max_char_seconds)
        for offset in range(count):
            char = out[first + offset]
            char.start = left + step * offset
            char.end = left + step * (offset + 1)
        repaired += count
    return out, repaired


def vocal_phrases(vocals: Sequence[tuple[float, float]], *,
                  max_gap: float = VOCAL_PHRASE_GAP_SECONDS) -> list[tuple[float, float]]:
    """把音符跨度并成「人声句」：间隔 ≤ `max_gap` 的算同一句。"""
    phrases: list[tuple[float, float]] = []
    for start, end in sorted((float(a), float(b)) for a, b in vocals if b > a):
        if phrases and start - phrases[-1][1] <= max_gap:
            phrases[-1] = (phrases[-1][0], max(phrases[-1][1], end))
        else:
            phrases.append((start, end))
    return phrases


def _phrase_edge(phrases: Sequence[tuple[float, float]], point: float,
                 direction: int, max_gap: float) -> float | None:
    """边界 `point` 落在人声里（或贴着人声）时，返回那一句的边缘。"""
    for start, end in phrases:
        if start - 1e-6 <= point <= end + 1e-6:
            return start if direction < 0 else end
    best: float | None = None
    for start, end in phrases:
        gap = point - end if direction < 0 else start - point
        if -1e-6 <= gap <= max_gap:
            best = start if direction < 0 else end
    return best


def widen_to_vocals(sections: Sequence["Section"],
                    vocals: Sequence[tuple[float, float]], *,
                    max_extend: float = MAX_VOCAL_EXTEND_SECONDS,
                    max_gap: float = VOCAL_PHRASE_GAP_SECONDS) -> list["Section"]:
    """切片边界不要切在人声中间。

    典型病例：《爱情转移》`structure.lab` 把 0.01–23.87s 判成前奏，而人声 22.63s
    就进来了——切片正好切在第一句「徘徊过多少橱窗」里面，前 5 个字直接没机会被识别。
    尾奏同理（最后一句的拖音被切掉）。

    规则：**只在「敞口」的那一边挪**。两个相邻切片之间的边界由两份切片共同覆盖，
    切在里面不丢音频，不用动；只有挨着被跳过的器乐段（或全曲首尾）的那一侧，
    边界切进人声才会真的丢东西。

    往外挪的距离同时受三个约束：人声句的边缘、`max_extend`、以及不与相邻切片重叠。
    """
    if not sections or not vocals:
        return list(sections)
    phrases = vocal_phrases(vocals, max_gap=max_gap)
    out = [Section(row.start, row.end, row.label) for row in sections]
    for index, row in enumerate(out):
        open_start = index == 0 or sections[index - 1].end < row.start - 1e-6
        open_end = index == len(out) - 1 or sections[index + 1].start > row.end + 1e-6
        if open_start:
            edge = _phrase_edge(phrases, row.start, -1, max_gap)
            if edge is not None:
                floor = sections[index - 1].end if index else 0.0
                row.start = max(edge, row.start - max_extend, floor)
        if open_end:
            edge = _phrase_edge(phrases, row.end, 1, max_gap)
            if edge is not None:
                ceiling = sections[index + 1].start if index + 1 < len(out) else edge
                row.end = min(edge, row.end + max_extend, max(row.end, ceiling))
    # 两侧同时往同一段间奏里挪会撞上：在那里对半分
    for index in range(len(out) - 1):
        if out[index].end > out[index + 1].start + 1e-6:
            middle = (sections[index].end + sections[index + 1].start) / 2
            out[index].end = middle
            out[index + 1].start = middle
    return [row for row in out if row.end > row.start]


#: 给界面用的语言选项。**空串 = 自动**（不给 language，让模型自己判），
#: 其余是 Qwen3-ASR 的规范语言名。加语言只改这一行。
LANGUAGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("", "自动"),
    ("Chinese", "中文"),
    ("English", "英语"),
    ("Japanese", "日语"),
    ("Cantonese", "粤语"),
    ("Korean", "韩语"),
)

#: 界面上"自动"的各种写法（含 CLI 里常见的 auto）。
AUTO_LANGUAGE_ALIASES = frozenset({"", "auto", "自动", "none", "null"})


def normalize_language(value: str | None) -> str | None:
    """界面/CLI 的语言取值 -> 传给模型的参数。**返回 None 表示自动**。

    Qwen3-ASR 只在给了具体语言时才把 `language X<asr_text>` 塞进 prompt；不给就自己判
    （结果里的 `result.language` 就是它判出来的语言）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in AUTO_LANGUAGE_ALIASES:
        return None
    # 规范成 "Chinese" 这种首字母大写的写法（cHINese / chinese 都认）
    return text[:1].upper() + text[1:].lower()


def language_choices() -> list[dict[str, str]]:
    return [{"value": value, "label": label} for value, label in LANGUAGE_CHOICES]


@dataclass
class AsrConfig:
    asr_model: str | Path
    aligner_model: str | Path | None = None
    #: 用哪个对齐器出字的时间：`"tifa"`（默认）或 `"qwen"`（和 ASR 一次调用）。
    #: 选了 tifa 但权重不在时**自动退回 qwen**，只是多一条告警——不能因为缺个可选
    #: 模型就把整条歌词链路做死。
    aligner: str = "tifa"
    #: TIFA 权重（`model.pt`）。只在不空、且 `aligner="tifa"` 时用。
    tifa_model: str | Path | None = None
    tifa_precision: str = "16-mixed"
    language: str | None = "Chinese"
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    max_slice_seconds: float = DEFAULT_MAX_SLICE_SECONDS
    tokens_per_second: float = DEFAULT_TOKENS_PER_SECOND
    max_new_tokens_cap: int = 1024
    #: 可选的显存预算（accelerate 的 `max_memory`，形如 `"5GiB"`）。给了就走
    #: `device_map="auto"`：放不下的层留在**内存**里，前向时临时搬上卡。
    #: 实测 1.7B：预算 3GiB -> 峰值显存 5996->5039MiB、转写 5.6->10.5s；
    #: 预算 2GiB -> 3982MiB、18.4s；识别文本逐字一致。见 `scripts/probe_asr_offload.py`。
    max_vram: str | None = None
    max_ram: str = "96GiB"

    @property
    def uses_tifa(self) -> bool:
        return str(self.aligner or "qwen").strip().lower() == "tifa"

    @property
    def slice_limit(self) -> float:
        """切片上限。TIFA 的上下文约 1 分钟，超过就不保证稳定。"""
        if self.uses_tifa:
            from .tifa_align import MAX_ALIGN_SECONDS

            return min(self.max_slice_seconds, MAX_ALIGN_SECONDS)
        return self.max_slice_seconds


def asr_load_kwargs(config: AsrConfig, dtype: Any) -> dict[str, Any]:
    """把 `AsrConfig` 折成 `Qwen3ASRModel.from_pretrained` 的参数。

    `qwen_asr` 把不认识的关键字原样转给 `transformers.AutoModel.from_pretrained`，
    所以 accelerate 的 `device_map` / `max_memory` 可以直接用。

    注意 `max_memory` 是**每个模型各自**的上限（ASR 与对齐器是两次 `from_pretrained`）：
    预算必须小于单个模型的权重大小才会真的 offload，否则它会全部放在卡上。
    """
    kwargs: dict[str, Any] = {"dtype": dtype, "max_inference_batch_size": 1}
    if config.max_vram:
        memory = {0: config.max_vram, "cpu": config.max_ram}
        kwargs["device_map"] = "auto"
        kwargs["max_memory"] = memory
        if config.aligner_model:
            kwargs["forced_aligner"] = str(config.aligner_model)
            kwargs["forced_aligner_kwargs"] = {"dtype": dtype, "device_map": "auto",
                                               "max_memory": memory}
        return kwargs
    kwargs["device_map"] = config.device
    if config.aligner_model:
        kwargs["forced_aligner"] = str(config.aligner_model)
        kwargs["forced_aligner_kwargs"] = dict(dtype=dtype, device_map=config.device)
    return kwargs


@dataclass
class AsrRun:
    """一次歌词识别的结果与开销。"""

    text: str = ""
    chars: list[AlignedChar] = field(default_factory=list)
    language: str | None = None
    sections: list[dict[str, Any]] = field(default_factory=list)
    slices: int = 0
    elapsed_seconds: float = 0.0
    peak_gpu_mib: float = 0.0
    zero_length_chars: int = 0     # 对齐器给不出时间的字数（start == end，插值前）
    repaired_chars: int = 0        # 其中被「摊到空档里」补了位置的字数
    max_gap_seconds: float = 0.0   # 相邻字之间的最大空白（秒）
    max_gap_at: int = 0            # 最大空白在第几个字之前
    text_coverage: float = 1.0     # 拿到时间戳的字数 / 识别出的文本长度
    warnings: list[str] = field(default_factory=list)

    @property
    def zero_length_ratio(self) -> float:
        return self.zero_length_chars / len(self.chars) if self.chars else 0.0

    def as_dict(self) -> dict[str, Any]:
        return dict(text=self.text, language=self.language, slices=self.slices,
                    chars=len(self.chars), elapsed_seconds=round(self.elapsed_seconds, 2),
                    peak_gpu_mib=round(self.peak_gpu_mib, 1),
                    zero_length_chars=self.zero_length_chars,
                    repaired_chars=self.repaired_chars,
                    zero_length_ratio=round(self.zero_length_ratio, 4),
                    max_gap_seconds=round(self.max_gap_seconds, 3),
                    max_gap_at=self.max_gap_at,
                    text_coverage=round(self.text_coverage, 4),
                    sections=self.sections, warnings=self.warnings)


# --------------------------------------------------------------------- 区间
def parsed_sections(text: str) -> list[Section]:
    """`structure.lab` → 段落：标签规范化成中文，**相邻同名合并**。

    模型是按乐句给的标签（`verse verse chorus chorus`），所以"合并"这一步不是修饰而是必需：
    不合并的话一首歌会变成二三十个碎片段，卷帘上全是重复块，切片也会被切碎。
    """
    rows: list[Section] = []
    for line in (text or "").splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 2:
            continue
        try:
            start, end = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if end > start:
            rows.append(Section(start, end,
                                canonical_label(parts[2] if len(parts) > 2 else None)))
    rows.sort(key=lambda item: item.start)
    return [Section(start, end, label)
            for start, end, label in merge_consecutive(
                [(row.start, row.end, row.label) for row in rows])]


def sections_from_lab(text: str, *, max_seconds: float = DEFAULT_MAX_SLICE_SECONDS,
                      min_seconds: float = 8.0,
                      skip_instrumental: bool = False) -> list[Section]:
    """把 `structure.lab` 变成切片区间：合并同名相邻段 → （可选）跳过器乐段 → 打包 → 超长切开。

    段落边界是音乐上的自然切点，比按固定秒数硬切更不容易切在字中间。

    `skip_instrumental=True` 时先丢掉**前奏 / 间奏 / 尾奏**（一般不唱）再打包：既省时间也省
    显存。注意跳过必须在**打包之前**——打包会把不同标签的段并成一段（label 变 `None`），
    那时就再也分不出哪一段是器乐了。全是器乐段时返回空列表，由调用方决定怎么办。
    """
    rows = parsed_sections(text)
    if not rows:
        return []
    if skip_instrumental:
        rows = [row for row in rows if not is_instrumental(row.label)]
        if not rows:
            return []

    # 相邻的短段攒到一起，避免切出一堆几秒钟的碎片
    packed: list[Section] = []
    for row in rows:
        if packed and packed[-1].seconds + row.seconds <= max_seconds:
            packed[-1].end = row.end
            if packed[-1].label != row.label:
                packed[-1].label = None
        else:
            packed.append(Section(row.start, row.end, row.label))

    # 仍然超长的再按上限切开
    sliced: list[Section] = []
    for row in packed:
        if row.seconds <= max_seconds:
            sliced.append(row)
            continue
        count = int(row.seconds // max_seconds) + 1
        step = row.seconds / count
        for index in range(count):
            sliced.append(Section(row.start + index * step,
                                  min(row.end, row.start + (index + 1) * step), row.label))

    # 丢掉过短的尾巴
    return [s for s in sliced if s.seconds >= min_seconds] or sliced


# --------------------------------------------------------------------- 引擎
class AsrEngine:
    """懒加载的歌词识别引擎。用完记得 `release()` 把显存还回去。"""

    def __init__(self, config: AsrConfig) -> None:
        self.config = config
        self._model = None
        self._tifa = None
        self._workdir: Path | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> "AsrEngine":
        if self._model is not None:
            return self
        import torch
        from qwen_asr import Qwen3ASRModel

        dtype = {"bfloat16": torch.bfloat16, "float16": torch.float16,
                 "float32": torch.float32}[self.config.dtype]
        kwargs = asr_load_kwargs(self.config, dtype)
        self._model = Qwen3ASRModel.from_pretrained(str(self.config.asr_model), **kwargs)
        return self

    def release(self) -> None:
        self._model = None
        if self._tifa is not None:
            self._tifa.release()
            self._tifa = None
        import gc

        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:
            pass

    # ------------------------------------------------------------------ 切片
    def _extract(self, audio: str | Path, section: Section, target: Path, *,
                 tifa: bool = False) -> Path:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("找不到 ffmpeg，无法切片")
        target.parent.mkdir(parents=True, exist_ok=True)
        # TIFA 的特征是 48k / mel-80（fmax 8k），切到 16k 会先把高频削掉再插值回去
        rate = 48000 if tifa else 16000
        command = [ffmpeg, "-v", "error", "-y", "-ss", f"{section.start:.3f}",
                   "-to", f"{section.end:.3f}", "-i", str(audio),
                   "-ac", "1", "-ar", str(rate), str(target)]
        result = subprocess.run(command, capture_output=True, timeout=300, check=False)
        if result.returncode:
            raise RuntimeError(f"切片失败：{result.stderr.decode(errors='replace')[-300:]}")
        return target

    def _tifa_ready(self) -> tuple[bool, str]:
        """TIFA 权重在不在。缺了就退回 Qwen，别让整个歌词阶段失败。"""
        from .tifa_align import default_model_path

        path = Path(self.config.tifa_model) if self.config.tifa_model else default_model_path()
        if path.is_file():
            return True, ""
        return False, (f"没找到 TIFA 权重（{path}），这次退回 Qwen 对齐器；"
                       f"要稳定得多，建议把 models/TIFA-1.0 放进来")

    def _align_with_tifa(self, pending: Sequence[tuple[Path, str, float]],
                         run: AsrRun, requested: str | None) -> list[AlignedChar]:
        """把攒下来的切片交给 TIFA，一次对齐完，时间换回整首曲的时间轴。"""
        from .tifa_align import TifaAligner, guess_language, tifa_language

        # 用户选的语言优先；但**文字**能推翻它：ASR 会把日文歌判成 Chinese，
        # 界面上的「自动」也会被 config 的默认值顶成 Chinese。假名一出现就该走 ja。
        joined = "".join(text for _clip, text, _offset in pending)
        language = tifa_language(requested or "zh")
        script = guess_language(joined)
        if script == "ja" and language == "zh":
            language = "ja"
        if self._tifa is None:
            self._tifa = TifaAligner(self.config.tifa_model, language=language,
                                     precision=self.config.tifa_precision)
        else:
            self._tifa.language = language
        charsets = self._tifa.align_many([(clip, text) for clip, text, _ in pending])
        chars: list[AlignedChar] = []
        for (clip, text, offset), spans in zip(pending, charsets):
            if not spans:
                run.warnings.append(f"切片 {offset:.1f}s 起的这段对齐失败（TIFA 没给出边界）")
                continue
            labels = self._labels_for(text, spans, language)
            for index, (item_text, start, end) in enumerate(spans):
                label = labels[index] if labels else item_text
                chars.append(AlignedChar(label, offset + start, offset + end))
        return chars

    @staticmethod
    def _labels_for(text: str, spans: Sequence, language: str) -> list[str]:
        """用**我们自己的**文本当歌词，TIFA 只提供边界。

        TIFA 的 G2P 前置会 `lowercase` + 去标点，英文词会被改成小写；数量对不上
        （罕见：它可能把缩写拆开）时就退回它给的字面。
        """
        units = text.split() if language == "en" else list(text)
        return units if len(units) == len(spans) else []

    def transcribe_sections(
        self,
        audio: str | Path,
        sections: Sequence[Section],
        *,
        progress: Any = None,
        on_slice: Any = None,
        language: str | None = None,
        workdir: str | Path | None = None,
    ) -> AsrRun:
        """按区间逐段识别，把每段的时间戳换算回整首曲的时间轴。

        ``on_slice(run, done, total)`` 在**每段结束后**回调，拿到的是已经累积的 run。
        WebUI 用它把"已经识别出来的歌词行"实时推给界面（模型逐段出结果，用户不必等全曲）。

        ``language`` 可以按次覆盖配置里的语言；``None`` 表示**自动**（让模型自己判）。
        不传则用 `config.language`。
        """
        if not sections:
            return AsrRun(warnings=["没有可用的区间"])
        self.load()
        import torch

        wanted_language = (normalize_language(self.config.language)
                           if language is None else normalize_language(language))
        run = AsrRun(sections=[s.as_dict() for s in sections])
        started = time.monotonic()
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()

        work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="sheetsage-asr-"))
        work.mkdir(parents=True, exist_ok=True)
        self._workdir = work

        tifa_mode = self.config.uses_tifa
        if tifa_mode:
            ready, reason = self._tifa_ready()
            if not ready:
                # 缺可选模型不能把整条链路做死：退回 Qwen，并让用户在详情里看到原因
                run.warnings.append(reason)
                tifa_mode = False
        #: TIFA 要整首一起对齐：先把切片留在盘上，最后一次性过模型
        pending: list[tuple[Path, str, float]] = []

        for index, section in enumerate(sections):
            if progress:
                progress({"stage": "asr", "index": index + 1, "total": len(sections),
                          "start": section.start, "end": section.end})
            clip = self._extract(audio, section, work / f"slice-{index:03d}.wav",
                                 tifa=tifa_mode)
            want_timestamps = self.config.aligner_model is not None and not tifa_mode
            tokens = int(min(self.config.max_new_tokens_cap,
                             max(64, section.seconds * self.config.tokens_per_second)))
            # 两次尝试（带/不带 max_new_tokens）都要被同一个 except 兜住：
            # 把重试写在 `except TypeError:` 里面的话，**重试自己抛的异常会逃出去**，
            # 变成整个任务的失败，而不是"这一段没识别出来"的告警。实测踩过：
            # 空切片会让第二次调用抛 RuntimeError，直接从 transcribe_sections 冒出去。
            try:
                try:
                    results = self._model.transcribe(
                        audio=str(clip), language=wanted_language,
                        return_time_stamps=want_timestamps, max_new_tokens=tokens)
                except TypeError:
                    # 某些版本不接受 max_new_tokens
                    results = self._model.transcribe(
                        audio=str(clip), language=wanted_language,
                        return_time_stamps=want_timestamps)
            except Exception as exc:
                run.warnings.append(
                    f"切片 {section.start:.1f}-{section.end:.1f}s 识别失败："
                    f"{type(exc).__name__}: {str(exc)[:160]}")
                continue

            result = results[0] if isinstance(results, (list, tuple)) else results
            text = (getattr(result, "text", "") or "").strip()
            run.language = run.language or getattr(result, "language", None)
            run.slices += 1
            if text:
                run.text += text
            stamps = getattr(result, "time_stamps", None)
            if tifa_mode:
                # 时间留到最后统一对齐；先把切片和文本攒着（别删）
                if text:
                    pending.append((clip, text, section.start))
                else:
                    clip.unlink(missing_ok=True)
            else:
                if stamps:
                    for item in stamps:
                        run.chars.append(AlignedChar(
                            text=str(getattr(item, "text", "")),
                            start=section.start + float(getattr(item, "start_time", 0.0)),
                            end=section.start + float(getattr(item, "end_time", 0.0)),
                        ))
                clip.unlink(missing_ok=True)
            if on_slice is not None:
                on_slice(run, index + 1, len(sections))

        if tifa_mode and pending:
            run.chars.extend(self._align_with_tifa(pending, run, wanted_language))
            for clip, _text, _offset in pending:
                clip.unlink(missing_ok=True)

        run.elapsed_seconds = time.monotonic() - started
        if torch.cuda.is_available():
            run.peak_gpu_mib = torch.cuda.max_memory_allocated() / 1024**2
        run.zero_length_chars = sum(1 for c in run.chars if c.end <= c.start + 1e-9)
        if run.zero_length_chars:
            # 对齐器给不出时间 ≠ 这个字不存在。先按前后空档把它摊开，再交给填词：
            # 填词拿到的不再是「一堆挤在同一瞬间的字」，而是单调、不重叠、有长度的时间。
            run.chars, run.repaired_chars = interpolate_degenerate(run.chars)
        if run.zero_length_ratio > 0.1:
            # 实测《光芒》有 20% 的字拿不到时间。这不影响"识别出什么词"，
            # 但会直接影响填词——这些字没有自己的边界，只能按前后空档估位置。
            run.warnings.append(
                f"{run.zero_length_chars}/{len(run.chars)} 个字没有可用时间戳"
                f"（{run.zero_length_ratio:.0%}），已按前后空档均分补位置"
                f"（这类位置是估的，不是测的）")
        # 两种对齐质量信号，都**只报告、不判定**（报告进 lyrics.json / summary）：
        #   1. 唱词中间的最大空白。前奏/间奏/长句之间本来就有空白，没法用统一阈值区分
        #      "正常停顿"和"对齐器漏了音频"——实测《相思》慢歌有 19s 的正常停顿，
        #      《皮囊》则出现 37.9s 的异常跳变（第一个字 10.4s、第二个字 48.4s）。
        #   2. 拿到时间戳的字数 / 识别出的文本长度。
        # 需要判断"这份时间戳能不能用"时，把这两个数和填词指标一起看（见
        # `scripts/check_pipeline_contract.py`）。
        run.max_gap_seconds, gap_at = _max_char_gap(run.chars)
        run.max_gap_at = gap_at
        text_chars = len("".join((run.text or "").split()))
        run.text_coverage = len(run.chars) / text_chars if text_chars else 1.0
        if run.max_gap_seconds > MAX_CHAR_GAP_SECONDS and gap_at > 1:
            run.warnings.append(
                f"唱词中间有 {run.max_gap_seconds:.1f}s 的空白（第 {gap_at + 1} 个字之前）："
                f"慢歌的长句停顿是正常的，但这么大的跳变也可能是对齐器漏了一段音频，"
                f"核对填词结果时留意")
        if not run.chars and (self.config.aligner_model or tifa_mode):
            run.warnings.append("没有拿到任何时间戳")
        return run
