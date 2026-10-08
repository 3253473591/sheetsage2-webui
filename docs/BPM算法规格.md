# BPM 算法规格

本文是 `src/sheetsage2_webui/tempo.py` 的**实现规格**。背景与实测数据见
`SheetSage2-集成调研.md` §5.1 / §5.3 / §7.3 / §7.7，这里只写"怎么算"。

---

## 0. 一句话

**BPM 不参与出谱。** 音符位置（tick）来自小节表；BPM 只用来显示、写 `set_tempo`、
以及给人看的速度摘要。所以 BPM 算错不会让谱子错，只会让播放时长偏。

---

## 1. 输入

| 来源 | 用途 |
|---|---|
| `beat.lab` | 主输入。每行 `时间 拍位 分子 分母` |
| `score.abc` | 交叉校验：`Q:1/4=`（模型自带 BPM）、`M:`（拍号） |
| `playback.json` | 小节表 → tick 映射（与 BPM 无关） |

`beat.lab` 的时间戳**只精确到 0.01s**。这是后面所有设计的出发点。

---

## 2. 拍号：`estimate_meter(rows)`

`beat.lab` 的第 3、4 列就是拍号，但它会变（弱起、转拍号），所以要按"段"看。

1. 按时间排序，把每行折成 `(time, "分子/分母")`。
2. 切成**连续同拍号段**。
3. **主拍号 = 拍数最多的那一段**（不是众数——众数会把弱起段混进来）。
4. **弱起识别**：如果第一段出现在最前面，且它的拍数 < 主拍号的分子，判为弱起小节，
   输出 `pickup = {meter, beats, start, end, seconds}`。
5. 其余非主段记为转拍号。

> 实测：光芒 `1/8` 弱起 1 拍、朋友的酒 `3/8` 弱起 3 拍、相思开头即 `6/8` 无弱起。
> 三首的 `beat.lab` 主拍号都与 `score.abc` 的 `M:` 一致。

---

## 3. 速度：`estimate_tempo(rows, method="robust")`

### 3.1 为什么不能用中位间隔

`beat.lab` 量化到 0.01s。104 BPM 的真拍长是 0.576923s，存下来是 `0.58`；
339 个拍点取中位间隔锁死在 0.58 → 读成 **103.45**，永远回不到 104。

**改用最小二乘**：对 `t_i = anchor + i·T` 拟合整条网格。量化误差零均值，
339 个点平均后斜率精度 `0.01/195 ≈ 5e-5`，足以还原 **104.0025**。

### 3.2 步骤

1. 取所有拍点时间，排序。
2. `seed = 拍间隔的众数`（按 1ms 取整）。
3. 容差 `tol = max(INLIER_MIN_TOLERANCE, INLIER_TOLERANCE × seed)`，
   标记落在 `±tol` 内的间隔为 inlier。
4. 取 **inlier 的最长连续段** → 在该段的拍点上做最小二乘。
   （取最长连续段是因为歌曲结尾常有散拍，全局拟合会被拖偏。）
5. 若最长段 < 2，退回中位间隔并标 `warnings`。

### 3.3 输出

| 字段 | 含义 |
|---|---|
| `bpm` | 归一化到**四分音符**的 BPM |
| `beat_unit_bpm` | 按 `beat.lab` 拍单位计的 BPM |
| `bpm_median_interval` | 朴素中位间隔法（同分母归一化），仅作对照 |
| `bpm_dominant` | 众数拍间隔对应的"主速度" |
| `anchor` / `period_seconds` | 拟合网格的原点与拍长 |
| `regular_coverage` / `residual_std` / `residual_max` | 稳定段覆盖率与残差 |
| `is_constant` | 是否可当恒速曲 |
| `tempo_range` / `local_tempo` | 局部速度的 min/max/median 与完整曲线 |

### 3.4 归一化公式（容易弄反）

`beat.lab` 每行的时值是 **1/分母 全音符**（4/4 与 2/4 → 四分音符；6/8 → 八分音符）。

```
四分音符 BPM = (60 / 拍长秒数) × 4 / 分母
```

> 实测：相思 6/8 拍长 0.3s → 拍单位 200 → 四分音符 **100.001**。

### 3.5 恒速判定

```
is_constant = regular_coverage >= 0.9 且 residual_std < 0.05 × period
```

