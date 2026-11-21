# SheetSage2 微调是否可行（调研）

> 日期：2026-11-21。结论先行：**权重公开、架构可训、但我们没有训练代码；用素材量补的是
> "音符层"的准确率，补不了三连音，也和挂字层无关**。

---

## 1. 权重与许可

| 项 | 值 |
|---|---|
| 权重 | **公开**：HF `m-a-p/SheetSage2`，本地 `models/SheetSage2/` |
| 形态 | **adapter**（`weights_format: "adapter"`，228MB）+ 基座 `m-a-p/MERT-v2-FullSong`（本地 2.36GB，fp32） |
| 许可 | **CC BY-NC 4.0（非商业）**；NOTICE 已登记 |
| 规模 | 编码器 24 层 / hidden 1024；解码器 6 层 / hidden 512；词表 31678；`max_output_seq_len 5120` |

adapter 在加载时合并（`modeling_sheetsage2.merge_lora()`，必须在 fp32 下、任何降精度之前）；
`save_pretrained` 能导出"合并后的独立快照"。

---

## 2. 能训到什么程度

- `forward(input_values, decoder_input_ids)` **返回 logits**，但**没有 `labels` / loss**，
  也没有任何 `optimizer / backward / Trainer`（仓库里搜不到）——**训练循环要自己写**。
- 目标构造有现成工具：`tokenization_sheetsage2.py` 能编码 pitch / duration / subbeat_shift /
  meter / chord / structure 等 token；`notation_sheetsage2.py` 能把 ABC/格子转回音符。
- **合成监督是可行的**：报告标题就是 *Synthetic Supervision*，仓库里带了 MIDI → 音频的
  渲染工具（`render.py`、`rendering_sheetsage2.py`、`setup_render.py`），
  所以"用 MIDI 渲染音频 + tokenizer 编目标"这条路是通的。

---

## 3. 训练要多少东西

| 项 | 现状 / 估算 |
|---|---|
| 数据 | 官方是 MIDI 派生合成监督（README 没写具体小时数）。要训出增益通常要**数千~数万条 MIDI** 渲染样本；我们本地只有 **6 个 ref.mid + 27 份人工 SVP**（且只有人声轨，没有和弦/结构/伴奏标注） |
| 算力 | 12GB 卡：**冻结基座、只训 LoRA（rank 64）+ 6 层解码器**是可行的（优化器状态很小）；全长 300s 窗口训练很重，建议 30~60s 切片 + 梯度检查点 |
| 时间 | 训练循环 + 目标编码 + 数据管道：**2~4 周**工程；再加上渲染与训练机时（3080 上按天算） |
| 评估 | `samples/量化` 的 `ref.mid`（6 首）可做**音符 F1 / onset ±50ms**；挂字指标（`bench_score`）与它无关 |

---

## 4. 微调能解决什么、不能解决什么

| 目标 | 微调有用吗 |
|---|---|
| 少漏音 / 少多音（巴拉莱卡少 124 个、还有什么更好的多 82 个） | **可能有用**——这就是音符层的召回/精度 |
| 中文流行/说唱/合唱这些曲风上的音符准确率 | **可能有用**（合成监督可用目标曲风的 MIDI 渲染） |
| **三连音** | **没用**：时值词表是"亚拍整数倍"（`DURATION_TEMPLATES = 1,2,3,4,6,8,…`，1 亚拍 = 1/4 拍），1/3 拍表示不出来。要加得**扩词表 + 重训**（模型手术），不是微调 |
| 歌词/挂字（`assign_lyrics`） | **完全无关**，那是下游另一层 |
| BPM/拍号/段落 | 不在输出词表里，微调不改变 |

---

## 5. 结论与建议

**现在不建议做。** 理由：

1. 它解决的是**音符层**的问题，而当前主线（挂字）和用户最能感知的"看不到字/差一个音"不在这一层；
2. 成本高：没有官方训练代码，要自己写训练循环 + 造合成数据；本地金标（6 MIDI + 27 SVP）只够评估，不够训练；
3. 三连音这条最硬的限制**微调也解决不了**（词表限制），只能重训/换模型；
4. CC BY-NC 4.0：只能在非商业场景用，派生权重也要沿用。

**如果以后要做**，最小可行路线：

1. 冻结基座，只训 LoRA + 解码器（12GB 可行）；
2. 数据：目标曲风的 MIDI 渲染（自建或 POP909/Lakh 这类公开 MIDI），30~60s 切片；
3. 目标：用 `tokenization_sheetsage2.py` 编 token，CE loss 直接算在 `forward` 的 logits 上；
4. 验收：`scripts/quantize_bench`（待写）报音符 F1 与 onset，**用 `samples/量化` 的 6 首做留出**；
5. 先跑 1 个 epoch 的小实验，确认 loss 能降、留出集不掉，再谈扩大。

---

## 6. 一句话

**权重是公开的、可以微调；但没有训练代码、数据只有几十首、而且微调补不了三连音。**
把它当成一个独立的"音符层改进"项目来评估，不要指望它改善挂字。
