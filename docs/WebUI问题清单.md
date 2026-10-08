# WebUI 问题清单

> **这份文档专门装 WebUI 的问题与改进**：发现一个记一个，修完标 ✅ 并写清根因、证据、
> 改了哪些文件。**不要再往 [`交接-下一次对话.md`](交接-下一次对话.md) 里加条目**——
> 那是"入口与现状"，保持短；这里才是流水账。
>
> 规格在 [`WebUI规格.md`](WebUI规格.md)，各功能的算法细节在各自的规格文档里
> （[`SVP导出调研.md`](SVP导出调研.md) / [`音符量化规格.md`](音符量化规格.md) / [`BPM算法规格.md`](BPM算法规格.md)）。

**状态**：🔴 待处理 · 🟡 已定位待复核 · ⛔ 不做（写清理由） · ✅ 已修

| # | 问题 | 状态 | 领域 |
|---|---|---|---|
| 1 | [扒谱音高整体高一个八度](#1-扒谱音高整体高一个八度-) | ✅ 已修（默认降一个八度） | 扒谱 / 音高 |
| 2 | [SVP 导出把拍号写满整个时间轴](#2-svp-导出把拍号写满整个时间轴-) | ✅ 已修 | 导出 SVP |
| 3 | [SVP 导出把速度写满整个时间轴](#3-svp-导出把速度写满整个时间轴-) | ✅ 已修 | 导出 SVP |
| 4 | [左侧工程列表重启就空](#4-左侧工程列表重启就空-) | ✅ 已修（以 `output/` 为准） | WebUI / 工程列表 |
| 5 | [SVP 下载 500：`NotesOverlappedError`](#5-svp-下载-500notesoverlappederror-) | ✅ 已修（端点换算 + 同轨去叠音） | 导出 SVP |

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