用 `std` 不用 `max`——个别拍点偏 50ms 不该否定"这首是恒速"。

---

## 4. 交叉校验：`reconcile(abc_bpm, estimate, tolerance=1.0)`

模型不把速度当预测任务（8 个 prompt 字段里没有 tempo），但它的 ABC 生成器会用一个
**首尾两点法**算一个值写进 `Q:1/4=`：

```python
quarter_notes / (subbeat_times[-1] - subbeat_times[0]) * 60
```

| 歌曲 | 模型 `Q:1/4=` | 网格拟合 | 结论 |
|---|---|---|---|
| 光芒 | 104 | 104.000 | 一致 |
| 朋友的酒 | 120 | 119.995 | 一致 |
| 相思 | **98** | **100.001** | **不一致**（结尾散拍拖偏端点法）|

**取值规则**：

- `is_constant` 为真 → **采用网格拟合值**（用到全部拍点，不受端点影响）
- `is_constant` 为假（变速曲）→ **采用模型 Q 值**（它是整条速度曲线的平均），
  并把 `confident` 置 `false`
- 差超过 `tolerance` → `agreement = false`，如实报警，不静默取舍

`Q:1/4=` 是模型 `round()` 过的整数，精度只有 ±0.5，本身也不适合当精细值。

---

## 5. 分段速度：`sectional_tempo(beat_times, sections, ...)`

歌里的速度变化几乎总在**段落边界**上（`皮囊`：176.28/226.72s 的切换与
`structure.lab` 边界重合到 1 秒内）。所以按段落拟合，而不是逐小节。

1. **分组**：按序把结构段攒成"至少 `min_beats` 拍"的组。
   （结构表里常夹 2 秒左右的碎片，单独拟合会出噪声值。）
2. **拟合**：每组最小二乘 → `bpm / residual / period / interval_consistency`。
3. **不可信判定**：
   ```
   reliable = residual_max <= period 且 interval_consistency >= MIN_INTERVAL_CONSISTENCY
   ```
   `interval_consistency` = 拍间隔落在拟合拍长 ±5% 内的比例。
   **残差只回答"能不能被一条直线穿过"**，对缓慢起伏的速度太宽松——
   `壁上观` 中段四段的残差都合格，但一致性只有 8%/24%/25%/23%。
4. **相邻不可信组合并**成一片（先做，避免它们被逐个当成"小段"并走）。
5. **合并规则**（`_should_merge`）：
   - 两段都不可信 → 并
   - 两段都可信 → 速度差 ≤ `tolerance` 就并；否则只要有一边拍数 < `min_segment_beats` 也并
   - 一可信一不可信 → 看不可信那片多大：
     - ≥ `LARGE_UNSTABLE_FRACTION` 拍数 → **绝不并**（自己作为"网格不可靠"报出来）
     - < `SMALL_UNSTABLE_FRACTION` → 并
     - 介于两者 → 速度接近才并
6. **不参与拟合的段**：拍数 < `min_segment_beats` 或不可信的段，并进去时**不贡献拟合点**。
   否则它会把好段的残差顶上去，反过来让好段也变成"不可信"，挡住后面本该合并的段。
7. **按整段重算一致性**：合并时保留的是可信子段的拟合结果，若直接沿用，报告就会拿
   一小段的自洽度去背书一整段（`壁上观` 会显示 95% 而不是 37%）。
8. **过渡段标记**：速度严格落在左右两段之间的可信段 → `is_transition`。
   `2020伴奏` 因此得到 `stable_tempos = [110, 75, 110]`。

---

## 6. 精度与位数：`TempoSegment.bpm_sigma / bpm_decimals`

时间戳量化只贡献 **±0.0004 BPM**（自助法：把拍点按 ±5ms 扰动重拟合 200 次）。
真正的误差来自**模型拍点自身的抖动**，用滑窗（切 3 份、每窗拟合一次）测标准差：

| 区段 | 滑窗 σ | 该报几位 |
|---|---|---|
| 巴拉莱卡 67.8–225.2s | ±0.003 | 3 位（真的）|
| 壁上观原唱 71.6–284s | ±0.000 | 3 位（真的）|
| 皮囊 0–172.3s | ±0.30 | 1 位 |
| 不由己 255.7–284.4s | ±3.09 | 1 位 |

