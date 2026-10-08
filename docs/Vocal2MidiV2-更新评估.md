# Vocal2Midi V2 更新评估：哪些能集成到本项目

> 输入：V2 README 声称的 13 项更新（使用 TiFA 对齐器、优化软件性能、新增批量模式、
> 支持粤语英语、MIDI 量化可对齐网格线、GUI 中英切换、支持深色模式、低性能可用
> pinyinASR、日语新增歌词 G2P、对齐更准）。
> 方法：直接对 `reference/Vocal2Midi`（V1）与 `reference/Vocal2Midi V2` 做代码级比对，
> 再逐项对照本项目（`src/sheetsage2_webui/`）现状。
> 结论日期：本次会话。

---

## 0. 一句话结论

**13 项里，12 项要么本项目已经有了、要么是 Vocal2Midi 自己的 GUI 桌面程序特性、
要么我们已经用别的方式解决。真正值得考虑的只有 1 项：TiFA 对齐器（连带它的日语 G2P）。**

**结论：决定不接 TiFA。** 理由不是"对齐不重要"，而是**换引擎的收益未被证实、
成本却是确定的**：本项目对齐链路（Qwen3-ASR + Qwen3-ForcedAligner）已经在中文和
日语上跑通、没有精度症状，而 TiFA 要新增 GB 级模型 + 约 4700 行代码 + 新依存，
且**没有对照实验证明它更准**。

> 读这句时别理解成"对齐以后不用管"：**现有的 Qwen3 对齐链路是项目的一部分，
> 要继续维护**；只是不再引入 TiFA 这第二个引擎。将来若真做日语/粤语歌且实测
> 对齐不满意，本文 §3.4 给了最小风险的接入办法（做成可切换后端 + 先 A/B）。

---

## 1. V1 → V2 到底改了什么（代码事实）

`.py` 文件数 **109 → 160**：仅 V2 新增 51 个，两边都有但内容变了的 46 个，**没有删除任何文件**。

新增代码的大头，按行数：

| 模块 | 规模 | 作用 |
|---|---|---|
| `inference/TiFA/` | 约 4,700 行（含 `g2p/` 一整套转换器树） | **第二个强制对齐引擎**（ONNX，zh/ja/en + 粤语 jyutping） |
| `inference/kashi_g2p_ja/g2p_onnx_runtime.py` | 1,069 行 | 日语 G2P（ONNX），带候选 beam search |
| `inference/pipeline/lyric_alignment.py` | 454 行 | 对齐调度层 |
| `gui/bpm_spinbox.py` + GUI 改动 | 约 240 行 + 9 个文件 | 桌面 GUI（批量、主题、i18n） |
| `scripts/auto_lyric_cli.py` | 372 行 | 无头批量 CLI |
| `inference/t2s_utils.py` | 34 行 | 繁→简（opencc） |
| `inference/pipeline/memory_utils.py` | **6 行** | 就一个 `gc.collect()` 包装 |
| `inference/pinyin_asr/`、`inference/romaji_asr/` | — | **V1 就有**，不是新增 |

`application/config.py` 新增 4 个配置项：`alignment_engine`（默认 `"tifa"`）、
`tifa_model_path`、`japanese_g2p_engine`（默认 `"kashi-g2p-onnx"`）、`kashi_g2p_model_path`。

模型目录（README §2）：`models/tifa-1.0-onnx`、`models/kashi-g2p-onnx`——**都不随仓库分发**。

---

## 2. 逐项判定

| # | V2 声称 | 判定 | 依据 |
|---|---|---|---|
| 1 | 使用 TiFA 对齐器 | **可集成（唯一真候选）** | 见 §3 |
| 2 | 优化软件性能 | ❌ **几乎不存在** | `memory_utils.py` 只有 6 行（`gc.collect()` 包装）；`qwen3asr_dml/runtime.py` V1→V2 只 +2 行；`llama.py` +11/−11（等价重排）；`onnx_runtime.py` +3/−1。**我们自己的回收比它多**：`webui/runner.py` 的 `release()` 是 `gc.collect()` + `torch.cuda.empty_cache()`，还有 `free_between_stages` 阶段间释放（见 `runner.py:291/296/359/433`、`asr.py:298/302`） |
| 3 | 新增批量模式 | ❌ 不适用 | 那是它的**桌面 GUI** 批量丢文件；我们是 WebUI，一个工程一首歌，批量由用户多次上传解决 |
| 4 | 支持粤语英语 | ⚠️ **本项目已有** | `asr.LANGUAGE_CHOICES` 已含 `English`/`Cantonese`（还有 `Korean`）；`svp.LANGUAGE_TO_SV` 已含 `"Cantonese": "cantonese"`。V2 那处改动只是给 runtime 加了 `yue`/`cantonese` 语言名归一 |
| 5 | MIDI 量化可对齐网格线 | ✅ **本项目已有（且做得更细）** | `quantize.py`：网格 = 一拍/96（20 tick @PPQ1920），并覆盖三连音族。V2 的 `GRID = QUARTER/32`（一拍/32）比我们粗。见 `docs/音符量化规格.md` §7–§9 |
| 6 | GUI 中英切换 | ❌ 不适用 | 它的桌面 GUI i18n；我们的界面是中文 WebUI，要 i18n 得自己在前端做 |
| 7 | 支持深色模式 | ❌ 不适用 | 同上（WebUI 的 CSS 主题是另一件事，与 V2 无关） |
| 8 | 低性能可用 pinyinASR | ⚠️ **本项目已有等价物** | 我们走 Qwen3-ASR + `--asr-max-vram` / `--asr-max-slice`（`webui --lock-params` 就是"4G 特供"模式）。`pinyin_asr` 在 V1 就有，不是 V2 新增 |
| 9 | 日语新增歌词 G2P | ⚠️ 只在"改用音素级对齐"时才有意义 | 见 §3.2 |
| 10 | 对齐更准 | ⚠️ **无法证实，且我们没症状** | 见 §3.3 |
| 11 | （Headless 批量 CLI） | ❌ 不适用 | 我们已有 CLI 与 WebUI 两条路 |
| 12 | （繁简转换 `t2s_utils`） | ⚠️ 可从别处拿 | 34 行、包一层 opencc；真需要再说，不必为此接 V2 |
| 13 | （GUI BPM spinbox 等） | ❌ 不适用 | 桌面 GUI 细节 |

