# WebUI 问题清单

> **这份文档专门装 WebUI 的问题与改进**：发现一个记一个，修完标 ✅ 并写清根因、证据、
> 改了哪些文件。**不要再往 [`交接-下一次对话.md`](交接-下一次对话.md) 里加条目**——
> 那是"入口与现状"，保持短；这里才是流水账。
>
> 规格在 [`WebUI规格.md`](WebUI规格.md)，各功能的算法细节在各自的规格文档里
> （[`SVP导出调研.md`](SVP导出调研.md) / [`音符量化规格.md`](音符量化规格.md) /
> [`八度校正规格.md`](八度校正规格.md) / [`BPM算法规格.md`](BPM算法规格.md)）。

**状态**：🔴 待处理 · 🟡 已定位待复核 · ⛔ 不做（写清理由） · ✅ 已修

| # | 问题 | 状态 | 领域 |
|---|---|---|---|
| 1 | [扒谱音高整体高一个八度](#1-扒谱音高整体高一个八度-) | ✅ 已修（默认降一个八度） | 扒谱 / 音高 |
| 2 | [SVP 导出把拍号写满整个时间轴](#2-svp-导出把拍号写满整个时间轴-) | ✅ 已修 | 导出 SVP |
| 3 | [SVP 导出把速度写满整个时间轴](#3-svp-导出把速度写满整个时间轴-) | ✅ 已修 | 导出 SVP |
| 4 | [左侧工程列表重启就空](#4-左侧工程列表重启就空-) | ✅ 已修（以 `output/` 为准） | WebUI / 工程列表 |
| 5 | [SVP 下载 500：`NotesOverlappedError`](#5-svp-下载-500notesoverlappederror-) | ✅ 已修（端点换算 + 同轨去叠音） | 导出 SVP |
| 6 | [个别段落低一个八度](#6-个别段落低一个八度-) | ✅ 已修（重复段八度一致性校正） | 扒谱 / 音高 |
| 7 | [没分到字的音被填成『啦』](#7-没分到字的音被填成啦-) | ✅ 已修（无缝就写延音） | 歌词 / 导出 SVP |

**新增一条的写法**：现象（谁在什么操作下看到什么）→ 证据（数字 / 文件 / 可复现命令）→
根因（指到具体函数）→ 修法与影响面 → 状态。

---

## 1. 扒谱音高整体高一个八度 ✅

**状态**：✅ 已修（默认「降一个八度」，参数面板可以退回「原样」/「降两个八度」）

### 现象

SheetSage2 扒出来的旋律**普遍比实际演唱高一个八度**：卷帘上的音、`transcription.mid`、
`score.abc`、导出的 SVP 都高。**人耳确认**。改拍号 / BPM / 量化都治不了它（那些都不看音高）。

### 证据

**① 不是我们转错的。** `src/sheetsage2_webui/` 里没有任何八度或移调处理
（`grep -i "octave|transpose|\\+12|-12"` 零命中），所以偏移只可能来自模型输出。

**② 模型的三个出口互相一致**，说明也不是某一环漏了换算：
`melody_vocal.lab` 第三列的 pitch、`transcription.mid`、`score.abc` 的 ABC 音名对得上
（`notation_sheetsage2.py::note_to_abc` 用 `octave=(note-60)//12`，大写字母 = C4 = 60，
是标准 ABC 八度记法）。

**③ 音域统计（`scripts/pitch_octave_check.py --stats`，`output/` 里 10 个真实工程，
`melody_vocal.lab`，MIDI 音高，60=C4 / 72=C5）**：

| 歌曲 | 音数 | 最低 / 中位 / 最高（MIDI） |
|---|---|---|
| 皮囊（萧敬腾，男） | 397 | 60 / **72（C5）** / **95（B6）** |
| 朋友的酒（李晓杰，男） | 501 | 61 / **71（B4）** / **90（F#6）** |
| 壁上观（张曦匀，男） | 330 | 68 / **78（F#5）** / 87 |
| 巴拉莱卡（门尼，男） | 441 | 52 / **69（A4）** / 83 |
| 光芒（凤凰传奇，女+男） | 324 | 65 / **76（E5）** / 86 |
| 相思（毛阿敏，女中音） | 214 | 69 / **81（A5）** / 88 |
| 2020 原唱 | 939 | 63 / 72（C5） / 82 |

B6 = 1975 Hz、F#6 = 1480 Hz，**人声不可能**；男声歌的中位普遍落在 A4–F#5，
比正常男声旋律（C4–G4 一带）高一个八度左右。注意**男女声的中位没有明显分层**
（男 69–78 / 女 76–81）——这一点本身就说明模型的八度不由歌手的实际音域决定。

**④ 混音上的 F0 实测（初步）**：用 `scripts/pitch_octave_check.py`（时值 ≥0.6s 的长音，
中点开 0.4s 窗、高通 130 Hz 后估 F0）与谱面音高相比：

- 扣掉整八度后残差中位 **0.00 / -0.03 半音**，`|残差| ≤ 1` 占 **65%~79%**
  （《皮囊》47~55 个样本、《相思》65 个样本）—— 即**音级（pitch class）是对的，错的就是八度**；
- 差值**全部落在整八度上**（+12 / +24 / +36 …），没有别的余数；
  中位 ≈ +24 半音（谱面比音频高约两个八度）。

> ⚠️ **这条还不能当结论**：测的是**全混音**，而自相关在伴奏能量大时会挑到 **2 倍周期**
> （少算一个八度）。不同选峰方式（纯自相关 / 谐波梳）给出的整八度分布并不一致
> （《皮囊》`{+12:21, +24:16, +36:10}` vs `{+12:9, +24:27, +36:17, +48:2}`），
> 说明八度的绝对倍数不可信——**能确定的只有"音级对、八度错"，真实偏移是 +12 还是 +24 要用干声定案**。

**⑤ 模型侧线索**：`models/SheetSage2/benchmarks/rock_corpus.md` 写明评分口径是
"把 MIDI 音高 **mod 12 折叠**后按 50 音分容差匹配"——**八度不进评测**，
所以模型没被训练/评测去把八度对准，出现整体八度偏移是合理结果。

### 影响面

| 受影响的 | 不受影响的 |
|---|---|
| 卷帘音高、`transcription.mid` / `melody*.lab` / `melody*.mid`、`project.json` 的 `pitch`、导出的 SVP | BPM / 拍号 / 弱起 / 音时值 / 量化 / 填词对齐 / 歌词识别（都不看音高） |

### 修法（已实现：`src/sheetsage2_webui/pitch.py`）

**只降旋律，和弦一根手指都不动**——这是上面证据直接给出来的结论：
同一份产物里 `chords.mid` 的音域（低音 36~47 = C2~B2，整体 36~69）完全是键盘/贝斯的常规音域，
只有旋律两轨高得离谱。人耳也确认"高八度"。

| 产物 | 处理 |
|---|---|
| `melody_full.lab` / `melody_vocal.lab` / `melody_instrumental.lab` | pitch 列 −12 |
| `melody.mid` / `melody_vocal.mid` / `melody_instrumental.mid` | 全部音符 −12 |
| `notation/song_melody.mid` + **重建 `score.abc`** | 模型自带的 `generate_abc_from_exports()`；实测能从原始 `notation/` **逐字节复现**模型写的 `score.abc`（2525B 全等），所以重建只改了音高 |
| `transcription.mid` | 只动 Vocal / Ins 轨，**Chords 轨保持** |
| `playback.json` | 只动 Vocal / Ins 轨 |
| `chords.mid` / `notation/song_chords.txt` / `events.*` / `tokens.*` / `result.json` | **不动**（和弦本来就对；后三者是原始模型输出，留着能复查） |

- **落点**：`pipeline.run(pitch_shift=…)` 在 `engine.transcribe()` 之后立刻做，
  内存里的 `labs` / `playback` / `abc` 和磁盘上的产物一起改——所以 tick 域工程、卷帘、SVP 导出、
  下载包里的 MIDI 全是同一份正确的音高，不会出现"工程对、下载的 MIDI 错"。
- **默认值**：`pitch.DEFAULT_SEMITONES = -12`，**固定，不做成面板选项**——
  虚拟歌姬本来也不会按原调调音，用户不需要选（这是用户拍板的）。
  命令行调试仍可用 `--pitch-shift 0|-12|-24`；跑完在阶段详情与 `highlights.pitch` 里能看到
  "音高 降一个八度（固定）"。
- **验证**：
  - `tests/test_pitch.py` 12 项（lab 列 / MIDI 轨过滤与夹取 / playback / 和弦不动 / 零位移不动 / ABC 逐字节复现）；
  - 真机端到端：同一段音频跑 `pitch_shift=0` 与 `-12` 两遍，音符音高**整齐 −12**、
    `tick`/`dur` 与小节表逐字节不变、`chords.mid` 一个音都没动、SVP 里人声也跟着降下来；
  - 全量回归：`pytest tests -q`（249 项，含"默认就该降 12 / `pitch_shift=0` 时不碰"两条）
    + `npm test`（前端 29 项，含"面板上不该有「音高」这一行"）全过。

### 复核办法（如果以后又怀疑倍数）

```powershell
# ① 音域统计（就是上面 ③ 那张表）
.\.venv\Scripts\python.exe scripts\pitch_octave_check.py --stats

# ② 逐音比 F0 —— **换成干声**（vocal stem）跑，八度就没有歧义了
.\.venv\Scripts\python.exe scripts\pitch_octave_check.py "干声.flac" "output\<工程>\melody_vocal.lab"
```

- 干声从哪来：WebUI 歌词阶段支持上传「干声」，或用外部工具分离出 vocal stem。
- 或者挑一首有公开简谱的歌逐音对。

### 相关文件

`src/sheetsage2_webui/pitch.py`（实现）、`tests/test_pitch.py`、
`scripts/pitch_octave_check.py`（查证脚本，`--stats` / 逐音比 F0 两种模式）、
`pipeline.run(pitch_shift=…)`（默认 `-12`，面板上不暴露）。

---

## 2. SVP 导出把拍号写满整个时间轴 ✅

**状态**：✅ 已修（`EXPORT_REVISION = 2`）

### 现象

整首 4/4 的歌，导出的 `.svp` 在**每个小节上面都标一次 4/4**。

### 证据（实测《光芒》45s 版）

- 我们的 `timeline.measures` 是 21 条（弱起 1/8 + 20 条 4/4）；
- 文件里 `time.meter` 是 **20 条一模一样的 `{numerator:4, denominator:4}`**（index 0…19）；
- 重导后：拍号 **20 条 → 1 条**，**速度轴 / 音符（起点·时值·音高·歌词）/ 轨名逐项相等**。

### 根因

`svp._meter_list()` 原来把逐小节的 `timeline.measures` 原样抄成 `TimeSignature` 列表，
而 libresvip 的 `synthv_generator.generate_project()` 只做
`skip_beat_list(list, 1)`（整体 bar_index −1），**不折叠连续相同的拍号**。
SVP 的 `time.meter` 语义是"从这里开始用这个拍号、直到下一条"，所以整首同拍号只需一条。

### 修法

- `_meter_list()` 折叠连续相同的 `(numerator, denominator)`，只留变化点；
  中途真转拍号（3/4、6/8）照样保留；弱起仍是 bar 0（被 SV 当前置小节吸收，
  `first_bar_tick` 正好等于弱起长度，音符位置零漂移）。
- 新增 `svp.EXPORT_REVISION` 并写进 `cache_key()`：编码逻辑改了就让
  `<job>/export/<hash>.svp` 的老缓存失效，否则已经导出过的工程会一直发旧文件。
- 测试：`tests/test_svp.py::test_meter_list_only_keeps_the_change_points`（纯函数）+
  往返用例改成断言文件里恰好一条 `(0,4,4)`；`scripts/verify_webui.py` 加了不变式
  "相邻两条拍号必须不同"。

### 涉及文件

`src/sheetsage2_webui/svp.py`、`tests/test_svp.py`、`scripts/verify_webui.py`、
[`SVP导出调研.md`](SVP导出调研.md) §5.2。

---

## 3. SVP 导出把速度写满整个时间轴 ✅

**状态**：✅ 已修（`EXPORT_REVISION = 3`）

### 现象

恒速曲（比如整首 125 BPM）导出的 `.svp`，**每个小节上面都标一次速度 125**。

### 证据（真工程，同一个工程改前 → 改后）

| 工程 | `tempo_map` | 文件里的 tempo 点 |
|---|---|---|
| 光芒（恒速 104） | 1 段 | **85 → 1** |
| 巴拉莱卡（变速 57.5→59.0） | 2 段 | 111 → 10 |
| 皮囊（变速 124.3/110/124.3） | 3 段 | 180 → 109 |

三种情况下**音符（起点·时值·音高）逐项不变**——速度轴只影响 tick→秒的换算，不影响谱面。

### 根因

`svp._tempo_list()` 原来把逐小节的 `measures[*].bpm`（= `tempo_map_exact`）原样抄进去。
恒速曲那张表其实是**同一个速度加一点网格抖动**：《光芒》86 个小节里只有 103.896 / 104.348
两个值来回跳，外加弱起与末小节的碎片值（111.1 / 244.1）——照抄就是每个小节一条速度。
`time.tempo` 的语义跟 `time.meter` 一样是"从这里开始生效"，所以恒速曲一条就够。

### 修法

`_tempo_list()`：

- **恒速曲**（`timeline.tempo_map` 只有一段）→ 写**一条**，值用那一段的拟合 BPM（界面上报的那个数）；
- **变速曲** → 保留逐小节的精确表（那里每个点都是真的转速度），只把相差 ≤0.5%
  （`TEMPO_MERGE_TOLERANCE`）的相邻点折叠掉。

`EXPORT_REVISION` 从 2 升到 3，让 `export/` 里的老 `.svp` 缓存失效。

**代价（写在明处）**：恒速曲用一条速度铺满整首，播完与原曲的时长差 **0.4%~1.8%**
（三首恒速曲实测 +1.09 / +1.28 / −3.34 秒）。逐小节的精确表仍在 `project.json` /
`report.json` 里，需要严格对齐原音频时用它。变速曲那条路没有这个代价（只折叠数值相同的点）。

### 验证

- `tests/test_svp.py`：`test_tempo_list_writes_one_marker_for_a_constant_song`、
  `test_tempo_list_keeps_changes_and_collapses_repeats`；往返用例不变（音符仍逐项相等）。
- 真工程对比（把改动前/后的 `_tempo_list` 各导一遍同一个工程）：`time.tempo` 条数如上表，
  `mainGroup.notes` 的 `(onset, duration, pitch)` **逐项相同**。
- `scripts/verify_webui.py` 加了两条不变式：「速度轴不写重复的相邻点」+
  「恒速曲的速度轴只有一条」（判据直接看 `project.json` 的 `tempo_map` 段数）。

### 涉及文件

`src/sheetsage2_webui/svp.py`、`tests/test_svp.py`、`scripts/verify_webui.py`、
[`SVP导出调研.md`](SVP导出调研.md) §5.2。

---

## 4. 左侧工程列表重启就空 ✅

**状态**：✅ 已修（**按用户拍板：以 `output/` 为准**）

### 现象

左侧的工程列表在**重启程序后是空的**：工程还在磁盘上，但列表不认它们。
另外命令行直接跑出来的 `output/<歌名>/` 从来没进过列表。

### 根因

`JobManager.__init__` 只做 `self._jobs = {}` / `self._order = []`——**从来不读盘**。
`job.json` 一直在写（`_write_metadata`），但没有任何代码把它读回来；而且工程本来放在
`tmp/webui/jobs/<uuid>/output/` 里，"产物"和"临时目录"是两套东西。

### 修法（工程目录 = `output/<歌名>/`）

- **数据根改成 `<项目根>/output`**（`SHEETSAGE_WEBUI_DATA` 仍可覆盖）；
  **一个子目录 = 一个工程**，而且**工程目录就是产物目录**——和 `cli.py` 跑出来的完全同形状。
- 工程内部：`job.json`（元数据，在工程根）+ `.webui/`（`input/` 原音频、`stems/`、
  `peaks-*.json`、`export/` 缓存的 `.svp`、`bundle.zip`）。产物清单与整包 zip **跳过** `job.json` 与 `.webui/`。
- **重启时扫数据根重建列表**：
  - 有 `job.json` → 按元数据恢复（参数、阶段、highlights、文件清单全都回来）；
    上次是 `running/queued` 的阶段标成 `idle` + "上次运行被中断（程序重启）"，**不装成还在跑**；
  - 只有 `project.json`/`report.json`（命令行或别处跑的）→ 作为**只读工程**收进来：
    `transcribe=done`、有 `lyrics.json` 就 `lyrics=done`，`highlights` 从产物里重建；
  - 其它目录一律不动。
- 只读工程（`imported=true`，界面标「已有产物」）：**能看卷帘、下载 zip、导 SVP**；
  改参数 / 重跑 / 换音源 / 删除 → **409** 并说明"要重跑请拖入音频新建工程"；
  `/audio` 与 `/files/job.json` → 404；而且**绝不往里写 `job.json`**（那会把别人的产物变成 WebUI 工程）。
- 同名不覆盖：重名目录自动 `-2`、`-3`……；`project_folder_name()` 与 `cli._slug()` **同一规则**
  （有单测盯着），两边目录名才能对上。

### 验证

- `tests/test_webui.py` 新增 8 条：目录形状与 `.webui/` 隐藏、**重启后列表与参数都恢复**、
  中断的阶段不装成运行中、命令行产物只读收编、只读工程在 API 层全套 409/404、
  `project_folder_name` 与 `cli._slug` 一致、拒收上传时不留半截目录。
- 前端：`stageActions` 对只读工程整组禁用（加了单测），侧栏打「已有产物」标签、删除按钮禁用，
  音轨那行显示"没有原音频 · 载入的已有产物"且不请求 `/audio`。
- **真机活体**：起真服务（数据根 = 仓库的 `output/`）→ `/api/jobs` 列出 **10 个命令行跑过的工程**
  （全部 `imported=true`、`transcribe=done`），`/project`、`/lanes`、`/files/project.json`、
  `/download` 全 200；`/audio` 404、重跑 409、删除 409，提示都是人话。
- 全量：`pytest tests -q` **280 项** + `npm test` **30 项**全过。

### 涉及文件

`src/sheetsage2_webui/webui/jobs.py`（布局 + 恢复 + 只读收编）、`webui/app.py`（数据根、
peaks/export 缓存路径、409/404 映射）、`web/src/stageActions.ts`、`AppSidebar.vue`、
`TrackList.vue`、`styles.css`、`web/src/types.ts`、`tests/test_webui.py`、
[`WebUI规格.md`](WebUI规格.md) §2「工程目录」。

### 顺带修的两处（真机日志里暴露出来的）

- **只读工程的 `/peaks` 以前是 500**：没有原音频，`job.upload` 指向一个不存在的路径，
  一路炸到 `media.compute_peaks`。现在先查文件在不在，**不在就 404** +
  "这个工程没有原音频"。实测：自建工程 200、只读工程 404。
- **`/favicon.ico` 以前 404**：浏览器每次开页面都来要一次图标，日志里天天一行 404。
  现在前端用**内联 SVG 图标**（`web/index.html`），后端也加了一条 `/favicon.ico` → **204**。
- 另外说明一下**不是 bug 的那个 404**：`/api/jobs/{id}/lanes` 在**还没扒谱**时就是 404
  （`build_lanes` 找不到 `project.json`），前端把它当"卷帘空"处理——刚上传完那一刻必然出现一次，
  扒谱跑完就 200 了。

---

## 5. SVP 下载 500：`NotesOverlappedError` ✅

**状态**：✅ 已修（`EXPORT_REVISION = 4`）

### 现象

点「下载 SVP」→ HTTP **500**，服务端日志：

```
libresvip.core.exceptions.NotesOverlappedError: Notes overlapped near bar 76
  libresvip/plugins/svp/synthv_generator.py:134 -> PitchSimulator(...)
```

出事的工程：`1cfa27124884`《忘川风华录 - 旷古回响》，**量化是关掉的**（`options.quantize = "off"`）。

### 根因（两件事叠在一起）

1. **tick 换算：起点和时长分别 round**。`_note_objects` 原来写的是
   `start = round(tick/4)`、`length = round(dur/4)`。未量化时 tick 不是 4 的倍数，
   两次独立取整会让**本来正好头尾相接**的两个音在 SV 域里叠 1 tick：
   我们的 `576478+478` 与 `576956` 相接，SV 域却是 `144120+120 = 144240` vs `144239`。
   （量化开着时 tick 都是 240 的倍数、÷4 整除，所以这个坑一直没露头。）
2. **SVP 一轨是单声部的**：只要叠了哪怕 1 tick，libresvip 的 `PitchSimulator` 就拒绝生成。

### 修法

- `svp.to_sv_window()`：把 `(tick, tick+dur)` **两个端点分别换算**，长度由端点相减得到。
- `svp._note_rows()`：再兜一层"一轨不叠音"（和和弦那条路一个道理）——
  起点被上一个音的终点顶到后面就**往后推**、终点越过下一个音的起点就**截短**、
  被挤到没有长度就**丢掉**（宁可少一个音也不能叠）。
- 动过几个音会记进 `stats["adjust"]`，并生成 `stats["warnings"]`；
  `app._svp_download` 把它写进工程日志（不然"怎么少了一个音"没法查）。
- `EXPORT_REVISION` 3 → 4：老 `.svp` 缓存作废（旧文件是按错位置算的）。

### 验证

- `tests/test_svp.py` 新增 5 条：端点换算（含"旧算法确实会叠 1 tick"的反向断言）、
  真叠音的推/截、同 tick 丢弃、端到端 build 一个会叠音的轨道**不再抛异常**、
  **真事故工程回归**（`output/忘川风华录_-_旷古回响` 导出成功）。
- **真机复现并复验**：起服务 → `GET /api/jobs/1cfa27124884/download?kind=svp&parts=vocal`
  **200**（122 808 字节），以前这里是 500。
- 全量：`pytest tests -q` **286 项**全过。

### 涉及文件

`src/sheetsage2_webui/svp.py`（`to_sv_window` / `_note_rows` / `stats.adjust`）、
`src/sheetsage2_webui/webui/app.py`（下载时把调整写进日志）、`tests/test_svp.py`、
[`SVP导出调研.md`](SVP导出调研.md) §5.2。

---

## 6. 个别段落低一个八度 ✅

**状态**：✅ 已修（`src/sheetsage2_webui/octave.py`，默认开；`--no-octave-fix` 可关）

> 算法细节在 [`八度校正规格.md`](八度校正规格.md)，这里只记现象、证据与影响面。

### 现象

《我本将心向明月》导出的 SVP 里，**同一段副歌在歌里出现三次，第二次整整低了一个八度**：
机扒给 66 / 54 / 66，人工版把中间那次抬回 66。整体 −12（§1 的修法）治不了它——
它只动绝对音区，动不了段与段之间的**相对**八度差。

### 证据

`samples/对齐/` 里那对 SVP（机扒 vs 用户在 Synthesizer V 修过的人工版）：

- **不是时间问题**：381 个机扒音里 372 个 onset 与人工版逐 tick 相同，时值分布也一样；
- **是八度问题**：音高差里 **76 个正好 +12**，其余 4 个（+7/+8/+10/+14）都出在人工合并/新增音处；
- 按人工版逐音对齐，机扒的音高命中 **292/372（79%）**。

### 根因

模型（SheetSage2）的音高**音级对、八度不可靠**（§1 已证）。它对同一段旋律的不同出现
可能给不同的八度，而 §1 的修法是**整体**平移，覆盖不了这种**局部**错误。

### 修法（`octave.py`）

靠一条**不需要外部真值、能自证**的不变量：**重复段必须同高**。

1. 按静音间隙（≥0.6s）切乐句；
2. 乐句两两做音级序列匹配（`difflib.SequenceMatcher`）+ 音程走向校验，找出"只差整八度"的重复段；
3. 段之间建约束 `k_A − k_B = Δ八度`，并查集连成分量；
4. 每个分量挑一个整体平移 K，**改动的音符数最少**（= 让证据最多的那一拨不动）；
5. **平票就不动手**（分不清哪次是错的，不猜——`光芒` 的器乐就是这个情况）。

实测 `output/` 13 个工程：**11 个一个音都不动**，动的只有《我本将心向明月》（82 音）与
《皮囊》（9 音）；《光芒》平票放弃。

| 指标 | 校正前 | 校正后 |
|---|---|---|
| 与人工版逐音相同 | 292/372（79%）| **361/372（97%）** |
| 人工改过的 80 个音里跟对 | — | 69 |

### 影响面

| 受影响的 | 不受影响的 |
|---|---|
| 与 §1 相同的一组产物（melody lab/MIDI、`transcription.mid` 的旋律轨、`playback.json`、`score.abc`、`project.json`、导出的 SVP）| 和弦轨（照旧一根手指不动）、时间（tick/秒/时值）、`events.*` / `tokens.*` / `result.json` |

`project.json` 的 `pitch.octave` 里能看到这次校正改了几个音、依据是哪几处重复段。

### 验证

- `tests/test_octave.py` 16 项（无重复不动 / 短重复不动 / 低八度抬回 / 平票不动 /
  非整八度不动 / 分轨独立 / lab-MIDI-playback 三方同步 / 关掉整体平移时照常跑）；
- `tests/test_octave_samples.py` 7 项（**直接读 `samples/对齐` 两个 `.svp`**：前提、结果、
  只动那两段、人工版喂回去不再动、《旷古回响》两个工程一个音都不动）；
- 全量：`pytest tests -q` **318 项**全过（6 跳过）。
- 端到端：把 `pitch.apply(semitones=0)` 应用到《我本将心向明月》产物的副本上，
  `melody_vocal.lab` / `melody_vocal.mid` / `playback.json` 三方都是同样的 73+9 个音、
  全部正好 +12，`melody.mid` 与 `melody_full.lab` 逐音一致，和弦轨 0 改动，`score.abc` 重建成功。

### 涉及文件

`src/sheetsage2_webui/octave.py`（新）、`src/sheetsage2_webui/pitch.py`（`apply` 接入 + 统计块）、
`src/sheetsage2_webui/pipeline.py`（`octave_fix` 参数 + `--no-octave-fix`）、
`tests/test_octave.py`、`tests/test_octave_samples.py`、`tests/test_pipeline.py`（2 处）、
[`八度校正规格.md`](八度校正规格.md)。

---

## 7. 没分到字的音被填成『啦』 ✅

**状态**：✅ 已修（`svp.py::_note_objects`，`EXPORT_REVISION` 4 → 5）

### 现象

导出的 SVP 人声轨里，没分到字的音全部被填成占位音节『啦』——《我本将心向明月》381 个音里有
**57 个**，唱出来是一串"啦啦啦"；用户在 Synthesizer V 里修过的版本里**一个都没有**。

### 证据

还是 [`samples/对齐/`](../samples/对齐) 那对 SVP（机扒 vs 人工）。把 373 个对得上的音槽逐槽分类：

| 类别 | 槽数 | 属于 |
|---|---|---|
| 都有字但不同（"今天哈哈哈" vs "朝为田舍"）| 141 | **识别**（ASR 认错字，另案）|
| 同位同字 / 双方都延音 | 135 | 一致 |
| 我们 `啦` / 人工**有字** | 35 | 对齐（字没挂上）|
| 我们 `啦` / 人工**延音** | 19 | 对齐（这条已修）|
| 我们延音 `-` / 人工**有字** | 22 | 对齐（见"还没做"）|
| 我们给了新字 / 人工**延音** | 21 | 对齐（见"还没做"）|

关键的结构性证据：**人工版 55 个延音标记全部与前一个音无缝相接（间隙 0），且全文 0 个占位音节**；
我们 57 个 `啦` 里有 54 个也是无缝的——它们本来就是同一个字的延续，只是逐字时间戳没给到那里。

### 根因

`svp._note_objects` 里一句 `lyric or filler`：没分到字的音一律填占位音节，
**没有区分"同一个字的延续"和"真的没有字"**。`lyrics.py` 的转音标记只覆盖
"落在字的时间窗内"的音，窗口之外的没字音就漏到了导出层。

### 修法

没分到字的音**不再一律填占位音节**，按"前面还有没有字可续"分两种：

- 与前面那个音之间没有像样的休止（间隙 ≤ `svp.CONTINUATION_MAX_GAP` = **半个四分音符**，
  SV 域 240 tick）→ 写延音 `-`；
- 隔着一个够长的休止（多半是下一句的开头，前面没有字可续）→ 退回占位音节。

两条补充规则，都是被样本逼出来的：

1. **占位音节自己也算锚点**：一句里连着几个没字的音，只有第一个（前面真的没字）拿占位，
   后面连续的都续成 `-`。不然"休止 + 连续没字"会变成"啦啦啦"。
2. **器乐轨不参与**：那种轨一个字都没有，每个音都"前面没有字"——照旧每个音一个占位音节，
   不会变成"整句只唱一个字"。

**效果**（在 `samples/对齐` 的机扒工程上实测）：占位音节 **57 → 1**，延音 35 → 91，
19 个槽与人工版直接一致。剩下那 1 个是第 247 拍（32 拍长休止之后的句首，ASR 也没给字），
前面根本没有字可续，占位是唯一选择。

这条规则同时兜住了 **SheetSage2 多扒 / 少扒音符**：

| 情况 | 样本里的数量 | 我们的处理 |
|---|---|---|
| **多扒**（机扒有、人工没有的音）| 8 个 | 7 个变成 `-`（人工是把它们并进相邻音，等价于延音）|
| **少扒**（人工有、机扒没有的音）| 5 个 | 人工补的都是 `-`；我们不加音（那是 SheetSage2 的活），但相邻音的字不会因此被顶掉 |

### 还没做的两件（都是"对齐"本身，不是导出口径）

1. **字的"头"挂在哪个音**：21+22 个槽的分歧里，能配成对的 7 个簇有 **6 个是"人工的头
   节拍更强、时值更长"**（典型：我们挂在 16 分短音上，人工挂在紧跟着的整拍长音上、
   把短音算延音）。要改就得**放宽 ASR 的时间窗、按节拍强弱/时值挑头**——会偏离
   逐字时间戳，需要先在别的歌上验证，没动。
2. **字根本没到那个音上**。把 35 个"我们 `啦` / 人工有字"的槽逐一查了
   （±0.6s 内我们 ASR 给了什么）：

   | 我们 ASR 在目标音附近给了什么 | 槽数 | 说明 |
   |---|---|---|
   | **就是人工那个字** | 8 | 听到了但挂到别的音上去了，有救 |
   | **别的字** | 8 | 听岔了（识别）|
   | **什么都没有** | 19 | **对齐器把那一带的时间戳压到别处**（176~207s 一整段），字在文本里有、时间不在 |

   加上 `dropped`(20) / `absorbed`(24) / 零长度(67/333)，根子都在对齐器的时间戳，
   不是挂字算法——这正是 §「歌词对齐质量」当年查过的"对齐器对短促/弱唱的音节
   本来就给不出时间"。要治得从对齐器下手（参考歌词只定位不认字那条路），
   或者上**切分模式**（`lyrics.assign_lyrics_split`，能救"音比字少"但要新增 60~93% 的
   音符，见 `reports/切分模式对照.md`，要不要开成面板项待定）。

### 验证

- `tests/test_svp_lyrics.py` **12 项**（不依赖 libresvip，用假的 `Note`）：
  没字 → 延音、**短休止（≤半拍）仍算同一个字**、长休止断链 → 占位、轨首 → 占位、
  器乐轨全占位、连续没字的音只占一个音节、休止后的占位音节也能被续、
  `+` 归一成 `-`、统计计数，以及 **3 条真样本回归**
  （前提 57 个没字；修完 **1 个占位 / 91 个延音**且沿链条复算无一违规；人工版 0 占位 / 55 延音）。
- 全量：`pytest tests -q` **333 项**全过（6 跳过）。

### 涉及文件

`src/sheetsage2_webui/svp.py`（`_note_objects` / `CONTINUATION_MAX_GAP` / `EXPORT_REVISION`）、
`tests/test_svp_lyrics.py`（新）。
