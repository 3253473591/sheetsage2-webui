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
import os
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
from ..fsutil import move_into, write_text_atomic
from .errors import StageFailure, describe as describe_error

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

#: 软删除的回收站（数据根下的 `.trash/`，点开头所以 `_load_existing` 不扫它）。
#: 删除 = 把工程目录 `rename` 进去（同卷原子），用户感知和"删了"一样，但能恢复。
TRASH_DIRNAME = ".trash"
#: 回收站保留策略：超过这些就真删（`_purge_trash` 在启动与每次删除时跑）。
TRASH_MAX_DAYS = 7.0
TRASH_MAX_BYTES = 2 * 1024 ** 3

#: 产物清单（`.webui/manifest.json`）：只有它列出的文件才是"当前这一版的结果"。
MANIFEST_NAME = "manifest.json"

#: 阶段级超时（秒）。`STALL` = 多久**没有任何进展**算卡死；`HARD` = 总时长上限。
#: 实测：一首 4 分钟的歌 GPU 扒谱约 2~4 分钟、CPU 30~90 分钟——CPU 退路靠 HARD 兜。
STALL_TIMEOUT = {"transcribe": 900.0, "lyrics": 600.0}
HARD_TIMEOUT = {"transcribe": 7200.0, "lyrics": 3600.0}

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
    #: 最后一次"有进展"（step/progress/log 任一变化）的时刻，看门狗拿它判卡死。
    last_advance_at: float | None = None
    #: **人话**（横条上显示的那一句），不是异常原文——见 `errors.py`。
    error: str | None = None
    #: 错误码 / 下一步 / 技术原文（`errors.describe()` 的四段，`error` 是第二段）。
    error_code: str | None = None
    error_action: str | None = None
    error_detail: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)   # 该阶段的产物统计

    def fail(self, exc: BaseException) -> None:
        """把一次异常落成"用户能看懂"的三段（调用方持有 `job.lock`）。"""
        error = describe_error(exc, phase=self.key)
        self.state = "failed"
        self.error = error.message
        self.error_code = error.code
        self.error_action = error.action
        self.error_detail = error.detail

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
            "last_advance_at": self.last_advance_at,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "error": self.error,
            "error_code": self.error_code,
            "error_action": self.error_action,
            "error_detail": self.error_detail,
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
    #: **产物版本戳**：每完成一轮阶段 +1（扒谱 +1、歌词 +1）。和 `rev` 分工不同——
    #: `rev` 管"用户改了参数"（界面上显示 rev N），这里是"磁盘上这批产物是第几版"，
    #: 所有派生产物的缓存键都带上它（zip / SVP / 波形），重跑之后缓存必然失效。
    artifacts_rev: int = 0
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

    @property
    def manifest_path(self) -> Path:
        """产物清单：`/files` 与 `/download` 只信它（见 `JobManager.write_manifest`）。"""
        return self.internal_dir / MANIFEST_NAME

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
                "artifacts_rev": self.artifacts_rev,
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
                "error_code": next((s.error_code for s in self.stages.values()
                                    if s.error_code), None),
                "error_action": next((s.error_action for s in self.stages.values()
                                      if s.error_action), None),
                "error_detail": next((s.error_detail for s in self.stages.values()
                                      if s.error_detail), None),
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
            self.run.last_advance_at = time.time()
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
            self.run.last_advance_at = time.time()
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
            self.run.last_advance_at = time.time()
        self.job.emit("log", phase=self.phase, message=message, **extra)

    def check(self) -> None:
        """取消检查点。

        用户点「停止」、或者看门狗判定这一阶段卡死/超时之后，`cancel_requested` 会被置上——
        下一次回调进来就抛 :class:`JobCancelled`，把工作线程放出来（推理本身不能被打断，
        但至少不会永远占着线程）。
        """
        with self.job.lock:
            if self.job.cancel_requested:
                raise JobCancelled("用户取消" if self.run.error_code is None else self.run.error or "任务已中止")


StageRunner = Callable[[Job, Progress], dict[str, Any]]


@dataclass
class _PhaseWatch:
    """看门狗盯的一趟运行（G6）。

    `fired` 一旦为真，说明这一阶段已经被判定失败（卡死/超时）——工作线程回来之后
    **不许再把它改回 done**（那正是"进度条永远停在 62%"的另一面：看起来失败，实际成功了）。
    """

    job_id: str
    phase: str
    started_at: float
    fired: bool = False