---

## 3. 唯一候选：TiFA 对齐器

### 3.1 它是什么

`inference/TiFA/aligner.py` 头部自述：

> Host-side TiFA alignment: audio + text -> phoneme/word boundaries.
> … G2P -> candidate grid -> spectrogram -> (optional) pronunciation scoring DP ->
> select -> model similarities -> Viterbi -> WordList.
> The returned prediction dict matches the **HubertFA contract**.

即：它和 HubertFA 是**同一类东西**——ONNX 强制对齐器，输出**音素/词**边界。
V2 把它设成默认（`alignment_engine = "tifa"`），HubertFA 保留为备选。

### 3.2 本项目现在用什么

`src/sheetsage2_webui/asr.py`：**Qwen3-ASR + Qwen3-ForcedAligner-0.6B**
（`models/Qwen3-ForcedAligner-0.6B`，1.75 GB），`qwen_asr.Qwen3ASRModel(forced_aligner=...)`。
它给的是**逐字**时间戳，我们再用 `lyrics.assign_lyrics` 把字挂到音符上。

**所以 TiFA 不是"加一个功能"，而是"换掉对齐引擎"**：
`asr.py` 的整个对齐链路、`aligner_model` 配置、以及"逐字 → 音符"的适配都要重做。
连带要下载 `models/tifa-1.0-onnx`（5 个 ONNX 图）与 `models/kashi-g2p-onnx`，
并把 TiFA 那 ~4,700 行连同 G2P 转换器树搬进来。

### 3.3 我们有没有"对齐不准"的症状

**没有实测证据。** 反而有两条反向证据：

1. **日语工程已经跑通**：`output/BAK___川崎鹰也_-_君が僕の心臓じゃないせいで_feat__川崎鷹也/`
   （256s 日语歌）`lyrics.json` 有 226 个字、`language_used = "Japanese"`、
   时间戳从 14.21s 起正常分布；它的 `instrumental` 轨也有歌词覆盖率。
2. **我们文档里唯一一条"对齐坏了"的案例是模型自身的**：
   `docs/音符量化规格.md` §6 记的《皮囊》——对齐器只给 360/430 个字，且第一个字
   10.4s、第二个字跳到 48.4s（中间 37.9s 无字），**直接跑对齐器也能复现**。
   这是"输入就坏"，换个对齐器不一定救得回来，得先有对照实验才知道。

真要用，**得先做对照**：拿那首日语歌，分别用 Qwen3-ForcedAligner 与 TiFA 对齐，
比"字/音挂接成功率"和"人工抽查若干句的边界偏差"。没有这个数，接 TiFA 是赌。

### 3.4 如果哪天真要做，怎么接最省

不要搬整个 `inference/TiFA/`。按它的 `aligner.py` 契约，只需要：

- `TiFA/aligner.py` + `decoding.py` + `runtime.py` + `lib/viterbi`（对齐主链）
- `TiFA/g2p/` 一整套 + `kashi_g2p_ja/`（G2P，日语那部分最重）
- 模型目录 `tifa-1.0-onnx` / `kashi-g2p-onnx`

然后在 `asr.py` 里把它做成**可切换的第二个对齐后端**（`aligner = "qwen" | "tifa"`），
默认仍是 Qwen，先用它做 A/B，验证有效再考虑换默认。这样风险最小。

---

## 4. 建议

| 优先级 | 动作 |
|---|---|
| **不做** | 批量模式、GUI i18n、深色模式、BPM spinbox —— 都是它的桌面 GUI 特性，与 WebUI 无关 |
| **本就有** | 粤语/英语语言支持、量化对齐网格线（我们还更细）、低性能档位、显存回收（`runner.release()` 比 V2 的 `gc.collect()` 包装更完整） |
| **可选小活** | 繁简转换（真需要时用 opencc 自己包 30 行，不必为此接 V2）；它的 `auto_lyric_hybrid.py` 从 624 行精简到 479 行，其**阶段编排**思路可以借鉴，但我们流水线结构不同，不是照搬 |
| **先验证再说** | TiFA + 日语 G2P：先跑"同一首日语歌，两个对齐器对照"，用数据决定 |
| **决定** | **本次不接 TiFA**（见 §0）。现有 Qwen3 对齐链路继续用、继续维护 |

**当前不建议动手**：本项目对齐链路已在日语上跑通，没有精度症状；
TiFA 的收益未被证实，成本（换引擎 + 新模型 + 4.7k 行）却是确定的。
**若将来要接**，按 §3.4 走：做成可切换的第二后端，默认仍用 Qwen，先 A/B 验证。
