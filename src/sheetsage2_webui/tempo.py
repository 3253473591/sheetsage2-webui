"""速度（BPM）的估计与干预。

`beat.lab` 里的时间戳只有 0.01s 精度，所以**不能**用"中位拍间隔"反推速度：
真值 0.576923s 会被存成 0.58s，于是 104 BPM 被读成 103.45。这里改为在整条
节拍网格上做最小二乘拟合，量化噪声互相抵消。

同时模型在歌曲结尾（淡出、散拍）常常丢掉节拍栅格，这些离群点会污染全局拟合，
所以先找**最长的稳定段**再拟合，并如实报告稳定段覆盖率。

三层能力
--------
1. 估计：`estimate_tempo()` —— 鲁棒估计，附带稳定段与残差
2. 吸附：`snap_bpm()` —— 把估计值对齐到整数 / 半整数 / 指定候选值
3. 干预：`compare_to_constant()` / `retime()` —— 用恒定速度网格替换模型网格，
   量化漂移，并可据此重排音符时间
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: 判定"拍间隔仍然属于同一速度"的相对容差。
INLIER_TOLERANCE = 0.05
#: 绝对容差下限，避免极快曲子把容差压得过小。
INLIER_MIN_TOLERANCE = 0.015
#: 一个速度段里"拍间隔落在拟合拍长 ±5% 内"的比例下限。
#: 残差只反映"能不能被一条直线穿过"，对缓慢起伏的速度很宽松；间隔一致性才能
#: 识别出"这不是恒速区，只是同一次自由速度被切成了几段"。
MIN_INTERVAL_CONSISTENCY = 0.6
#: 不稳定区占全曲拍数的比例低于此值时，可以并进相邻段（太短，不值得单独报告）。
SMALL_UNSTABLE_FRACTION = 0.15
#: 高于此值时绝不并进相邻段，而是自己作为"网格不可靠"的一段报出来。
#: `壁上观` 的 71.5–249.5s 占全曲 69% 的拍点，模型在这一段没锁住速度——
#: 把它并进前面的 90 BPM 段会造出"0–249.5s 都是 90"这种假结论。
LARGE_UNSTABLE_FRACTION = 0.25
#: 滑窗速度的标准差超过速度本身的这个比例时，这段的 BPM 只能当粗略值用。
BPM_UNCERTAIN_FRACTION = 0.02


@dataclass
class TempoEstimate:
    """一次速度估计的完整结果，故意把所有中间量都摊开，便于判断可信度。"""

    method: str
    beats_total: int
    meter: str | None = None
    denominator: int | None = None
    beat_unit_bpm: float | None = None      # 按 beat.lab 的拍单位计
    bpm: float | None = None                # 归一化到四分音符
    bpm_median_interval: float | None = None  # 朴素中位间隔法，保留作对照
    bpm_dominant: float | None = None       # 众数拍间隔给出的"主速度"
    tempo_range: dict[str, float] | None = None   # 局部速度的 min/max/median
    local_tempo: list[dict[str, float]] = field(default_factory=list)  # 局部速度曲线
    anchor: float | None = None             # 拟合出的第 0 拍位置
    period_seconds: float | None = None     # 拍单位时长（秒）
    regular_beats: int = 0                  # 落在最长稳定段里的拍数
    regular_start: float | None = None
    regular_end: float | None = None
    regular_coverage: float = 0.0           # 稳定段拍数 / 总拍数
    residual_std: float | None = None
    residual_max: float | None = None
    is_constant: bool = False               # 是否可当作恒速曲处理
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        for key in ("beat_unit_bpm", "bpm", "bpm_median_interval", "bpm_dominant",
                    "anchor", "period_seconds", "regular_start", "regular_end",
                    "regular_coverage", "residual_std", "residual_max"):
            value = data.get(key)
            if isinstance(value, float):
                data[key] = round(value, 4)
        return data


def _least_squares_period(times: Sequence[float]) -> tuple[float, float]:
    """对 t_i = anchor + i·T 做最小二乘，返回 (T, anchor)。"""
    n = len(times)
    if n < 2:
        raise ValueError("至少需要两个拍点")
    mean_i = (n - 1) / 2
    mean_t = sum(times) / n
    numerator = sum((i - mean_i) * (t - mean_t) for i, t in enumerate(times))
    denominator = sum((i - mean_i) ** 2 for i in range(n))
    if denominator == 0:
        raise ValueError("拍点数量不足")
    period = numerator / denominator
    if period <= 0:
        raise ValueError("拟合出的拍长非正")
    return period, mean_t - period * mean_i


def _longest_run(flags: Sequence[bool]) -> tuple[int, int]:
    """返回 True 的最长连续区间 [start, end)（下标）。"""
    best = (0, 0)
    start = None
    for index, flag in enumerate(list(flags) + [False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if index - start > best[1] - best[0]:
                best = (start, index)
            start = None
    return best


def _dominant_interval(intervals: Sequence[float]) -> float:
    """拍间隔的众数（按 1ms 取整），比中位数更抗离群。"""
    if not intervals:
        raise ValueError("没有可用的拍间隔")
    counts: dict[int, int] = {}
    for value in intervals:
        counts[round(value * 1000)] = counts.get(round(value * 1000), 0) + 1
    best = max(counts.items(), key=lambda kv: (kv[1], -abs(kv[0])))
    return best[0] / 1000.0


def local_tempo(times: Sequence[float], window: int = 32, denominator: int = 4) -> list[dict[str, float]]:
    """用滑动窗口给出局部速度曲线，用来判断"这首歌是不是真的恒速"。

    恒速曲的曲线是一条直线；`萧敬腾 - 皮囊` 这种段落速度不同的曲子会明显起伏。
    与全局估计一致，速度按拍号分母归一化到四分音符。
    """
    ordered = sorted(float(t) for t in times)
    if window < 2 or len(ordered) < window or denominator <= 0:
        return []
    scale = 4.0 / denominator
    step = max(1, window // 2)
    curve: list[dict[str, float]] = []
    for start in range(0, len(ordered) - window + 1, step):
        segment = ordered[start:start + window]
        span = segment[-1] - segment[0]
        if span <= 0:
            continue
        curve.append({"time": round(segment[0], 3),
                      "bpm": round(60.0 * (len(segment) - 1) / span * scale, 3)})
    return curve


def tempo_summary(curve: Sequence[dict[str, float]]) -> dict[str, float] | None:
    """把局部速度曲线压成 min / max / median，便于放进摘要。"""
    values = [point["bpm"] for point in curve]
    if not values:
        return None
    return {"min": round(min(values), 2), "max": round(max(values), 2),
            "median": round(statistics.median(values), 2)}


def estimate_tempo(rows: Sequence[Sequence[Any]], method: str = "robust") -> TempoEstimate:
    """从 `beat.lab` 的行推出速度。

    rows 的字段是 ``time, position, numerator, denominator``，后三项可以缺。
    """
    times: list[float] = []
    for row in rows:
        try:
            times.append(float(row[0]))
        except (ValueError, IndexError, TypeError):
            continue

    times = sorted(t for t in times)
    estimate = TempoEstimate(method=method, beats_total=len(times))
    # 拍号只有一处真源：estimate_meter()（取最长的一个拍号段，而不是简单众数）
    meter_info = estimate_meter(rows)
    estimate.meter = meter_info.primary
    estimate.denominator = meter_info.denominator

    if len(times) < 2:
        estimate.warnings.append("拍点少于两个，无法估计速度")
        return estimate

    intervals = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not intervals:
        estimate.warnings.append("所有拍点在时间上重合，无法估计速度")
        return estimate

    median_interval = statistics.median(intervals)
    median_unit_bpm = 60.0 / median_interval if median_interval > 0 else None
    dominant_interval = _dominant_interval(intervals)

    if method == "median":
        period, anchor = median_interval, times[0]
        run = (0, len(times))
        warnings = []
    else:
        tolerance = max(INLIER_MIN_TOLERANCE, INLIER_TOLERANCE * dominant_interval)
        flags = [abs(value - dominant_interval) <= tolerance for value in intervals]
        start, end = _longest_run(flags)
        if end - start < 2:
            # 找不到稳定段，退回中位数，并明确标注不可信
            period, anchor = median_interval, times[0]
            run = (0, len(times))
            warnings = ["找不到稳定的拍间隔段，速度估计不可信"]
        else:
            # 稳定段 [start, end) 覆盖拍点 start..end（含末端）
            segment = times[start:end + 1]
            period, anchor = _least_squares_period(segment)
            anchor -= period * start          # 换算回整条网格的第 0 拍
            run = (start, end + 1)
            warnings = []

    estimate.period_seconds = period
    estimate.anchor = anchor
    estimate.beat_unit_bpm = round(60.0 / period, 3)
    if estimate.denominator:
        estimate.bpm = round(estimate.beat_unit_bpm * 4.0 / estimate.denominator, 3)
        # 众数间隔给出的"主速度"：变速曲里它代表占主导的那个段落速度
        estimate.bpm_dominant = round(
            60.0 / dominant_interval * 4.0 / estimate.denominator, 3)
        if median_unit_bpm:
            # 朴素方法也按同样的分母归一化，两种方法才可比
            estimate.bpm_median_interval = round(
                median_unit_bpm * 4.0 / estimate.denominator, 3)
    else:
        estimate.bpm_dominant = round(60.0 / dominant_interval, 3)
        if median_unit_bpm:
            estimate.bpm_median_interval = round(median_unit_bpm, 3)

    start, end = run
    estimate.regular_beats = end - start
    if end > start:
        estimate.regular_start = round(times[start], 3)
        estimate.regular_end = round(times[end - 1], 3)
    estimate.regular_coverage = round(estimate.regular_beats / len(times), 4)

    residuals = [times[i] - (anchor + period * i) for i in range(start, end)]
    if residuals:
        estimate.residual_std = statistics.pstdev(residuals)
        estimate.residual_max = max(abs(r) for r in residuals)

    # 局部速度曲线：恒速曲是直线，变速曲会明显起伏
    curve = local_tempo(times, denominator=estimate.denominator or 4)
    estimate.local_tempo = curve
    estimate.tempo_range = tempo_summary(curve)

    # 恒速判定：稳定段覆盖绝大多数拍点，且抖动远小于一拍。
    # 用 std 而不是 max——个别拍点偏 50ms 不影响"这首歌是恒速"的判断。
    estimate.is_constant = (
        estimate.regular_coverage >= 0.9
        and estimate.residual_std is not None
        and estimate.residual_std < period * 0.05
    )
    if not estimate.is_constant:
        span = ""
        if estimate.tempo_range:
            span = (f"，局部速度在 {estimate.tempo_range['min']}–"
                    f"{estimate.tempo_range['max']} BPM 之间变化")
        estimate.warnings.append(
            f"稳定段仅覆盖 {estimate.regular_coverage:.0%} 的拍点{span}；"
            f"整首曲不是单一速度，任何单一 BPM 都只是摘要")
    estimate.warnings.extend(warnings)
    return estimate


# --------------------------------------------------------------------- 吸附
def snap_bpm(bpm: float | None, mode: str = "none", candidates: Iterable[float] | None = None) -> float | None:
    """把估计出的速度吸附到更"像谱面"的值。

    mode: none | integer（取整）| half（半整数）| candidates（取最近的候选值）
    """
    if bpm is None:
        return None
    if mode == "none":
        return round(bpm, 3)
    if mode == "integer":
        return float(round(bpm))
    if mode == "half":
        return round(bpm * 2) / 2
    if mode == "candidates":
        options = [float(c) for c in (candidates or [])]
        if not options:
            raise ValueError("mode='candidates' 需要提供候选速度")
        return min(options, key=lambda value: abs(value - bpm))
    raise ValueError(f"未知的吸附模式：{mode}")


# --------------------------------------------------------------------- 干预
def constant_beat_times(anchor: float, bpm: float, denominator: int, count: int) -> list[float]:
    """生成恒定速度的拍网格（bpm 指四分音符速度）。"""
    if bpm <= 0 or denominator <= 0 or count <= 0:
        raise ValueError("参数必须为正")
    period = 240.0 / (bpm * denominator)
    return [anchor + index * period for index in range(count)]


def compare_to_constant(
    beat_times: Sequence[float],
    anchor: float,
    bpm: float,
    denominator: int = 4,
) -> dict[str, Any]:
    """把模型的拍网格和"恒定速度网格"对齐，量化漂移有多大。

    这是决定"要不要干预"的依据：平均/最大漂移越小，越有把握把速度钉成常数。
    """
    times = sorted(float(t) for t in beat_times)
    if len(times) < 2:
        return {"beats": len(times), "error": "拍点不足"}
    grid = constant_beat_times(anchor, bpm, denominator, len(times))
    drifts = [actual - ideal for actual, ideal in zip(times, grid)]
    absolute = sorted(abs(d) for d in drifts)
    return {
        "beats": len(times),
        "bpm": bpm,
        "denominator": denominator,
        "drift_max": round(max(absolute), 4),
        "drift_mean": round(sum(absolute) / len(absolute), 4),
        "drift_p95": round(absolute[int(len(absolute) * 0.95) - 1], 4),
        "drift_end": round(drifts[-1], 4),
        "span_seconds": round(times[-1] - times[0], 3),
    }


def retime(
    times: Sequence[float],
    ratio: float,
    anchor: float = 0.0,
) -> tuple[list[float], dict[str, Any]]:
    """按比例重排时间：``t' = anchor + (t - anchor) × ratio``。

    用于把模型网格整体缩放到目标速度。返回 (新时间, 位移报告)，位移报告可以让
    调用方判断这次干预动了多少内容。
    """
    if ratio <= 0:
        raise ValueError("ratio 必须为正")
    moved = [anchor + (float(t) - anchor) * ratio for t in times]
    shifts = [abs(new - float(old)) for new, old in zip(moved, times)]
    report = {
        "ratio": ratio,
        "anchor": anchor,
        "shift_max": round(max(shifts), 4) if shifts else 0.0,
        "shift_mean": round(sum(shifts) / len(shifts), 4) if shifts else 0.0,
    }
    return moved, report


# --------------------------------------------------------------- 拍号
ABC_TEMPO_RE = re.compile(r"^Q:\s*1/4\s*=\s*(\d+(?:\.\d+)?)", re.MULTILINE)
ABC_METER_RE = re.compile(r"^M:\s*(\d+\s*/\s*\d+)", re.MULTILINE)


@dataclass
class MeterEstimate:
    """拍号的完整情况。

    拍号**是模型预测的**（`downbeat_meter` 任务 → `rhythm.meter`），只是它采用稀疏
    编码：只在开头和变化处给出，后续事件沿用。`beat.lab` 的第 3/4 列是模型代码
    （`rhythm_rows()`）继承展开后的结果，这里就基于它统计。

    开头那个"短小节"不是噪声，而是**弱起小节（anacrusis）**：光芒开头是 1/8 的
    弱起，朋友的酒开头是 3/8 的弱起，之后才切到 4/4。上层卷帘需要的就是这段的秒数。
    """

    primary: str | None = None
    denominator: int | None = None
    distribution: dict[str, int] = field(default_factory=dict)
    consistent: bool = True          # 除弱起外，拍号是否全程不变
    pickup: dict[str, Any] | None = None
    changes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(primary=self.primary, denominator=self.denominator,
                    distribution=self.distribution, consistent=self.consistent,
                    pickup=self.pickup, changes=self.changes, warnings=self.warnings)


def estimate_meter(rows: Sequence[Sequence[Any]]) -> MeterEstimate:
    """从 `beat.lab` 的行推出拍号、弱起小节与转拍号位置。"""
    entries: list[tuple[float, str]] = []
    for row in rows:
        if len(row) < 4:
            continue
        numerator, denominator = str(row[2]), str(row[3])
        if not (numerator.isdigit() and denominator.isdigit()):
            continue
        try:
            time = float(row[0])
        except (ValueError, TypeError):
            continue
        entries.append((time, f"{numerator}/{denominator}"))

    estimate = MeterEstimate()
    if not entries:
        estimate.warnings.append("没有可用的拍号信息")
        return estimate

    entries.sort(key=lambda item: item[0])
    for _, meter in entries:
        estimate.distribution[meter] = estimate.distribution.get(meter, 0) + 1

    runs: list[dict[str, Any]] = []
    for time, meter in entries:
        if runs and runs[-1]["meter"] == meter:
            runs[-1]["beats"] += 1
            runs[-1]["end"] = time
        else:
            runs.append({"meter": meter, "beats": 1, "start": time, "end": time})

    # 主拍号取"拍数最多"的那一段，而不是简单的众数
    main = max(runs, key=lambda run: run["beats"])
    estimate.primary = main["meter"]
    estimate.denominator = int(main["meter"].split("/")[1])

    others = [run for run in runs if run is not main]
    for run in others:
        is_pickup = (
            run is runs[0] and run["beats"] < int(main["meter"].split("/")[0])
        )
        info = {
            "meter": run["meter"],
            "beats": run["beats"],
            "start": round(run["start"], 3),
            "end": round(main["start"], 3),
        }
        if is_pickup:
            info["seconds"] = round(main["start"] - run["start"], 3)
            estimate.pickup = info
        else:
            estimate.changes.append(info)

    estimate.consistent = not estimate.changes
    if estimate.changes:
        estimate.warnings.append(
            f"中途有 {len(estimate.changes)} 次转拍号：" 
            + "、".join(f"{c['meter']}@{c['start']}s" for c in estimate.changes))
    return estimate


def abc_meter(abc_text: str | None) -> str | None:
    """从 `score.abc` 的 ``M:`` 字段读模型记谱用的拍号，用于交叉校验。"""
    if not abc_text:
        return None
    match = ABC_METER_RE.search(abc_text)
    return match.group(1).replace(" ", "") if match else None


# ----------------------------------------------------- 分段速度（按段落）
@dataclass
class TempoSegment:
    """一个速度恒定的区段。"""

    start: float
    end: float
    bpm: float                    # 四分音符速度
    beats: int
    anchor: float
    label: str | None = None
    residual_max: float = 0.0
    period_seconds: float = 0.0    # 该段一拍（拍单位）的秒数
    interval_consistency: float = 1.0  # 拍间隔落在拟合拍长 ±5% 内的比例
    bpm_sigma: float | None = None  # 滑窗速度的标准差，也就是这个 BPM 的实际精度
    merged: int = 1               # 合并了几个原始段落
    is_transition: bool = False   # 夹在两个速度之间、且比两侧都短的过渡段

    @property
    def bpm_decimals(self) -> int:
        """按实际精度决定该报几位小数。

        时间戳量化到 0.01s 只贡献 ±0.0004 BPM，所以三位小数**不是**被量化误差限制的；
        限制来自模型拍点本身的抖动。`巴拉莱卡` 第 2 段的滑窗标准差是 0.003 BPM（三位有意义），
        第 1 段是 1.5 BPM（报三位就是假的）。
        """
        if self.bpm_sigma is None:
            return 3
        if self.bpm_sigma >= 0.05:
            return 1
        if self.bpm_sigma >= 0.005:
            return 2
        return 3

    @property
    def bpm_rounded(self) -> float | None:
        return None if self.bpm is None else round(self.bpm, self.bpm_decimals)

    @property
    def bpm_uncertain(self) -> bool:
        """滑窗标准差是否大到让这个 BPM 只能当粗略值。"""
        if self.bpm_sigma is None or not self.bpm:
            return False
        return self.bpm_sigma / self.bpm > BPM_UNCERTAIN_FRACTION

    @property
    def reliable(self) -> bool:
        """这一段能不能当作"恒速区"。

        两个条件都要满足：拟合残差小于一整拍，且多数拍间隔确实落在拟合拍长附近。
        只看残差会把缓慢起伏的自由速度误判成恒速。
        """
        return (self.period_seconds > 0
                and self.residual_max <= self.period_seconds
                and self.interval_consistency >= MIN_INTERVAL_CONSISTENCY)

    def as_dict(self) -> dict[str, Any]:
        return dict(start=round(self.start, 3), end=round(self.end, 3),
                    bpm=round(self.bpm, 4), beats=self.beats,
                    anchor=round(self.anchor, 4), label=self.label,
                    residual_max=round(self.residual_max, 4),
                    period_seconds=round(self.period_seconds, 4),
                    interval_consistency=round(self.interval_consistency, 4),
                    reliable=self.reliable, is_transition=self.is_transition,
                    bpm_sigma=None if self.bpm_sigma is None else round(self.bpm_sigma, 5),
                    bpm_rounded=self.bpm_rounded,
                    bpm_uncertain=self.bpm_uncertain,
                    bpm_decimals=self.bpm_decimals,
                    merged=self.merged)


def _tempo_sigma(times: Sequence[float], denominator: int, windows: int = 3) -> float | None:
    """把一段拍点切成若干滑窗分别拟合，返回这些速度的标准差。

    这是这个 BPM 的**实际精度**：时间戳量化只贡献 ±0.0004 BPM，真正的误差来自模型
    拍点自身的抖动和段内的速度起伏。
    """
    count = len(times)
    if count < 16 or denominator <= 0:
        return None
    window = max(8, count // windows)
    step = max(1, window // 4)
    values: list[float] = []
    for start in range(0, count - window + 1, step):
        try:
            period, _ = _least_squares_period(times[start:start + window])
        except ValueError:
            continue
        values.append((60.0 / period) * 4.0 / denominator)
    if len(values) < 3:
        return None
    return statistics.pstdev(values)


def _credible(segment: TempoSegment, min_segment_beats: int) -> bool:
    """这一段值不值得作为拟合依据。

    统计上够稳（`reliable`）**并且**拍点够多，才算一个真正的"恒速区"。
    被当作噪声吸收进来的小段不参与拟合——否则它会把好段的残差顶上去，
    反过来让好段也变成"不可信"，挡住后面本该合并的段。
    """
    return segment.reliable and segment.beats >= min_segment_beats


def _should_merge(segment: TempoSegment, previous: TempoSegment, tolerance: float,
                  min_segment_beats: int, total_beats: int) -> bool:
    """当前段要不要并进前一段。

    1. 两段都不可信 → 并（它们属于同一片网格不可靠的区域）
    2. 两段都可信 → 速度接近就并；否则只要有一边拍点太少也并
       （拍点太少的段不足以支撑"这里变速了"，例如 `巴拉莱卡` 结尾那 9 拍）
    3. 一边可信一边不可信 → 只看那片不可信区有多大：
       - 占全曲超过 `LARGE_UNSTABLE_FRACTION` → **绝不并**
         （`壁上观` 71.5–249.5s 占 69% 的拍点，并进去就变成"整首都是 90 BPM"）
       - 低于 `SMALL_UNSTABLE_FRACTION` → 并进去
       - 介于两者之间 → 速度接近才并
    """
    if (not segment.reliable) and (not previous.reliable):
        return True

    if segment.reliable and previous.reliable:
        if min(segment.beats, previous.beats) < min_segment_beats:
            return True
        return abs(segment.bpm - previous.bpm) <= tolerance

    unreliable = segment if not segment.reliable else previous
    if unreliable.beats >= LARGE_UNSTABLE_FRACTION * total_beats:
        return False
    if unreliable.beats < SMALL_UNSTABLE_FRACTION * total_beats:
        return True
    return abs(segment.bpm - previous.bpm) <= tolerance


def _interval_consistency(times: Sequence[float], period: float,
                          tolerance: float = INLIER_TOLERANCE) -> float:
    """拍间隔落在拟合拍长 ±tolerance 内的比例。"""
    if len(times) < 2 or period <= 0:
        return 0.0
    intervals = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not intervals:
        return 0.0
    limit = max(tolerance * period, 1e-9)
    return sum(1 for value in intervals if abs(value - period) <= limit) / len(intervals)


def _fit_segment(inside: Sequence[float], denominator: int) -> tuple[float, float, float, float, float]:
    """在一段拍点上做最小二乘。

    返回 (四分音符 BPM, 该段首拍时间, 最大残差, 拍单位秒数, 间隔一致性)。
    """
    period, anchor = _least_squares_period(inside)
    bpm = (60.0 / period) * 4.0 / denominator if denominator else 60.0 / period
    residual = max(abs(t - (anchor + period * i)) for i, t in enumerate(inside))
    return bpm, inside[0], residual, period, _interval_consistency(inside, period)


def sectional_tempo(
    beat_times: Sequence[float],
    sections: Sequence[Mapping[str, Any]],
    *,
    denominator: int = 4,
    tolerance: float = 1.0,
    min_beats: int = 8,
    min_segment_beats: int = 16,
) -> list[TempoSegment]:
    """按段落拟合速度，再把速度相同的相邻段落合并。

    歌里的速度变化几乎总是发生在段落边界上（`萧敬腾 - 皮囊`：172.28s 进 110、226.72s
    回 124，与 `structure.lab` 的小节边界重合）。所以逐段落拟合比逐小节拟合既准得多、
    又短得多——181 个速度点塌成 3 个，也正是人描述一首歌的方式。

    sections 每项需要 ``start`` / ``end``，可选 ``label``。
    """
    times = sorted(float(t) for t in beat_times)
    if len(times) < 2 or not sections:
        return []

    # 第一步：把连续的短段落攒成"够长"的组。
    # 结构表里常夹着 2 秒左右的小段（`皮囊` 的 189.76–191.92s 只有 4 拍），单独拟合会
    # 得到 111.94 这种充满噪声的值，把整段 110 的合并打断。
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for section in sections:
        start, end = float(section["start"]), float(section["end"])
        inside = [t for t in times if start - 1e-9 <= t < end - 1e-9]
        if current is None:
            current = {"start": start, "end": end, "beats": list(inside), "labels": set()}
        else:
            current["end"] = end
            current["beats"].extend(inside)
        current["labels"].add(str(section.get("label")) if section.get("label") is not None else "")
        if len(current["beats"]) >= min_beats:
            groups.append(current)
            current = None
    if current is not None:
        if groups:
            groups[-1]["end"] = current["end"]
            groups[-1]["beats"].extend(current["beats"])
            groups[-1]["labels"] |= current["labels"]
        else:
            groups.append(current)

    # 第二步：拟合每一组，并标出"不可信"的组。
    # 不可信有两种：残差超过一整拍（`朋友的酒` 的自由前奏），或拍间隔一致性太低
    # （`壁上观` 中间那段 rubato 被切成 4 段，各段一致性只有 8–25%）。
    raw: list[tuple[TempoSegment, list[float]]] = []
    for group in groups:
        inside = sorted(group["beats"])
        if len(inside) < 2:
            continue
        bpm, anchor, residual, period, consistency = _fit_segment(inside, denominator)
        labels = {label for label in group["labels"] if label}
        raw.append((TempoSegment(
            start=group["start"], end=group["end"], bpm=bpm, beats=len(inside),
            anchor=anchor, residual_max=residual, period_seconds=period,
            interval_consistency=consistency,
            label=labels.pop() if len(labels) == 1 else None), inside))

    # 开头就是不可信段（自由前奏）时不需要特殊处理：它不参与拟合，会被下面的循环
    # 并进第二段，而 `previous` 正是它，start 自然从 0 开始。

    # 先把**相邻的**不可信组合成一片，再进入主循环。
    # 否则它们会被逐个当成"小段"并进相邻的恒速段，一大片网格不可靠的区域就被拆散、
    # 凭空变成了某个速度的一部分（`壁上观` 正是这样变成"0–249.5s 都是 90"的）。
    collapsed: list[tuple[TempoSegment, list[float]]] = []
    for segment, beats in raw:
        if collapsed and (not segment.reliable) and (not collapsed[-1][0].reliable):
            previous, keep = collapsed[-1]
            previous.end = segment.end
            previous.merged += 1
            previous.beats += segment.beats
            collapsed[-1] = (previous, keep)
        else:
            collapsed.append((segment, list(beats)))
    raw = collapsed

    merged: list[TempoSegment] = []
    fit_beats: list[list[float]] = []
    total_beats = max(1, len(times))
    for segment, beats in raw:
        if merged:
            join = _should_merge(segment, merged[-1], tolerance,
                                 min_segment_beats, total_beats)
        else:
            join = False

        if not join:
            merged.append(segment)
            fit_beats.append(list(beats) if _credible(segment, min_segment_beats) else [])
            continue

        previous = merged[-1]
        keep = fit_beats[-1]
        credible = _credible(segment, min_segment_beats)
        gap = beats[0] - keep[-1] if keep else None
        contiguous = (gap is not None
                      and gap <= 2.0 * max(segment.period_seconds or 1e-9,
                                           previous.period_seconds or 1e-9))

        previous.end = segment.end
        previous.merged += 1
        if segment.label != previous.label:
            previous.label = None

        if credible and keep and contiguous:
            combined = keep + list(beats)
            (previous.bpm, previous.anchor, previous.residual_max,
             previous.period_seconds, previous.interval_consistency) = _fit_segment(
                combined, denominator)
            fit_beats[-1] = combined
        elif credible:
            # 前面那段不值得作为依据（噪声/太短），改用这一段的拟合
            (previous.bpm, previous.anchor, previous.residual_max,
             previous.period_seconds, previous.interval_consistency) = _fit_segment(
                list(beats), denominator)
            fit_beats[-1] = list(beats)
        # 否则：并进来的是噪声段，不参与拟合，保留原来的拟合结果

    # 第三步：把一致性按"整段"重算。
    # 合并时为了不拖坏拟合，保留的是可信子段的拟合结果；但 start/end 描述的是整段，
    # 若直接沿用子段的一致性，报告就会拿一小段的自洽度去背书一整段（`壁上观` 会显示
    # 95%，而它 0–249.5s 其实是一次自由速度）。
    for segment in merged:
        span = [t for t in times if segment.start - 1e-9 <= t < segment.end - 1e-9]
        if len(span) >= 2 and segment.period_seconds > 0:
            segment.interval_consistency = _interval_consistency(span, segment.period_seconds)
            segment.beats = len(span)
            # 这个 BPM 到底精确到几位：滑窗标准差就是它的实际精度
            segment.bpm_sigma = _tempo_sigma(span, denominator)

    # 第四步：标出过渡段——速度严格落在左右两段之间，且自身是可信的。
    # `2020伴奏` 是 110 → 75 → 110，中间的 77.4 与 108.0 是加速过渡，标出来之后
    # 摘要就能显示成"110 → 75 → 110"，而细节仍然保留在报告里。
    for index, segment in enumerate(merged):
        if 0 < index < len(merged) - 1:
            left, right = merged[index - 1].bpm, merged[index + 1].bpm
            between = min(left, right) - 1e-9 <= segment.bpm <= max(left, right) + 1e-9
            segment.is_transition = bool(between and segment.reliable)
    return merged


@dataclass
class GridRecovery:
    """一片"网格不可靠"的区域，能不能自动重建出可用的拍网格。"""

    bpm: float | None = None
    period_seconds: float | None = None
    phase: float | None = None
    beats: int = 0
    hit_ratio: float = 0.0        # 模型拍点落在重建网格 ±50ms 内的比例
    tight_ratio: float = 0.0      # ±20ms
    missing_beats: int = 0        # 重建网格比模型多出来的拍数（模型漏拍）
    median_shift: float | None = None   # 按重建网格重排时，拍点的位移中位/最大
    max_shift: float | None = None
    confident: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(bpm=self.bpm, period_seconds=self.period_seconds, phase=self.phase,
                    beats=self.beats, hit_ratio=self.hit_ratio,
                    tight_ratio=self.tight_ratio, missing_beats=self.missing_beats,
                    median_shift=self.median_shift, max_shift=self.max_shift,
                    confident=self.confident)


def suggest_grid_recovery(
    beat_times: Sequence[float],
    start: float,
    end: float,
    *,
    min_bpm: float = 40.0,
    max_bpm: float = 240.0,
    tolerance: float = 0.05,
    denominator: int = 4,
) -> GridRecovery:
    """给一片网格不可靠的区域找出"最像"的恒定网格，并量化重建的代价。

    做法是把拍长和相位都搜一遍，取命中率最高的组合。注意**命中率高不代表模型是对的**：
    `壁上观` 71.5–249.5s 的最佳网格是 0.500s（也就是 120 BPM，和人工核对一致），但命中率
    只有 50%、±20ms 内 0%——说明这段的拍点本身就错了或漏了，**不是相位猜错**。

    因此这个函数回答两个不同的问题：

    - **速度值**能不能自动定？能。最佳拍长就是速度，还有模型自己的干净邻段和音频上的
      独立估计做交叉验证。
    - **网格**能不能自动重建？只有 `confident` 为真才行。否则重建会把大量音符挪位，
      位移中位/最大摆在这里，由调用方决定要不要接受。
    """
    times = sorted(float(t) for t in beat_times if start - 1e-9 <= t < end - 1e-9)
    recovery = GridRecovery(beats=len(times))
    if len(times) < 4:
        return recovery

    # 拍长越短，命中率天生越高（0.25s 的网格线密到任何拍点都就近），所以必须限制
    # 网格密度：网格线数量不能明显多于实际拍点数。不加这条约束会选出 240 BPM 这种
    # 假阳性——它命中 81%，但 ±20ms 内只有 18%，一眼假。
    span = times[-1] - times[0]
    best: tuple[float, float, float] | None = None   # (hit, period, phase)
    for index in range(201):
        bpm = min_bpm + (max_bpm - min_bpm) * index / 200.0
        period = 60.0 / bpm
        if span / period + 1 > 1.5 * len(times):
            continue
        for step in range(20):
            phase = period * step / 20.0
            residual = [abs((t - phase) % period) for t in times]
            residual = [min(r, period - r) for r in residual]
            hit = sum(1 for r in residual if r <= tolerance) / len(residual)
            if best is None or hit > best[0]:
                best = (hit, period, phase)

    if best is None:
        return recovery
    hit, period, phase = best

    # 粗搜只试了 20 个相位，粒度最粗能到 ±period/40（约 ±14ms），会把 ±20ms 的命中率
    # 系统性压低。这里以"残差中位数"为目标再做一次细搜（残差中位数是连续量，
    # 不像命中率那样有台阶）。
    def median_residual(candidate_period: float, candidate_phase: float) -> float:
        values = [abs((t - candidate_phase) % candidate_period) for t in times]
        values = [min(v, candidate_period - v) for v in values]
        return statistics.median(values)

    best_score = median_residual(period, phase)
    for half_step in range(-10, 11):
        candidate_period = period * (1.0 + half_step * 0.002)
        if candidate_period <= 0:
            continue
        for step in range(201):
            candidate_phase = phase + (step - 100) / 100.0 * candidate_period / 20.0
            score = median_residual(candidate_period, candidate_phase)
            if score < best_score:
                best_score, period, phase = score, candidate_period, candidate_phase
    residuals = [min(r, period - r) for r in (abs((t - phase) % period) for t in times)]
    grid_count = int((times[-1] - times[0]) / period) + 1

    recovery.bpm = round((60.0 / period) * 4.0 / max(1, denominator), 4)
    recovery.period_seconds = round(period, 6)
    recovery.phase = round(phase, 4)
    recovery.hit_ratio = round(hit, 4)
    recovery.tight_ratio = round(sum(1 for r in residuals if r <= 0.02) / len(residuals), 4)
    recovery.missing_beats = max(0, grid_count - len(times))
    recovery.median_shift = round(statistics.median(residuals), 4)
    recovery.max_shift = round(max(residuals), 4)
    # 校准：模型自己完全干净的段在 ±50ms 上是 100%、±20ms 上约 72%
    # （时间戳只有 0.01s 精度，这就是上限）；达不到就说明是拍点本身坏了。
    recovery.confident = recovery.hit_ratio >= 0.95 and recovery.tight_ratio >= 0.6
    return recovery


def segments_to_tempo_map(    segments: Sequence[TempoSegment],
    timeline: Any,
) -> list[dict[str, Any]]:
    """把分段速度换算成 tick 域的速度表（可直接写 MIDI 的 set_tempo）。"""
    entries: list[dict[str, Any]] = []
    for segment in segments:
        if segment.bpm <= 0:
            continue
        match = next((m for m in timeline.measures
                      if m.start <= segment.start < m.end), None)
        if match is None:
            continue
        entries.append(dict(tick=match.tick_start,
                            # 用按实际精度取整后的值：写 124.311 会暗示一个并不存在的精度
                            bpm=segment.bpm_rounded if segment.bpm_rounded is not None
                            else round(segment.bpm, 4),
                            bpm_sigma=None if segment.bpm_sigma is None
                            else round(segment.bpm_sigma, 4),
                            numerator=match.numerator, denominator=match.denominator,
                            label=segment.label, beats=segment.beats,
                            start_seconds=round(segment.start, 3)))
    entries.sort(key=lambda entry: entry["tick"])
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        if deduped and deduped[-1]["tick"] == entry["tick"]:
            deduped[-1] = entry     # 同一 tick 只保留最后一个
        else:
            deduped.append(entry)
    return deduped




def abc_tempo(abc_text: str | None) -> float | None:
    """从 `score.abc` 的 ``Q:1/4=N`` 里读模型自带的 BPM。

    模型并不把速度当作预测任务（8 个任务字段里没有 tempo），这个值是它的 ABC
    生成器 `notation_sheetsage2.estimate_tempo()` 顺带算出来的：

        quarter_notes / seconds × 60      # 取全曲首尾两点

    也就是**首尾两点法**，只用到第一个和最后一个 subbeat，因此对端点异常很敏感
    （歌曲结尾散拍会把结果拖偏）。模型把它四舍五入成整数再写进 ABC。

    注意：beat 少于两个或不含调性时根本不会生成 `score.abc`，此时没有 Q 可读。
    """
    if not abc_text:
        return None
    match = ABC_TEMPO_RE.search(abc_text)
    return float(match.group(1)) if match else None


def reconcile(
    abc_bpm: float | None,
    estimate: TempoEstimate,
    tolerance: float = 1.0,
) -> dict[str, Any]:
    """把"模型自带的 Q 值"和"我们从节拍网格拟合的值"对起来。

    取值规则：节拍网格是时间的真源，且鲁棒拟合用到了全部拍点，所以优先采用它；
    模型 Q 值作为**独立的交叉校验**。两者不一致时如实报告，不做静默取舍。
    """
    beat_bpm = estimate.bpm
    chosen, source = beat_bpm, "beat_robust"
    if beat_bpm is None or not estimate.is_constant:
        # 网格不可信（非常速或拍点不足）时才退回模型自带的 Q 值
        if abc_bpm is not None:
            chosen, source = abc_bpm, "abc_q"
    elif abc_bpm is None:
        chosen, source = beat_bpm, "beat_robust"

    delta = None
    if abc_bpm is not None and beat_bpm is not None:
        delta = round(beat_bpm - abc_bpm, 3)

    notes: list[str] = []
    agreement = True
    constant = estimate.is_constant

    if delta is None:
        if abc_bpm is None and beat_bpm is None:
            agreement = False
            notes.append("两个来源都没有值")
        elif abc_bpm is None:
            notes.append("模型没有生成 score.abc，没有 Q 值可交叉校验")
        else:
            notes.append("节拍网格不可用，只能采用模型的 Q 值")
    elif abs(delta) > tolerance:
        agreement = False
        if constant:
            notes.append(
                f"模型 Q 值 {abc_bpm} 与节拍网格拟合值 {beat_bpm} 相差 {delta:+.3f} BPM；"
                f"该曲判定为恒速，采用节拍网格拟合值")
        else:
            notes.append(
                f"模型 Q 值 {abc_bpm} 与节拍网格拟合值 {beat_bpm} 相差 {delta:+.3f} BPM；"
                f"该曲**不是恒速曲**，两个数都只是摘要，单一 BPM 无法描述它")
    else:
        notes.append(f"模型 Q 值 {abc_bpm} 与节拍网格拟合值 {beat_bpm} 一致（差 {delta:+.3f}）")

    if not constant:
        span = ""
        if estimate.tempo_range:
            span = (f"，局部速度 {estimate.tempo_range['min']}–"
                    f"{estimate.tempo_range['max']} BPM")
        notes.append(f"该曲不是恒速曲（稳定段仅覆盖 "
                     f"{estimate.regular_coverage:.0%} 的拍点{span}）")

    return {
        "abc_bpm": abc_bpm,
        "beat_bpm": beat_bpm,
        "bpm_dominant": estimate.bpm_dominant,
        "naive_bpm": estimate.bpm_median_interval,
        "tempo_range": estimate.tempo_range,
        "chosen_bpm": chosen,
        "source": source,
        "delta": delta,
        "agreement": agreement,
        "constant": constant,
        "confident": constant,
        "tolerance": tolerance,
        "notes": notes,
    }