def _per_phase(value: float | Mapping[str, float] | None,
               defaults: Mapping[str, float]) -> dict[str, float]:
    """超时参数：一个数 = 两个阶段都用它；一个映射 = 分别给。"""
    if value is None:
        return dict(defaults)
    if isinstance(value, Mapping):
        return {phase: float(value.get(phase, defaults[phase])) for phase in defaults}
    return {phase: float(value) for phase in defaults}


def _duration_text(seconds: float) -> str:
    """秒 -> 人话（"35 秒" / "12 分钟" / "1.5 小时"），只用在超时提示里。"""
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


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
        stall_timeout: float | Mapping[str, float] | None = None,
        hard_timeout: float | Mapping[str, float] | None = None,
        watchdog_interval: float = 1.0,
        trash_max_days: float = TRASH_MAX_DAYS,
        trash_max_bytes: float = TRASH_MAX_BYTES,
        use_trash: bool = True,
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
        #: 阶段守护（G6）：无进展超过 `stall_timeout` 或总时长超过 `hard_timeout`
        #: 就把阶段判失败。测试里传一个很小的数就能验证（见 `test_watchdog_*`）。
        self.stall_timeout = _per_phase(stall_timeout, STALL_TIMEOUT)
        self.hard_timeout = _per_phase(hard_timeout, HARD_TIMEOUT)
        self.watchdog_interval = max(0.05, float(watchdog_interval))
        self._watch: dict[tuple[str, str], _PhaseWatch] = {}
        self._watchdog: threading.Thread | None = None
        #: 软删除（G2）：删除 = 挪进 `.trash/`，能恢复；`use_trash=False` 是硬删（测试用）
        self.use_trash = bool(use_trash)
        self.trash_max_days = float(trash_max_days)
        self.trash_max_bytes = float(trash_max_bytes)
        self.root.mkdir(parents=True, exist_ok=True)
        # **重启不丢**：把 output/ 里已有的工程读回来（WebUI 建的读 job.json，
        # 命令行跑出来的当"只读产物"收进来）
        self._load_existing()
        self._purge_trash()

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
                run.error_code = saved.get("error_code")
                run.error_action = saved.get("error_action")
                run.error_detail = saved.get("error_detail")
        job.files = scan_files(root)
        job.highlights = meta.get("highlights")
        job.decisions = meta.get("decisions")
        job.summary = meta.get("summary")
        job.warnings = list(meta.get("warnings") or [])
        job.rev = int(meta.get("rev") or 1)
        job.artifacts_rev = int(meta.get("artifacts_rev") or 0)
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
            if self._watchdog is None:
                self._watchdog = threading.Thread(target=self._watch_stages,
                                                  name="sheetsage-watchdog", daemon=True)
                self._watchdog.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """请求停止；正在跑的阶段不会被粗暴中断。"""
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
        if self._watchdog is not None:
            self._watchdog.join(timeout=timeout)
            self._watchdog = None

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

        缓存**跟着产物版本走**（G3）：`bundle.meta.json` 里记着打包时的 `artifacts_rev`，
        重跑过（rev 变了）就一定重打，不会把上一版的结果发出去。

        打包含一把独立的锁：它跑在请求线程里，不能占用 ``job.lock``——那个锁还要给
        工作线程汇报进度，一个几百 MB 的产物目录会把进度卡住。
        """
        target = job.internal_dir / "bundle.zip"
        meta = job.internal_dir / "bundle.meta.json"
        with self._bundle_lock:
            if target.is_file() and self._bundle_is_fresh(job, meta):
                return target
            listed = self.manifest_files(job)
            files = [item for item in scan_files(job.out_dir)
                     if listed is None or item["path"] in listed]
            if not files:
                raise FileNotFoundError("还没有产物可打包")
            job.internal_dir.mkdir(parents=True, exist_ok=True)
            temp = job.internal_dir / "bundle.zip.part"
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as zf:
                for item in files:
                    path = job.out_dir / item["path"]
                    if not path.is_file():          # 清单里列着但磁盘上没了：跳过
                        continue
                    zf.write(path, arcname=f"{safe_name(job.name)}/{item['path']}")
            temp.replace(target)
            try:
                write_text_atomic(meta, json.dumps({
                    "artifacts_rev": job.artifacts_rev,
                    "files": len(files),
                    "built_at": time.time(),
                }, ensure_ascii=False))
            except OSError:
                pass
        return target

    @staticmethod
    def _bundle_is_fresh(job: Job, meta: Path) -> bool:
        """缓存还认不认：版本戳一致 + 清单在 + 包里有东西。"""
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        try:
            stamped = int(data.get("artifacts_rev", -1))
        except (TypeError, ValueError):
            return False
        return stamped == job.artifacts_rev and int(data.get("files") or 0) > 0

    # ---------------------------------------------------------------- 删除 / 回收站
    @property
    def trash_root(self) -> Path:
        return self.root / TRASH_DIRNAME

    def delete(self, job_id: str, *, permanent: bool = False) -> bool:
        """删除工程。默认是**软删除**：目录 rename 进 `.trash/`，能恢复（G2）。

        为什么不是直接 `rmtree`：删除按钮就在「下载 ▾」旁边，手指偏 20 像素就是几小时的
        活全没（连原音频都一起没了）。`rename` 是原子的，用户感知和"删了"一样。

        `output/` 里**本来就有的**产物不在这里删：那是命令行 / 别处跑出来的东西，
        删它等于删用户的作品。
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
        if permanent or not self.use_trash:
            shutil.rmtree(job.root, ignore_errors=True)
            return True
        self.trash_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        entry = self.trash_root / f"{safe_name(job.name)}-{job.id}-{stamp}"
        try:
            move_into(job.root, entry)
        except OSError:
            # 挪不进回收站就别硬删：宁可报错让用户重试，也不能把东西弄丢
            with self._lock:
                self._jobs[job_id] = job
                self._order.insert(0, job_id)
            raise RuntimeError(f"删除失败：工程目录被占用（{job.root}），关掉相关程序再试")
        self._purge_trash()
        return True

    def trash(self) -> list[dict[str, Any]]:
        """回收站列表（新的在前）。每项就是一个被挪进来的工程目录。"""
        if not self.trash_root.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for entry in sorted(self.trash_root.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            meta: dict[str, Any] = {}
            try:
                meta = json.loads((entry / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            size = 0
            for path in entry.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except OSError:
                    continue
            stat = entry.stat()
            entries.append({
                "id": entry.name,
                "name": str(meta.get("name") or entry.name),
                "job_id": meta.get("id"),
                "deleted_at": stat.st_mtime,
                "size": size,
                "files": len(meta.get("files") or []),
            })
        return entries

    def restore(self, entry_id: str) -> Job | None:
        """把回收站里的工程挪回 `output/`（重名就 `-2`），并重新登记。"""
        source = self.trash_root / safe_name(entry_id)
        if not source.is_dir() or TRASH_DIRNAME not in str(source):
            return None
        try:
            meta = json.loads((source / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        base = str(meta.get("name") or source.name)
        target = unique_project_dir(self.root, project_folder_name(base))
        move_into(source, target)
        job = self._restore(target)
        if job is None:
            return None
        with self._lock:
            if job.id in self._jobs:                  # id 撞了（极少见）：挪个名字
                job.id = f"{job.id}-{uuid.uuid4().hex[:4]}"
            self._jobs[job.id] = job
            self._order.insert(0, job.id)
        job.emit("log", phase=None, message=f"已从回收站恢复：{job.name}")
        self._write_metadata(job)
        return job

    def purge_from_trash(self, entry_id: str) -> bool:
        """彻底删掉回收站里的一项（不可恢复）。"""
        target = self.trash_root / safe_name(entry_id)
        if not target.is_dir() or TRASH_DIRNAME not in str(target):
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    def _purge_trash(self) -> None:
        """回收站保留策略：超过 7 天、或总共超过 2GB，就真删最旧的。"""
        if not self.trash_root.is_dir():
            return
        entries = []
        for entry in self.trash_root.iterdir():
            if not entry.is_dir():
                continue
            try:
                entries.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
        entries.sort()
        deadline = time.time() - self.trash_max_days * 86400.0
        total = 0
        sizes: list[tuple[float, int, Path]] = []
        for mtime, entry in entries:
            size = 0
            for path in entry.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except OSError:
                    continue
            sizes.append((mtime, size, entry))
            total += size
        for mtime, size, entry in sizes:
            if mtime < deadline or total > self.trash_max_bytes:
                shutil.rmtree(entry, ignore_errors=True)
                total -= size

    # ---------------------------------------------------------------- 看门狗
    def _watch_begin(self, job: Job, phase: str) -> _PhaseWatch:
        watch = _PhaseWatch(job.id, phase, time.time())
        with self._lock:
            self._watch[(job.id, phase)] = watch
        return watch

    def _watch_end(self, job: Job, phase: str) -> None:
        with self._lock:
            self._watch.pop((job.id, phase), None)

    def _watch_stages(self) -> None:
        """每秒扫一遍在跑的阶段：**无进展**太久或总时长超上限就判失败（G6）。

        为什么要它：推理卡死（驱动/显存/cuBLAS）时一个进度回调都不会来，用户点「停止」
        也没用（协作式取消要等下个检查点），界面就永远停在 62%。这里从**另一个线程**
        判死并把 `cancel_requested` 置上，让工作线程在下个检查点退出、并把阶段标成
        失败 + 人话（"卡住了 N 分钟没有进展"）。
        """
        while not self._stop.wait(self.watchdog_interval):
            with self._lock:
                watches = list(self._watch.values())
            for watch in watches:
                if watch.fired:
                    continue
                job = self.get(watch.job_id)
                if job is None:
                    continue
                with job.lock:
                    run = job.stages.get(watch.phase)
                    if run is None or run.state != "running":
                        continue
                    now = time.time()
                    started = run.started_at or watch.started_at
                    last = run.last_advance_at or started
                    idle = max(0.0, now - last)
                    elapsed = max(0.0, now - started)
                stall = self.stall_timeout.get(watch.phase, 0.0)
                hard = self.hard_timeout.get(watch.phase, 0.0)
                if stall and idle >= stall:
                    self._abort_phase(
                        watch, "E_STALLED",
                        f"卡住了 {_duration_text(idle)}没有任何进展",
                        f"最后一次进展 {_duration_text(idle)}前；"
                        f"这一阶段已运行 {_duration_text(elapsed)}")
                elif hard and elapsed >= hard:
                    self._abort_phase(
                        watch, "E_TIMEOUT",
                        f"这一阶段跑了 {_duration_text(elapsed)}，超过上限",
                        f"启动于 {_duration_text(elapsed)}前，最后一次进展 "
                        f"{_duration_text(idle)}前")

    def _abort_phase(self, watch: _PhaseWatch, code: str, message: str,
                     detail: str = "") -> None:
        """看门狗判死一个阶段：落失败态 → 发事件 → 让工作线程退出。"""
        job = self.get(watch.job_id)
        if job is None:
            return
        watch.fired = True
        with job.lock:
            run = job.stages.get(watch.phase)
            if run is None or run.state != "running":
                return
            run.fail(StageFailure(code, message, detail=detail))
            run.finished_at = time.time()
            run.message = message
            job.cancel_requested = True          # 让 runner 在下个检查点退出
            job.stage = None
            error = {"code": run.error_code, "message": run.error,
                     "action": run.error_action, "detail": run.error_detail}
        job.emit("phase_error", phase=watch.phase, **error)
        job.emit("log", phase=watch.phase, message=f"已强制中止：{message}")
        self._write_metadata(job)

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
                self._fail(job, exc)

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
            run.last_advance_at = run.started_at
            run.finished_at = None
            run.error = None
            run.error_code = None
            run.error_action = None
            run.error_detail = None
            job.stage = phase
        # 这一阶段会重写产物：上一版的派生缓存（zip / SVP / 波形）一定过期了，
        # 旧产物先挪进 `.webui/prev/`——`output/` 永远只表示"当前这一版"。
        self._clean_derived(job)
        self._quarantine_stage_outputs(job, phase)
        job.emit("phase", phase=phase, state="running", label=run.label)
        runner = self._runners.get(phase)
        if runner is None:
            from .runner import default_runners

            self.set_runners(default_runners())
            runner = self._runners[phase]
        progress = Progress(job, phase)
        watch = self._watch_begin(job, phase)
        try:
            payload = runner(job, progress) or {}
        except JobCancelled:
            if not watch.fired:                 # 看门狗判过死就别覆盖它的失败态
                with job.lock:
                    run.state = "cancelled"
                    run.finished_at = time.time()
                job.emit("phase_cancelled", phase=phase, message="阶段已取消")
        except Exception as exc:
            if not watch.fired:                 # 同上：卡死/超时的结论优先
                with job.lock:
                    run.fail(exc)
                    run.finished_at = time.time()
                    error = {"code": run.error_code, "message": run.error,
                             "action": run.error_action, "detail": run.error_detail}
                job.emit("phase_error", phase=phase, **error)
        else:
            detail = payload.get("detail") or {}
            if watch.fired:
                # 判死之后 runner 才回来：保留失败态，但把这一轮的真实统计收下
                with job.lock:
                    run.detail.update(detail)
            else:
                self._settle_phase(job, phase, detail)
        finally:
            self._watch_end(job, phase)
            with job.lock:
                job.stage = None
            self._write_metadata(job)

    # ---------------------------------------------------------------- 完成门禁
    def _settle_phase(self, job: Job, phase: str, detail: Mapping[str, Any]) -> None:
        """阶段收尾：**先体检产物，通过了才算 done**（G4）。"""
        failure: StageFailure | None = None
        with job.lock:
            run = job.stages[phase]
            run.detail.update(detail)
            failure = self._completion_gate(job, phase, run.detail)
            if failure is None:
                run.state = "done"
                run.progress = 1.0
                run.finished_at = time.time()
                # 产物换了一版：所有派生缓存（zip / SVP / peaks）都靠它失效
                job.artifacts_rev += 1
                error = None
            else:
                run.fail(failure)
                run.finished_at = time.time()
                error = {"code": run.error_code, "message": run.error,
                         "action": run.error_action, "detail": run.error_detail}
        if error is None:
            job.emit("phase_done", phase=phase, detail=run.detail)
            self._flush_prev(job)
        else:
            job.emit("phase_error", phase=phase, **error)
        self.write_manifest(job)

    def _completion_gate(self, job: Job, phase: str,
                         detail: Mapping[str, Any]) -> StageFailure | None:
        """"完成"的依据不是"runner 没抛异常"，而是**产物真的齐了**。

        扒谱要 `project.json` / `report.json` / `summary.json` 都在、都可解析；
        歌词要 `lyrics.json` 可解析且工程还在。runner 自己做的体检
        （`detail["artifacts"]`）不通过也算失败——那是最细的一层（15 个文本 + 6 个 MIDI）。
        """
        report = detail.get("artifacts")
        if isinstance(report, Mapping) and not report.get("ok", True):
            missing = list(report.get("missing") or [])
            problems = list(report.get("problems") or [])
            text = "产物自检没通过"
            if missing:
                text += f"：缺 {len(missing)} 个文件（{', '.join(missing[:4])}"
                text += " 等）" if len(missing) > 4 else "）"
            elif problems:
                text += f"：{problems[0]}"
            return StageFailure(
                "E_ARTIFACTS_INCOMPLETE", text,
                "重跑这一阶段；如果反复出现，把诊断信息发给我",
                "；".join(problems) or f"missing={missing}")

        required = ("project.json", "report.json", "summary.json") \
            if phase == "transcribe" else ("project.json", "lyrics.json")
        missing = [name for name in required
                   if not (job.out_dir / name).is_file()
                   or (job.out_dir / name).stat().st_size == 0]
        if missing:
            return StageFailure(
                "E_ARTIFACTS_INCOMPLETE", f"产物不完整：缺 {', '.join(missing)}",
                "重跑这一阶段；如果反复出现，把诊断信息发给我",
                f"{phase} 结束时缺 {missing}")
        for name in required:
            if not name.endswith(".json"):
                continue
            try:
                json.loads((job.out_dir / name).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return StageFailure(
                    "E_ARTIFACTS_INCOMPLETE", f"{name} 读不出来（可能只写了一半）",
                    "重跑这一阶段；如果反复出现，把诊断信息发给我",
                    f"{name}: {type(exc).__name__}: {exc}")
        return None

    # ---------------------------------------------------------------- 产物版本
    @staticmethod
    def _derived_patterns() -> tuple[str, ...]:
        return ("bundle.zip", "bundle.meta.json", "peaks-*.json")

    def _clean_derived(self, job: Job) -> None:
        """阶段开始前清掉上一版的派生缓存（它们都能重算，留着只会发旧货）。"""
        internal = job.internal_dir
        for pattern in self._derived_patterns():
            for path in internal.glob(pattern):
                path.unlink(missing_ok=True)
        export = internal / "export"
        if export.is_dir():
            for path in export.glob("*.svp*"):
                path.unlink(missing_ok=True)

    def _quarantine_stage_outputs(self, job: Job, phase: str) -> None:
        """把这阶段上一轮写的产物挪进 `.webui/prev/<rev>-<时间>/`。

        重跑中途失败/取消时，`output/` 里不会出现"上一轮的歌词 + 这一轮的项目"这种
        混搭；旧版本在 `prev/` 里还留了一份，跑成功之后再清掉（`_flush_prev`）。
        """
        from ..result import EXPECTED_MIDI, EXPECTED_TEXT

        if phase == "transcribe":
            # 重跑扒谱会连带作废歌词（音符位置变了），所以旧 `lyrics.json` 也一起挪走——
            # 不然会留下"这一轮的新谱面 + 上一轮的旧歌词"这种混搭，下载包里也会带上旧歌词。
            names: list[str] = (["project.json", "report.json", "summary.json", "lyrics.json"]
                                + list(EXPECTED_TEXT) + list(EXPECTED_MIDI))
        else:
            names = ["lyrics.json"]
        present = [name for name in names if (job.out_dir / name).is_file()]
        if not present:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = job.internal_dir / "prev" / f"{job.artifacts_rev:04d}-{stamp}"
        for name in present:
            try:
                move_into(job.out_dir / name, target / name)
            except OSError:
                # 挪不动（被占用/权限）就算了：宁可留着旧文件，也不能把这一阶段搞挂
                pass

    @staticmethod
    def _flush_prev(job: Job) -> None:
        """阶段成功后清掉 `prev/`（旧版本已经没有用了）。"""
        prev = job.internal_dir / "prev"
        if prev.is_dir():
            shutil.rmtree(prev, ignore_errors=True)

    def manifest_files(self, job: Job) -> dict[str, Any] | None:
        """读产物清单；没有就是 `None`（老工程 / 只读工程按目录扫描走）。"""
        try:
            data = json.loads(job.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        files = data.get("files")
        return dict(files) if isinstance(files, dict) else None

    def write_manifest(self, job: Job) -> None:
        """落 `<工程>/.webui/manifest.json`：`/files` 与 `/download` 只信它。

        这样"上一轮残留但这一轮没产出"的文件不会混进下载包——磁盘上留着是一回事，
        当成这一版的结果发出去是另一回事。
        """
        if job.imported:
            return
        try:
            with job.lock:
                payload = {
                    "artifacts_rev": job.artifacts_rev,
                    "rev": job.rev,
                    "updated_at": time.time(),
                    "options": dict(job.options),
                    "stages": {key: run.state for key, run in job.stages.items()},
                    "files": {item["path"]: {"size": item["size"]}
                              for item in scan_files(job.out_dir)},
                }
            write_text_atomic(job.manifest_path,
                              json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            pass

    # ---------------------------------------------------------------- 收尾
    def _finish(self, job: Job) -> None:
        """所有阶段都跑完（或停了）之后：汇总任务级状态 + 扫产物。"""
        with job.lock:
            states = [job.stages[phase].state for phase in PHASES]
            if job.cancel_requested and "failed" not in states:
                job.state = "cancelled"
            elif "cancelled" in states:
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
        self.write_manifest(job)
        self._write_metadata(job)

    def _fail(self, job: Job, exc: BaseException | str) -> None:
        """工作线程兜底：连 `_run` 都炸了，至少给一句人话（不是异常类名）。"""
        error = describe_error(exc) if isinstance(exc, BaseException) else None
        with job.lock:
            job.state = "failed"
        payload: dict[str, Any] = {
            "message": error.message if error else str(exc),
        }
        if error is not None:
            payload.update(code=error.code, action=error.action, detail=error.detail)
        job.emit("error", phase=job.stage, **payload)
        self._write_metadata(job)

    def _write_metadata(self, job: Job) -> None:
        # 只读工程（output/ 里本来就有的产物）不写元数据：那会把别人跑出来的目录
        # 变成"WebUI 工程"，下次重启就按 WebUI 工程恢复了
        if job.imported:
            return
        try:
            with job.lock:
                payload = job.metadata()
            write_text_atomic(job.root / "job.json",
                              json.dumps(payload, ensure_ascii=False, indent=2))
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
