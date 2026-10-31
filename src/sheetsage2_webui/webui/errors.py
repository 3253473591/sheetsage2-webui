"""错误码表：把异常翻译成「人话 + 下一步 + 技术细节」。

**为什么要有这个模块**：以前 `jobs._run_phase` 把 `f"{type(exc).__name__}: {exc}"`
直接塞进快照、SSE 与 `job.json`，页面上显示的是
`TranscriptionError: 转写失败：RuntimeError: CUDA error: an illegal memory access was encountered`
这种给开发者看的东西。用户看不懂，也就没法据此行动。

这里把一次失败拆成四段：

| 字段 | 给谁看 | 例子 |
|---|---|---|
| `code` | 机器（前端按码换图标、诊断包文件名、测试断言） | `E_DISK_FULL` |
| `message` | 用户（横条上那一句，中文、不出现异常类名） | 「磁盘满了，结果写不进去」 |
| `action` | 用户（下一步干什么，一句话） | 「清理 output/ 或换个大点的盘，再重跑这一阶段」 |
| `detail` | 开发者 / 诊断包（技术原文） | `OSError: [Errno 28] No space left on device` |

约定：**`message` 与 `action` 里不出现英文异常名**，`detail` 里保留原文。
模型自己抛出的中文错误（比如 `RuntimeError("没有拿到任何逐字时间戳")`）原样当 `message` 用——
那本来就是人话，别为了统一而盖掉。

未知异常统一兜底成 `E_INTERNAL-<8 位 hash>`：同一个 bug 的错误码稳定，用户报"错误码
E_INTERNAL-3f2a9c1d"就能对上日志。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

#: 错误码 -> (人话, 下一步)。`describe()` 按这张表兜底；阶段自己抛的
#: :class:`StageFailure` 可以自带更具体的文案。
CATALOG: dict[str, tuple[str, str]] = {
    "E_DISK_FULL": (
        "磁盘满了，结果写不进去",
        "清理 output/ 里不要的工程，或把数据目录换到大一点的盘，再重跑这一阶段",
    ),
    "E_VRAM_OOM": (
        "显卡内存不够，模型跑不动",
        "把「精度」改成 BF16、或把「范围」改成前 30 秒再试；也可以重启服务释放显存",
    ),
    "E_AUDIO_UNREADABLE": (
        "这个音频读不出来，可能不是有效的音频文件",
        "确认文件能正常播放（wav / mp3 / flac），必要时先转成 mp3 再传",
    ),
    "E_AUDIO_SILENT": (
        "这个音频是纯静音，没有内容可识别",
        "换一首有声音的再传；如果是伴奏/录音，确认选对了文件",
    ),
    "E_ASR_EMPTY": (
        "没听到人声，歌词识别拿不到任何字",
        "换一首含人声的混音，或上传干净的人声干声再试",
    ),
    "E_ARTIFACTS_INCOMPLETE": (
        "产物不完整，这一轮不能算完成",
        "重跑这一阶段；如果反复这样，点顶栏「诊断包」发给任意 AI 帮你看",
    ),
    "E_STALLED": (
        "任务卡住了，很久没有任何进展",
        "点「强制中止」后重跑；如果反复卡在同一处，点顶栏「诊断包」发给任意 AI 帮你看",
    ),
    "E_TIMEOUT": (
        "任务跑太久，已经超过上限",
        "把「范围」改成前 30 秒先试一遍，或换到有显卡的机器上跑",
    ),
    "E_CANCELLED": ("任务已取消", "重新点「开始」就可以再跑一次"),
    "E_PERMISSION": (
        "没有权限写这个目录",
        "把工程目录从杀毒软件/同步盘的占用里放出来，或换个数据目录",
    ),
    "E_MODEL_MISSING": (
        "模型权重没找到",
        "双击 01_install.bat 把模型下齐，再重启服务",
    ),
    "E_JA_ALIGNER": (
        "日文歌要用日语对齐器，但这台机器上还没装",
        "双击 02_models.bat，选 2（日语特供）补下模型，再重跑「识别歌词」",
    ),
    "E_DEVICE": (
        "显卡/设备不可用",
        "把「设备」改成 CPU 再试，或更新显卡驱动",
    ),
    "E_INTERNAL": (
        "程序内部出错了",
        "先重试一次；反复出错就把错误码和「诊断包」发给任意 AI 帮你看",
    ),
}


@dataclass(frozen=True, slots=True)
class StudioError:
    """一次失败的可展示形态（见模块头的四段说明）。"""

    code: str
    message: str
    action: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "action": self.action,
                "detail": self.detail}


class StageFailure(RuntimeError):
    """阶段自己判定"这一轮不算成功"时抛它（产物不完整、卡死、超时……）。

    继承 `RuntimeError`：老的 `except Exception` 分支照样接得住。
    """

    def __init__(self, code: str, message: str, action: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.error = StudioError(code, message, action or CATALOG.get(code, ("", ""))[1],
                                 detail)


def _catalog(code: str, detail: str = "", text: str = "") -> StudioError:
    """按码取文案；`text` 本身是中文（模型/流水线自己给的人话）时优先用它。

    模型抛的 `RuntimeError("没有拿到任何逐字时间戳")` 本来就是给用户看的，
    为了统一而盖成模板反而更差——码保留、话用原文。
    """
    message, action = CATALOG.get(code, ("程序内部出错了", ""))
    if text and _looks_chinese(text):
        message = text.strip()
    return StudioError(code, message, action, detail)


def _looks_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _internal_code(exc: BaseException) -> str:
    """未知异常 -> 稳定的 `E_INTERNAL-xxxxxxxx`（同一个 bug 每次都是同一个码）。"""
    raw = f"{type(exc).__module__}.{type(exc).__qualname__}:{exc}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"E_INTERNAL-{digest}"


def describe(exc: BaseException, *, phase: str | None = None) -> StudioError:
    """异常 -> :class:`StudioError`。**不会抛异常**（诊断路径本身不能再炸）。"""
    if isinstance(exc, StageFailure):
        return exc.error

    name = type(exc).__name__
    text = str(exc).strip()
    lowered = text.lower()
    detail = f"{name}: {text}" if text else name
    if phase:
        detail = f"[{phase}] {detail}"

    # ---------------------------------------------------------- 取消
    if name == "JobCancelled":
        return _catalog("E_CANCELLED", detail, text)

    # ---------------------------------------------------------- 磁盘 / 权限
    errno = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    if errno == 28 or "no space left" in lowered or "磁盘空间不足" in text:
        return _catalog("E_DISK_FULL", detail, text)
    if errno in (13, 11) or winerror in (5, 32, 33) or "permission denied" in lowered:
        return _catalog("E_PERMISSION", detail, text)

    # ---------------------------------------------------------- 显存 / 设备
    if any(marker in lowered for marker in
           ("cuda out of memory", "outofmemoryerror", "not enough memory",
            "cuda error", "illegal memory access", "device-side assert")):
        return _catalog("E_VRAM_OOM", detail, text)
    if "显存" in text and ("不够" in text or "不足" in text):
        return _catalog("E_VRAM_OOM", detail, text)
    if any(marker in lowered for marker in
           ("no cuda", "cuda is not available", "cuda unavailable", "device not found",
            "target device")):
        return _catalog("E_DEVICE", detail, text)

    # ---------------------------------------------------------- 音频解码
    if any(marker in text for marker in ("解码音频失败", "解码失败", "音频读不出")):
        return _catalog("E_AUDIO_UNREADABLE", detail, text)
    if any(marker in lowered for marker in
           ("invalid data found", "could not find codec", "unsupported codec",
            "invalid argument", "moov atom not found", "end of file")):
        if "ffmpeg" in lowered or "decode" in lowered or "音频" in text:
            return _catalog("E_AUDIO_UNREADABLE", detail, text)

    # ---------------------------------------------------------- 歌词
    if name == "JaAlignerMissing":
        return _catalog("E_JA_ALIGNER", detail, text)
    if "没有拿到任何逐字时间戳" in text or ("噪" in text and "人声" in text):
        return _catalog("E_ASR_EMPTY", detail, text)

    # ---------------------------------------------------------- 模型权重
    if isinstance(exc, FileNotFoundError) and ("config.json" in text or "model" in lowered):
        return _catalog("E_MODEL_MISSING", detail, text)

    # ---------------------------------------------------------- 中文原文直接用
    if text and _looks_chinese(text):
        return StudioError("E_STAGE_FAILED", text, "按提示处理后重跑这一阶段", detail)

    # ---------------------------------------------------------- 兜底
    code = _internal_code(exc)
    message, action = CATALOG["E_INTERNAL"]
    return StudioError(code, f"{message}（错误码 {code}）", action, detail)


def snapshot_fields(exc: BaseException, *, phase: str | None = None) -> dict[str, Any]:
    """方便调用方一次性拿四个字段（`error` 是**人话**，不是异常原文）。"""
    error = describe(exc, phase=phase)
    return {"error": error.message, "error_code": error.code,
            "error_action": error.action, "error_detail": error.detail}
