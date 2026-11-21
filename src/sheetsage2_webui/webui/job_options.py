"""参数面板的唯一真源：spec 表、默认值、校验、标签与"改了哪些"的人话。

纯函数与常量，不碰磁盘、不起线程、不导入调度器——参数是变化最频繁的一块，
它变的时候不该连带任何运行时代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

#: 页面上可调的参数（`静态页面.html` 的参数面板 + 本项目多的几处）。
#:
#: 一条 spec 就是面板上的一行：`key` 是 :attr:`Job.options` 里的字段名，`options` 是可选值，
#: `widget` 决定前端画成标签按钮（默认）还是下拉框。**这是取值的唯一真源**：
#: `/api/env` 把它发给前端，`validate_options` 拿它校验 PATCH。
#:
#: 比原型多的几处：扒谱的「量化」，歌词的「段落」「语言」（原型只有 4 个语言，
#: 这里是下拉框全量）。
#:
#: **不在面板上的**：旋律的音高修正。模型的旋律整体高一个八度（和弦是对的），
#: 一律降一个八度就完了——虚拟歌姬本来也不会按原调调音，没必要让用户选。
#: 实现见 `pitch.py`，固定值 `pipeline.PITCH_SEMITONES`，只在命令行留了 `--pitch-shift`。
PARAM_SPECS: dict[str, list[dict[str, Any]]] = {
    "transcribe": [
        {"key": "dtype", "label": "精度", "default": "bf16",
         "options": [{"value": "bf16", "label": "BF16 快速"},
                     {"value": "fp32", "label": "FP32 精确"}]},
        {"key": "device", "label": "设备", "default": "auto",
         "options": [{"value": "auto", "label": "自动 · GPU"},
                     {"value": "cpu", "label": "CPU"}]},
        {"key": "range", "label": "范围", "default": "full",
         "options": [{"value": "full", "label": "整首"},
                     {"value": "30", "label": "前 30 秒"},
                     {"value": "60", "label": "前 60 秒"}]},
        {"key": "quantize", "label": "量化", "default": "grid",
         "options": [{"value": "grid", "label": "128 分三连音网格"},
                     {"value": "off", "label": "不量化"}]},
    ],
    "lyrics": [
        {"key": "asr_model", "label": "模型", "default": "1.7B",
         "options": [{"value": "1.7B", "label": "1.7B"},
                     {"value": "0.6B", "label": "0.6B"}]},
        {"key": "source", "label": "音源", "default": "stem",
         "options": [{"value": "stem", "label": "干声（推荐）"},
                     {"value": "mix", "label": "混音原曲"}]},
        # 前奏 / 间奏 / 尾奏一般不唱，跳过它们省时间也省显存；模型标错时会丢那段的字，
        # 所以日志与阶段详情里都会把跳过的段落列出来。
        {"key": "skip_instrumental", "label": "段落", "default": "vocal",
         "options": [{"value": "vocal", "label": "只跑唱段",
                      "hint": "跳过前奏 / 间奏 / 尾奏（一般不唱），省时间也省显存"},
                     {"value": "all", "label": "全部段落",
                      "hint": "每个段落都跑，包括前奏 / 间奏 / 尾奏"}]},
        # 语言的可选值来自 `asr.LANGUAGE_CHOICES`（`options` 留空，见 `option_values` /
        # `param_choices`）；原型是标签按钮，这里保留本项目的**下拉框**。
        {"key": "language", "label": "语言", "default": "", "widget": "select",
         "options": []},
        # 歌词源 LRC/TXT：给了就跳过语音识别，官方词直接对齐（见 asr.LyricLine）。
        # 输入交互由前端 LyricsSource 组件接管（拖文件/粘贴），这里只提供值的行位，
        # 让"改了歌词源 → 歌词结果作废"的口径与其他参数一致。
        {"key": "lyrics_lrc", "label": "歌词LRC", "default": "", "widget": "text",
         "hint": "官方歌词 LRC/TXT：给了就跳过语音识别，直接对齐"},
    ],
}

#: `key -> 它属于哪个阶段`。改了这个参数，对应的阶段结果就要作废。
PARAM_OWNER: dict[str, str] = {
    spec["key"]: phase for phase, specs in PARAM_SPECS.items() for spec in specs
}
PARAM_INDEX: dict[str, dict[str, Any]] = {
    spec["key"]: spec for specs in PARAM_SPECS.values() for spec in specs
}


#: 歌词源（LRC/TXT 原文）的最大长度。整首歌词通常几 KB；200 KB 已经是非常宽的上限。
#: 再大就不是歌词，而是误把音频/日志粘进来了。
MAX_LYRICS_SOURCE_CHARS = 200_000


def option_values(key: str) -> set[str]:
    """某个参数认哪些值。

    「语言」的可选值不写死在 `PARAM_SPECS` 里（那是 `asr.LANGUAGE_CHOICES` 的事），
    这里延迟 import，免得 `jobs.py` 在模块层依赖 asr。

    文本类参数（`widget="text"`，目前只有歌词源）没有枚举白名单，返回空集合；
    它的校验走 `_validate_text_option`，不要拿 `in` 去查。
    """
    spec = PARAM_INDEX[key]
    if spec.get("widget") == "text":
        return set()
    if key == "language":
        from ..asr import LANGUAGE_CHOICES

        return {value for value, _ in LANGUAGE_CHOICES}
    return {str(item["value"]) for item in spec["options"]}


def _validate_text_option(spec: Mapping[str, Any], value: Any) -> str:
    """文本类参数：限长、统一换行、必须能解析出唱词行。空串表示清空。

    歌词源既可以是 LRC（带时间戳）也可以是 TXT（纯文本），所以这里只要求
    `parse_lyric_text` 能解析出至少一行——真正的口径（LRC 精确对齐 / TXT 估算）
    由 runner 决定。
    """
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{spec['label']}必须是文本")
    text = "" if value is None else value.strip()
    if not text:
        return ""
    if len(text) > MAX_LYRICS_SOURCE_CHARS:
        raise ValueError(
            f"{spec['label']}太长（{len(text)} 字，最多 {MAX_LYRICS_SOURCE_CHARS} 字）")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    from ..asr import parse_lyric_text

    if not parse_lyric_text(text):
        raise ValueError(f"{spec['label']}里没有解析出唱词行（只有标题/制作名单？）")
    return text


def default_options() -> dict[str, Any]:
    """新建工程时的默认参数（= 原型面板上默认高亮的那一项）。"""
    return {spec["key"]: spec["default"] for spec in PARAM_INDEX.values()}


def validate_options(values: Mapping[str, Any]) -> dict[str, Any]:
    """把前端发来的参数收成 `Job.options` 的补丁；不认识/越界就抛 :class:`ValueError`。"""
    out: dict[str, Any] = {}
    for key, value in values.items():
        spec = PARAM_INDEX.get(str(key))
        if spec is None:
            raise ValueError(f"未知参数 {key}")
        if spec.get("widget") == "text":
            out[key] = _validate_text_option(spec, value)
            continue
        text = "" if value is None else str(value).strip()
        if key == "language":                     # 大小写/auto 之类的写法都收敛一下
            from ..asr import normalize_language

            text = normalize_language(text) or ""
        if text not in option_values(key):
            allowed = "、".join(sorted(item or "自动" for item in option_values(key)))
            raise ValueError(f"不支持的{spec['label']} {value!r}；可选 {allowed}")
        out[key] = text
    return out


def param_choices(asr_models: Mapping[str, Any] | None = None,
                  default_asr_model: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """发给前端的参数表（`/api/env` 的 `params`）。浅拷贝够用——前端只读。

    `asr_models` 是本机实际有的识别模型（`{"1.7B": 路径, ...}`），`default_asr_model` 是
    服务端启动时指定的那个——**「模型」那一栏的默认值要跟着它走**，否则 `--asr-model`
    会被页面上的默认值悄悄覆盖掉。
    """
    groups = {phase: [dict(spec) for spec in specs] for phase, specs in PARAM_SPECS.items()}
    for spec in groups["lyrics"]:
        if spec["key"] == "language":
            from ..asr import LANGUAGE_CHOICES

            spec["options"] = [{"value": value, "label": label}
                               for value, label in LANGUAGE_CHOICES]
        elif spec["key"] == "asr_model" and asr_models:
            labels = list(asr_models)
            default = asr_model_label(default_asr_model) or spec["default"]
            if default not in labels:
                default = spec["default"] if spec["default"] in labels else labels[0]
            spec["default"] = default
            # 默认那个排在最前面（原型上高亮的就是它）
            ordered = [default] + [label for label in labels if label != default]
            spec["options"] = [{"value": label, "label": label} for label in ordered]
    return groups


def describe_change(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """把"哪些参数变了"说成一句人话（日志与作废提示用）。"""
    parts: list[str] = []
    for key, value in after.items():
        if before.get(key) == value:
            continue
        spec = PARAM_INDEX.get(key)
        label = _option_label(spec, value)
        if spec is not None:
            parts.append(f"{spec['label']}改成 {label}")
        else:
            parts.append(f"{key} 改成 {label}")
    return "、".join(parts)


def _option_label(spec: Mapping[str, Any] | None, value: Any) -> str:
    """一个取值 -> 显示名（"自动"给空串；标签表里没有就原样返回）。"""
    text = "" if value is None else str(value)
    if spec is not None:
        if spec.get("widget") == "text":
            if not text.strip():
                return "空"
            # 不要把整首 LRC 塞进日志/作废提示；行数 + 字数足够定位。
            return f"{len(text.splitlines())} 行 / {len(text)} 字"
        hit = next((str(item["label"]) for item in spec["options"]
                    if str(item["value"]) == text), None)
        if hit is not None:
            return hit
    return text or "自动"


#: 识别模型权重目录名的前缀（`models/Qwen3-ASR-1.7B`）。
ASR_MODEL_PREFIX = "Qwen3-ASR-"


def asr_model_label(name: str | Path | None) -> str:
    """`models/Qwen3-ASR-1.7B` -> `1.7B`（页面上「模型」那一栏的标签）。"""
    text = Path(str(name or "")).name
    return text[len(ASR_MODEL_PREFIX):] if text.startswith(ASR_MODEL_PREFIX) else text
