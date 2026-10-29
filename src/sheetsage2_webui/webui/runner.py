"""真实 runner：扒谱 + 歌词识别两条阶段，以及模型的懒加载与显存腾挪。

**进度为什么要翻译**：模型的 `progress` 回调是逐窗口、逐解码步触发的
（`stage` 有 audio / encoding / decoding / window_complete / notation），而界面上的两张卡
各自要一个进度条和一句"当前小步骤"。这里把它压成：

| phase | 小步骤（界面显示） | 进度 |
|---|---|---|
| `transcribe` | 加载模型 → 读取音频 → 识别音乐 → 生成工程 → 整理产物 | 0 → 1 |
| `lyrics` | 读取工程 → 加载识别模型 → 语音识别 → 歌词对齐 → 写入卷帘 | 0 → 1 |

阶段 key 与 `jobs.PHASES` 一致；小步骤名是**真实步骤**，不是凑数的装饰。

**两个模型轮流用显存**：SheetSage2 常驻约 0.5G（含 MERT 更多），Qwen3-ASR-1.7B 峰值
约 6.3G。10G 的卡上两个一起留着就危险了，所以默认在切换阶段前把上一个放掉
（`free_between_stages`）。代价是重跑扒谱要重新加载模型，换来的是不 OOM。
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..asr import (AsrConfig, AsrEngine, normalize_language, parsed_sections,
                   sections_from_lab, widen_to_vocals)
from ..engine import EngineConfig, SheetSage2Engine
from ..fsutil import write_text_atomic
from ..lyrics import DEFAULT_TOLERANCE, fill_project, note_spans_seconds
from ..pipeline import run as pipeline_run
from ..result import EXPECTED_MIDI, EXPECTED_TEXT, inspect_output_dir
from ..sections import is_instrumental
from ..timeline import PPQ, from_stored_measures
from .jobs import (PHASE_LABELS, Job, Progress, StageRunner, asr_model_label)

#: 扒谱阶段的小步骤与进度锚点。`识别音乐` 占大头是因为它就是推理本身。
STEP_LOAD = ("加载模型", 0.00)
STEP_READ = ("读取音频", 0.04)
STEP_RECOGNIZE = ("识别音乐", 0.06)
STEP_BUILD = ("生成工程", 0.88)
STEP_PACK = ("整理产物", 0.98)
RECOGNIZE_SPAN = 0.80

#: 歌词阶段的小步骤与进度锚点。
LYRIC_READ = ("读取工程", 0.02)
LYRIC_LOAD = ("加载识别模型", 0.05)
LYRIC_ASR = ("语音识别", 0.10)
LYRIC_ALIGN = ("歌词对齐", 0.85)
LYRIC_WRITE = ("写入卷帘", 0.95)
ASR_SPAN = 0.72

#: 解码进度的分子估计：模型单窗口的 token 上限约 2k，超过这个数就按 90% 封顶。
TOKENS_REFERENCE = 1500.0
SUB_PROGRESS_CAP = 0.90

#: `PPQ / grid_ticks` -> 网格名。键 = 一个四分音符被切成几份。
#:
#: 命名与 **Synthesizer V 的 SNAP 菜单**逐条对齐（用户看到的就是这套词）：
#: 四分音符 / 8 分 / 16 分 / **16 分三连音** / 32 分 / **32 分三连音** / 64 分 /
#: **64 分三连音** / 128 分。SynthV 的两支是 1/2^k（直线）与 1/(3·2^k)（三连音），
#: 我们的一拍/96 = 1/96 正好落在三连音支上（96 = 24 × 4，即 64 分三连音的再细分）。
GRID_DIVISIONS = {1: "四分音符", 2: "8 分音符", 3: "16 分三连音", 4: "16 分音符",
                  6: "16 分三连音", 8: "32 分音符", 12: "32 分三连音", 16: "64 分音符",
                  24: "64 分三连音", 32: "128 分音符", 48: "128 分三连音",
                  96: "128 分三连音"}

#: 网格步长不是 PPQ 的整数分之一时的兜底说法（正常口径下不会走到）。
FALLBACK_GRID_NOTE = "自定义网格"

#: 歌词分行：相邻两字的间隔超过这个秒数就断行（乐句之间的换气）。
LINE_GAP_SECONDS = 1.0
#: 一行最多几个字（中文歌词一行 10~20 字，超过就断，免得整段糊成一行）。
LINE_MAX_CHARS = 18


def grid_note_name(grid_ticks: int | None, ppq: int = PPQ) -> str:
    """把网格步长（项目 tick）翻译成 Synthesizer V SNAP 菜单里的说法。

    网格步长 = 一拍 / 96，即 `ppq / grid_ticks == 96` → "128 分三连音"
    （96 = 32 × 3：同时覆盖直线族与三连音族）。
    """
    if not grid_ticks:
        return "未量化"
    if ppq % grid_ticks == 0:
        name = GRID_DIVISIONS.get(ppq // grid_ticks)
        if name:
            return name
    return FALLBACK_GRID_NOTE


def group_lyric_lines(chars: Sequence[Any], *,
                      gap: float = LINE_GAP_SECONDS,
                      max_chars: int = LINE_MAX_CHARS) -> list[dict[str, Any]]:
    """把逐字时间戳按"换气间隙"和"最大字数"折成歌词行。

    界面上的歌词卷帘是按行画的（原型里就是这样），所以行是**派生的展示结构**：
    字才是真数据。同一个函数既服务"边识别边出"的实时事件，也服务落盘的 `lyrics.json`，
    两边一致。

    英文一个词会被拆成多个 token，这里只按时间间隙断行，不做分词——它决定的是**显示单位**，
    真正的对齐与填词仍然按字走。
    """
    lines: list[dict[str, Any]] = []
    for item in chars:
        text = str(getattr(item, "text", "") or "")
        start = float(getattr(item, "start", 0.0))
        end = float(getattr(item, "end", 0.0))
        if not text:
            continue
        joinable = bool(lines) and (start - float(lines[-1]["end"])) <= gap \
            and len(lines[-1]["text"]) < max_chars
        if joinable:
            # 英文按词来，拼行时要补空格；中文一个字一个 token，直接连
            previous = lines[-1]["text"]
            spacer = (" " if previous and previous[-1].isascii() and previous[-1].isalnum()
                      and text[0].isascii() and text[0].isalnum() else "")
            lines[-1]["text"] = previous + spacer + text
            lines[-1]["end"] = max(float(lines[-1]["end"]), end)
        else:
            lines.append({"text": text, "start": start, "end": end})
    return [{"text": line["text"], "start": round(line["start"], 3),
             "end": round(line["end"], 3)} for line in lines]


#: 「范围」参数 -> 秒数。`full` 不在表里 = 整首。
RANGE_SECONDS: dict[str, float] = {"30": 30.0, "60": 60.0}

#: 「量化」参数：`off` 关掉，其余（默认 `grid`）吸到网格（一拍 / 32）。
QUANTIZE_OFF = "off"
#: 面板与日志里给用户看的量化网格名。网格步长 = 一拍 / 96（直线支 × 三连音支）。
QUANTIZE_LABEL = "128 分三连音网格"

#: 旋律音高修正：**固定**降一个八度（模型旋律整体高一个八度，而和弦轨是准的）。
#: 不做成面板参数——虚拟歌姬本来就不会按原调调音，用户不需要选。值就是
#: `pipeline.run()` 的默认参数（`pitch.DEFAULT_SEMITONES`），只有命令行留了 `--pitch-shift`。
#: 实现见 `pitch.py`、`docs/WebUI问题清单.md` §1。
PITCH_LABEL = "降一个八度（固定）"


def transcribe_options(job: Job) -> dict[str, Any]:
    """工程参数 -> `pipeline.run` 的关键字（范围 / 量化）。

    「精度 / 设备」不走这里：那是**加载模型时**的事，见 `EngineProvider.config_for`。
    音高也不走这里：它是固定值（`pipeline.run` 的默认参数就是 `PITCH_SEMITONES`）。
    """
    options: dict[str, Any] = {
        "quantize": str(job.options.get("quantize") or "") != QUANTIZE_OFF,
    }
    span = str(job.options.get("range") or "full")
    seconds = RANGE_SECONDS.get(span)
    if seconds is not None:
        options["max_seconds"] = seconds
    return options


def _fallback_since(engine: Any, before: int) -> str | None:
    """这一趟 `pipeline_run` 里有没有触发"OOM 退回 CPU"（G17）。

    `engine.records` 是**进程内累计**的（同一个引擎会被多个工程复用），所以只看
    这次调用之前到现在的增量，别把上一个工程的历史 fallback 记到这个工程头上。
    """
    for record in engine.records[before:]:
        if getattr(record, "fallback_reason", None):
            return str(record.fallback_reason)
    return None


# --------------------------------------------------------------------- 模型持有
class EngineProvider:
    """进程内复用一个 `SheetSage2Engine`（加载一次几十秒，之后每个任务都省掉）。

    页面上的「精度 / 设备」是**每个工程**自己的，所以这里按配置缓存：配置和手上这台不一样
    就把它放掉、按新配置重载。代价照实说——切一次要重新加载（本机实测 ~15s）。
    """

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self._engine: SheetSage2Engine | None = None
        self._key: tuple[Any, ...] | None = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def device(self) -> str:
        return self._engine.device if self._engine is not None else self.config.device

    def describe(self) -> dict[str, Any]:
        return {"model": str(self.config.model_id), "device": self.device, "loaded": self.loaded}

    def config_for(self, job: Job) -> EngineConfig:
        """把工程的「精度 / 设备」叠到服务端基准配置上（权重精度不在这里，那是启动开关）。"""
        return replace(
            self.config,
            dtype=str(job.options.get("dtype") or self.config.dtype),
            device=str(job.options.get("device") or self.config.device),
        )

    @staticmethod
    def _key_of(config: EngineConfig) -> tuple[Any, ...]:
        return (str(config.model_id), config.device, config.dtype,
                config.weights_dtype, config.preset)

    def get(self, progress: Progress | None = None,
            config: EngineConfig | None = None) -> SheetSage2Engine:
        config = config or self.config
        key = self._key_of(config)
        with self._lock:
            if self._engine is not None and self._key != key:
                self._engine = None
                _free_cuda()
                if progress is not None:
                    progress.log("扒谱参数变了，按新参数重新加载模型")
            if self._engine is None:
                if progress is not None:
                    progress.set(0.01, "首次运行：正在把模型加载进显存（约 30s）")
                engine = SheetSage2Engine(config)
                engine.load()
                self._engine = engine
                if progress is not None:
                    progress.log(f"模型就绪：device={engine.device}，"
                                 f"dtype={engine.effective_dtype}")
            self._key = key
            return self._engine

    def release(self) -> None:
        with self._lock:
            self._engine = None
            self._key = None
        _free_cuda()


class AsrProvider:
    """Qwen3-ASR 的懒加载持有者。用完 `release()` 把显存还回去。

    `choices` 是页面上「模型」那一栏的可选项（`{"1.7B": 路径, "0.6B": 路径}`），
    工程选了哪个就用哪个；换了模型同样要重载。
    """

    def __init__(self, config: AsrConfig,
                 choices: Mapping[str, Any] | None = None) -> None:
        self.config = config
        self.choices: dict[str, str] = {str(key): str(value)
                                        for key, value in (choices or {}).items()}
        if not self.choices:
            self.choices[asr_model_label(config.asr_model)] = str(config.asr_model)
        self._engine: AsrEngine | None = None
        self._key: tuple[Any, ...] | None = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def model_name(self) -> str:
        return Path(str(self.config.asr_model)).name

    def describe(self) -> dict[str, Any]:
        return {"model": self.model_name, "device": self.config.device, "loaded": self.loaded,
                "models": dict(self.choices)}

    def config_for(self, job: Job) -> AsrConfig:
        """按工程选的「模型 / 对齐器」换一个权重目录；认不出的标签就用服务端默认。"""
        label = str(job.options.get("asr_model") or "")
        path = self.choices.get(label)
        config = self.config if path is None else replace(self.config, asr_model=path)
        aligner = str(job.options.get("aligner") or "").strip().lower()
        if aligner and aligner != config.aligner:
            config = replace(config, aligner=aligner)
        return config

    def get(self, progress: Progress | None = None,
            config: AsrConfig | None = None) -> AsrEngine:
        config = config or self.config
        key = (str(config.asr_model), config.device, config.max_vram)
        with self._lock:
            if self._engine is not None and self._key != key:
                engine, self._engine = self._engine, None
                if engine is not None:
                    engine.release()
                _free_cuda()
                if progress is not None:
                    progress.log("换了识别模型，重新加载")
            if self._engine is None:
                if progress is not None:
                    progress.set(0.06, f"加载 {Path(str(config.asr_model)).name}（约 20s）")
                engine = AsrEngine(config)
                engine.load()
                self._engine = engine
                if progress is not None:
                    progress.log(f"识别模型就绪：{Path(str(config.asr_model)).name} "
                                 f"@ {config.device}")
            self._key = key
            return self._engine

    def release(self) -> None:
        with self._lock:
            engine, self._engine = self._engine, None
            self._key = None
        if engine is not None:
            engine.release()
        _free_cuda()


def _free_cuda() -> None:
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:                                   # torch 没装/没卡都不该影响任务
        pass


#: 识别模型的兄弟目录怎么收成选项（`models/Qwen3-ASR-*`）。
def asr_model_choices(default_model: str | Path) -> dict[str, str]:
    """页面上能选的识别模型：默认权重目录的**兄弟目录**里所有 `Qwen3-ASR-*`。

    有什么权重就列什么——`/api/env` 的「模型」选项就是这么来的，所以前端不会给出一个
    本机根本不存在的选择。
    """
    path = Path(str(default_model))
    choices: dict[str, str] = {}
    if path.parent.is_dir():
        for child in sorted(path.parent.iterdir()):
            label = asr_model_label(child)
            if child.is_dir() and label != child.name:
                choices[label] = str(child)
    if str(default_model) not in choices.values():      # 兜底：至少认默认那个
        choices[asr_model_label(path)] = str(default_model)
    return choices


# --------------------------------------------------------------------- 会话
class RunnerSession:
    """一次服务进程里的两个阶段实现 + 两个模型持有者。"""

    def __init__(
        self,
        engine_config: EngineConfig | None = None,
        asr_config: AsrConfig | None = None,
        *,
        free_between_stages: bool = True,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> None:
        base_asr = asr_config or default_asr_config()
        self.engine = EngineProvider(engine_config)
        self.asr = AsrProvider(base_asr, asr_model_choices(base_asr.asr_model))
        self.free_between_stages = free_between_stages
        self.tolerance = tolerance

    def runners(self) -> dict[str, StageRunner]:
        return {"transcribe": self.transcribe, "lyrics": self.lyrics}

    def describe(self) -> dict[str, Any]:
        info = self.engine.describe()
        info.update({
            "device": self.engine.device,
            "asr_model": self.asr.model_name,
            "asr_loaded": self.asr.loaded,
            "asr_models": dict(self.asr.choices),
        })
        return info

    def release(self) -> None:
        self.engine.release()
        self.asr.release()

    # ---------------------------------------------------------------- 扒谱
    def transcribe(self, job: Job, progress: Progress) -> dict[str, Any]:
        progress.check()
        progress.step(*STEP_LOAD)
        if self.free_between_stages and self.asr.loaded:
            self.asr.release()
        config = self.engine.config_for(job)
        options = transcribe_options(job)
        span = str(job.options.get("range") or "full")
        progress.log(f"扒谱参数：精度 {config.dtype} · 设备 {config.device} · "
                     f"范围 {span if span != 'full' else '整首'} · "
                     f"量化 {'关闭' if not options['quantize'] else QUANTIZE_LABEL} · "
                     f"音高 {PITCH_LABEL}")
        engine = self.engine.get(progress, config)
        progress.check()
        progress.step(*STEP_READ)
        before_records = len(engine.records)
        payload = pipeline_run(job.upload, job.out_dir, engine=engine,
                               progress=_model_progress(progress), **options)
        # **OOM 退 CPU 要明确告知**（G17）：以前这条 fallback 只写进 engine.records，
        # 界面毫无感知——用户以为在用 GPU，实际一首歌要跑半小时。
        fallback = _fallback_since(engine, before_records)
        if fallback:
            progress.log("显存不够，已自动改用 CPU 重跑（会慢很多，约几十分钟）")
        progress.check()
        progress.step(*STEP_PACK)
        progress.set(0.99, "整理产物清单")
        payload["highlights"] = build_highlights(payload)
        _store_highlights(job, payload, progress)
        grid = (payload.get("highlights") or {}).get("quantize") or {}
        # **完成门禁**（G4）：跑完不等于成功。`result.inspect_output_dir` 是 CLI 一直在用
        # 的那套体检（15 个文本产物 + 6 个 MIDI 的完整性/自洽性），WebUI 以前从不调用，
        # 于是半成品也被标成 ✅。这里跑一次，结果交给 `JobManager._settle_phase` 判定。
        artifacts = inspect_artifacts(job.out_dir,
                                      (payload.get("summary") or {}).get("duration_seconds"))
        if not artifacts["ok"]:
            progress.log("产物自检没通过：" + "；".join(artifacts["problems"][:3]))
        warnings = list(payload.get("warnings") or [])
        if fallback:
            warnings.append("显存不够，已自动改用 CPU 重跑")
        return {"detail": {
            "dtype": engine.effective_dtype,
            "device": engine.device,
            "range": span,
            "quantize": grid.get("grid_note"),
            "pitch": PITCH_LABEL,
            "notes": (payload.get("highlights") or {}).get("notes") or {},
            "warnings": warnings,
            "fallback": fallback,
            "files": len(payload.get("files") or []),
            "artifacts": artifacts,
        }}

    # ---------------------------------------------------------------- 歌词
    def lyrics(self, job: Job, progress: Progress) -> dict[str, Any]:
        progress.check()
        progress.step(*LYRIC_READ)
        project_path = job.out_dir / "project.json"
        if not project_path.is_file():
            raise RuntimeError("还没有扒谱工程：请先跑「扒谱」")
        project = json.loads(project_path.read_text(encoding="utf-8"))
        # 音源规则：页面选了干声、而且干声**确实传上来了**就用干声；否则用混音原曲。
        # **不校验上传内容**——用户故意把混音原曲（甚至别的歌）传进干声栏也随他，
        # 我们只如实记录这一趟用的是哪条。
        stem = job.stems.get("vocal")
        want_stem = str(job.options.get("source") or "stem") == "stem"
        if stem and want_stem:
            source, source_file, audio_path = "stem", str(stem["name"]), Path(str(stem["path"]))
            progress.log(f"歌词音源：{source_file}（干声）")
        else:
            source, source_file, audio_path = "mix", job.audio, job.upload
            if want_stem:
                progress.log("歌词音源：还没上传干声，自动回退到混音原曲")
            else:
                progress.log(f"歌词音源：{source_file}（按参数选了混音原曲）")
        # 语言：工程级可调项（"" = 自动让模型判）。
        raw_language = job.options.get("language")
        language = normalize_language(raw_language)
        language_label = raw_language or "自动"
        progress.log(f"识别语言：{language_label}")
        # 切片：按模型识别出来的段落切，段落边界是音乐上的自然切点。
        # 「段落」参数默认"只跑唱段"——前奏/间奏/尾奏一般不唱，跳过省时间也省显存。
        asr_config = self.asr.config_for(job)
        skip = str(job.options.get("skip_instrumental") or "vocal") == "vocal"
        sections, skipped = _sections_for(job, project,
                                          max_seconds=asr_config.slice_limit,
                                          skip_instrumental=skip, progress=progress)
        if skipped:
            names = "、".join(f"{s.label or '段落'}({s.start:.0f}-{s.end:.0f}s)"
                             for s in skipped)
            progress.log(f"跳过 {len(skipped)} 个不唱的段落：{names}")
        progress.log(f"按 {len(sections)} 个段落跑歌词识别"
                     f"（切片上限 {asr_config.slice_limit:.0f}s）"
                     + ("；已按段落跳过前奏/间奏/尾奏" if skipped else ""))

        progress.step(*LYRIC_LOAD)
        if self.free_between_stages and self.engine.loaded:
            self.engine.release()
        progress.log(f"识别模型：{Path(str(asr_config.asr_model)).name}")
        engine = self.asr.get(progress, asr_config)

        progress.step(*LYRIC_ASR)
        state: dict[str, Any] = {"lines": 0}

        def on_slice(run, done: int, total: int) -> None:
            lines = group_lyric_lines(run.chars)
            state["lines"] = len(lines)
            progress.set(LYRIC_ASR[1] + ASR_SPAN * done / max(1, total),
                         f"已识别 {done}/{total} 段 · {len(run.chars)} 字 / {len(lines)} 行")
            progress.detail(chars=len(run.chars), lines=len(lines), slices=done,
                            slices_total=total)
            progress.lyrics(text=run.text, chars=[c.as_dict() for c in run.chars],
                            lines=lines, slices=done, slices_total=total)

        def on_slice_start(value: Mapping[str, Any]) -> None:
            progress.check()
            index = int(value.get("index") or 0)
            total = int(value.get("total") or 1)
            progress.set(LYRIC_ASR[1] + ASR_SPAN * (index - 1) / max(1, total),
                         f"第 {index}/{total} 段 {float(value.get('start') or 0):.0f}-"
                         f"{float(value.get('end') or 0):.0f}s")

        run = engine.transcribe_sections(audio_path, sections, progress=on_slice_start,
                                         on_slice=on_slice, language=language)
        progress.check()
        if not run.chars:
            hint = ("；上传的干声如果和这首不是同一首歌，或者长度对不上，就会这样"
                    if stem else "")
            raise RuntimeError("没有拿到任何逐字时间戳，填词无法进行"
                               + hint
                               + ("；" + "；".join(run.warnings) if run.warnings else ""))

        progress.step(*LYRIC_ALIGN)
        result = fill_project(project, run.chars, track_id="vocal", tolerance=self.tolerance)
        if not result["found"]:
            raise RuntimeError(f"填词失败：{result['reason']}")

        progress.step(*LYRIC_WRITE)
        lines = group_lyric_lines(run.chars)
        write_text_atomic(project_path, json.dumps(project, ensure_ascii=False, indent=2))
        write_text_atomic(job.out_dir / "lyrics.json", json.dumps({
            "text": run.text, "fill": result["fill"], "lines": lines,
            "chars": [c.as_dict() for c in run.chars],
            "source": source, "source_file": source_file,
            "language": language_label, "language_used": run.language,
            "slices": run.slices, "zero_length_chars": run.zero_length_chars,
            "repaired_chars": run.repaired_chars,
            # 切片口径：跑过哪些段落、跳过了哪些、切片上限是多少
            "sections": [s.as_dict() for s in sections],
            "skipped_sections": [s.as_dict() for s in skipped],
            "max_slice_seconds": asr_config.slice_limit,
            "aligner": "tifa" if asr_config.uses_tifa else "qwen",
            "text_coverage": round(run.text_coverage, 4),
            "elapsed_seconds": round(run.elapsed_seconds, 2),
            "warnings": list(run.warnings),
        }, ensure_ascii=False, indent=2))

        fill = result["fill"]
        progress.set(0.99, f"挂字 {fill['assigned']} · 转音 {fill['melisma']} · "
                           f"没分到字 {fill['unmatched_notes']}")
        artifacts = _lyrics_artifacts(job)
        if not artifacts["ok"]:
            progress.log("歌词产物自检没通过：" + "；".join(artifacts["problems"][:3])
                         if artifacts["problems"] else "歌词产物不完整")
        detail = {
            "source": source, "source_file": source_file,
            "asr_model": Path(str(asr_config.asr_model)).name,
            "language": language_label, "language_used": run.language,
            "slices": run.slices, "skipped_instrumental": len(skipped),
            "sections": [s.as_dict() for s in sections],
            "skipped_sections": [s.as_dict() for s in skipped],
            "max_slice_seconds": asr_config.slice_limit,
            "aligner": "tifa" if asr_config.uses_tifa else "qwen",
            "lines": len(lines), "chars": len(run.chars), "slices": run.slices,
            "zero_length_chars": run.zero_length_chars,
            "repaired_chars": run.repaired_chars,
            "assigned": fill["assigned"], "melisma": fill["melisma"],
            "dropped": fill["dropped"], "absorbed": fill["absorbed"],
            "unmatched_notes": fill["unmatched_notes"],
            "text_coverage": round(run.text_coverage, 4),
            "warnings": list(run.warnings),
            "artifacts": artifacts,
        }
        progress.detail(**detail)
        return {"detail": detail}


def default_asr_config(models_root: Path | None = None) -> AsrConfig:
    """默认歌词识别配置：本地 1.7B + 对齐器（专有名词更准，见集成调研 §6）。"""
    from ..bootstrap import PROJECT_ROOT

    root = Path(models_root) if models_root is not None else PROJECT_ROOT / "models"
    return AsrConfig(
        asr_model=root / "Qwen3-ASR-1.7B",
        aligner_model=root / "Qwen3-ForcedAligner-0.6B",
        language="Chinese",
        device="cuda:0",
    )


def default_runners(**options: Any) -> dict[str, StageRunner]:
    """给 `JobManager` 用的默认实现（进程内只建一次会话）。"""
    session = options.pop("session", None) or RunnerSession(**options)
    return session.runners()


def inspect_artifacts(directory: str | Path,
                      duration_seconds: float | None = None) -> dict[str, Any]:
    """扒谱产物自检 -> 阶段详情里的 `artifacts`（G4 的"完成门禁"数据源）。

    `result.inspect_output_dir` 是 CLI 一直在用的那套体检（15 个文本 + 6 个 MIDI 的
    完整性/自洽性）。WebUI 以前**从不调用**，`summary["artifacts_ok"]` 是硬编码的 True，
    于是"模型吐了半套产物但没报错"也会被标成 ✅ 完成。
    """
    report = inspect_output_dir(directory, duration_seconds)
    total = len(EXPECTED_TEXT) + len(EXPECTED_MIDI)
    return {
        "ok": bool(report.ok),
        "checked": total - len(report.missing),
        "total": total,
        "missing": list(report.missing),
        "problems": list(report.problems),
    }


def _lyrics_artifacts(job: Job) -> dict[str, Any]:
    """歌词阶段的产物自检：`project.json` 与 `lyrics.json` 都得在、都能解析。"""
    required = ("project.json", "lyrics.json")
    missing: list[str] = []
    problems: list[str] = []
    for name in required:
        path = job.out_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(name)
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{name} 解析失败：{exc}")
    return {
        "ok": not missing and not problems,
        "checked": len(required) - len(missing),
        "total": len(required),
        "missing": missing,
        "problems": problems,
    }


def _vocal_spans(project: Mapping[str, Any]) -> list[tuple[float, float]]:
    """工程里人声轨的音符跨度（秒）。没有就返回空——切片退回原样。"""
    track = next((t for t in project.get("tracks") or [] if t.get("id") == "vocal"), None)
    notes = (track or {}).get("notes") or []
    if not notes:
        return []
    try:
        timeline = from_stored_measures(project["timeline"]["measures"])
        return note_spans_seconds(timeline, notes)
    except (KeyError, TypeError, ValueError):
        return []


def _sections_for(job: Job, project: Mapping[str, Any], *, max_seconds: float,
                  skip_instrumental: bool,
                  progress: Progress | None = None) -> tuple[list[Any], list[Any]]:
    """切片区间优先用 `structure.lab`（段落是音乐上的自然切点），没有就整首一段。

    返回 `(要跑的切片, 被跳过的段落)`：

    - `max_seconds` 是**服务端**的切片上限（`--asr-max-slice`）。以前这里漏了它，
      小显存模式那个 30s 一直是空谈，现在真的透传下去。
    - `skip_instrumental=True` 时跳过前奏 / 间奏 / 尾奏；**如果一个唱段都不剩**
      （比如整首是器乐），就退回全部段落——别让任务死在"没有可用区间"上，但要说明白。
    """
    structure_path = job.out_dir / "structure.lab"
    text = structure_path.read_text(encoding="utf-8") if structure_path.is_file() else ""
    if text.strip():
        sections = sections_from_lab(text, max_seconds=max_seconds,
                                     skip_instrumental=skip_instrumental)
        skipped: list[Any] = []
        if skip_instrumental:
            candidates = [s for s in parsed_sections(text) if is_instrumental(s.label)]
            if sections:
                skipped = candidates
            else:
                # 全是器乐段：回退到全部段落，别把任务做死
                if progress is not None:
                    progress.log("识别出来的段落全是前奏/间奏/尾奏，"
                                 "回退到「全部段落」而不是空跑")
                sections = sections_from_lab(text, max_seconds=max_seconds)
        if sections:
            # 边界让位给人声：`structure.lab` 的前奏/尾奏边界常常切在句子中间
            # （实测《爱情转移》前奏判到 23.87s，人声 22.63s 就进来了，第一句前 5 个字
            # 直接没进识别范围）。只在挨着被跳过段落的那一侧挪，扫掉的音频才不会丢。
            vocals = _vocal_spans(project)
            if vocals:
                widened = widen_to_vocals(sections, vocals)
                moved = [(round(before.start - after.start, 2),
                          round(after.end - before.end, 2))
                         for before, after in zip(sections, widened)
                         if abs(before.start - after.start) > 1e-6
                         or abs(before.end - after.end) > 1e-6]
                if moved and progress is not None:
                    progress.log(f"切片边界让出 {len(moved)} 处给人声"
                                 f"（前面补 {sum(m[0] for m in moved):.1f}s / "
                                 f"后面补 {sum(m[1] for m in moved):.1f}s）")
                sections = widened
            return sections, skipped
        if progress is not None:
            progress.log("structure.lab 解析不出可用区间，按整首跑")

    duration = float(project.get("duration_seconds") or 0.0)
    if duration <= 0:
        raise RuntimeError("工程里没有时长信息，无法切分歌词识别区间")
    from ..asr import Section

    return [Section(0.0, duration, label="整首")], []


def _store_highlights(job: Job, payload: Mapping[str, Any], progress: Progress) -> None:
    """把扒谱结果写到任务上（快照/列表/首屏都要用，不能只活在 SSE 事件里）。"""
    highlights = payload.get("highlights") or {}
    with job.lock:
        job.highlights = highlights
        job.decisions = payload.get("decisions") or None
        job.summary = payload.get("summary") or None
        job.warnings = list(payload.get("warnings") or [])
    progress.detail(meter=highlights.get("meter"), bpm=highlights.get("bpm"),
                    keys=highlights.get("keys") or [],
                    notes=highlights.get("notes") or {},
                    quantize=highlights.get("quantize") or {})


# --------------------------------------------------------------------- 进度翻译
def _model_progress(progress: Progress) -> Callable[[Mapping[str, Any]], None]:
    """模型的 progress 回调 -> 界面上的小步骤 + 阶段进度。"""

    def callback(value: Mapping[str, Any]) -> None:
        progress.check()
        stage = str(value.get("stage") or "")
        window = int(value.get("window") or 0)
        windows = max(1, int(value.get("windows") or 1))
        if stage == "audio":
            progress.step(*STEP_READ)
            progress.set(STEP_READ[1], "解码音频")
            return
        if stage == "notation":
            # 模型开始导出 lab / midi / abc；之后 pipeline 还会做自动裁决与工程化
            progress.step(*STEP_BUILD)
            progress.set(STEP_BUILD[1], "导出模型产物")
            return
        if stage not in ("encoding", "decoding", "window_complete"):
            return
        inner = (window - 1) / windows
        message = f"窗口 {window}/{windows}"
        if stage == "decoding":
            tokens = int(value.get("tokens") or 0)
            inner += min(SUB_PROGRESS_CAP, tokens / TOKENS_REFERENCE) / windows
            message += f" · 解码 {tokens} tokens"
        elif stage == "window_complete":
            inner = window / windows
            message += " · 完成"
        else:
            message += " · 起始"
        fraction = STEP_RECOGNIZE[1] + RECOGNIZE_SPAN * min(1.0, inner)
        progress.step(*STEP_RECOGNIZE)
        progress.set(fraction, message)

    return callback


# --------------------------------------------------------------------- 收窄
def build_highlights(payload: Mapping[str, Any]) -> dict[str, Any]:
    """从 pipeline 的 payload 里挑出界面真正要显示的字段。

    前端不该去理解 `decisions` / `report` 的完整结构（那是给人看报告的），所以这里做一次
    收窄，顺带把"网格步长 -> 三十二分"这类换算做掉。字段变了只会影响这一处。
    """
    decisions = payload.get("decisions") or {}
    summary = payload.get("summary") or {}
    project = payload.get("project") or {}
    quantize = project.get("quantize") or {}
    # 位移统计在 project 的**顶层**（`quantize_shift_seconds`）；只有 report.json 里才把它
    # 合进 `quantize`。两个都认，免得换个来源就读不到。
    shift = project.get("quantize_shift_seconds") or quantize.get("shift_seconds") or {}
    notes = summary.get("notes") or {}
    grid_ticks = quantize.get("grid_ticks")
    chords = project.get("chords") or []
    sections = project.get("sections") or []
    return {
        "duration_seconds": float(summary.get("duration_seconds") or 0.0),
        "meter": decisions.get("meter") or summary.get("meter"),
        "pickup_seconds": float(decisions.get("pickup_seconds") or 0.0),
        "bpm": decisions.get("bpm"),
        "bpm_source": decisions.get("bpm_source"),
        "bpm_confident": bool(decisions.get("bpm_confident", True)),
        "tempo_constant": bool(decisions.get("tempo_constant")),
        "tempo_segments": int(decisions.get("tempo_segments") or 0),
        "stable_tempos": list(decisions.get("stable_tempos") or []),
        "halftime_hint": decisions.get("halftime_hint"),
        "keys": list(decisions.get("keys") or []),
        "notes": {
            "melody": int(notes.get("melody") or 0),
            "vocal": int(notes.get("vocal") or 0),
            "instrumental": int(notes.get("instrumental") or 0),
            "events": int(summary.get("events") or 0),
        },
        "quantize": {
            "enabled": bool(quantize.get("enabled")),
            "grid_ticks": grid_ticks,
            "grid_note": grid_note_name(grid_ticks),
            "moved": int(quantize.get("moved") or 0),
            "moved_ratio": float(quantize.get("moved_ratio") or 0.0),
            "max_shift_seconds": shift.get("max"),
            "failed_tracks": int(quantize.get("failed_tracks") or 0),
        },
        # 音高后期处理：旋律整体降了几度、score.abc 有没有跟着重建（固定值，不是参数）
        "pitch": {
            "enabled": bool((project.get("pitch") or {}).get("enabled")),
            "semitones": int((project.get("pitch") or {}).get("semitones") or 0),
            "label": PITCH_LABEL,
            "abc": str((project.get("pitch") or {}).get("abc") or ""),
        },
        "chord_count": len(chords),
        "chord_kinds": len({str(item.get("label")) for item in chords}),
        "section_count": len(sections),
        "elapsed_seconds": float(summary.get("elapsed_seconds") or 0.0),
        "peak_gpu_mib": float(summary.get("peak_gpu_mib") or 0.0),
        "device": str(summary.get("device") or ""),
    }
