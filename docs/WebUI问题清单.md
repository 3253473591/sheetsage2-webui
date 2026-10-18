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
| 8 | [双击两次 `webui.bat` 会起两个服务](#8-双击两次-webuibat-会起两个服务-) | ✅ 已修（单实例锁） | 启动 / 并发 |
| 9 | [删除按钮单击即删，误删不可恢复](#9-删除按钮单击即删误删不可恢复-) | ✅ 已修（确认弹层 + 回收站） | 数据安全 |
| 10 | [重跑之后下载到的还是上一版](#10-重跑之后下载到的还是上一版-) | ✅ 已修（产物版本戳） | WebUI / 缓存 |
| 11 | [半成品也会被标成完成](#11-半成品也会被标成完成-) | ✅ 已修（完成门禁） | WebUI / 状态 |
| 12 | [写盘不是原子的，中断后留半截文件](#12-写盘不是原子的中断后留半截文件-) | ✅ 已修（原子写 + `prev/`） | WebUI / 一致性 |
| 13 | [卡住就是永远转圈](#13-卡住就是永远转圈-) | ✅ 已修（超时 + 看门狗） | WebUI / 守护 |
| 14 | [报错直接吐异常名与英文原文](#14-报错直接吐异常名与英文原文-) | ✅ 已修（错误码表） | WebUI / 可理解 |
| 15 | [两个标签页互相覆盖（无乐观锁）](#15-两个标签页互相覆盖无乐观锁-) | ✅ 已修（`expected_rev` + 409） | WebUI / 一致性 |
| 16 | [并发上传同名文件撞进同一目录](#16-并发上传同名文件撞进同一目录-) | ✅ 已修（原子认领 + uuid 前缀） | WebUI / 并发 |
| 17 | [重复点「开始」重复入队](#17-重复点开始重复入队-) | ✅ 已修（去重 + 取消墓碑） | WebUI / 幂等 |
| 18 | [上传只查后缀不查内容](#18-上传只查后缀不查内容-) | ✅ 已修（ffmpeg 体检 + 静音 + 去重指纹） | WebUI / 入口 |
| 19 | [没有磁盘预检与产物回收](#19-没有磁盘预检与产物回收-) | ✅ 已修（阶段前预检 + 清理缓存） | WebUI / 数据安全 |
| 20 | [日志只在内存、崩溃后无现场](#20-日志只在内存崩溃后无现场-) | ✅ 已修（滚动日志 + 诊断包） | WebUI / 可诊断 |
| 21 | [无 CSRF/Origin 校验、非回环裸奔](#21-无-csrrorigin-校验非回环裸奔-) | ✅ 已修（Origin/Host 校验 + token） | WebUI / 安全 |
| 22 | [启动不做完整预检](#22-启动不做完整预检-) | ✅ 已修（`/api/env.preflight` 横幅） | WebUI / 入口 |
| 23 | [服务消失后页面静默冻结](#23-服务消失后页面静默冻结-) | ✅ 已修（存活探测 + 红色横幅） | WebUI / 守护 |
| 24 | [显存不足退 CPU 不告知](#24-显存不足退-cpu-不告知-) | ✅ 已修（`fallback_reason` 进日志与告警） | WebUI / 可理解 |

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

---

## 8. 双击两次 `webui.bat` 会起两个服务 ✅

**状态**：✅ 已修（单实例锁）

### 现象

第一次双击没反应（窗口被挡住 / 浏览器没弹），再双击一次 —— 看起来"一切正常"，
实际是**两个 uvicorn + 两个 `JobManager` 盯同一个 `output/`**：

1. 两个进程恢复出同一批工程（id 来自目录名 / `job.json`），两边各有一份内存状态；
2. A 窗口跑扒谱、B 窗口跑歌词 → 两套 writer 写同一个 `project.json`；
3. 两个进程各自加载模型 → 显存翻倍直接 OOM；
4. A 窗口删工程，删的可能是 B 窗口正在写的目录。

### 根因

`scripts/launch.py` 里没有任何互斥：`pick_port` 发现 8000 被占就**换个随机端口再起一个**
（这个行为本身是为了"端口被别的软件占了也能用"，但没区分"占端口的是另一个自己"）。

### 修法

- 新增 `webui/singleton.py`：数据根下两个文件 —— `.webui.lock`（`msvcrt.locking` /
  `flock` 独占一个字节的**互斥体**，跟着进程走，进程没了自动释放）与 `.webui.port.json`
  （**名片**：`{pid, port, url, started_at}`）。
  > 为什么分两个文件：Windows 上被字节锁住的文件，别的进程连打开读都可能被拒，
  所以"该打开哪个地址"必须写在另一个能读的文件里。
- `scripts/launch.py`：起服务前先探活；拿到锁说明没人跑（立刻释放，真正的锁由服务进程持有），
  拿不到就按名片**直接把浏览器打开**（"已经有一个在跑了 → http://127.0.0.1:8129"），退出码 0。
- `webui/__main__.py`：服务进程自己加锁并刷新端口；`--no-singleton`（或
  `SHEETSAGE_ALLOW_MULTI=1`）是逃生口。

### 验证

- `tests/test_singleton.py`（6 项）：第二个实例拿锁抛 `AlreadyRunning` 并带着地址、
  释放后能再拿到、残留锁文件不挡路、名片可被另一个实例读到、`pid_alive` 判定。
- **真机**：起服务后把 `webui.bat` 再跑一次 →

  ```
  已经有一个 SheetSage2 Studio 在跑了，这次不再启动第二个。
  正在跑的那个：http://127.0.0.1:8129（PID 315020）
  ```

  退出码 0，没有第二个服务。

### 涉及文件

`src/sheetsage2_webui/webui/singleton.py`（新）、`scripts/launch.py`、
`src/sheetsage2_webui/webui/__main__.py`、`tests/test_singleton.py`（新）。

---

## 9. 删除按钮单击即删，误删不可恢复 ✅

**状态**：✅ 已修（确认弹层 + 软删除回收站）

### 现象

「删除」就在「下载 ▾」旁边，单击即 `shutil.rmtree` 整个工程目录：几小时的扒谱 + 歌词 +
手改的参数一次清空，连 `.webui/input` 里的原音频一起没了。

### 修法

**两层，都做了**：

1. **前端**：`web/src/components/ConfirmDialog.vue` —— 文案带上"删几个产物 / 有没有原音频 /
   7 天内能恢复"，且**弹层出现后 1 秒内确认按钮是灰的**（防连点，规则在 `web/src/confirm.ts`，
   纯函数有单测）。侧栏 `×` 和工程头「删除」都走同一个确认。
2. **后端**：`JobManager.delete()` 改成**软删除** —— 目录 `rename` 进 `<数据根>/.trash/`
   （同卷原子），立刻从列表消失；新增 `GET /api/trash`、`POST /api/trash/{id}/restore`、
   `DELETE /api/trash/{id}`；回收站超过 7 天或超过 2GB 自动清理最旧的。
   `DELETE /api/jobs/{id}?permanent=true` 才是真删。
3. 前端导航栏新增「回收站」入口（`TrashDialog.vue`）：恢复 / 彻底删除（彻底删除同样要等 1 秒）。

### 验证

- `tests/test_webui.py::test_delete_goes_to_the_trash_and_can_be_restored`：
  删除 → 404 → 回收站有 1 项 → 恢复 → **同一个 job id 回来**、产物可下载、回收站清空。
- `test_delete_with_permanent_flag_skips_the_trash`：`?permanent=true` 不进回收站。
- `web/test/confirm.test.ts`（4 项）：计时进度、1 秒阈值、进度夹在 0~1、文案要素。
- **真机**（`curl` 打本地服务）：上传 → `DELETE /api/jobs/{id}` → `/api/trash` 有
  `probe-<id>-<时间戳>` → `POST /api/trash/.../restore` → 列表里回到 `probe`，状态 `draft`。

### 涉及文件

`src/sheetsage2_webui/webui/jobs.py`（`delete` / `trash` / `restore` /
`purge_from_trash` / `_purge_trash`）、`src/sheetsage2_webui/webui/app.py`（三个 `/api/trash` 路由）、
`web/src/components/ConfirmDialog.vue`（新）、`web/src/components/TrashDialog.vue`（新）、
`web/src/confirm.ts`（新）、`web/test/confirm.test.ts`（新）、`web/src/App.vue`、
`web/src/api.ts`、`web/src/composables/useStudio.ts`、`web/src/styles.css`。

---

## 10. 重跑之后下载到的还是上一版 ✅

**状态**：✅ 已修（产物版本戳 `artifacts_rev` + 缓存失效）

### 现象

参数选错 → 重跑 → 点「下载 SVP」→ 拿到的还是旧参数那一版。三处缓存都是"文件在就直接返回"，
**键里没有产物版本**：整包 zip（只有路径）、SVP（`parts + version + EXPORT_REVISION + language`）、
波形（`peaks-<kind>-<buckets>.json`，换干声不失效）。

### 根因

作废只发生在内存（`_invalidate_*` 换一个 `StageRun` 对象），**磁盘上的旧产物和缓存一个字节都没动**。

### 修法

1. `Job.artifacts_rev`：每完成一轮阶段 +1（扒谱完成 +1、歌词完成 +1），写进 `job.json`；
2. 整包 zip：`bundle.meta.json` 记下打包时的 `artifacts_rev`，版本不符就重打；
3. SVP：`svp.cache_key(..., rev=job.artifacts_rev)`；
4. 波形缓存里存**源文件指纹**（大小 + mtime），换了干声/原曲自然失效；
5. 下载响应带 `X-Artifact-Rev` 头（页面/脚本能看出这是第几版）；
6. 阶段开始时还会**清掉派生缓存**（`bundle*` / `export/*` / `peaks-*`）——它们一定是旧的。

### 验证

- `test_rerunning_rebuilds_the_bundle_and_bumps_the_artifact_rev`：第一版 zip 里
  `report.json` 是 `v1`、`X-Artifact-Rev: 1`；重跑后 zip 里变成 `v2`、`X-Artifact-Rev: 2`。
- `test_svp_cache_key_follows_the_artifact_rev`：`rev` 不同 → 键不同（同 rev 稳定）。
- `test_peaks_cache_is_invalidated_when_the_audio_changes`：同一路径换一段音频 → 指纹变、确实重算。

### 涉及文件

`src/sheetsage2_webui/webui/jobs.py`、`src/sheetsage2_webui/svp.py`、
`src/sheetsage2_webui/webui/media.py`、`src/sheetsage2_webui/webui/app.py`。

---

## 11. 半成品也会被标成完成 ✅

**状态**：✅ 已修（完成门禁）

### 现象

`pipeline.run` 里 `summary["artifacts_ok"] = True` 是**硬编码**的；真正的体检
`result.inspect_output_dir`（15 个文本 + 6 个 MIDI 的完整性与自洽性）**只有 CLI 调**。
WebUI 的 `done` 唯一依据是"runner 没抛异常"。模型中途吐半套产物（磁盘满、编码器异常、
杀毒软件锁文件）→ 界面显示 ✅ 完成，下载的 SVP 少一半音符。

### 修法

1. `runner.transcribe` 结尾跑 `inspect_output_dir`，结果进 `detail.artifacts`
   （`{ok, checked, total, missing, problems}`）；歌词阶段查 `project.json` + `lyrics.json` 可解析。
2. `JobManager._settle_phase` 的**完成门禁**：产物自检不通过、或缺
   `project.json` / `report.json` / `summary.json`、或 JSON 解析失败 → 阶段 `failed`
   （错误码 `E_ARTIFACTS_INCOMPLETE`），并把缺哪个文件用人话列出来。
3. `pipeline` 不再硬编码：写完产物后按 `result.EXPECTED_TEXT/EXPECTED_MIDI` 做便宜的自检，
   结果写进 `summary["artifacts_ok"]` 与 `artifacts_missing`。
4. 前端阶段卡显示「自检 21/21 通过」；不通过时标黄并列出缺哪几个。

### 验证

- `test_incomplete_artifacts_are_not_reported_as_done`：假 runner 只写 `project.json`
  → 任务 `failed`、`error_code == "E_ARTIFACTS_INCOMPLETE"`、文案里点名 `report.json`。
- `test_the_runners_own_artifact_report_blocks_completion`：`detail.artifacts.ok = False`
  同样拦住（并点名 `chords.mid`）。
- 既有的 60 项 WebUI 测试全部照旧（假 runner 的 `write_artifacts` 补上了 `summary.json`）。

### 涉及文件

`src/sheetsage2_webui/webui/runner.py`（`inspect_artifacts` / `_lyrics_artifacts`）、
`src/sheetsage2_webui/webui/jobs.py`（`_completion_gate`）、
`src/sheetsage2_webui/pipeline.py`、`web/src/components/StageCard.vue`。

---

## 12. 写盘不是原子的，中断后留半截文件 ✅

**状态**：✅ 已修（原子写 + 阶段前隔离旧产物）

### 现象

`project.json` / `report.json` / `summary.json` / `job.json` 都是 `write_text` 直接盖。
进程在写 JSON 时被杀（Ctrl+C / 任务管理器 / 断电）→ 文件是半截；
`/lanes`、`/exports`、`/download?kind=svp` 全靠 `project.json` → 用户看到 500 或"还没扒谱"。
更隐蔽的是**重跑失败在中途**：磁盘上混着"这一轮的新 project.json + 上一轮的旧 lyrics.json"，
下载包里会带上旧歌词。

### 修法

1. 新增 `src/sheetsage2_webui/fsutil.py`：`write_text_atomic` / `write_bytes_atomic`
   （同目录 `.part` → `os.fsync` → `os.replace`）。`pipeline`、`pitch`、`runner`、`jobs`
   的 JSON / lab / MIDI / ABC 全部改走它。
2. **阶段开始前**把这阶段上一轮的产物挪进 `<工程>/.webui/prev/<rev>-<时间>/`：
   `output/` 永远只表示"当前这一版"；跑成功之后清掉 `prev/`（`_flush_prev`）。
   重跑扒谱时旧 `lyrics.json` 也一起挪走（音符位置变了，旧歌词本来就要作废）。
3. 完成时写 `<工程>/.webui/manifest.json`（`artifacts_rev` + 文件清单 + 参数 + 阶段状态）；
   `/files` 与 `/download` 只认清单里的文件 —— 残留文件留在磁盘上是一回事，当成这一版发出去是另一回事。

### 验证

- `test_atomic_write_never_leaves_a_part_file`：写完没有残留 `.part`。
- `test_half_written_project_json_is_404_not_500`：把 `project.json` 弄成半截 →
  `/api/jobs/{id}/project` 回 **404**（"还在写"），不是 500。
- `test_failed_rerun_does_not_leave_the_old_lyrics_in_output`：重跑扒谱失败 →
  `output/` 里没有旧 `lyrics.json`，旧的那份在 `.webui/prev/` 里。

### 涉及文件

`src/sheetsage2_webui/fsutil.py`（新）、`src/sheetsage2_webui/pipeline.py`、
`src/sheetsage2_webui/pitch.py`、`src/sheetsage2_webui/webui/runner.py`、
`src/sheetsage2_webui/webui/jobs.py`、`src/sheetsage2_webui/webui/media.py`、
`src/sheetsage2_webui/webui/app.py`。

---

## 13. 卡住就是永远转圈 ✅

**状态**：✅ 已修（阶段超时 + 无进展看门狗）

### 现象

进度条停在"识别音乐 62%"半小时，刷新页面还是 62%。没有超时、没有心跳之外的存活证据；
取消是协作式的，而**模型加载 / CUDA 卡死时一个回调都不会来**，点「停止」没用。

### 修法

1. `Progress` 记录 `last_advance_at`（`step` / `set` / `log` 任一变化都刷新）；
2. `JobManager` 起一个 daemon 看门狗线程（每 `watchdog_interval` 扫一遍，默认 1s）：
   - **无进展** > `STALL_TIMEOUT`（扒谱 15min / 歌词 10min）→ 阶段 `failed`，
     错误码 `E_STALLED`，文案"卡住了 N 分钟没有任何进展"；
   - **总时长** > `HARD_TIMEOUT`（扒谱 2h / 歌词 1h）→ `E_TIMEOUT`；
   - 判死的同时置 `cancel_requested`，工作线程在下个检查点退出（不会两个线程抢同一个阶段），
     且 `watch.fired` 之后 runner 哪怕回来了也不会把状态改回 `done`。
3. `media.compute_peaks` 的 ffmpeg 加 `timeout=300s` + `CREATE_NEW_PROCESS_GROUP`：
   解码卡住不会再永久占一个线程池线程，Python 侧被杀也不会留下一个孤儿 ffmpeg。
4. 界面：阶段卡显示"上次进展 xx 秒前"，超过 60 秒标黄。

### 验证

- `test_a_stalled_stage_is_failed_by_the_watchdog`：假 runner `sleep(2)`、
  `stall_timeout=0.3` → 任务 `failed`、`error_code == "E_STALLED"`、文案含"卡住"、有下一步建议。
- `test_a_stage_that_exceeds_the_hard_timeout_is_failed`：一直报进度但跑不完 →
  `E_TIMEOUT`。
- 既有的取消测试照旧通过（协作式取消没被破坏）。

### 涉及文件

`src/sheetsage2_webui/webui/jobs.py`（`_PhaseWatch` / `_watch_stages` / `_abort_phase`）、
`src/sheetsage2_webui/webui/media.py`、`web/src/components/StageCard.vue`。

---

## 14. 报错直接吐异常名与英文原文 ✅

**状态**：✅ 已修（错误码表 + 人话 + 下一步）

### 现象

`message = f"{type(exc).__name__}: {exc}"` 原样进快照、SSE、`job.json`，页面上是
`TranscriptionError: 转写失败：RuntimeError: CUDA error: an illegal memory access was encountered`、
`OSError: [Errno 28] No space left on device`。用户看不懂，也没法据此行动。

### 修法

新增 `webui/errors.py`：`code` + `message`（人话）+ `action`（下一步）+ `detail`（技术原文）。

| code | 人话 | 下一步 |
|---|---|---|
| `E_DISK_FULL` | 磁盘满了，结果写不进去 | 清理 `output/` 或换个大点的盘，再重跑 |
| `E_VRAM_OOM` | 显卡内存不够，模型跑不动 | 精度改 BF16 / 范围改前 30 秒再试 |
| `E_AUDIO_UNREADABLE` | 这个音频读不出来 | 确认能正常播放，必要时转 mp3 |
| `E_ASR_EMPTY` | 没听到人声 | 换含人声的混音 / 上传干声 |
| `E_ARTIFACTS_INCOMPLETE` | 产物不完整，这一轮不能算完成 | 重跑；反复出现就发诊断信息 |
| `E_STALLED` / `E_TIMEOUT` | 卡住了 / 跑太久超过上限 | 强制中止后重跑，或先跑前 30 秒 |
| `E_INTERNAL-<8 位 hash>` | 程序内部出错了（带错误码） | 重试一次；带上错误码反馈 |

约定：**人话里不出现异常类名**；模型自己抛的中文错误（"没有拿到任何逐字时间戳"）
原样当人话用（那本来就是给用户看的）。未知异常统一兜底成稳定的
`E_INTERNAL-<hash>`（同一个 bug 同一个码，便于对日志）。

快照与 SSE 里现在同时有 `error`（人话）、`error_code`、`error_action`、`error_detail`；
前端阶段卡显示人话 + 「下一步：…」，技术原文折叠在「详情」里。

### 验证

- `test_errors_are_translated_into_human_text_with_a_code`：`OSError(28)` →
  `E_DISK_FULL`、人话含"磁盘满"、不含 `OSError`、有 `action`、`detail` 里有原文。
- `test_error_table_maps_common_failures`：磁盘满 / 显存 / 无人声 / 坏音频的码，
  以及未知异常的稳定 `E_INTERNAL-*`。

### 涉及文件

`src/sheetsage2_webui/webui/errors.py`（新）、`src/sheetsage2_webui/webui/jobs.py`、
`web/src/components/StageCard.vue`、`web/src/types.ts`。

---

## 15. 两个标签页互相覆盖（无乐观锁）✅

**状态**：✅ 已修（`set_options(expected_rev=)` + `StaleRevision` → 409 + 当前快照）

- **现象**：A 标签页改精度、B 标签页（停在旧快照）改语言 → A 的改动被**静默回滚**。
- **修法**：`PATCH /options` 接受 body 里的 `expected_rev`（或 `If-Match` 头），不匹配回
  **409 + `detail.snapshot`**；`rev` 语义改成"任何影响结果的变更都 +1"（改参数/换音源/重跑）；
  前端 `setJobOptions` 带上 `job.rev`，收到 `StaleOptionsError` 就用服务端给的新快照原地刷新。
- **验证**：`tests/test_webui_p1.py::test_optimistic_lock_rejects_a_stale_revision`、
  `test_rev_bumps_on_any_result_changing_edit`。
- **涉及文件**：`webui/jobs.py`、`webui/app.py`、`web/src/api.ts`、`web/src/composables/useStudio.ts`。

## 16. 并发上传同名文件撞进同一目录 ✅

**状态**：✅ 已修（原子认领 + 输入文件 uuid 前缀）

- **现象**：`unique_project_dir` 先 `candidate.exists()` 再 `mkdir(exist_ok=True)`，判断与创建之间
  有窗口 → 并发同名上传写进同一个目录。
- **修法**：新增 `claim_project_dir`（`mkdir(exist_ok=False)` + 重试 `-2/-3`，创建即互斥）；
  输入文件落盘名改成 `<job_id>-<原名>`（展示名仍存 `job.audio`），`_job_from_meta` 恢复时按
  "任意前缀 + 原名"找回。
- **验证**：`test_concurrent_creates_get_distinct_directories`（10 线程 → 10 个不同目录）、
  `test_claim_project_dir_is_atomic`。
- **涉及文件**：`webui/jobs.py`。

## 17. 重复点「开始」重复入队 ✅

**状态**：✅ 已修（queued/running 早退 + 取消墓碑 5s）

- **现象**：连点两次「开始扒谱」= 同一个 `job_id` 进队两次；第二次点击还会把
  `cancel_requested` 抹掉（取消被复活）。
- **修法**：`run_stage` 在锁内对 `stages[phase].state in ("queued","running")` 直接早退；
  `cancel` 记 `canceled_at`，之后 5 秒内的「开始」直接 409（"刚刚取消，稍等几秒再开始"）。
- **验证**：`test_run_stage_deduplicates_queued_and_running`（`phase_done` 恰一条）、
  `test_cancel_tombstone_blocks_immediate_restart`。
- **涉及文件**：`webui/jobs.py`。

## 18. 上传只查后缀不查内容 ✅

**状态**：✅ 已修（ffmpeg 体检 + 静音 RMS + 内容指纹去重）

- **现象**：把 Word 文档改名成 `.mp3` 也能传，几十秒后才炸；纯静音/伴奏版传进去
  白跑半小时没人知道。
- **修法**：`media.probe_audio`：`ffmpeg -i` 探容器/时长/音频流（读不出 → `E_AUDIO_UNREADABLE`），
  解码前 20s 算 RMS（纯静音 → `E_AUDIO_SILENT`）；`content_fingerprint`（大小 + 前 1MB sha1）
  命中已有工程时快照里给 `duplicate_of`。`create_app(probe_upload=True)` 是产品默认，
  测试默认关。
- **验证**：`test_probe_audio_rejects_a_renamed_document`、`test_probe_audio_accepts_a_real_tone`、
  `test_probe_upload_rejects_bad_content`、`test_content_fingerprint_changes_with_bytes`。
- **涉及文件**：`webui/media.py`、`webui/jobs.py`、`webui/errors.py`（`E_AUDIO_SILENT`）。

## 19. 没有磁盘预检与产物回收 ✅

**状态**：✅ 已修（阶段前预检 + `/api/env.disk` + `POST /cleanup`）

- **现象**：笔记本跑到第 5 首歌磁盘满 → 写一半 `OSError`，一句英文（叠加 #14）。
- **修法**：`_assert_disk` 在阶段开跑前按"音频 × 3 + 50MB"预检，不够就 `E_DISK_FULL`（不白跑）；
  `/api/env` 带 `disk.free/total/used`（顶栏"剩余 12.3 GB"，< 2GB 变黄）；
  `POST /api/jobs/{id}/cleanup` 清掉 zip/SVP/波形/prev 等可重算缓存并报告释放字节数。
- **验证**：`test_env_reports_disk_and_cleanup_frees_bytes`、`test_bytes_text_is_human_readable`。
- **涉及文件**：`webui/jobs.py`、`webui/app.py`、`web/src/App.vue`、`ProjectHead.vue`。

## 20. 日志只在内存、崩溃后无现场 ✅

**状态**：✅ 已修（滚动日志 + 诊断包 + 崩溃钩子）

- **现象**：进程被强杀/蓝屏/驱动重置后现场全丢，用户能提供的只有"它转圈了"。
- **修法**：`webui/logs.py`：`logging` + `RotatingFileHandler` 写 `<数据根>/.logs/studio.log`
  （10MB × 5），uvicorn access/error 接进去；`sys.excepthook` + `threading.excepthook`
  写 `crash-<时间>.txt`；`GET /api/diagnose`（或 `GET /api/jobs/{id}/diagnose`）打包
  `job.json` + `summary.json` + `env.json` + 日志下载成 zip。
- **验证**：`test_diagnose_bundle_contains_env_and_metadata`、`test_setup_logging_writes_a_log_file`。
- **涉及文件**：`webui/logs.py`（新）、`webui/jobs.py`（`diagnose_bundle`）、`webui/app.py`、`webui/__main__.py`。

## 21. 无 CSRF/Origin 校验、非回环裸奔 ✅

**状态**：✅ 已修（`loopback_guard` + `require_token`）

- **现象**：任意网页都能向 `127.0.0.1` 发简单 POST（取消/开跑/删工程）；DNS rebinding 能读
  `/api/jobs`；`--host 0.0.0.0` 时同 WiFi 下任何人可上传/删除/导出。
- **修法**：`webui/security.py` + `app.py` 中间件两个开关（默认关，`__main__.py` 打开）：
  `loopback_guard`（非 GET 校验 `Origin`/`Referer` 回环 + `/api/*` 校验 `Host`）；
  `require_token`（非回环监听强制，token 从 `X-Pair-Token` / Bearer / `?token=` / HttpOnly cookie 取，
  启动时打印 `/?token=...`）。
- **验证**：`test_loopback_guard_rejects_cross_origin_post`、`test_require_token_blocks_requests_without_the_pair_token`、
  `test_security_helpers`。
- **涉及文件**：`webui/security.py`（新）、`webui/app.py`、`webui/__main__.py`。

## 22. 启动不做完整预检 ✅

**状态**：✅ 已修（`/api/env.preflight` + 前端横幅）

- **现象**：缺 ffmpeg / 缺权重 / 没显卡都要到点「开始」才炸，浏览器里没有任何提示。
- **修法**：`webui/preflight.py`：ffmpeg / 数据目录可写 / 扒谱权重 / 歌词权重 / CUDA / 磁盘
  六项体检，结果进 `/api/env.preflight`；前端按最严重一项（error > warn > info）画横幅。
- **验证**：`test_preflight_returns_the_expected_checks`、`test_env_exposes_preflight`。
- **涉及文件**：`webui/preflight.py`（新）、`webui/app.py`、`web/src/App.vue`、`web/src/types.ts`。

## 23. 服务消失后页面静默冻结 ✅

**状态**：✅ 已修（`/api/healthz` 每 3s 探测 + 红色横幅）

- **现象**：任务在跑时用户关掉黑色窗口/电脑睡一觉 → 页面进度条还停在 62%，看起来一切正常，
  只是不动了，用户可能就这么等下去。
- **修法**：`useStudio` 起一个 3s 一次的存活探测（`GET /api/healthz`），连续 3 次失败置
  `serviceDown`；`App.vue` 顶部红色横幅"后台服务已停止（可能被关闭或崩溃了）"。
- **验证**：`npm run typecheck` + `npm run build` + `npm test`（34 项）兜住；后端无新增断言。
- **涉及文件**：`web/src/composables/useStudio.ts`、`web/src/App.vue`。

## 24. 显存不足退 CPU 不告知 ✅

**状态**：✅ 已修（`engine.records` 的 `fallback_reason` 进日志与告警）

- **现象**：`engine` 捕获 OOM → `_move_to_cpu()` → 重跑，这条 fallback 只写进 `engine.records`，
  界面毫无感知——用户以为在用 GPU，实际一首歌要跑半小时。
- **修法**：`runner._fallback_since(engine, before)` 只看这一趟的增量 records，命中就
  `progress.log("显存不够，已自动改用 CPU 重跑…")`，并把这条告警并进阶段 `warnings`
  （顺带进 `detail.fallback`）——`ResultsPanel` 的告警块会自动显示。
- **验证**：`test_fallback_since_reads_only_the_new_records`（只看增量、别翻旧账）。
- **涉及文件**：`webui/runner.py`。

## 25. 进度条会倒退（SSE 打回旧值）✅

**状态**：✅ 已修（`web/src/progress.ts`：只增不减 + 旧事件过滤 + 只有新一轮才归零）

- **现象**：重跑一个阶段后重连（带 `?since=`），后端先把这一趟的事件补发过来、又把上一趟
  残留的事件补发过来，`applyEvent` 直接 `run.progress = event.data.progress`，于是进度条
  从 12% 跳回 80%。用户会以为任务在倒着跑。
- **修法**：把规则提成纯函数并单测：
  1. `monotonicProgress(当前, 事件)` = `max`，并夹在 0~1；
  2. `isStaleEvent(event.time, run.started_at)`——比这一趟开始还早的事件不算数；
  3. `startsNewRun()`——只有时间不早于 `started_at` 的 `phase` 事件才能把进度归零；
  4. **不用**"改参数后把 `lastSeq` 归零"：那会重放整段历史，反而更容易把旧快照打到新状态上。
- **验证**：`web/test/progress.test.ts` 13 项（含"从 12% 跳回 80%"这条真实场景）。
- **涉及文件**：`web/src/progress.ts`（新）、`web/src/composables/useStudio.ts`。

## 26. 前端请求没有超时，服务卡住就永远转圈 ✅

**状态**：✅ 已修（`api.fetchWithTimeout`：普通 30s / 上传 10min / 存活探测 5s）

- **现象**：`api.ts` 全是裸 `fetch`，没有 `AbortController`/超时。模型卡死（#13）时后端不再
  回话，页面上的请求就**永远 pending**，用户只能刷新——而且刷完还是 pending。
- **修法**：所有非 SSE 请求走 `fetchWithTimeout()`（`AbortController` + `setTimeout`，
  超时真的掐断，不留 pending 的 Promise）；XHR 上传/导入设 `timeout = 10 分钟` 并处理
  `ontimeout`；超时文案是人话（"服务没响应（已等 30 秒），可能还在忙…"），
  连不上是另一句（"连不上后台服务…可能已经关闭或崩溃了"）；存活探测单独 5 秒超时。
- **验证**：`web/test/api.test.ts` 5 项（"永不 resolve、只响应 abort"的 fetch 造场景）。
- **涉及文件**：`web/src/api.ts`、`web/src/composables/useStudio.ts`。

## 27. 不知道一首歌要跑多久 ✅

**状态**：✅ 已修（`webui/eta.py` → 快照 `stages.*.eta` → 阶段卡「⏱ 预计 2~4 分钟」）

- **现象**：拖进一首 6 分钟的歌，用户不知道要等 3 分钟还是 40 分钟；进度条第一次长时间不动
  时，他没有参照物判断"该等"还是"卡死了"。
- **修法**：`eta.estimate(phase, audio_seconds, device, options)`
  - 口径：扒谱 `音频秒数 × 0.6（GPU）/ × 8（CPU）`，歌词 `× 0.5 / × 4`，再加模型加载开销
    （25s / 45s）；区间 = 低估 20% / 高估 30%；选了「前 30/60 秒」按截断后的时长算。
  - 时长来源按可信度排：上传体检 `audio_info` > 产物 `summary` > `highlights`；都没有就
    **不给预估**（`eta: null`，界面不显示）。
  - 设备口径：「设备」参数优先；`auto` 用启动探测结果（`preflight.cuda_available()`）；
    都不知道按显卡估（低估比高估不吓人）。
  - 界面：没跑时「预计 2~4 分钟」，跑起来「预计还需约 3 分钟」，估快了就说"预计快好了"。
- **验证**：`tests/test_webui_p2.py` 6 项（GPU/CPU 系数不能写反、范围截断、缺时长不给、
  快照字段、设备优先级、跑完就撤）。
- **涉及文件**：`webui/eta.py`（新）、`webui/jobs.py`（`stage_eta` / `eta_for` / `set_device_hint`）、
  `webui/app.py`、`webui/__main__.py`、`webui/preflight.py`（`cuda_available` 转公开）、
  `web/src/components/StageCard.vue`、`web/src/format.ts`。

## 28. 换电脑 / 重装系统没有备份引导 ✅

**状态**：✅ 已修（顶栏「备份」「导入备份」+ `GET /api/backup` + `POST /api/backup/restore`）

- **现象**：`SHEETSAGE_WEBUI_DATA` 能改，但小白不会用；换电脑就是"把整个 `output/` 拷过去"，
  而 `job.json` 里存的是绝对路径（`_job_from_meta` 有兜底重建，但没人指望用户知道）。
- **修法**：`webui/backup.py` + 两个接口：
  - **导出**：`backup.json`（版本/时间/工程清单/参数）+ 产物 + `job.json` + `.webui/input` 原音频
    + `manifest.json`；**默认不带**可重算的缓存（`bundle.zip` / `peaks-*` / `export/*.svp` / `prev/`），
    `?include_cache=true` 才带。先写 `.part` 再改名；临时的包随下载结束清掉（`BackgroundTask`）。
  - **导入**：解到临时目录 → 逐条目校验 → 每个工程走和重启恢复相同的 `_restore`（原音频按布局
    重找）。重名**绝不覆盖**（`-2`），同一个包导两次会换 id，删一个不连坐另一个。
  - **安全**：拒绝绝对路径 / `..` / 盘符 / 符号链接条目（zip-slip）、条目数与解压总量有上限
    （zip 炸弹）、必须有 `backup.json` 且版本认得出，否则 400 + 人话。
- **验证**：`tests/test_webui_p2.py` 8 项（清单与内容、缓存默认不带、换数据根后产物/原音频/参数
  都在、导两次不覆盖、非本程序包 400、zip-slip 整包拒绝、版本不认拒绝）。
- **涉及文件**：`webui/backup.py`（新）、`webui/jobs.py`、`webui/app.py`、`web/src/api.ts`、
  `web/src/App.vue`、`web/src/types.ts`、`web/src/composables/useStudio.ts`。

## 29. 断点续跑（G21）⏸ 未做，单独排期

**状态**：⏸ 评估过、有依据地推后（不是漏了）

- **为什么**：SheetSage2 的窗口循环在**上游 remote code** 里——`engine.transcribe()` 只是把
  `progress` 回调透传给 `self._model.transcribe()`（`engine.py:253-256`），我们只收到
  `window_complete` 这种**通知**，拿不到窗口级中间结果，也就没法"跳过已完成的窗口"。
- **能做的那半**：歌词阶段的切片循环确实在 `asr.transcribe_sections`（我们的代码）里，
  落 `.webui/ckpt/<rev>/s<idx>.json` 可行；但它需要一整套"缓存 vs 参数/音源/切片边界"的
  一致性校验，错了就是**给错结果**（比慢严重得多）。
- **下一步**：单独一期，先做歌词切片续跑 + 一致性校验，扒谱等上游给窗口级接口。
- **现状兜底**：重启后阶段标"上次运行被中断，可以重跑"；卡死有看门狗（#13）；
  重跑不会混旧产物（#12 的 `.webui/prev/`）。

## 30. 验证脚本自己会崩、还会误判（工具链）✅

**状态**：✅ 已修（`scripts/verify_webui.py`）

- **现象 1（崩）**：默认 SVP 下载失败时（人声那组没有音符 → 后端 400），脚本紧接着
  `json.loads(400 的 body)` 再取 `svp_json["time"]["tempo"]`，直接抛
  `KeyError: 'time'`，把后面 8/8b/8c/9 全部验证跳过，只留下一条与真实原因无关的报错。
- **现象 2（误判）**：`max(tempo.position) > 1_000_000` 这条断言本意是"位置单位是 blick
  不是我们的 tick"，但**恒速曲的 tempo 只有一条、位置正好是 0**，于是稳定 FAIL。
- **修法**：①解析前先判状态码，解析失败就当空结构，后续断言各自报 FAIL 而不是整脚本崩；
  ②单位判定改成"所有 tempo 位置 + 音符 onset 都是 705600000/480 = 1_470_000 的整数倍
  且最大位置 ≥ 1_470_000"（实测：`onset` 36_698_550_000、`tempo.position` 0）。
- **验证**：`verify_webui.py` 由 `FAILED：1 项` 变成**全部通过**（45s 切片，真跑两个模型）。
- **涉及文件**：`scripts/verify_webui.py`。

---

## 附：第一期（P0）整体验证

- `pytest tests -q`：**354 项通过 / 6 跳过**（原来是 334 / 6；新增 20 项 = 14 项健壮性 + 6 项单实例锁）。
- `cd web && npm run typecheck && npm test`：**34 项通过**（新增 `confirm.test.ts` 4 项）。
- `cd web && npm run build`：通过，前端产物已更新。
- 真机：`webui.bat` 双击两次只起一个服务；上传 → 删除 → 回收站 → 恢复 全链路走通。

## 附：第二期（P1）整体验证

- `pytest tests -q`：**374 项通过 / 6 跳过**（P0 后新增 `tests/test_webui_p1.py` 20 项）。
- `cd web && npm run typecheck && npm test`：typecheck 通过，**34 项通过**。
- `cd web && npm run build`：通过，前端产物已更新（含预检/存活横幅、磁盘显示、诊断包入口）。

## 附：第三期（P2）整体验证

- `pytest tests -q`：**388 项通过 / 6 跳过**（P1 后新增 `tests/test_webui_p2.py` 14 项：
  G20 耗时预估 6 项、G22 备份/迁移 8 项）。
- `cd web && npm test`：**52 项通过**（新增 `progress.test.ts` 13 项 = G18、
  `api.test.ts` 5 项 = G19）。
- `cd web && npm run typecheck`：通过。
- `cd web && npm run build`：通过，前端产物已更新（阶段卡的「⏱ 预计…」、顶栏「备份」「导入备份」）。
- **真机端到端**：`scripts\verify_webui.py`（45s 切片、真跑 SheetSage2 + Qwen3-ASR）
  由「FAILED 1 项」变成**全部通过**（顺带修了脚本自己的两处问题，见 §30）。
- **真机 UI 冒烟**：`cd web; node smoke.mjs http://127.0.0.1:8123`（对着上面那台真服务）
  → 「组件树在真实数据上渲染正常，控制台无 Vue 报错」，页面文字里能看到
  `诊断包 · 备份 · 导入备份 · 回收站`。
- **真机备份/迁移**：对三个真工程（14.8 MB）导出 → 99 个条目、含原音频、**不含**
  `bundle.zip`/`peaks-*`；再导回同一个数据根 → 201、三个工程各落成 `-2`，原有工程没被覆盖。

> 还剩一件事没做：**G21 断点续跑**（理由与下一步见 §29）。
> 其余 P2（前端超时 / SSE 单调进度 / 耗时预估 / 数据迁移引导）已全部落地。
