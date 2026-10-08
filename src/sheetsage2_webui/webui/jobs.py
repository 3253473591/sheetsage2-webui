"""任务管理：上传落到磁盘 → 等用户选参数 → 按阶段跑 runner → 产物可下载。

一个任务有**两个阶段**（和界面上的两张卡一一对应）：

| phase | 界面 | 谁跑 |
|---|---|---|
| `transcribe` | 扒谱 / SheetSage2 | `runner.RunnerSession.transcribe` → `pipeline.run` |
| `lyrics` | 歌词识别 / Qwen3-ASR | `runner.RunnerSession.lyrics` → `asr` + `lyrics` |

设计约束（都是踩过的）：

1. **丢音频不立刻开跑（门禁）。** 上传只建工程，状态是 :data:`DRAFT_STATE`；用户在页面上
   选完参数点「开始扒谱」才排队。参数本身挂在 :attr:`Job.options` 上（见 ``PARAM_SPECS``）。
2. **一次只跑一个阶段。** GPU 只有一块，并发只会一起变慢、还容易 OOM；
   队列本身也是给用户看的（"排队 2"）。
3. **进度按阶段分开记。** 界面是两张独立的卡，各有进度条和"当前小步骤"，
   所以 `stage/progress/step` 都挂在阶段上，不能只留一个全局数。
4. **事件必须能续传。** 每个事件带单调递增的 `seq`，客户端断线重连时带 `since`，
   后端从下一条补发——所以事件列表要留着。
5. **取消是协作式的。** 推理不可中断，只能在下一次回调里抛 :class:`JobCancelled`。
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from ..bootstrap import PROJECT_ROOT

#: 阶段顺序与显示名。界面上的两张卡就是这两个。
PHASES = ("transcribe", "lyrics")
PHASE_LABELS = {"transcribe": "扒谱", "lyrics": "歌词识别"}

#: 新工程默认**一个阶段都不排**（门禁）。
#:
#: 丢音频只做两件事：把文件收下来、把工程建出来。参数面板这时才露出来，用户选完参数
#: 再点「开始扒谱」/「开始识别歌词」，任务才进队列。想一条龙就用「⏵ 自动提取」
#: （它走 `/chain`，明确要求串联）。
DEFAULT_PLAN: tuple[str, ...] = ()

#: 刚建好、还没排任何阶段的工程状态。界面上是"待开始"。
DRAFT_STATE = "draft"

#: 任务状态机：draft -> queued -> running -> (done | failed | cancelled)
TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
STAGE_TERMINAL = frozenset({"done", "failed", "cancelled", "skipped"})

#: 保留的 SSE 事件条数上限。一个任务的解码事件可能上千条，全留着没意义。
MAX_EVENTS = 4000

#: 允许上传的音频后缀。其余一律挡在上传阶段，别等 ffmpeg 报错。
AUDIO_SUFFIXES = frozenset(
    {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".oga",
     ".aif", ".aiff", ".wma", ".mp4", ".wv", ".ape", ".alac", ".caf"}
)

#: 可单独上传的音源（stem）。目前只有歌词用的 `vocal`，将来要加伴奏/鼓就直接加 key。
STEM_KINDS = ("vocal",)
STEM_LABELS = {"vocal": "干声"}

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
    ],
}

#: `key -> 它属于哪个阶段`。改了这个参数，对应的阶段结果就要作废。
PARAM_OWNER: dict[str, str] = {
    spec["key"]: phase for phase, specs in PARAM_SPECS.items() for spec in specs
}
PARAM_INDEX: dict[str, dict[str, Any]] = {
    spec["key"]: spec for specs in PARAM_SPECS.values() for spec in specs
}


def option_values(key: str) -> set[str]:
    """某个参数认哪些值。

    「语言」的可选值不写死在 `PARAM_SPECS` 里（那是 `asr.LANGUAGE_CHOICES` 的事），
    这里延迟 import，免得 `jobs.py` 在模块层依赖 asr。
    """
    if key == "language":
        from ..asr import LANGUAGE_CHOICES

        return {value for value, _ in LANGUAGE_CHOICES}
    return {str(item["value"]) for item in PARAM_INDEX[key]["options"]}


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

#: 产物文件后缀 -> 给前端看的类别。
FILE_KINDS = {
    ".json": "data", ".lab": "label", ".tsv": "label", ".txt": "text",
    ".mid": "midi", ".midi": "midi", ".abc": "score", ".wav": "audio",
}

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class JobCancelled(RuntimeError):
    """协作式取消：在下一次进度检查点抛出。"""


class UploadRejected(ValueError):
    """上传被拒（后缀不对 / 太大 / 空文件）。"""


class StageNotReady(RuntimeError):
    """阶段的前置条件没满足（例：还没扒谱就想跑歌词识别）。

    API 把它翻成 409 + 一句人话（"请先完成扒谱"），界面直接显示这句。
    """


class ImportedProject(RuntimeError):
    """"这是 `output/` 里已经存在的产物，不是 WebUI 建的工程"。

    工程列表**以 `output/` 为准**：别人（或命令行）跑出来的目录也会列进侧栏，
    能看卷帘 / 下载 / 导出 SVP，但没有原音频、也不知道当时的参数，所以**不能重跑**。
    API 把它翻成 409 + 一句人话。
    """


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


# --------------------------------------------------------------------- 事件
@dataclass
class Event:
    seq: int
    type: str
    time: float
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "time": self.time, "data": self.data}


# --------------------------------------------------------------------- 阶段
@dataclass
class StageRun:
    """一个阶段（扒谱 / 歌词）的状态。界面上就是一张卡。"""

    key: str
    label: str
    state: str = "idle"          # idle | queued | running | done | failed | cancelled | skipped
    progress: float = 0.0
    step: str = ""               # 当前小步骤（"识别音乐"…）
    message: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)   # 该阶段的产物统计

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "progress": round(self.progress, 4),
            "step": self.step,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "error": self.error,
            "detail": self.detail,
        }


# --------------------------------------------------------------------- 任务
@dataclass
class Job:
    """一次上传 + 一串阶段运行。所有字段的读写都走 ``lock``。

    **磁盘布局（`output/<歌名>/`）**——工程目录就是产物目录，和命令行跑出来的一模一样::

        output/<歌名>/project.json  report.json  melody_vocal.lab  …   <- 产物（CLI 也写这里）
        output/<歌名>/job.json                                        <- WebUI 元数据
        output/<歌名>/.webui/input/<原音频>  stems/  peaks-*.json
                             .webui/export/*.svp  bundle.zip           <- WebUI 自己的东西

    ``imported=True`` 表示这个目录是**别人跑出来的**（命令行 / 另一个工具）：能看能下载，
    但不能重跑阶段，也不会往里写 `job.json`。
    """

    id: str
    name: str
    audio: str
    root: Path
    upload: Path
    out_dir: Path
    created_at: float
    rev: int = 1
    state: str = DRAFT_STATE
    stage: str | None = None                        # 当前正在跑的阶段 key
    plan: list[str] = field(default_factory=list)   # 待跑的阶段队列
    stages: dict[str, StageRun] = field(default_factory=dict)
    message: str = "等待参数"
    files: list[dict[str, Any]] = field(default_factory=list)
    #: 单独上传的音源：kind -> {name, path, size, created_at}。目前只有 `vocal`（干声）。
    #: 有干声且「音源」选了干声就用它，否则用混音原曲——回退规则在 `runner.lyrics`。
    stems: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 每个工程自己的可调参数（见 `PARAM_SPECS`）。**这是页面上放开给用户的东西**：
    #: 精度 / 设备 / 范围 / 量化 / 识别模型 / 音源 / 语言。
    options: dict[str, Any] = field(default_factory=default_options)
    highlights: dict[str, Any] | None = None
    decisions: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    cancel_requested: bool = False
    #: `output/` 里本来就有的产物目录（不是 WebUI 建的）——只读。
    imported: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _seq: int = 0

    def __post_init__(self) -> None:
        for phase in PHASES:
            self.stages.setdefault(phase, StageRun(phase, PHASE_LABELS[phase]))

    # ---------------------------------------------------------------- 布局
    @property
    def internal_dir(self) -> Path:
        """WebUI 自己的东西（元数据 / 音频 / 缓存 / 打包）都塞这里，不污染产物。"""
        return self.root / INTERNAL_DIRNAME

    @property
    def stems_dir(self) -> Path:
        return self.internal_dir / "stems"

    # ---------------------------------------------------------------- 事件
    def emit(self, type_: str, **data: Any) -> Event:
        with self.lock:
            self._seq += 1
            event = Event(self._seq, type_, time.time(), data)
            self.events.append(event)
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
            return event

    def events_since(self, seq: int) -> list[Event]:
        with self.lock:
            return [event for event in self.events if event.seq > seq]

    @property
    def terminal(self) -> bool:
        with self.lock:
            return self.state in TERMINAL_STATES

    @property
    def elapsed_seconds(self) -> float:
        with self.lock:
            starts = [s.started_at for s in self.stages.values() if s.started_at]
            ends = [s.finished_at for s in self.stages.values() if s.finished_at]
            if not starts:
                return 0.0
            end = max(ends) if ends else time.time()
            return max(0.0, end - min(starts))

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            current = self.stages.get(self.stage) if self.stage else None
            return {
                "id": self.id,
                "name": self.name,
                "audio": self.audio,
                "rev": self.rev,
                "state": self.state,
                "stage": self.stage,
                "stages": {key: run.as_dict() for key, run in self.stages.items()},
                # 下面几个是"当前阶段"的扁平视图，顶栏/列表用起来方便
                "stage_label": current.label if current else "",
                "step": current.step if current else "",
                "progress": round(current.progress, 4) if current else 0.0,
                "message": self.message,
                "created_at": self.created_at,
                "elapsed_seconds": round(self.elapsed_seconds, 2),
                "error": next((s.error for s in self.stages.values() if s.error), None),
                "warnings": list(self.warnings),
                "files": list(self.files),
                "stems": {kind: dict(info) for kind, info in self.stems.items()},
                "options": dict(self.options),
                "highlights": self.highlights,
                "decisions": self.decisions,
                "summary": self.summary,
                "events": len(self.events),
                "cancel_requested": self.cancel_requested,
                # `output/` 里本来就有的产物：只读（不能重跑、不能改参数）
                "imported": self.imported,
            }

    def metadata(self) -> dict[str, Any]:
        """落盘的 ``job.json``：比快照多一份完整摘要，够人工复盘。"""
        data = self.snapshot()
        data["output_dir"] = str(self.out_dir)
        data["upload"] = str(self.upload)
        return data


# --------------------------------------------------------------------- 进度
class Progress:
    """runner 汇报进度 + 检查取消的唯一入口（**按阶段**）。

    用法（`runner.py` 里就是这么用的）::

        progress.step("识别音乐", 0.10)
        progress.set(0.42, "窗口 2/6 · 解码 512 tokens")
        progress.check()          # 用户点了取消就在这里抛 JobCancelled
    """

    #: 进度事件的最小间隔（秒）与最小增量的双重节流——解码事件太密，不筛会淹掉日志。
    MIN_INTERVAL = 0.25
    MIN_DELTA = 0.002

    def __init__(self, job: Job, phase: str) -> None:
        self.job = job
        self.phase = phase
        self.run = job.stages[phase]
        self._last_emit = 0.0
        self._last_value = -1.0

    # ---------------------------------------------------------------- 小步骤
    def step(self, label: str, fraction: float | None = None) -> None:
        """切换当前小步骤。**只有 label 变了才发事件**，否则逐窗口回调会刷爆事件流。"""
        with self.job.lock:
            changed = self.run.step != label
            self.run.step = label
            if fraction is not None:
                self.run.progress = max(self.run.progress, fraction)
            progress = self.run.progress
        if changed:
            self.job.emit("step", phase=self.phase, step=label, progress=progress)

    # ---------------------------------------------------------------- 进度
    def set(self, fraction: float, message: str | None = None, *, force: bool = False) -> None:
        fraction = min(1.0, max(0.0, float(fraction)))
        with self.job.lock:
            self.run.progress = max(self.run.progress, fraction)
            if message is not None:
                self.run.message = message
                self.job.message = message
        now = time.monotonic()
        due = (now - self._last_emit >= self.MIN_INTERVAL
               or fraction - self._last_value >= self.MIN_DELTA)
        if force or due:
            self._last_emit = now
            self._last_value = fraction
            self.job.emit("progress", phase=self.phase, progress=round(fraction, 4),
                          message=message or "")

    def detail(self, **values: Any) -> None:
        """记录该阶段的产物统计（歌词行数、音符数……），进快照与 job.json。"""
        with self.job.lock:
            self.run.detail.update(values)

    def lyrics(self, **payload: Any) -> None:
        """把"已经识别出来的歌词"实时推给界面（模型逐段出结果）。"""
        self.job.emit("lyrics", phase=self.phase, **payload)

    def log(self, message: str, **extra: Any) -> None:
        with self.job.lock:
            self.job.message = message
        self.job.emit("log", phase=self.phase, message=message, **extra)

    def check(self) -> None:
        with self.job.lock:
            if self.job.cancel_requested:
                raise JobCancelled("用户取消")


StageRunner = Callable[[Job, Progress], dict[str, Any]]


# --------------------------------------------------------------------- 管理器
class JobManager:
    """任务队列 + 单工作线程。

    测试里把 ``runners`` 换成立刻返回假 payload 的函数，就能在不加载任何模型的情况下
    跑通 HTTP 层、SSE 与打包逻辑。
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        runners: Mapping[str, StageRunner] | None = None,
        max_upload_mb: float = 512.0,
        workers: int = 1,
        lock_params: bool = False,
        option_defaults: Mapping[str, Any] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else PROJECT_ROOT / "tmp" / "webui"
        self.max_upload_mb = float(max_upload_mb)
        #: 「4G 特供」模式：参数被服务端锁死（页面只读），PATCH `/options` 直接 409。
        self.lock_params = bool(lock_params)
        #: 新工程的参数默认值。服务端用 `--asr-model` 指定了别的识别模型时，
        #: 「模型」那一栏的默认值要跟着它走，否则页面上的默认值会把它悄悄覆盖掉。
        self.option_defaults: dict[str, Any] = dict(option_defaults or default_options())
        self._runners: dict[str, StageRunner] = dict(runners or {})
        self._workers = max(1, int(workers))
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []            # 新的在前
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._bundle_lock = threading.Lock()
        self._stop = threading.Event()
        self.root.mkdir(parents=True, exist_ok=True)
        # **重启不丢**：把 output/ 里已有的工程读回来（WebUI 建的读 job.json，
        # 命令行跑出来的当"只读产物"收进来）
        self._load_existing()

    # ---------------------------------------------------------------- 启动恢复
    def _load_existing(self) -> None:
        """扫描数据根（默认 `<项目根>/output/`），一个子目录 = 一个工程。

        - 有 `job.json` → WebUI 建的工程，按元数据恢复（上次没跑完的阶段标成中断）；
        - 没有 `job.json` 但有 `project.json`/`report.json` → 别处跑出来的产物，
          作为**只读**工程收进列表（能看卷帘 / 下载 / 导 SVP）；
        - 其它目录（比如空的、放别的东西的）跳过，不动它。
        """
        if not self.root.is_dir():
            return
        restored: list[Job] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            job = self._restore(entry)
            if job is not None:
                restored.append(job)
        restored.sort(key=lambda item: item.created_at, reverse=True)
        with self._lock:
            for job in restored:
                self._jobs[job.id] = job
                self._order.append(job.id)
        if restored:
            logos = [job.name for job in restored[:5]]
            print(f"  已载入 {len(restored)} 个工程：{', '.join(logos)}"
                  + (" …" if len(restored) > 5 else ""), flush=True)

    def _restore(self, root: Path) -> Job | None:
        meta_path = root / "job.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = None
            if isinstance(meta, dict):
                return self._job_from_meta(root, meta)
        if (root / "project.json").is_file() or (root / "report.json").is_file():
            return self._import(root)
        return None

    def _job_from_meta(self, root: Path, meta: Mapping[str, Any]) -> Job:
        audio = str(meta.get("audio") or "")
        # `job.json` 里存的是绝对路径；**包（或数据目录）被整体搬走**时就对不上了，
        # 这时候按布局重新算一遍（工程目录 + `.webui/input/<文件名>`）。
        upload = Path(str(meta.get("upload") or ""))
        if not upload.is_file():
            candidate = root / INTERNAL_DIRNAME / "input" / audio
            upload = candidate if candidate.is_file() else root / INTERNAL_DIRNAME / "input"
        job = Job(
            id=str(meta.get("id") or root.name),
            name=str(meta.get("name") or root.name),
            audio=audio,
            root=root,
            upload=upload,
            out_dir=root,
            created_at=float(meta.get("created_at") or root.stat().st_mtime),
            options=dict(self.option_defaults, **(meta.get("options") or {})),
        )
        for phase in PHASES:
            saved = (meta.get("stages") or {}).get(phase) or {}
            run = job.stages[phase]
            run.label = str(saved.get("label") or run.label)
            run.progress = float(saved.get("progress") or 0.0)
            run.detail = dict(saved.get("detail") or {})
            run.started_at = saved.get("started_at")
            run.finished_at = saved.get("finished_at")
            state = str(saved.get("state") or "idle")
            if state in {"queued", "running"}:
                # 上次是在这个进程里跑的，进程没了就永远不会再推进——别装成"还在跑"
                run.state = "idle"
                run.progress = 0.0
                run.step = ""
                run.message = "上次运行被中断（程序重启），可以重跑"
            else:
                run.state = state
                run.error = saved.get("error")
        job.files = scan_files(root)
        job.highlights = meta.get("highlights")
        job.decisions = meta.get("decisions")
        job.summary = meta.get("summary")
        job.warnings = list(meta.get("warnings") or [])
        job.rev = int(meta.get("rev") or 1)
        interrupted = any(run.message.startswith("上次运行被中断")
                          for run in job.stages.values())
        job.state = DRAFT_STATE if interrupted else str(meta.get("state") or "done")
        job.message = "上次运行被中断（程序重启），可以重跑" if interrupted else "已恢复"
        job.emit("log", phase=None, message="工程已恢复（重启后从 output/ 载入）"
                 + ("；上次没跑完，可以重跑" if interrupted else ""))
        return job

    def _import(self, root: Path) -> Job:
        """`output/` 里本来就有的产物目录：只读收编。"""
        created = root.stat().st_mtime
        job = Job(
            id=root.name, name=root.name, audio="", root=root,
            upload=root / INTERNAL_DIRNAME / "input",
            out_dir=root, created_at=created,
            options=dict(self.option_defaults), imported=True,
        )
        transcribed = (root / "project.json").is_file() or (root / "report.json").is_file()
        if transcribed:
            job.stages["transcribe"].state = "done"
            job.stages["transcribe"].label = PHASE_LABELS["transcribe"]
        if (root / "lyrics.json").is_file():
            job.stages["lyrics"].state = "done"
        job.state = "done" if transcribed else DRAFT_STATE
        job.message = "已有产物（只读）"
        job.files = scan_files(root)
        job.highlights = self._highlights_from_disk(root)
        job.emit("log", phase=None,
                 message="从 output/ 载入已有产物（只读：能看卷帘 / 下载 / 导 SVP，不能重跑）")
        return job

    @staticmethod
    def _highlights_from_disk(root: Path) -> dict[str, Any] | None:
        """给只读工程补一份 `highlights`（产物里有 project/report/summary 就能补）。"""

        def load(name: str) -> Any:
            path = root / name
            if not path.is_file():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

        project = load("project.json")
        if not isinstance(project, dict):
            return None
        payload = {"project": project, "summary": load("summary.json") or {},
                   "decisions": ((load("report.json") or {}).get("decisions")
                                 if isinstance(load("report.json"), dict) else {})}
        try:
            from .runner import build_highlights
        except ImportError:                      # pragma: no cover - 只有拆包用了才发生
            return None
        try:
            return build_highlights(payload)
        except Exception:                        # noqa: BLE001 - 产物不齐时别把恢复搞崩
            return None

    # ---------------------------------------------------------------- 配置
    def set_runners(self, runners: Mapping[str, StageRunner]) -> None:
        self._runners.update(runners)

    @property
    def runners(self) -> Mapping[str, StageRunner]:
        return self._runners

    def env(self) -> dict[str, Any]:
        with self._lock:
            running = next((j.id for j in self._jobs.values() if j.state == "running"), None)
            pending = sum(1 for j in self._jobs.values() if j.state == "queued")
            return {"data_root": str(self.root), "max_upload_mb": self.max_upload_mb,
                    "params_locked": self.lock_params,
                    "queue": {"pending": pending, "running": running}}

    # ---------------------------------------------------------------- 生命周期
    def _ensure_workers(self) -> None:
        with self._lock:
            if self._threads:
                return
            for index in range(self._workers):
                thread = threading.Thread(target=self._work, name=f"sheetsage-worker-{index}",
                                          daemon=True)
                thread.start()
                self._threads.append(thread)

    def shutdown(self, timeout: float = 5.0) -> None:
        """请求停止；正在跑的阶段不会被粗暴中断。"""
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    # ---------------------------------------------------------------- 查询
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    # ---------------------------------------------------------------- 建任务
    def create(self, filename: str, stream: Any,
               plan: Iterable[str] = DEFAULT_PLAN) -> Job:
        """把上传流写到 ``output/<歌名>/.webui/input/``，**默认什么都不排**。

        ``stream`` 只要有 ``read(n)`` 就行（`UploadFile.file` 就是）。超限会立刻中断并
        删掉半截文件——磁盘不留给失败的上传。

        ``plan`` 非空才排队（`/chain` 与显式 ``plan=`` 用得上）；空 plan 时工程停在
        :data:`DRAFT_STATE`，等用户在页面上选完参数点「开始扒谱」——这就是门禁。
        """
        audio = _check_audio(filename)
        wanted = [phase for phase in plan if phase in PHASES]
        job_id = uuid.uuid4().hex[:12]
        created = time.time()
        # 工程目录 = `output/<歌名>/`（和命令行一个形状；重名绝不覆盖）
        root = unique_project_dir(self.root, project_folder_name(Path(audio).stem))
        upload_dir = root / INTERNAL_DIRNAME / "input"
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload = upload_dir / audio

        limit = int(self.max_upload_mb * 1024 * 1024)
        try:
            written = _write_stream(upload, stream, limit, self.max_upload_mb)
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        if written == 0:
            shutil.rmtree(root, ignore_errors=True)
            raise UploadRejected("上传的是空文件")

        job = Job(id=job_id, name=Path(audio).stem or audio, audio=audio, root=root,
                  upload=upload, out_dir=root, created_at=created,
                  plan=list(wanted), options=dict(self.option_defaults))
        job.state = "queued" if wanted else DRAFT_STATE
        job.message = "等待开始" if wanted else "等待参数"
        for phase in wanted:
            job.stages[phase].state = "queued"
        job.emit("log", phase=None, message=f"新建工程 · {audio}"
                 + ("" if wanted else "（选好参数后点「开始扒谱」）"))
        with self._lock:
            self._jobs[job_id] = job
            self._order.insert(0, job_id)
        self._write_metadata(job)
        if wanted:
            self._ensure_workers()
            self._queue.put(job_id)
        return job

    def run_stage(self, job_id: str, phase: str, *, chain: bool = False) -> Job | None:
        """单独（重）跑一个阶段：清掉它的旧状态，塞回待跑队列。

        工作流是 **扒谱 → 歌词**（原型也是这么写的），所以手动跑歌词**必须先有扒谱**：
        没扒谱就抛 :class:`StageNotReady`（API 回 409"请先完成扒谱"）。

        ``chain=True`` 是例外——那是「⏵ 自动提取（扒谱 → 歌词）」：它**故意**在扒谱还没跑完时
        就把歌词排进队列，所以跳过前置检查（计划里本来就先跑扒谱）。
        """
        job = self.get(job_id)
        if job is None:
            return None
        if phase not in PHASES:
            raise ValueError(f"未知阶段 {phase}")
        if job.imported:
            raise ImportedProject(
                f"{job.name} 是 output/ 里已有的产物（没有原音频），不能在这里重跑；"
                f"要重新扒谱请拖入音频新建工程")
        if phase == "lyrics" and not chain:
            self._assert_transcribed(job)
        with job.lock:
            if job.state == "running" and job.stage == phase:
                return job
            run = job.stages[phase]
            run.state = "queued"
            run.progress = 0.0
            run.error = None
            run.step = ""
            run.message = ""
            run.started_at = None
            run.finished_at = None
            job.stages[phase] = run
            if phase not in job.plan:
                job.plan.append(phase)
            # 重跑扒谱会让歌词对不上（音符位置变了）：把歌词退回去，必须重跑
            if phase == "transcribe":
                job.rev += 1
                self._invalidate_lyrics(job, "扒谱重跑")
            job.state = "queued"
            job.cancel_requested = False
        job.emit("log", phase=phase, message=f"{PHASE_LABELS[phase]}：已加入队列")
        self._write_metadata(job)
        self._ensure_workers()
        self._queue.put(job_id)
        return job

    def run_chain(self, job_id: str) -> Job | None:
        """「自动提取」：扒谱 → 歌词，一条龙排进队列（两个都重跑）。"""
        job = self.run_stage(job_id, "transcribe")
        if job is None:
            return None
        return self.run_stage(job_id, "lyrics", chain=True)

    def _assert_transcribed(self, job: Job) -> None:
        """歌词识别的前置条件：扒谱已经完成。

        "有扒谱但没填词"是完全正常的状态——那正是要做歌词识别的时候，所以这里只看扒谱。
        """
        with job.lock:
            state = job.stages["transcribe"].state
        if state != "done":
            raise StageNotReady("请先完成扒谱")

    # ---------------------------------------------------------------- 可调项
    def set_options(self, job_id: str, **values: Any) -> Job | None:
        """改工程级的可调参数（页面上的参数面板，见 :data:`PARAM_SPECS`）。

        改参数 = 换口径，旧结果就不作数了（和换音源、重跑扒谱一个道理）：

        - 扒谱参数（精度 / 设备 / 范围 / 量化）变了 -> 扒谱退回「待开始」，歌词一并作废；
        - 歌词参数（模型 / 音源 / 语言）变了 -> 只作废歌词。

        值不合法抛 :class:`ValueError`（API 回 400）；参数所属的阶段正在跑抛
        :class:`RuntimeError`（API 回 409）；4G 特供模式一律拒绝。
        """
        job = self.get(job_id)
        if job is None:
            return None
        if job.imported:
            raise ImportedProject(
                f"{job.name} 是 output/ 里已有的产物（没有原音频、也没有当时的参数），"
                f"不能在这里改；要重跑请拖入音频新建工程")
        if self.lock_params:
            raise RuntimeError("当前是 4G 特供模式，参数已由服务端锁定")
        values = validate_options(values)
        self._assert_stages_idle(job, {PARAM_OWNER[key] for key in values}, "改参数")
        with job.lock:
            before = dict(job.options)
            job.options.update(values)
            changed = {key for key, value in values.items()
                       if before.get(key) != job.options.get(key)}
            if not changed:
                return job
            why = describe_change(before, job.options)
            invalidated = False
            if any(PARAM_OWNER[key] == "transcribe" for key in changed):
                invalidated = self._invalidate_transcribe(job, why)
            invalidated = self._invalidate_lyrics(job, why) or invalidated
            if not invalidated:
                job.emit("log", phase=None, message=f"参数：{why}")
        self._write_metadata(job)
        return job

    def _assert_stages_idle(self, job: Job, phases: Iterable[str], action: str) -> None:
        """参数所属的阶段正在跑就不让改——那一趟用的是改动前的值，改了界面会和结果对不上。"""
        with job.lock:
            for phase in phases:
                if phase in PHASES and job.stages[phase].state == "running":
                    raise RuntimeError(f"{PHASE_LABELS[phase]}正在跑，{action}请等它结束或先取消")

    # ---------------------------------------------------------------- 音源
    def attach_stem(self, job_id: str, kind: str, filename: str, stream: Any) -> Job | None:
        """给工程补一个音源（目前只有歌词用的 `vocal` 干声）。

        **不做内容校验**：用户故意把混音原曲（甚至别的歌）传进"干声"栏也是允许的——
        那是他自己的选择，我们只如实记录用了哪条。有干声就用干声，没有就用混音原曲，
        页面上不暴露这个选择。

        换音源会让已经识别出来的歌词对不上（音源变了），所以跟重跑扒谱一样作废重来。
        """
        job = self.get(job_id)
        if job is None:
            return None
        if job.imported:
            raise ImportedProject(f"{job.name} 是只读的已有产物，不能补音源")
        if kind not in STEM_KINDS:
            raise ValueError(f"未知音源 {kind}")
        self._assert_lyrics_idle(job, "换音源")
        audio = _check_audio(filename)
        target_dir = job.stems_dir / kind
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / audio
        limit = int(self.max_upload_mb * 1024 * 1024)
        try:
            written = _write_stream(target, stream, limit, self.max_upload_mb)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if written == 0:
            target.unlink(missing_ok=True)
            raise UploadRejected("上传的是空文件")

        with job.lock:
            previous = job.stems.get(kind)
            job.stems[kind] = {"kind": kind, "name": audio, "path": str(target),
                               "size": written, "created_at": time.time()}
            if previous and previous.get("name") != audio:
                (target_dir / str(previous["name"])).unlink(missing_ok=True)
            self._invalidate_lyrics(job, f"歌词音源换成 {audio}")
        job.emit("log", phase="lyrics",
                 message=f"{STEM_LABELS[kind]}已就位：{audio}"
                         if not previous else f"{STEM_LABELS[kind]}已更换：{audio}")
        self._write_metadata(job)
        return job

    def detach_stem(self, job_id: str, kind: str) -> Job | None:
        """移除音源，歌词识别自动回退到混音原曲。"""
        job = self.get(job_id)
        if job is None:
            return None
        if job.imported:
            raise ImportedProject(f"{job.name} 是只读的已有产物，不能改音源")
        if kind not in STEM_KINDS:
            raise ValueError(f"未知音源 {kind}")
        self._assert_lyrics_idle(job, "改音源")
        with job.lock:
            info = job.stems.pop(kind, None)
            if info:
                Path(str(info["path"])).unlink(missing_ok=True)
            self._invalidate_lyrics(job, f"已移除{STEM_LABELS[kind]}，回退到混音原曲")
        if info:
            job.emit("log", phase="lyrics",
                     message=f"已移除{STEM_LABELS[kind]}：歌词识别将用混音原曲")
            self._write_metadata(job)
        return job

    def _assert_lyrics_idle(self, job: Job, action: str) -> None:
        """只有**歌词阶段正在跑**的时候才不许动音源——它正拿这条音频在切片。

        不用 `job.state == "running"` 来判：阶段跑完到任务收尾之间有一个很短的窗口，
        那时任务还是 running，但歌词早就写完了，拦下来只会让人莫名其妙。
        """
        with job.lock:
            if job.stages["lyrics"].state == "running":
                raise RuntimeError(f"歌词识别正在跑，{action}请等它结束或先取消")

    def _invalidate_transcribe(self, job: Job, why: str) -> bool:
        """扒谱参数变了：已经扒出来的谱（连带歌词）不再是"当前结果"，退回待跑。

        返回是否真的作废了什么——没有任何已完成的结果时就不必刷日志。
        """
        run = job.stages["transcribe"]
        if run.state not in STAGE_TERMINAL:
            return False
        job.stages["transcribe"] = StageRun("transcribe", PHASE_LABELS["transcribe"])
        job.emit("log", phase="transcribe", message=f"{why}：已有的扒谱作废，需要重新扒谱")
        return True

    def _invalidate_lyrics(self, job: Job, why: str) -> bool:
        """音源/谱面/参数变了，已识别的歌词就不作数了（调用方持有 job.lock）。"""
        run = job.stages["lyrics"]
        if run.state not in STAGE_TERMINAL:
            return False
        job.stages["lyrics"] = StageRun("lyrics", PHASE_LABELS["lyrics"])
        job.emit("log", phase="lyrics", message=f"{why}：已识别的歌词作废，需要重跑")
        return True

    # ---------------------------------------------------------------- 取消/删除
    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        with job.lock:
            if job.state in TERMINAL_STATES:
                return job
            job.cancel_requested = True
            if job.state in (DRAFT_STATE, "queued"):
                # 还没开跑（或停在「等参数」），直接落终态，工作线程拿到时会跳过
                job.state = "cancelled"
                for phase in list(job.plan):
                    job.stages[phase].state = "cancelled"
                job.plan.clear()
                job.stage = None
        job.emit("cancelled", phase=job.stage, message="已在排队阶段取消")
        self._write_metadata(job)
        return job

    def delete(self, job_id: str) -> bool:
        """删除任务与它的全部产物。正在跑的任务必须先取消。

        `output/` 里**本来就有的**产物不在这里删：那是命令行 / 别处跑出来的东西，
        删它等于删用户的作品——要让列表干净请手动删目录（提示里会写清楚）。
        """
        job = self.get(job_id)
        if job is None:
            return False
        if job.imported:
            raise ImportedProject(
                f"{job.name} 是 output/ 里已有的产物，不能从这里删"
                f"（要删请手动删目录 {job.root}）")
        if job.state == "running":
            raise RuntimeError("任务正在运行，请先取消")
        with self._lock:
            self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        shutil.rmtree(job.root, ignore_errors=True)
        return True

    # ---------------------------------------------------------------- 打包
    def bundle(self, job: Job) -> Path:
        """把 ``output/`` 打成一个 zip（懒生成，落盘缓存）。

        打包含一把独立的锁：它跑在请求线程里，不能占用 ``job.lock``——那个锁还要给
        工作线程汇报进度，一个几百 MB 的产物目录会把进度卡住。
        """
        target = job.internal_dir / "bundle.zip"
        if target.is_file():
            return target
        with self._bundle_lock:
            if target.is_file():                       # 等锁期间别人已经打好了
                return target
            files = scan_files(job.out_dir)
            if not files:
                raise FileNotFoundError("还没有产物可打包")
            job.internal_dir.mkdir(parents=True, exist_ok=True)
            temp = job.internal_dir / "bundle.zip.part"
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as zf:
                for item in files:
                    path = job.out_dir / item["path"]
                    zf.write(path, arcname=f"{safe_name(job.name)}/{item['path']}")
            temp.replace(target)
        return target

    # ---------------------------------------------------------------- 工作线程
    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                return
            job = self.get(job_id)
            if job is None:
                continue
            try:
                self._run(job)
            except Exception as exc:  # 工作线程绝不能死掉
                self._fail(job, f"内部错误：{type(exc).__name__}: {exc}")

    def _next_phase(self, job: Job) -> str | None:
        with job.lock:
            while job.plan:
                phase = job.plan.pop(0)
                if job.stages[phase].state in STAGE_TERMINAL:
                    continue
                return phase
        return None

    def _run(self, job: Job) -> None:
        with job.lock:
            if job.cancel_requested:
                job.state = "cancelled"
                return
            job.state = "running"
        while True:
            phase = self._next_phase(job)
            if phase is None:
                break
            self._run_phase(job, phase)
            with job.lock:
                if job.cancel_requested:
                    break
        self._finish(job)

    def _run_phase(self, job: Job, phase: str) -> None:
        run = job.stages[phase]
        with job.lock:
            run.state = "running"
            run.started_at = time.time()
            run.finished_at = None
            job.stage = phase
        job.emit("phase", phase=phase, state="running", label=run.label)
        runner = self._runners.get(phase)
        if runner is None:
            from .runner import default_runners

            self.set_runners(default_runners())
            runner = self._runners[phase]
        progress = Progress(job, phase)
        try:
            payload = runner(job, progress) or {}
        except JobCancelled:
            with job.lock:
                run.state = "cancelled"
                run.finished_at = time.time()
            job.emit("phase_cancelled", phase=phase, message="阶段已取消")
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            with job.lock:
                run.state = "failed"
                run.error = message
                run.finished_at = time.time()
            job.emit("phase_error", phase=phase, message=message)
        else:
            detail = payload.get("detail") or {}
            with job.lock:
                run.state = "done"
                run.progress = 1.0
                run.finished_at = time.time()
                run.detail.update(detail)
            job.emit("phase_done", phase=phase, detail=run.detail)
        finally:
            with job.lock:
                job.stage = None
            self._write_metadata(job)

    def _finish(self, job: Job) -> None:
        """所有阶段都跑完（或停了）之后：汇总任务级状态 + 扫产物。"""
        with job.lock:
            states = [job.stages[phase].state for phase in PHASES]
            if job.cancel_requested or "cancelled" in states:
                job.state = "cancelled"
            elif "failed" in states:
                job.state = "failed"
            else:
                job.state = "done"
            job.files = scan_files(job.out_dir)
            job.warnings = list(job.stages["transcribe"].detail.get("warnings") or [])
            job.stage = None
            state = job.state
        payload = {"message": "全部完成" if state == "done" else "任务已结束", "state": state}
        job.emit(state if state != "failed" else "error", **payload)
        self._write_metadata(job)

    def _fail(self, job: Job, message: str) -> None:
        with job.lock:
            job.state = "failed"
        job.emit("error", phase=job.stage, message=message)
        self._write_metadata(job)

    def _write_metadata(self, job: Job) -> None:
        # 只读工程（output/ 里本来就有的产物）不写元数据：那会把别人跑出来的目录
        # 变成"WebUI 工程"，下次重启就按 WebUI 工程恢复了
        if job.imported:
            return
        try:
            job.root.mkdir(parents=True, exist_ok=True)
            (job.root / "job.json").write_text(
                json.dumps(job.metadata(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # 元数据只是诊断用，写不进去不该影响任务本身
            pass


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
