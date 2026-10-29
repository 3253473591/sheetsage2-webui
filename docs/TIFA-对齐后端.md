# TIFA 对齐后端

> 上游：[openvpi/TIFA](https://github.com/openvpi/TIFA)。**源码 MIT**，模型权重
> **CC BY-NC-SA 4.0**（与 SheetSage2 的 BY-NC 不同，多了 SA；本项目非商用，随包保留
> 原许可与出处即可）。推理用的代码子集 vendored 在 `src/sheetsage2_webui/vendor/tifa/`
> （1.6MB，含 LICENSE），权重放 `models/TIFA-1.0/`（不进仓库）。

## 1. 为什么换

Qwen3-ForcedAligner 的时间戳有 **13~17% 是零宽度**（`start == end`），另有一类塌方是
一个字被给了 3~4 秒、后面七八个字挤在它末尾。这两类后处理都救不回来：前者只能估，
后者根本没有信息。TIFA 按帧做 Viterbi 解码，**每个词必然分到一段**，从结构上没有这两种病。

## 2. 实测

| 语料 | 后端 | 字 | 零宽度 | hit（同一套 SheetSage2 音符 + 同一个贪心） |
|---|---|---|---|---|
| 《爱情转移》中文 | Qwen3-FA 0.6B | 539 | 70（13%） | **89.4%**（hidden 55） |
| 《爱情转移》中文 | **TIFA** | 539 | **0** | **99.8%**（hidden 0，\|Δ\|>1s 0） |
| 《富士山下》粤语（喂 LRC 原文） | TIFA | 538 | **0** | 行首 vs LRC：中位 **-0.15s**、p90 +0.16s，68 行里只有 1 行差 >1s |
| 《富士山下》粤语（端到端，ASR 出字） | **TIFA** | 540 | **0** | **99.6%**（hidden 2） |
| The Wellerman 英语（端到端） | **TIFA** | 359 词 | **0** | **99.2%**（hidden 3，续音 31 个 `+`） |

英文那一行顺带验证了两件事：**续音 `+` 是我们自己的 `continuation_marker()` 产出的**，与后端无关
（31 个 `+`、0 个 `-`）；TIFA 的 G2P 虽然会 lowercase + 去标点，但歌词用的是我们的 ASR 文本
（`AsrEngine._labels_for`），所以 `There` 的大小写和 `sea` 的干净写法都保住了。
粤语那两行的差别说明**文本和边界是两件事**：对齐 99.6%，但 ASR 把「拦」听成「難」、
漏了句尾的「吗」——这是识别的问题，换对齐器治不了。

耗时：一首 4 分钟的歌，TIFA 约 20s（含加载），Qwen FA 约 +19s 加载 / +1.4s 推理。
**总墙钟时间基本打平**（ASR 才是大头），收益在显存（峰值少约 1.3GB）和质量。

## 3. 语言矩阵（实测）

| 语言 | G2P 依赖 | 状态 |
|---|---|---|
| 中文 | `dictionaries/ds-zh-pinyin-lite.txt`（6.5KB） | ✅ 跑通 |
| 粤语 | `dictionaries/jyutping_dict.txt`（6.9KB） | ✅ 跑通（见下方 OOV） |
| 英语 | `dictionaries/ds_cmudict-07b.txt`（3.4MB）+ `assets/LstmG2p-Eng/`（4.1MB）+ **onnxruntime**（`g2pflow[lstm]`） | ✅ 跑通（359 词 / hit 99.2%） |
| 日语 | `g2pflow[ja]` → MeCab + unidic 词典 | ❌ **暂不支持**，见 §5 |

## 4. 三个踩过的坑

1. **ASR 会把日文歌判成 Chinese**。实测一首假名全对的日文歌，`result.language` 报的是
   `Chinese`，TIFA 拿中文 G2P 处理假名 → 整段 OOV → **一个字都出不来**。
   现在按文字兜底：出现假名就强制走 `ja`。
2. **`oov_handling` 默认是 `discard`，一个音素就废掉整段**。粤语《富士山下》「调」的
   `eu` 不在词表里，整个 50s 窗口归零。改成 `force`（只丢出问题的那条发音路径）。
3. **上下文约 1 分钟**。切片上限从 90s 压到 50s（`AsrConfig.slice_limit`）。

## 5. 日语为什么暂时不行

`g2pflow[ja]` 需要 MeCab + unidic 词典：

- `unidic`（PyPI 包）**首次使用会联网下载 ~500MB 词典**，离线包不能这么做；
- `unidic-lite` 自带 ~50MB 词典，但模块名是 `unidic_lite`，而 g2pflow 硬编码 `import unidic`。

所以日语场景现在仍走 Qwen 对齐器（`aligner="qwen"`），两个后端都保留即可。

## 6. 体积账（已落地：打 inside 代码包）

| 项 | 大小 |
|---|---|
| fp32 `model.pt` | 151.2MB |
| **fp16 `model.pt`** | **75.7MB**（边界差 ≤10ms，可忽略） |
| + 词典 + 英语 G2P 资产 | 82.7MB |
| + vendored 源码 | 1.6MB（压缩后约 0.4MB） |

打包时由 `scripts/build_alpha_package.py` 的 `include_tifa()` **在构建期转 fp16**，
随包放进 `models/TIFA-1.0/`（附 `LICENSE-model.txt`：CC BY-NC-SA 4.0、来源、只改了精度）。

实测 `alpha4`：**zip 98.9MB / 解压 148.8MB**，在 100MB 预算内（余量 1.1MB）。
再紧就往回收英语 G2P（`assets/LstmG2p-Eng` + cmudict 共 7.5MB，英语改走 Qwen）。

## 7. 怎么用

默认就是 `aligner="tifa"`；**权重不在时自动退回 Qwen** 并写一条告警（不让链路死掉）。

```python
AsrConfig(asr_model=..., aligner="tifa", tifa_model=models/"TIFA-1.0"/"model.pt")
```

`AsrConfig.slice_limit` 在 tifa 模式下把切片压到 50s；WebUI 通过 `job.options["aligner"]` 选。

复现脚本：`tmp/tifa_pipeline.py`（中文整首）、`tmp/tifa_yue.py`（粤语 + LRC 对表）。

