# SVP 导出调研（带变速 + 歌词）

> 目标：**点击下载默认导出 `.svp`**（Synthesizer V Studio 工程），**把 ZIP 降级成下拉里的可选项**；
> 导出的 SVP 必须带上**变速**（我们的 `tempo_map_exact`）和**歌词**（挂在音符上的字）。
>
> 本文只做选型调研：**能不能用现成库，而不是自己写 `.svp` 序列化**。结论是可以。

---

## 0. 结论（先看这个）

**用 [LibreSVIP](https://github.com/SoulMelody/LibreSVIP)（`pip install libresvip`，MIT，纯 Python）。**

它就是一个 Python 包，`libresvip/plugins/svp/` 是一个**读写 `.svp` 的完整插件**；
我们要做的只是把 `project.json` 填进它的通用模型 `libresvip.model.base.Project`，
然后调 `SynthVStudioConverter.dump()`。**不需要自己动 `.svp` 的 JSON 结构。**

| 项目 | 语言/形态 | 许可证 | 能写 `.svp` | 对我们（Python 后端）的可用性 |
|---|---|---|---|---|
| **LibreSVIP** | **Python 包（PyPI `libresvip`）** | **MIT** | ✅ `plugins/svp/` | ⭐ **直接 `import`，同进程调用** |
| UtaFormatix3 | **Kotlin/JS + React（浏览器应用）** | Apache-2.0 | ✅ | 要 JS 运行时（Gradle/浏览器），后端不是 Python |
| utaformatix-ts | **TypeScript**（UtaFormatix3 的 TS 封装） | 见仓库 | ✅（走 UtaFormatix3） | 要 Node 子进程，多一层 |
| （第四个？） | —— | —— | —— | **你没给第四个链接**，见 §5 |

---

## 1. LibreSVIP —— 推荐方案

来源：[仓库 README](https://github.com/SoulMelody/LibreSVIP)、[PyPI 元数据](https://pypi.org/pypi/libresvip/json)、
以及仓库里 `libresvip/` 的实际代码（下面引用的都是 `main` 分支的原文）。

### 1.1 形态与许可（都合适）

- PyPI 包名 `libresvip`，**最新 2.9.0**，`summary = "Universal Converter for Singing Voice Projects"`。
- **MIT**；`requires_python = ">=3.10,<3.15"`，classifier 列到 **Python 3.14**
  → 本项目的 `.venv`（**Python 3.12.11**）在范围内。
- 纯 Python（wheel 是 `py3-none-any`）；依赖里没有 torch/numpy 这种重家伙
  （pydantic 2 / construct-typing / cbor2 / pypinyin / wanakana-python / jyutping / ko-pron …）。
- 可选 extras：`cli`（typer）、`desktop`（PySide6 GUI）、`webui`、`tui`、`mobile`、`lxml`、`ujson`。
  **我们只需要基础包**，GUI/CLI 都不要。

### 1.2 格式插件是一目录一个格式

`libresvip/plugins/` 下（GitHub API 列目录）有 40 来个：`svp`、`svip`、`svip3`、`ufdata`、`mid`、
`musicxml`、`ust`、`ustx`、`vsq`、`vsqx`、`vpr`、`ccs`、`dv`、`s5p`、`vspx`、`vshp`、`vog`、
`tlp`、`tlpx`、`tssln`、`tsmsln`、`nn`、`y77`、`xvsq`、`vvproj`、`vxf`、`ass`、`lrc`、`srt`、
`svg`、`json`、`dspx`、`ace`、`acep`、`aisp`、`ps_project`、`mtp`…

**和 UtaFormatix3 的关系**：LibreSVIP 的 README 自己写明"highly inspired by
[OpenSVIP](https://github.com/yqzhishen/opensvip) 和 [Utaformatix3](https://github.com/sdercolin/utaformatix3)"。

### 1.3 `.svp` 插件的真实接口（这是关键证据）

`libresvip/plugins/svp/synthv_studio_converter.py` 里就是一个标准转换器，`alias = "svp"`：

```python
class SynthVStudioConverter(plugin_base.SVSConverter):
    input_option_cls = InputOptions
    output_option_cls = OutputOptions
    _alias_ = "svp"
    _version_ = "1.11.2"

    @classmethod
    def load(cls, path, options) -> Project: ...          # .svp -> 通用模型
    @classmethod
    def dump(cls, path, project: Project, options) -> None: ...   # 通用模型 -> .svp
```

`dump()` 内部走 `SynthVGenerator.generate_project(project)`，最后
`json.dumps(..., separators=(",", ":"))` 写盘；**1.x 兼容模式会在末尾补一个 `\x00`**
（`version_compatibility != ABOVE_2_0_0` 时）——这种细节自己写很容易踩，用库就免了。

同目录还有 `synthv_parser.py`（读）、`model.py`（24KB 的 SVP pydantic 模型）、
`options.py`（导入/导出选项）、`phoneme_dictionary.json`（144KB 音素表）等。

### 1.4 要填的通用模型（`libresvip/model/base.py`）

```python
class SongTempo(BaseModel):        # position(ticks), bpm
class TimeSignature(BaseModel):    # bar_index, numerator, denominator
class Note(BaseModel):             # start_pos, length, key_number, lyric, pronunciation, ...
class SingingTrack(TrackMixin):    # title, ai_singer_name, note_list, edited_params
class Project(BaseModel):
    version: str
    song_tempo_list: list[SongTempo]
    time_signature_list: list[TimeSignature]
    track_list: list[Track]
```

**和我们的 `project.json` 几乎一一对应**：

| 我们的产物 | LibreSVIP 的 `Project` |
|---|---|
| `timeline.tempo_map_exact`（逐小节 BPM） | `song_tempo_list`（position + bpm）→ **变速** |
| `timeline.measures[]`（分子/分母、弱起） | `time_signature_list`（bar_index/numerator/denominator） |
| `tracks[id=vocal].notes[]`（`tick`/`dur`/`pitch`/`lyric`/`raw_tick`） | `Note(start_pos/length/key_number/lyric)` → **歌词** |
| `project.duration_seconds` / 调性 / 段落 / 和弦 | SVP 不直接表达（可丢，或写进 track title/备注） |

### 1.5 两个必须注意的换算/选项

1. **tick 分辨率不一样**：LibreSVIP 的基准是 `libresvip/core/constants.py::TICKS_IN_BEAT = 480`，
   我们的是 `timeline.PPQ = 1920` → **要 ÷4**（我们的 32 分网格 240 tick → 60 tick，正好整除，没有精度损失）。
2. **`OutputOptions` 要显式给**（默认值不一定是我们想要的）：

   | 选项 | 默认 | 我们该选 |
   |---|---|---|
   | `version_compatibility` | `BELOW_1_9_0` | 按用户 SV 版本定（`BETWEEN_1_10_0_AND_1_11_2` / `ABOVE_2_0_0`） |
   | `vibrato` | `NONE` | 保持 `NONE`（"所有音符颤音深度 0，保证输出音高曲线 = 输入"，我们本来就没有颤音数据） |
   | `down_sample` | `20` | 保持默认（我们没有参数曲线，这一项无所谓） |
   | **`language_override`** | **`MANDARIN`** | 中文歌 `mandarin`、**日语歌 `japanese`**（`synthv_language_presets` 里日语的 phoneset 就是 **`romaji`**！） |

   > 最后一行和上一轮的罗马音话题接上了：SynthV 里日语声库的 **phone set 本来就是 romaji**，
   > 所以日语歌词（假名/汉字）进 SVP 后由 SV 自己转音素，我们**不必**在导出前转罗马音。

3. SVP 是**歌手工程**，`SingingTrack.ai_singer_name` 要有个值（可以是空/占位），
   具体用什么声库由用户在 SV 里选；我们只负责给音符和歌词。

### 1.6 集成方式（三选一）

| 方式 | 说明 | 建议 |
|---|---|---|
| **同进程 import** | `from libresvip.plugins.svp.synthv_studio_converter import SynthVStudioConverter`，构造 `Project` 后 `dump()` | ⭐ 首选（无子进程、无网络） |
| CLI | 仓库有 `libresvip/cli/`（`cli` extra 装 typer） | 备选：把我们的工程先写成它认的中间格式再转 |
| 中间格式 `ufdata` | LibreSVIP **也有 `ufdata` 插件**（[UtaFormatix Data](https://github.com/sdercolin/utaformatix-data) 公开格式） | 不想构造 Python 对象时可用（多一层，没必要） |

**代价**：`pip install libresvip` 会拉一批依赖（pydantic 2 / pypinyin / wanakana-python / cbor2 …），
都要写进 `pyproject.toml` 的可选依赖（照现有 `web = [...]` 的写法加一个 `svp = [...]`），
否则换台机器就没了。

---

## 2. UtaFormatix3

来源：[仓库 README](https://github.com/sdercolin/utaformatix3)。

- **形态**：`3.x` 用 **Kotlin for JavaScript + React** 写的——**浏览器应用**（`./gradlew jsBrowserDevelopmentRun`）。
  不是 Python 库，也不是 Node 库。
- **许可**：Apache-2.0。
- **能力（很强）**：
  - 导出格式里有 **`.svp`**（还有 `.vsqx(4)`/`.vpr`/`.vsq`/`.mid`/`.ust`/`.ustx`/`.ccs`/`.musicxml`/`.s5p`/`.dv`/`.ufdata`…）；
  - **"Keep information including: tracks, notes, tempo labels, time signatures"** → 变速保得住；
  - 日语歌词：**CV↔VCV、假名↔罗马音**互转（这正好是上一轮那个问题的现成答案）；
  - SVP 音高/颤音**导入导出都支持**（表格里 SVP 三个勾都有）；
  - 还有一个专门的"**Project splitting with a max track count in each project for SVP export**"。
- **为什么不用它做后端**：要在我们的 Python 服务里用它，就得跑 JS（Gradle 构建产物 / 浏览器 / Node），
  比 `pip install libresvip` 重得多。它更适合当"人工转换的工具"或前端里跑。

## 3. utaformatix-ts

来源：[仓库首页](https://github.com/sevenc-nanashi/utaformatix-ts)。

- 一句话定位（仓库描述）：**"Unofficial UtaFormatix3 wrapper for TypeScript"** —— 给 TS/JS 调用者的封装，
  底层还是 UtaFormatix3。（作者 `sevenc-nanashi` 同时是 UtaFormatix3 的 contributor。）
- **对我们的意义**：如果要走 JS 生态，它是比"自己拼 UtaFormatix3 的 JS 构建"更省事的一层；
  但我们后端是 Python，用它就得**额外起 Node 子进程**——除非以后前端要"浏览器里直接转格式"。
- 结论：**不作为首选**，留作备选（比如以后想做纯前端导出）。

## 4. 落地到本项目的方案（待你确认后实施）

1. **依赖**：`pyproject.toml` 加可选依赖 `svp = ["libresvip>=2.9"]`（不进基础依赖，避免拖累 CLI/验证路径）。
2. **新模块** `src/sheetsage2_webui/svp.py`：
   `project.json` → `libresvip.model.base.Project` → `SynthVStudioConverter.dump()`。
   - tick ÷4；`tempo_map_exact` → `song_tempo_list`；`measures` → `time_signature_list`；
     人声轨音符（`tick`/`dur`/`pitch`/`lyric`）→ `Note`；
   - `language_override` 跟随工程的**歌词识别语言**（中文→mandarin、日语→japanese）；
   - 转音标记 `-` / `+` 的处理要在实现时定（SV 的重音/连续音记法）。
3. **产物与下载**：工程产物里多一个 `<歌名>.svp`；
   - `GET /api/jobs/{id}/download` **默认回 `svp`**；
   - 新增/复用 `?kind=zip` 走原来的整包 zip，**前端下拉**：`SVP（默认）` / `ZIP（全部产物）`；
   - 前端 `ProjectHead.vue` 的「下载 ▾」加这两项。
4. **测试**：`tests/test_svp.py`（纯函数：tick 换算 / tempo_list / note_list / 选项），
   端到端在 `verify_webui.py` 里断言 SVP 能生成、能被 LibreSVIP 自己 `load()` 回来（自洽性）。

## 5. 落地记录（已实现）

`pip install libresvip` 真装过、跑了往返、接了 API 与弹窗。**下面这些数字都是本机实测的**。

### 5.1 环境：libresvip 装在**项目自己的独立环境**里

> 这一节是后来改的：一开始我把 libresvip 装进了当时那个共享 Anaconda base 的 `.venv`，
> 它把 **pydantic 2.10.4 顶到 2.13.5**、还带进 `pyyaml-ft` / `ruamel-yaml 0.19` / `packaging 26`，
> base 里的 `conda`（要 ruamel-yaml<0.19）和 `streamlit`（要 packaging<26）会抱怨。
> 现在**整个项目换成独立环境**（Python 3.12 + `.venv`），libresvip 就装在这个环境里，
> 与 base 完全隔离，那些冲突警告自然消失。

`pip install -e .[svp]` 会带进 **35 个包**（pydantic / cbor2 / pypinyin / wanakana-python /
xsdata / tatsu-lts / portion / ko-pron / jyutping …），这在**项目自己的环境**里是正常代价；
`svp.py` 仍然**延迟 import**，没装 `[svp]` 时其余功能照常，只有导出回 501 + 安装提示。

环境本身见 `docs/开发指南.md` §1：**Python 3.12.11**（torch 的本地轮子是 `cp312`）+ torch 2.10.0+cu130。
装完的验证：`pytest tests -q` **234 项全过**、真机端到端 **103 项判据全过**（模型在 GPU 上跑通）。

### 5.2 时间轴：两个坑都踩过了

1. **tick 分辨率**：我们 `PPQ=1920`，LibreSVIP 基准 `TICKS_IN_BEAT=480` → 全部 **÷4**。
   我们的 32 分网格 240 tick → 60，整除。
2. **`time_signature_list[0]` 是"前置小节（bar 0）"**：`synthv_generator` 里
   `first_bar_tick = time_signature_list[0].bar_length()`，之后拍号整体 −1 小节、速度整体 −`first_bar_tick`。
   **我们的第一小节通常就是弱起**，正好当这个 bar 0；它自己的那个 tempo 会被丢掉
   （可接受：一小节太短，估出来的 BPM 本来就不稳）。

**实测《光芒》（86 小节 / 324 人声音符）**：

| 项 | 结果 |
|---|---|
| 音符数 | 324 → 324 |
| 音符 tick | **324 个全部零漂移**（读回来逐一相等） |
| 变速 | 86 个 tempo 点 → 文件里 **1 个**（恒速曲只写一条；见下） |
| 拍号 | 前置小节 1/8 被吸收，文件里从第一个完整小节（4/4）开始，且**只写变化点** |

> **拍号只写变化点（`EXPORT_REVISION = 2`）**：`time.meter` 的语义是"从这里开始用这个拍号"，
> 一直生效到下一条，所以整首同拍号的曲子只需要一条。以前是把 `timeline.measures` 逐小节抄进去，
> 结果 SynthV 在**每个小节上面都标一次 4/4**（实测《光芒》45s 版：21 条 → 文件里 20 条一模一样的
> 4/4）。现在只写变化点，`_meter_list()` 里做连续相同拍号的折叠；中途真转拍号（3/4、6/8）照样保留。
> 实测同一个工程重导：拍号 20 条 → **1 条**，**速度轴 / 音符（起点·时值·音高·歌词）/ 轨名逐项相等**。

> **速度同理（`EXPORT_REVISION = 3`）**：`time.tempo` 也是"从这里开始生效"。恒速曲逐小节抄
> `tempo_map_exact` 的结果是**同一个速度加一点网格抖动**（《光芒》86 小节里只有 103.896 / 104.348
> 两个值来回跳，外加弱起与末小节的碎片值 111.1 / 244.1），于是 SynthV 在每个小节上都标一次速度。
> 现在 `_tempo_list()` 的规则是：
>
> - **恒速曲**（`timeline.tempo_map` 只有一段）→ **一条**，值就是界面上报的那个 BPM；
> - **变速曲** → 保留逐小节的精确表（那里每个点都是真的变化），只把相差 ≤0.5% 的相邻点折叠掉。
>
> 实测（同一个工程，改前 → 改后）：《光芒》85 → **1** 条、《巴拉莱卡》111 → 10 条、
> 《皮囊》180 → 109 条；三种情况下**音符（起点·时值·音高）逐项不变**。
> **代价**：恒速曲用一条速度铺满整首，播完与原曲的时长差 0.4%~1.8%（三首恒速曲实测
> +1.09 / +1.28 / −3.34 秒）。逐小节的精确表仍在 `project.json` 里，需要严格对齐原音频时用它。

另外记一条：`dump()` 与 `load()` **不是严格互逆**（dump 按弱起长度前移速度轴，load 复原时加回第一个完整小节），
所以**契约按"SV 读到的文件内容"来定**，`tests/test_svp.py` 就是这么断言的。

> **tick 换算的两个坑（`EXPORT_REVISION = 4`）**：
>
> 1. **起点和终点各自换算，不能"起点 + 分开 round 的时长"**。我们 PPQ=1920、SV 480，
>    未量化（或该轨量化失败）时 tick 不是 4 的倍数，两次独立 `round()` 会让**本来正好头尾相接**
>    的两个音在 SV 域里叠 1 tick，libresvip 的 `PitchSimulator` 直接抛
>    `NotesOverlappedError: Notes overlapped near bar N` → 下载 **500**。
>    真事故《旷古回响》（量化关掉）bar 76：我们 tick `576478+478` / `576956`，
>    旧写法算出 `144120+120 = 144240`，而下一个音起点是 `144239`。
>    现在 `to_sv_window()` 把 `(tick, tick+dur)` 两个端点分别换算。
> 2. **SVP 一轨是单声部的**，所以 `_note_rows()` 再兜一层：起点被上一个音的终点顶到后面就往后推、
>    终点越过下一个音的起点就截短、挤到没有长度就丢掉（**宁可少一个音也不能叠**）。
>    动过几个音会进 `stats["adjust"]` 与 `stats["warnings"]`，下载时写进工程日志。

### 5.3 三组内容怎么进（下载弹窗勾选）

| 勾选项 | 来源 | 轨数 | 歌词 |
|---|---|---|---|
| 人声轨（**默认勾**） | `project.json` 的 `tracks[vocal]` | 1 | 填词阶段挂上的 `lyric` |
| 乐器轨（默认不勾） | `project.json` 的 `tracks[instrumental]` | 1 | 没有歌词 → 默认音节（中文 `啦` / 日语 `ラ` / 其它 `la`） |
| 和弦轨（默认不勾） | `chords.mid` | **4**（实测） | 同上 |

- **SVP 一轨是单声部的**，而 `chords.mid` 里每个和弦是 **4 个同时发声**的音
  （实测 125 个和弦 × 4 = 500 个音）→ 必须**按同时发声拆轨**：
  按起始时间分组、组内按音高从低到高、第 i 个音进第 i 轨（**低音永远在第 1 轨**），
  同轨里前一个音还没结束就截到下一个音的起点。这就是"一轨只能有一个音"的落地。
- 乐器轨/和弦轨在 SVP 里只能是 **SingingTrack**（SV 只有"唱歌轨"和"音频轨"两种，
  音频轨挂的是 wav 不是音符），所以它们会用默认音节被唱出来——当参考旋律用。
- `language_override` 跟着工程的识别语言走（`job.options.language`，选"自动"就退回
  `lyrics.json` 的 `language_used`）。顺带：SynthV 日语的 phoneset 本来就是 **romaji**。
- 转音标记：`+`（英文后续音节）统一写成 `-`（SV 里延续就是 `-`）。
- 版本：`version_compatibility = BELOW_1_9_0`（最兼容；1.x 会在文件尾补 `\x00`）。

### 5.4 接口与界面

- `GET /api/jobs/{id}/exports` —— 弹窗的数据源：哪几组能导、各多少音、占几轨、默认勾哪个。
- `GET /api/jobs/{id}/download?kind=svp&parts=vocal,instrumental,chords` —— 导 SVP；
  不传 `parts` 就是默认（只有人声）。同一组勾选**缓存**在 `<job>/export/<hash>.svp`，重复点不重算。
- `GET /api/jobs/{id}/download`（不带 `kind`）**仍然是整包 zip**——接口层保持原语义，
  是**界面**把 SVP 放成了主按钮。
- 界面：`ProjectHead.vue` 的「下载 SVP」主按钮 → 弹窗（`ExportDialog.vue`，三个勾选框，
  默认只勾人声）→ 确定即下载；旁边的小箭头 ▾ 才展开原来的产物菜单（zip + 单个文件）。

### 5.5 怎么验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q            # 234 项（含 tests/test_svp.py 13 项）
.\.venv\Scripts\python.exe scripts\verify_webui.py       # 真机端到端，含 7b「导出 SVP」
```

`tests/test_svp.py` 既有纯函数（tick 换算、和弦拆轨、可选项），也有**写出来再读回来**的往返断言；
`verify_webui.py` 的 7b 段会把 `.svp` 下下来直接解析 JSON，查音符/歌词/速度轴/拍号与 blick 单位。

### 5.6 还没做的（要继续时看这里）

- **弱起与音频的绝对对齐**：导出的 SVP 时间轴从第一个完整小节开始（弱起那段不在轴上），
  纯 SVP 工程无影响；要严格对着原音频对齐得另想办法（比如整体后移一小节）。
- **参数曲线**（音高曲线 / 颤音 / 力度）没有导出：`vibrato=NONE`，只给音符与歌词。
- **段落**（前奏/主歌/副歌…）没写进 SVP（SV 没有段落轨；可以塞进轨名或备注）。
- `utaformatix3` / `utaformatix-ts` 仍是备选，没有采用。

## 6. 待确认（开工前）

1. **"四个项目"只给了三个链接**——第四个是哪个？我猜可能是下面之一，你点一下我就补调研：
   - [`sdercolin/utaformatix-data`](https://github.com/sdercolin/utaformatix-data)——UtaFormatix 的公开内部格式 `.ufdata`（LibreSVIP 也有它的插件）；
   - [`yqzhishen/opensvip`](https://github.com/yqzhishen/opensvip)——C# 的 OpenSVIP（LibreSVIP 的另一半灵感来源）；
   - 或者就是 **UtaFormatix 的 Web 应用**（sdercolin/utaformatix-web）。
2. ~~SVP 目标版本~~ → **已定：1.9 及以下（最兼容）**。
3. ~~导出哪些轨~~ → **已定：人声 / 乐器 / 和弦都能进，下载弹窗勾选，默认只勾人声**。
4. ~~转音标记~~ → **已定：`+` 统一成 `-`**。