位数规则：`σ ≥ 0.05 → 1 位`；`σ ≥ 0.005 → 2 位`；否则 3 位。
`bpm_uncertain = σ / bpm > BPM_UNCERTAIN_FRACTION` 时标出来。
**速度表（`tempo_map`）也写取整后的值**，不在 MIDI 里暗示不存在的精度。

---

## 7. 网格重建评估：`suggest_grid_recovery(...)`

给一片"网格不可靠"的区域找"最像"的恒定网格，并量化重建代价。

- 在 `[min_bpm, max_bpm]` × 20 个相位上搜命中率（拍点落在网格 ±50ms 内的比例）
- **网格密度约束**：`span/period + 1 > 1.5 × 拍点数` 的候选直接跳过。
  不加这条，拍长越短命中率越高，会选出 240 BPM 这种假阳性。
- 粗搜后用"残差中位数"做一次细搜（命中率有台阶，中位残差是连续量）
- 输出：最佳 `bpm/period/phase`、`hit_ratio`(±50ms)、`tight_ratio`(±20ms)、
  `missing_beats`、`median_shift/max_shift`、`confident`

**校准**：模型自己完全干净的段在 ±50ms 上是 100%、±20ms 上约 72–100%。
`confident = hit_ratio >= 0.95 且 tight_ratio >= 0.60`。
实测六个干净段全部通过，三个问题段全部不通过。

> 注意：`壁上观` 71.5–249.5s 的最佳网格命中率只有 **50%**（±20ms 为 0%），
> 说明**那段拍点本身就错了或漏了，不是相位猜错**。这种情况下值能自动定（靠相邻
> 干净段 + 音频上的独立估计），但网格要人工拍板。

---

## 8. 干预 API

| 层级 | 函数 | 说明 |
|---|---|---|
| 估计 | `estimate_tempo()` | 鲁棒 LS + 稳定段 |
| 吸附 | `snap_bpm(bpm, "none"\|"integer"\|"half"\|"candidates")` | 取整 / 半整数 / 最近候选 |
| 重排 | `constant_beat_times()` / `compare_to_constant()` / `retime()` | 恒定网格、漂移量化、按比例重排 |
| 分段 | `segments_to_tempo_map()` | 分段速度 → tick 域速度表 |

`tempo_report.py --apply` 会**另写** `beat.constant.lab`，不动模型原始产物。

---

## 9. 参数表（与代码同步）

| 常量 | 值 | 作用 |
|---|---|---|
| `INLIER_TOLERANCE` | 0.05 | 拍间隔相对容差（找稳定段） |
| `INLIER_MIN_TOLERANCE` | 0.015 | 绝对容差下限 |
| `MIN_INTERVAL_CONSISTENCY` | 0.6 | 判"恒速区"的一致性门槛 |
| `SMALL_UNSTABLE_FRACTION` | 0.15 | 小于此比例的不稳定区直接并走 |
| `LARGE_UNSTABLE_FRACTION` | 0.25 | 大于此比例的不稳定区绝不并 |
| `BPM_UNCERTAIN_FRACTION` | 0.02 | σ/bpm 超过它就标"速度不确定" |
| `sectional_tempo.min_beats` | 8 | 攒够多少拍才拟合一组 |
| `sectional_tempo.tolerance` | 1.0 | 相邻段速度差多少算同一档 |
| `sectional_tempo.min_segment_beats` | 16 | 少于此拍数不能独立成段 |
| `suggest_grid_recovery` 密度约束 | 1.5× | 网格线不得超过拍点数的 1.5 倍 |

---

## 10. 已知边界

1. **变速曲没有"正确"的单一 BPM**，只能给摘要 + 分段表。不要用单一 BPM 换算 tick。
2. **对齐器/模型的节拍网格本身可能坏掉**（`壁上观` 伴奏版 3 分钟）。这种情况下
   值可以自动定，网格不行。判据是 `suggest_grid_recovery().confident`。
3. **半速锁定只提示不自动纠**：`halftime_hint` 给 ×2 候选值。慢歌本身也可能是对的。
4. **`beat.lab` 的拍号是稀疏编码**（只在开头/变化处给 `meter`，其余靠继承），
   所以不要逐事件读 meter，要按 `beat.lab` 展开后的列读。
