r"""探针：Qwen3-ASR 能不能把日语转写成**罗马音**？

    .\.venv\Scripts\python.exe scripts\probe_romaji.py
    .\.venv\Scripts\python.exe scripts\probe_romaji.py --start 60 --seconds 30
    .\.venv\Scripts\python.exe scripts\probe_romaji.py --audio "samples\某首日语歌.flac"

要回答的两件事：

1. **有没有"罗马音"这个语言选项？** —— 看模型自报的
   `SUPPORTED_LANGUAGES`（`qwen_asr.inference.utils`）：30 种，含 `Japanese`，
   **不含 Romaji/Latin**；`validate_language()` 对表外的值直接抛
   `ValueError: Unsupported language`。所以"语言"上没法选罗马音。
2. **能不能用 `context`（system prompt）把输出文字改成罗马音？** —— `transcribe(context=...)`
   会被塞进 system prompt（`_build_messages()` -> `{"role": "system", "content": context}`），
   这是唯一的指令通道；脚本把几种说法都跑一遍，用"文本里拉丁字母占比"来量。

顺带记一个 API 事实：本机装的 `qwen_asr` 里 `max_new_tokens` 是
`from_pretrained()` 的参数，**不是** `transcribe()` 的——`asr.py` 现在把
`max_new_tokens` 传给 `transcribe()` 会被 `except TypeError` 兜住，等于没生效
（逐切片 token 上限一直是模型的默认 512）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from sheetsage2_webui.bootstrap import find_ffmpeg, prepare  # noqa: E402

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
KANA = re.compile(r"[\u3040-\u30ff]")
LATIN = re.compile(r"[A-Za-z]")

DEFAULT_AUDIO = PROJECT / "samples" / "麻枝准,やなぎなぎ - 死にゆく季節のきみへ.flac"
#: 没有日语素材时的退路：拿中文歌 + `language=Japanese` 也能验"指令改不了文字"这一条。
FALLBACK_AUDIO = PROJECT / "samples" / "凤凰传奇 - 光芒.flac"

#: (标签, 强制语言, context, 说明)。context 是唯一能下"用罗马音写"这道指令的通道。
CASES: list[tuple[str, str | None, str, str]] = [
    ("基线·自动", None, "", "不给语言也不给指令：让模型自己判"),
    ("基线·Japanese", "Japanese", "", "本项目的现状（选了日语）"),
    ("日语指令", "Japanese", "次の歌詞をローマ字（ラテン文字）で書き出してください。",
     "用日语要求罗马音"),
    ("英语指令", "Japanese",
     "Transcribe the lyrics in romaji (Latin letters only), e.g. kimi no na wa.",
     "英文 prompt"),
    ("中文指令", "Japanese", "用罗马音（拉丁字母）转写歌词，例如 kimi no na wa。",
     "中文 prompt"),
]


def slice_clip(audio: Path, start: float, seconds: float, target: Path) -> Path:
    if target.is_file() and target.stat().st_size > 0:
        print(f"复用已有切片 {target.relative_to(PROJECT)}")
        return target
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg")
    import subprocess

    target.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-v", "error", "-y", "-ss", f"{start:g}", "-t", f"{seconds:g}",
               "-i", str(audio), "-ac", "1", "-ar", "16000", str(target)]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"切片失败：{result.stderr.decode(errors='replace')[-300:]}")
    print(f"切 {start:g}s~{start + seconds:g}s -> {target.relative_to(PROJECT)}")
    return target


def stats(text: str) -> str:
    body = text.replace(" ", "")
    total = max(1, len(body))
    return (f"假名 {len(KANA.findall(text)):>3} · CJK {len(CJK.findall(text)):>3} · "
            f"拉丁 {len(LATIN.findall(text)):>3} 字符（占 {len(LATIN.findall(text)) / total:.0%}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qwen3-ASR 罗马音转写探针")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--start", type=float, default=60.0, help="从第几秒开始截（默认 60）")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--model", default=str(PROJECT / "models" / "Qwen3-ASR-1.7B"))
    parser.add_argument("--aligner", default=str(PROJECT / "models" / "Qwen3-ForcedAligner-0.6B"))
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--no-timestamps", action="store_true",
                        help="不要逐字时间戳（更快；但看不出对齐器吃不吃罗马音）")
    args = parser.parse_args(argv)

    prepare()
    from sheetsage2_webui.asr import AsrConfig, AsrEngine

    audio = args.audio
    if not audio.is_file() and audio == DEFAULT_AUDIO and FALLBACK_AUDIO.is_file():
        print(f"没有日语素材 {audio.name}，退回到 {FALLBACK_AUDIO.name}"
              "（用 language=Japanese 照样能验「指令改不了输出文字」这一条）")
        audio = FALLBACK_AUDIO
    if not audio.is_file():
        print(f"找不到音频 {audio}", file=sys.stderr)
        return 2
    clip = slice_clip(audio, args.start, args.seconds,
                      PROJECT / "tmp" / "probe-romaji"
                      / f"{audio.stem}-{args.start:g}+{args.seconds:g}s.wav")
    print(f"模型 {Path(args.model).name}\n")

    engine = AsrEngine(AsrConfig(asr_model=args.model, aligner_model=args.aligner,
                                 device=args.device))
    engine.load()
    supported = engine._model.get_supported_languages()
    hits = [name for name in supported if "rom" in name.lower() or "latin" in name.lower()]
    print(f"模型自报支持 {len(supported)} 种语言：{', '.join(supported)}")
    print(f"其中和罗马音/拉丁有关的：{hits or '没有（Romanian 是罗马尼亚语，不是罗马音）'}\n")

    wanted = not args.no_timestamps
    try:
        for label, language, context, note in CASES:
            results = engine._model.transcribe(
                audio=str(clip), context=context, language=language,
                return_time_stamps=wanted)
            result = results[0] if isinstance(results, (list, tuple)) else results
            text = (getattr(result, "text", "") or "").strip()
            stamps = getattr(result, "time_stamps", None) or []
            print(f"### {label}  ·  {note}")
            if context:
                print(f"  context: {context}")
            print(f"  判定语言 {getattr(result, 'language', '') or '（空）'!r} · "
                  f"时间戳 {len(stamps)} 条 · {stats(text)}")
            print(f"  文本: {text or '（空）'}\n")
    finally:
        engine.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
