"""秒 ↔ tick 的时间模型。

**核心约定：BPM 不参与时间换算。**

唯一的时间真源是小节表。每个小节同时给出：

- 秒：`start` / `end`（来自模型自己推导的节拍网格）
- 乐谱位置：`score_start` / `score_end`（单位是全音符）

于是每个小节贡献一段线性映射，小节之间严丝合缝（四首样例实测零缝隙、覆盖 = 时长）。
秒 ↔ tick 就是这张分段线性表的正反查，**恒速曲、变速曲、弱起小节走同一套代码**。

BPM 因此被降级成纯元数据：用来显示、以及给 MIDI 写 `set_tempo`。它算错与否都不影响
出谱的正确性——这正是"用户不用调 BPM"的前提。

两种小节表来源：

1. `playback.json.measures` —— 模型自己生成的，首选。
2. `beat.lab` 的强拍 —— 兜底。模型在"节拍不足两个"或"没识别出调性"时**根本不生成
   `score.abc`**，`build_playback` 里小节表写成 `[]`，此时没有第一条来源可用。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

PPQ = 1920
#: 一个全音符对应的 tick 数。
WHOLE_NOTES = 4
WHOLE_TICKS = WHOLE_NOTES * PPQ

#: 弱起小节的 tick 处理方式。
#: - "actual"：按实际长度（弱起是几拍就占几个 tick），速度与主段一致（默认）
#: - "padded"：保持模型记谱，弱起补成完整小节
PICKUP_MODES = ("actual", "padded")


@dataclass
class Measure:
    """一个小节：秒跨度 + tick 跨度 + 该小节的速度。"""

    index: int
    start: float
    end: float
    tick_start: int
    tick_end: int
    numerator: int = 4
    denominator: int = 4
    leading_rest: float = 0.0
    trailing_rest: float = 0.0
    padded_ticks: int = 0            # 模型记谱的原始 tick 跨度（含补全）
    is_pickup: bool = False

    @property
    def seconds(self) -> float:
        return self.end - self.start

    @property
    def ticks(self) -> int:
        return self.tick_end - self.tick_start

    @property
    def bpm(self) -> float:
        """该小节的四分音符速度。短小节给出很大的值，这是正常的。"""
        if self.seconds <= 0 or self.ticks <= 0:
            return 0.0
        quarter_notes = self.ticks / PPQ
        return 60.0 * quarter_notes / self.seconds

    def as_dict(self) -> dict[str, Any]:
        return dict(index=self.index, start=round(self.start, 4), end=round(self.end, 4),
                    tick_start=self.tick_start, tick_end=self.tick_end,
                    numerator=self.numerator, denominator=self.denominator,
                    leading_rest=self.leading_rest, trailing_rest=self.trailing_rest,
                    padded_ticks=self.padded_ticks, is_pickup=self.is_pickup,
                    bpm=round(self.bpm, 3))


@dataclass
class Timeline:
    """整首曲的时间映射。"""

    measures: list[Measure] = field(default_factory=list)
    ppq: int = PPQ
    source: str = "playback"        # playback | beats
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 查询
    @property
    def _starts(self) -> list[float]:
        return [m.start for m in self.measures]

    def measure_at(self, seconds: float) -> Measure | None:
        if not self.measures:
            return None
        index = bisect.bisect_right(self._starts, seconds) - 1
        index = max(0, min(index, len(self.measures) - 1))
        return self.measures[index]

    def sec_to_tick(self, seconds: float) -> float:
        """秒 → tick（浮点，调用方自行取整）。越界按最近的小节外推。"""
        if not self.measures:
            raise ValueError("小节表为空，无法换算")
        measure = self.measure_at(seconds)
        span = measure.seconds
        if span <= 0:
            return float(measure.tick_start)
        ratio = (seconds - measure.start) / span
        return measure.tick_start + ratio * measure.ticks

    def tick_to_sec(self, tick: float) -> float:
        if not self.measures:
            raise ValueError("小节表为空，无法换算")
        for measure in self.measures:
            if measure.tick_start <= tick <= measure.tick_end and measure.ticks > 0:
                ratio = (tick - measure.tick_start) / measure.ticks
                return measure.start + ratio * measure.seconds
        last = self.measures[-1]
        if tick > last.tick_end and last.ticks > 0:
            ratio = (tick - last.tick_end) / last.ticks
            return last.end + ratio * last.seconds
        return self.measures[0].start

    # ------------------------------------------------------------ 派生
    @property
    def tick_length(self) -> int:
        return self.measures[-1].tick_end if self.measures else 0

    def tempo_map(self) -> list[dict[str, Any]]:
        """逐小节的速度表，可直接写成 MIDI 的 set_tempo。"""
        return [{"tick": m.tick_start, "bpm": round(m.bpm, 4),
                 "numerator": m.numerator, "denominator": m.denominator}
                for m in self.measures if m.bpm > 0]

    def pickup_seconds(self) -> float:
        for measure in self.measures:
            if measure.is_pickup:
                return round(measure.seconds, 4)
        return 0.0

    def as_dict(self) -> dict[str, Any]:
        return dict(ppq=self.ppq, source=self.source, measures=len(self.measures),
                    tick_length=self.tick_length, pickup_seconds=self.pickup_seconds(),
                    warnings=self.warnings)


# --------------------------------------------------------------------- 构建
def _meter_of(numerator: int, denominator: int) -> float:
    """一小节的乐谱长度（单位：全音符）。"""
    return numerator / denominator


def from_playback(
    playback: Mapping[str, Any],
    *,
    meter: tuple[int, int] = (4, 4),
    pickup: str = "actual",
) -> Timeline:
    """用模型自己的 `playback.json` 小节表构建时间映射。

    `playback.json` 本身不带拍号（拍号在 `beat.lab` / `score.abc` 里），所以由调用方
    把 `tempo.estimate_meter()` 的结果传进来；弱起小节的拍号再按它自己的实际长度推断。
    """
    if pickup not in PICKUP_MODES:
        raise ValueError(f"pickup 只能是 {PICKUP_MODES}")

    rows = list(playback.get("measures") or [])
    timeline = Timeline(source="playback")
    if not rows:
        timeline.warnings.append("playback.json 没有小节表（模型可能未生成 ABC）")
        return timeline

    numerator, denominator = meter
    cursor = 0
    for position, row in enumerate(rows):
        score_start = float(row.get("score_start") or 0.0)
        score_end = float(row.get("score_end") or 0.0)
        leading = float(row.get("leading_rest") or 0.0)
        trailing = float(row.get("trailing_rest") or 0.0)
        notated = score_end - score_start                       # 模型记谱的小节长度
        actual = notated - leading - trailing                   # 去掉补全后的实际长度
        padded_ticks = int(round(notated * WHOLE_TICKS))
        actual_ticks = int(round(actual * WHOLE_TICKS))
        if actual_ticks <= 0:
            actual_ticks = padded_ticks

        # 弱起：模型记谱把它补成完整小节（`z24z4a4` 这种），实际只有 actual 那么多
        is_pickup = position == 0 and padded_ticks > 0 and actual_ticks < padded_ticks
        ticks = actual_ticks if (is_pickup and pickup == "actual") else padded_ticks

        # 弱起小节用自己的实际长度做拍号，这样报告里能显示"1/8 弱起"
        if is_pickup:
            guess = _guess_meter(actual)
            row_meter = guess or (numerator, denominator)
        else:
            row_meter = (numerator, denominator)

        timeline.measures.append(Measure(
            index=position,
            start=float(row.get("start") or 0.0),
            end=float(row.get("end") or 0.0),
            tick_start=cursor,
            tick_end=cursor + ticks,
            numerator=row_meter[0],
            denominator=row_meter[1],
            leading_rest=leading,
            trailing_rest=trailing,
            padded_ticks=padded_ticks,
            is_pickup=is_pickup,
        ))
        cursor += ticks

    if pickup == "actual":
        for measure in timeline.measures:
            if measure.is_pickup:
                timeline.warnings.append(
                    f"弱起小节按实际长度处理：{measure.padded_ticks} → {measure.ticks} tick"
                    f"（{measure.numerator}/{measure.denominator}）")
    return timeline


_COMMON_METERS = ((4, 4), (3, 4), (2, 4), (6, 8), (12, 8), (9, 8), (5, 4), (7, 8), (2, 2), (3, 8), (1, 8))


def _guess_meter(score_length: float) -> tuple[int, int] | None:
    """由小节的全音符长度反推拍号（小节表本身不带拍号）。"""
    if score_length <= 0:
        return None
    best, best_error = None, None
    for numerator, denominator in _COMMON_METERS:
        error = abs(_meter_of(numerator, denominator) - score_length)
        if best_error is None or error < best_error:
            best, best_error = (numerator, denominator), error
    return best if best_error is not None and best_error < 1e-6 else None


def from_stored_measures(rows: Sequence[Mapping[str, Any]], *,
                         ppq: int = PPQ) -> Timeline:
    """从 `project.json` 里已经存好的小节表恢复时间映射。

    必须直接用存下来的 `tick_start` / `tick_end`，**不能**像 `from_playback` 那样
    由 `score_*` 重算——弱起小节压缩过（7680 → 960 tick），重算会把它又展开回去。
    """
    timeline = Timeline(source="stored", ppq=ppq)
    for index, row in enumerate(rows):
        tick_start = int(row.get("tick_start", 0))
        tick_end = int(row.get("tick_end", tick_start))
        timeline.measures.append(Measure(
            index=int(row.get("index", index)),
            start=float(row.get("start") or 0.0),
            end=float(row.get("end") or 0.0),
            tick_start=tick_start,
            tick_end=tick_end,
            numerator=int(row.get("numerator", 4)),
            denominator=int(row.get("denominator", 4)),
            leading_rest=float(row.get("leading_rest") or 0.0),
            trailing_rest=float(row.get("trailing_rest") or 0.0),
            padded_ticks=int(row.get("padded_ticks", tick_end - tick_start)),
            is_pickup=bool(row.get("is_pickup", False)),
        ))
    if not timeline.measures:
        timeline.warnings.append("存下来的小节表是空的")
    return timeline


def from_beats(
    beat_rows: Sequence[Sequence[Any]],
    duration: float,
    *,
    meter: tuple[int, int] = (4, 4),
    pickup: str = "actual",
) -> Timeline:
    """没有 `playback.json` 时的兜底：用 beat.lab 的强拍构造小节表。

    `beat.lab` 的第 2 列是"小节内第几拍"（1 起），所以 `position == 1` 的拍点就是强拍。
    """
    downbeats: list[float] = []
    for row in beat_rows:
        if len(row) < 2:
            continue
        try:
            position = float(row[1])
            time = float(row[0])
        except (ValueError, TypeError):
            continue
        if position == 1:
            downbeats.append(time)
    downbeats = sorted(set(downbeats))

    timeline = Timeline(source="beats")
    if len(downbeats) < 2:
        timeline.warnings.append("强拍少于两个，无法构造小节表")
        return timeline

    numerator, denominator = meter
    cursor = 0
    first = downbeats[0]
    if first > 1e-6:
        # 开头到第一个强拍之间是弱起
        ticks = int(round(_meter_of(numerator, denominator) * WHOLE_TICKS))
        pickup_ticks = ticks
        if pickup == "actual":
            # 按实际时长折算成音符时值，而不是塞一个完整小节
            span = downbeats[1] - downbeats[0]
            if span > 0:
                pickup_ticks = max(1, int(round(ticks * (first / span))))
        timeline.measures.append(Measure(
            index=0, start=0.0, end=first, tick_start=0, tick_end=pickup_ticks,
            numerator=numerator, denominator=denominator,
            leading_rest=0.0, trailing_rest=0.0,
            padded_ticks=ticks, is_pickup=True))
        cursor = pickup_ticks
        timeline.warnings.append(f"用强拍兜底：开头 {first:.3f}s 视为弱起")

    ticks_per_measure = int(round(_meter_of(numerator, denominator) * WHOLE_TICKS))
    for index, start in enumerate(downbeats):
        end = downbeats[index + 1] if index + 1 < len(downbeats) else max(duration, start)
        if end <= start:
            continue
        timeline.measures.append(Measure(
            index=len(timeline.measures), start=start, end=end,
            tick_start=cursor, tick_end=cursor + ticks_per_measure,
            numerator=numerator, denominator=denominator,
            padded_ticks=ticks_per_measure))
        cursor += ticks_per_measure
    return timeline


# --------------------------------------------------------------------- 音符
def notes_to_ticks(
    notes: Iterable[Sequence[float]],
    timeline: Timeline,
    *,
    min_ticks: int = 1,
) -> list[dict[str, Any]]:
    """把 `melody_*.lab` 的秒制音符搬进 tick 域。

    notes 每行是 `start, end, pitch[, track]`。
    """
    result: list[dict[str, Any]] = []
    for row in notes:
        if len(row) < 3:
            continue
        start, end, pitch = float(row[0]), float(row[1]), int(float(row[2]))
        track = int(float(row[3])) if len(row) > 3 else 0
        tick = int(round(timeline.sec_to_tick(start)))
        end_tick = int(round(timeline.sec_to_tick(end)))
        duration = max(min_ticks, end_tick - tick)
        result.append(dict(tick=tick, dur=duration, pitch=pitch, track=track,
                           start_sec=round(start, 4), end_sec=round(end, 4)))
    result.sort(key=lambda note: (note["tick"], note["pitch"]))
    return result


def round_trip_error(timeline: Timeline, times: Iterable[float]) -> float:
    """秒 → tick → 秒 的最大误差，用来证明映射是无损的。"""
    worst = 0.0
    for value in times:
        tick = timeline.sec_to_tick(value)
        worst = max(worst, abs(timeline.tick_to_sec(tick) - value))
    return worst
