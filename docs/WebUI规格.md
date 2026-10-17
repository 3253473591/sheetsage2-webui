# WebUI 规格（P1）

> 一句话：**丢音频 → 选参数 → 扒谱 + 歌词识别 → 下载产物**。界面就是工作区根目录那份
> `静态页面.html` 原型接到真数据上：原型参数面板上的那几组按钮**是真的能点的**，
> 比原型多几处——扒谱多了「量化」「段落」，歌词的「语言」是下拉框（选项比原型多）。
> **丢音频不会立刻扒谱**（门禁）：先把工程建出来，用户选完参数点「开始扒谱」才开跑。
> 本文是这条链路的实现规格；算法侧（BPM / 量化 / 填词）各有独立文档，这里不重复。

---

## 1. 边界

| 做 | 不做 |
|---|---|
| 原型的**工作台**页：工程列表 / 音轨波形与播放 / 两张阶段卡 / 结果卷帘 / 日志抽屉 | 原型的**编辑**页（导航上留了位置，标着"还没做"） |
| 两个阶段各自可单独跑、可重跑、可停止、可排队串联 | 段落级歌词编辑 / 手动改谱 |
| **参数面板可调**（精度 / 设备 / 范围 / 量化 / 识别模型 / 音源 / 段落 / 语言），值挂在工程上 | 每个阶段各一套模型实例（GPU 只有一块，参数变了就重载） |
| 干声**手动上传**（可选音源，选干声又没传就自动回退）；**一次拖两个文件**也行 | 自动人声分离（没有 demucs 权重；且自己传干声更可控） |
| **丢音频不自动开跑**：工程建完停在 `draft`，等用户点「开始扒谱」/「⏵ 自动提取」 | 拖进来就开始转（那正是这一轮要改掉的旧行为） |
| 歌词按**识别出来的段落**切片（合并相邻同名 + 标签翻中文 + 跳过前奏/间奏/尾奏） | 按固定秒数硬切（段落边界是音乐上的自然切点，切在字中间更糟） |
| 阶段级进度、小步骤、实时歌词（识别一段推一段） | 钢琴卷帘 / MIDI 预览 |
| 卷帘（小节线 / 段落 / 和弦 / 歌词 / 播放头）与音频播放（Range 支持拖动） | 多用户、鉴权、配额、数据库持久化 |
| 单产物下载 + 整包 zip；删除工程 | —— |
| **导出 SVP**（Synthesizer V Studio 工程，带变速与歌词）：下载弹窗勾选人声 / 乐器 / 和弦 | 音高曲线 / 颤音 / 力度等参数曲线；段落轨（SV 没有那种轨） |

> **参数为什么放开了**：原型里那几组按钮（精度 / 设备 / 范围 / 1.7B / 音源 / 语言）本来就是
> 设计稿的交互；现在**照着原型做**，取值表由后端 `/api/env` 的 `params` 下发
> （唯一真源是 `jobs.PARAM_SPECS`）。本项目比原型多的几处：扒谱的「量化」「段落」、
> 歌词的「语言」下拉框（原型只有 4 个语言，这里是 `asr.LANGUAGE_CHOICES` 全量）。
> **故意不上面板的**：旋律音高修正（模型的旋律整体高一个八度 → 一律降一个八度，
> 虚拟歌姬不会按原调调音，用户不需要选；见 [`WebUI问题清单.md`](WebUI问题清单.md) §1）。
> 「4G 特供」模式下参数由服务端锁死，面板只读（见 §7）。

---

## 2. 结构

```
web/                                  前端（Vue 3 + Vite + TypeScript）
  index.html
  vite.config.ts                      构建产物直接写进 Python 包的 static/
  smoke.mjs                           冒烟测试：构建产物 + jsdom + 真服务
  src/App.vue                         顶栏 / 侧栏 / 主区 / 日志抽屉的编排
  src/components/
    AppSidebar.vue                    工程列表 + 新建 + 拖入（原型的侧栏）
    ProjectHead.vue                   工程头 + 删除 / 下载▾（原型的 proj-head）
    TrackList.vue                     三条音轨：混音原曲（真波形+播放）、干声（可上传）、人声主旋律
    WaveBars.vue                      canvas 波形（1200 根柱子，DOM 会卡）
    PipelineStages.vue                两张阶段卡 + 箭头 + 按钮状态机 + 参数面板的数据
    StageCard.vue                     单张阶段卡：pill / 进度条 / 小步骤 / 参数面板
    ResultsPanel.vue                  概要数字 + 三个明细页（和弦 / 歌词 / 音符）
    LaneView.vue                      卷帘四条轨（小节线 / 段落 / 和弦 / 歌词）
    Playhead.vue                      播放头（单独一个组件，避免整块卷帘跟着刷新）
    LogDrawer.vue                     日志抽屉（原型的 drawer）
  src/composables/
    useStudio.ts                      唯一持有状态的地方（上传、SSE、重连、阶段动作、改参数）
    useAudio.ts                       播放器（模块级单例，与播放头共享时间轴）
  src/params.ts                       参数面板的纯逻辑（行 + 补丁 + 只读判定，可单测）
  src/api.ts                           HTTP + SSE 契约
  src/types.ts                         与后端 JSON 一一对应
  static -> ../src/sheetsage2_webui/webui/static    （`npm run build`）


src/sheetsage2_webui/webui/            后端（FastAPI）
  app.py                              路由：工程 / 阶段 / 参数 / 进度 / 卷帘 / 媒体 / 下载 / 静态资源
  jobs.py                             两阶段任务：PARAM_SPECS + StageRun 状态机 + 事件流 + 队列 + 打包
  runner.py                           真实实现：pipeline.run + Qwen3-ASR + 填词 + 歌词分行 + 段落切片
  lanes.py                            卷帘数据：tick 域 → **秒**（秒↔tick 的唯一真源在 timeline.py）
  media.py                            波形包络（ffmpeg + numpy，带缓存）+ Range 解析
  __main__.py                         python -m sheetsage2_webui.webui
  static/                             前端构建产物

src/sheetsage2_webui/sections.py      段落标签：英文 → 中文 + 合并相邻同名（纯函数，有单测）
src/sheetsage2_webui/asr.py           parsed_sections / sections_from_lab：段落 → 切片区间
```

**进程模型**：一个 uvicorn 进程；请求协程干完轻活（读快照、打包、算波形），推理交给
`JobManager` 里的**一个**工作线程。GPU 只有一块，并发只会一起变慢、还容易 OOM。

```
浏览器 ──POST /api/jobs──▶ 落盘 input/ ──▶ 工程停在 draft（**不排队**）
   │                                            │
   │  PATCH /options（选参数）                   │  用户点「开始扒谱」/「自动提取」
   ▼                                            ▼
SSE /api/jobs/{id}/events ◀──── 队列 ──▶ 阶段 1 transcribe（SheetSage2，按工程参数跑）
                                        └─▶ 阶段 2 lyrics（Qwen3-ASR + 填词）
                        └─▶ output/（30 个产物）──▶ /lanes /peaks /audio /download
```

### 工程目录：`output/<歌名>/`（**重启不丢**）

**一个子目录 = 一个工程**，而且就是**产物目录**（和命令行 `cli.py` 跑出来的同形状），
所以两边的东西能互相认：

```
output/<歌名>/project.json  report.json  melody_vocal.lab  score.abc  …   <- 产物（30 个）
output/<歌名>/job.json                                                   <- WebUI 元数据
output/<歌名>/.webui/input/<原音频>  stems/  peaks-*.json
              .webui/export/*.svp  bundle.zip                            <- WebUI 自己的东西
```

- 数据根默认 `<项目根>/output`（`SHEETSAGE_WEBUI_DATA` 可覆盖；测试与 `verify_webui.py` 都用临时目录）。
- **启动时扫描数据根重建列表**：有 `job.json` 的按元数据恢复（上次中断的阶段标成 `idle` +
  "上次运行被中断"，绝不装成还在跑）；只有 `project.json`/`report.json` 的当**只读工程**收进来。
- 只读工程（`snapshot.imported = true`，界面上标「已有产物」）：
  `/project`、`/lanes`、`/files`、`/download`、`/download?kind=svp` **照常**；
  改参数 / 重跑阶段 / 换音源 / 删除一律 **409**，`/audio` 与 `/files/job.json` **404**
  （没有原音频，也不把内部文件当产物）。
- 产物清单（`/api/jobs/{id}.files`、整包 zip）**跳过 `job.json` 与 `.webui/`**。
- 同名不覆盖：新工程目录重名就是 `-2`、`-3`……

---

## 3. 两个阶段

阶段 key 是前后端唯一的展示契约（后端 `jobs.PHASES`、`runner.py`，前端 `types.ts`）。

| key | 卡上的名字 | 引擎 | 小步骤（界面显示，都是真步骤） |
|---|---|---|---|
| `transcribe` | 扒谱 | SheetSage2 | 加载模型 → 读取音频 → 识别音乐 → 生成工程 → 整理产物 |
| `lyrics` | 歌词识别 | Qwen3-ASR | 读取工程 → 加载识别模型 → 语音识别 → 歌词对齐 → 写入卷帘 |

- **丢音频不自动开跑**（门禁）：`POST /api/jobs` 只建工程，状态停在 `draft`，两个阶段都
  `idle`，**不进队列**。要跑就得点「开始扒谱」/「开始识别歌词」，或者点「⏵ 自动提取」
  一条龙。参数面板这时才露出来，所以用户是先选参数、再开跑。
- **重跑扒谱会让已识别的歌词作废**（音符位置变了，字号时间戳对不上）——后端把歌词阶段退回
  `idle` 并记一条日志，界面上的歌词块随之清空。这是刻意的，不是 bug。
- **两个模型轮流用显存**：切到歌词阶段前把 SheetSage2 放掉（默认 `free_between_stages=True`）。
  0.5G + 6.3G 挤在 10G 卡上太危险；代价是下首歌重跑扒谱要重新加载（本机实测 14.7s）。
  显存富裕就用 `--keep-models` 关掉。
- 歌词是**边识别边推**的：`asr.AsrEngine.transcribe_sections(..., on_slice=...)` 每段结束回调，
  runner 把"已出的字 / 行"推成 `lyrics` 事件，界面上的歌词块一段一段长出来。

### 工作流：扒谱 → 歌词，按钮有门禁

原型就是这么排的（1 扒谱 → 2 歌词识别，中间一个箭头），所以**歌词阶段有前置条件**，
而且**丢音频不会自己开跑**：

| 状态 | 发生什么 |
|---|---|
| 拖入音频 | **只建工程**（`draft`，两个阶段 `idle`）。参数面板这时候才露出来 |
| 选参数 | `PATCH /options`；改扒谱参数 = 换口径 → 谱面与歌词一起作废（见下） |
| 点「开始扒谱」 | `POST /stages/transcribe` → 才真的排队 |
| 扒谱进行中 | 「开始识别歌词」**禁用**（悬停提示"扒谱进行中…"）；扒谱参数**不能改**（那一趟用的是旧值），歌词参数照常能改 |
| 想一条龙 | 点「⏵ 自动提取」→ 走 `POST /api/jobs/{id}/chain`，扒谱完自动接歌词 |

| 扒谱状态 | 「开始识别歌词」 | 「⏵ 自动提取（扒谱 → 歌词）」 |
|---|---|---|
| 进行中 / 排队 | **禁用**（悬停提示"扒谱进行中，完成后再点（或用「自动提取」排队）"） | 可用——它就是排队链路 |
| 没跑过 / 失败 / 取消 | 可点，点了提示 **"请先完成扒谱"** | 可用 |
| 已完成（**有谱、可能还没填词**） | 可用（"开始识别歌词" / "重新识别"） | 可用（"重跑全流程"） |

两道门，**同一句话**：

- **界面**：`web/src/stageActions.ts`（纯函数，`npm test` 有 11 条单测锁着），
  禁用 / 提示 / 放开三态都在这里决定，组件只负责渲染。
- **后端**：`POST /api/jobs/{id}/stages/lyrics` 在扒谱未完成时回 **409 `请先完成扒谱`**，
  界面把它显示在同一处提示条里——手动点不会白跑一次队列。
- **例外是 `/chain`**：`POST /api/jobs/{id}/chain` 是「自动提取」，它**故意**在扒谱还没跑完时
  就把歌词排进队列（计划里先跑扒谱），所以不受那道门限制。这也说明它不能由两次
  `/stages/*` 拼出来——第二次会被门挡住。
- 工程列表里点一个"**有扒谱但没填词**"的工程：按钮正常放开（判据就是所选工程的
  `stages.transcribe.state === 'done'`，与它是第几个工程无关）。

### 参数面板：照着原型做，值挂在工程上

版式与原型一致（每阶段一组标签按钮），改动立刻 `PATCH /api/jobs/{id}/options`：

| 阶段 | 行 | 取值 | 对应开关 |
|---|---|---|---|
| 扒谱 | 精度 | `BF16 快速` / `FP32 精确` | 计算精度 `dtype`（**只影响 CUDA 上的计算**，权重精度是启动开关 `--engine-weights`） |
| 扒谱 | 设备 | `自动 · GPU` / `CPU` | `EngineConfig.device`（`auto` / `cpu`） |
| 扒谱 | 范围 | `整首` / `前 30 秒` / `前 60 秒` | 透传 `max_seconds` 给模型（调试用） |
| 扒谱 | **量化**（比原型多） | `32 分网格` / `不量化` | `pipeline.run(quantize=…)` |
| 歌词 | 模型 | `1.7B` / `0.6B` | 换权重目录；可选项来自 `models/Qwen3-ASR-*` 的兄弟目录（有什么列什么） |
| 歌词 | 音源 | `干声（推荐）` / `混音原曲` | 选干声但没上传 → 自动回退到混音原曲并在日志里写明 |
| 歌词 | **段落** | `只跑唱段` / `全部段落` | 只跑唱段 = 跳过前奏 / 间奏 / 尾奏（见下） |
| 歌词 | **语言**（比原型多） | 下拉框 `自动/中文/英语/日语/粤语/韩语` | `asr.LANGUAGE_CHOICES` 全量 |

- **取值表由后端下发**：`jobs.PARAM_SPECS` 一处定义，`/api/env` 的 `params` 发给前端，
  `PATCH /options` 拿同一张表校验。加一个参数只改那一处 + 前端不用动。
- **改参数 = 换口径，旧结果作废**（和换音源、重跑扒谱同样处理）：
  改扒谱参数 → 扒谱退回 `idle`，歌词一并作废；改歌词参数 → 只作废歌词。
  事件流里会记一句"量化改成 不量化：已有的扒谱作废，需要重新扒谱"。
- **正在跑的阶段，属于它的参数改不了**（409）：那一趟用的是改动前的值，改了界面会和结果对不上。
- **4G 特供模式**（`--lock-params`）：服务端锁死参数，`/api/env.params_locked = true`，
  前端把面板画成只读，`PATCH /options` 一律 409。

### 识别语言：下拉框（比原型多的那一处）

阶段 2 的参数面板里有一个下拉框（`自动 / 中文 / 英语 / 日语 / 粤语 / 韩语`），
选项来自 `/api/env` 的 `params.lyrics[language]`（真源是 `asr.LANGUAGE_CHOICES`，加语言只改一行）。
它存在**工程级**（`job.options.language`，`PATCH /api/jobs/{id}/options`），跑完 `detail` 里还会记
`language` / `language_used`。

- **空串 = 自动**：不给 `language`，让模型自己判（结果里的 `language_used` 就是它判的）。
- 改语言 = 换识别口径 → **已识别的歌词作废**（和换音源、重跑扒谱同样处理）。
- 歌词阶段正在跑时下拉框禁用；乱填语言后端回 400。

实测（0.6B，20s 切片，中文歌）：

| 语言 | 模型判定 | 文本 | 逐字时间戳 |
|---|---|---|---|
| **自动** | Chinese | 纯净天空上，雄鹰在飞翔，守望着那片神界的天堂。雅鲁。 | 22 条 |
| 中文 | Chinese | **与自动逐字相同** | 22 条 |
| 日语 | Japanese | 純潔天空上、雄鶴在飞翔、守望着那片神界的天堂。雅露。 | **12 条**（4/12 零长度） |

两条结论：① **默认用"自动"是安全的**（中文歌上与指定中文逐字一致）；
② 语言是"**用哪种语言去听**"，不是翻译——拿中文歌选日语只会更差，而且要验日语精度
得用日语素材。

### 歌词音源：干声可选，自动回退

原型的第二条音轨叫 `干声 · 歌词用`，另外还有一组 `干声（推荐）/ 混音原曲` 单选按钮——
**这一组是真的**（`job.options.source`），但**上传与否仍然是事实**：

| 选的音源 | 上传了干声吗 | 歌词阶段用哪条 |
|---|---|---|
| 干声（推荐） | 有 | 用干声（`POST /api/jobs/{id}/stems/vocal`），并记录文件名 |
| 干声（推荐） | 没有 | **自动回退**到混音原曲，日志里写明"还没上传干声，自动回退到混音原曲" |
| 混音原曲 | 有 | 就用混音原曲（用户明确选的，不再"有干声就用"） |
| 混音原曲 | 没有 | 用混音原曲 |

- **不校验上传内容**：故意把混音原曲（甚至别的歌）传进干声栏也允许——那是用户的选择。
  我们只如实记录这一趟用了哪条（`stages.lyrics.detail.source` = `stem` / `mix`，
  `source_file` = 文件名），界面上在阶段卡下方写一句"本次音源：…"，日志里也记一条。
- 干声跟混音原曲一样**支持试听**（`/stems/vocal/audio` 带 Range）和独立的波形
  （`/peaks?source=stem`）。
- 换/删干声会让已识别的歌词作废（音源变了），跟重跑扒谱一样处理。
- **只有歌词阶段正在跑的时候**才拒绝改音源（那条音频正被切片读着）；扒谱在跑时随便改。
- `STEM_KINDS` 目前只有 `vocal`，将来要加伴奏/鼓直接加 key。

### 段落切片：按识别出来的段落切，并跳过器乐段

歌词识别**不是按固定秒数硬切**的，边界直接用 SheetSage2 的 `structure.lab`——段落本来就是
音乐上的自然切点，比按固定窗口切更不容易切在字中间。这一步在 `asr.sections_from_lab()`：

```
structure.lab（模型原始产物，不动）
   ↓ ① 标签规范化：英文 → 中文（sections.canonical_label）
   ↓ ② 合并相邻同名：模型的标签是按**乐句**给的，`verse verse verse` 本来就是一段主歌
   ↓ ③ 跳过器乐段：前奏 / 间奏 / 尾奏 一般不唱（参数「段落」= 只跑唱段时）
   ↓ ④ 打包：相邻短段攒到 `--asr-max-slice` 以内（默认 90s，4G 模式 30s）
   ↓ ⑤ 仍超长的按上限切开；丢掉 < 8s 的尾巴
切片区间（秒）
```

**② 合并为什么必需**：不合并的话 `project.json` 里全是 "verse、verse、chorus、chorus"，
卷帘上画出一串重复块；合并之后一首歌才是人描述它的样子。

**③ 跳过器乐段为什么能省**：实测 10 个真实工程的段落标签只有 7 种
（`chorus / verse / interlude / intro / outro / pre-chorus / bridge`），
而前奏 / 间奏 / 尾奏一般不唱。在 `output/` 的真实 `structure.lab` 上算一遍：

| 工程 | `structure.lab` 行数 | 合并后 | 送去识别的音频（全部段落） | 只跑唱段 | 省掉 |
|---|---|---|---|---|---|
| 2020原唱 | 28 | 10 段 | 392.5s | 355.5s | 36.9s（2 段） |
| 光芒 | 12 | 7 段 | 195.2s | 146.4s | **48.8s（25%）** |
| 壁上观 | 16 | 7 段 | 284.0s | 211.3s | **72.7s（26%）** |

- 段落标签的翻译表在 `sections.SECTION_LABELS`（加别名只改那一处）；**认不出来的标签原样
  保留**，不猜。
- `structure.lab` 本身**一个字节都不改**：它是模型的原始产物（自洽性检查逐行比），中文只
  出现在工程化之后的 `project.json` / `/api/lanes` / 日志里。
- 速度拟合的输入（`structure_seconds`）也**照旧用原始行**——否则会悄悄改掉已经验证过的
  速度结构（见 `docs/BPM算法规格.md`）。段落合并只影响"显示 + 歌词切片"。
- **全是器乐段时会回退**到全部段落（别让任务死在"没有可用区间"上），日志里会写明。
- 跳过了哪几段写进 `stages.lyrics.detail.skipped_sections`（阶段卡与日志都看得到），
  `lyrics.json` 里同时记 `sections`（真跑过的切片）/ `skipped_sections` / `max_slice_seconds`。
- **模型标错的风险照实说**：如果模型把有人声的段标成 `interlude`，那段歌词会被跳过。
  改成「全部段落」重跑即可——这就是它放在参数面板上的原因。

### 导出 SVP：下载弹窗勾选人声 / 乐器 / 和弦

界面上的「**下载 SVP**」是主按钮（整包 zip 退到旁边小箭头展开的产物菜单里）。
点开是 `ExportDialog.vue`：三个勾选框，**默认只勾人声**，乐器与和弦默认关。

| 勾选 | 内容 | 轨数 |
|---|---|---|
| 人声轨（默认勾） | 填了词的音符（`tracks[vocal]`） | 1 |
| 乐器轨（默认关） | 模型扒出来的伴奏旋律，没有歌词 → 用默认音节占位 | 1 |
| 和弦轨（默认关） | `chords.mid` 的和弦音 | **4**（SVP 一轨只能有一个音，按同时发声拆轨） |

实现细节（tick ÷4、**弱起当 SV 的"前置小节"**、`dump()`/`load()` 并不严格互逆、
`language_override` 跟随识别语言、`+`→`-`、版本 1.9 兼容）与实测数字
（《光芒》324 音符零漂移、86 个 tempo 点、和弦拆 4 轨）都在
[`docs/SVP导出调研.md`](SVP导出调研.md) §5，这里不重复。

装法：**可选依赖** `pip install -e .[svp]`（libresvip 会带 35 个包并把 pydantic 升到 2.13；
没装时只有导出这一条路回 501 + 安装提示，其余功能照常）。
---

## 4. API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/healthz` | 存活探针 |
| GET | `/api/env` | 版本 / 两个模型 / 设备 / ffmpeg / 数据目录 / 队列 / 阶段表 / **参数取值表 + 是否锁定** |
| GET | `/api/jobs` | 工程列表（新的在前） |
| POST | `/api/jobs` | multipart `file`；`plan` 可选。**默认一个阶段都不排**（建完停在 `draft`） |
| GET | `/api/jobs/{id}` | 工程快照（两个阶段 + **参数** + 产物清单 + highlights） |
| POST | `/api/jobs/{id}/cancel` | 请求取消，下一检查点生效 |
| DELETE | `/api/jobs/{id}` | 删除工程。**默认软删除**（目录挪进 `.trash/`，7 天内可恢复）；`?permanent=true` 才是真删。运行中 409 |
| POST | `/api/jobs/{id}/stages/{phase}` | 单独（重）跑一个阶段；歌词需先有扒谱（否则 409） |
| POST | `/api/jobs/{id}/chain` | 「自动提取」扒谱 → 歌词一条龙（不受那道门限制） |
| PATCH | `/api/jobs/{id}/options` | 改参数（精度/设备/范围/量化/模型/音源/语言）；见 §3 的取值表 |
| POST | `/api/jobs/{id}/stems/vocal` | 上传/更换干声（multipart `file`，内容不校验） |
| DELETE | `/api/jobs/{id}/stems/vocal` | 移除干声，歌词回退到混音原曲 |
| GET | `/api/jobs/{id}/stems/vocal/audio` | 干声试听，支持 Range |
| GET | `/api/jobs/{id}/events` | SSE，`?since=<seq>` 续传 |
| GET | `/api/jobs/{id}/project` | tick 域工程（小节表 / 和弦 / 段落 / 音符+歌词） |
| GET | `/api/jobs/{id}/lyrics` | 歌词识别结果（text / chars / lines / fill / source） |
| GET | `/api/jobs/{id}/lanes` | 卷帘数据（**秒**）：小节 / 段落 / 和弦 / 人声音符 / 歌词行；还没扒谱时 **404**（前端当"卷帘空"处理） |
| GET | `/api/jobs/{id}/peaks?buckets=` | 波形包络（`source=mix\|stem`，带缓存）；**没有音频时 404**（只读工程 / 干声没了） |
| GET | `/api/jobs/{id}/audio` | 上传的音频，**支持 Range**（播放 / 拖动进度条）；只读工程 404 |
| GET | `/favicon.ico` | 204（前端用内联 SVG 图标，这条只是让浏览器别在日志里刷 404） |
| GET | `/api/jobs/{id}/files/{path}` | 单个产物 |
| GET | `/api/jobs/{id}/download` | 下载：默认整包 zip；`?kind=svp&parts=vocal,instrumental,chords` 导 **SVP 工程** |
| GET | `/api/jobs/{id}/exports` | 导出弹窗的数据源：哪几组能导 / 各多少音 / 占几轨 / 默认勾哪个 |
| GET | `/api/trash` | 回收站列表（软删除的工程：`id` / `name` / `deleted_at` / `size` / `files`） |
| POST | `/api/trash/{entry}/restore` | 从回收站恢复（目录挪回 `output/`，返回恢复后的快照） |
| DELETE | `/api/trash/{entry}` | 彻底删除回收站里的一项（不可恢复） |

下载类响应（`/download`、`/files/{path}`）带 `X-Artifact-Rev` 头 = 工程的**产物版本戳**：
重跑过阶段之后它一定变大，据此能判断手里的文件是不是最新那一版。

错误一律是 `{"detail": "人话"}`：后缀不支持 400、超限 400、空文件 400、参数越界 400、
工程不存在 404、运行中删除 409、阶段正在跑时改参数 409、4G 特供锁定时改参数 409、
Range 越界 416。

**阶段失败**不再只给一句异常原文：快照与 SSE 里同时有
`error`（人话）/ `error_code`（`E_DISK_FULL` 这类稳定码）/ `error_action`（下一步）/
`error_detail`（技术原文）。码表与判定见 `src/sheetsage2_webui/webui/errors.py`。
`done` 也不再等于"runner 没抛异常"：产物自检（`detail.artifacts`）不通过就落 `failed`
（`E_ARTIFACTS_INCOMPLETE`），见 `docs/健壮性保障评估.md` 的 G4/G7。

### 快照里的阶段

```jsonc
{
  "id": "99f533bcf535", "name": "凤凰传奇 - 光芒-45s", "rev": 1, "state": "done",
  "artifacts_rev": 2,                  // 产物版本戳：每完成一轮阶段 +1（派生缓存都靠它失效）
  "stage": null,                       // 当前正在跑的阶段（没跑就是 null）
  "options": {                         // 页面上可调的参数（`/api/env.params` 是取值表）
    "dtype": "bf16", "device": "auto", "range": "full", "quantize": "grid",
    "asr_model": "1.7B", "source": "stem", "language": ""
  },
  "stages": {
    "transcribe": {"key": "transcribe", "label": "扒谱", "state": "done", "progress": 1.0,
                   "step": "整理产物", "elapsed_seconds": 23.07, "error": null,
                   "error_code": null, "error_action": null, "error_detail": null,
                   "last_advance_at": 1792223158.07,
                   "detail": {"dtype": "bf16", "device": "cuda", "range": "full",
                              "quantize": "三十二分", "notes": {"...": 0},
                              // 产物自检（完成门禁）：不通过就不会是 done
                              "artifacts": {"ok": true, "checked": 21, "total": 21,
                                            "missing": [], "problems": []}}},
    "lyrics":     {"key": "lyrics", "label": "歌词识别", "state": "done", "progress": 1.0,
                   "step": "写入卷帘", "elapsed_seconds": 14.84,
                   "detail": {"chars": 34, "lines": 3, "assigned": 34, "absorbed": 5,
                              "asr_model": "Qwen3-ASR-1.7B", "source": "stem",
                              "language": "自动", "language_used": "Chinese"}}
  },
  "highlights": { "...": "收窄过的概要（拍号/BPM/调性/音符/量化/和弦数）" },
  "files": [ {"path": "project.json", "size": 100243, "kind": "data"} ]
}
```

`last_advance_at` 是"最后一次有进展"的时刻：看门狗拿它判卡死，界面拿它显示
"上次进展 xx 秒前"（超过 60 秒标黄）。

### SSE

```
id: 12
data: {"seq":12,"type":"progress","time":0.0,"data":{"phase":"transcribe","progress":0.42,"message":"窗口 2/6 · 解码 512 tokens"}}
```

| type | 含义 |
|---|---|
| `phase` | 某阶段开始（`{phase, state, label}`） |
| `step` | 阶段内小步骤切换（**只在变了时发**，否则逐窗口回调会刷爆） |
| `progress` | 阶段进度（0.25s / 0.2% 双重节流） |
| `lyrics` | 实时歌词（`{text, chars, lines, slices, slices_total}`） |
| `log` | 给人看的日志（模型就绪 / 告警） |
| `phase_done` / `phase_error` / `phase_cancelled` | 阶段收尾（带 `detail`） |
| `done` / `error` / `cancelled` | 任务级终态，**永远是最后一条** |

- `seq` 单调递增；断线后带 `?since=<已有最大 seq>` 重连，后端从下一条补发，刷新页面不丢进度。
- 连上一个已结束的任务时补发一条合成终态事件，否则 `EventSource` 会对着已关闭的流反复重连。
- 事件在内存里留最近 4000 条。

---

## 5. 原型对照

| 原型元素 | 实现 | 数据来源 |
|---|---|---|
| 侧栏工程列表 + `扒谱 ✓` / `歌词 45%` | `AppSidebar.vue` | `/api/jobs` → `stages.{phase}.state/progress` |
| 工程头 `rev N · 创建于 … · 1:24.51` | `ProjectHead.vue` | 快照 `rev` / `created_at` / `highlights.duration_seconds` |
| 音轨 `混音原曲` 波形 + ▶ + 时长 | `TrackList.vue` + `WaveBars.vue` | `/api/peaks`（真波形）、`/api/audio`（Range） |
| 音轨 `干声` | **真的能上传**：`TrackList.vue` 第二行 | `/api/jobs/{id}/stems/vocal` + `/peaks?source=stem`；没传就标明"自动用混音原曲" |
| 音轨（第三行）`人声主旋律` | 模型扒出来的人声音符活跃度 | `/api/lanes` 的 `vocal_notes`（模型没做人声分离，不假装有干声） |
| 原型里的 `语言 自动/中文/英语/日语` 按钮 | `PipelineStages.vue` 的下拉框（写进 `job.options.language`）——**比原型多的那一处** | `/api/env.params.lyrics[language]` + `PATCH /options` |
| 阶段卡 pill / 进度条 / 小步骤 / ETA | `StageCard.vue` | SSE `phase` / `progress` / `step`；ETA 换成**已用秒数**（不猜） |
| 阶段卡的参数面板（精度 / 设备 / 范围 / 1.7B / 音源 / 语言） | **全部可点**（`StageCard.vue` 的标签按钮 + 语言下拉框），锁定态只读 | `/api/env.params`（取值表）+ `PATCH /jobs/{id}/options`；面板版式见 `docs/WebUI规格.md` §3 |
| 原型没有的「量化」「段落」两行 | `StageCard.vue`（量化：32 分网格 / 不量化；段落：只跑唱段 / 全部段落，按钮带悬停说明）——**比原型多的两处** | 同上 |
| 卷帘的 `段落` 轨 | `LaneView.vue` 直接画 `section.label` | `/api/lanes` 的 `sections`（已在 `pipeline` 里合并同名 + 翻成中文） |
| `开始扒谱` / `停止` / `重新扒谱` | `POST /stages/transcribe`、`/cancel` | 阶段状态决定按钮 |
| `⏵ 自动提取（扒谱 → 歌词）` | 依次 `POST /stages/transcribe` + `/stages/lyrics` | 队列按 plan 顺序跑 |
| `和弦` / `歌词` 两个 tab | `ResultsPanel.vue`（多了个 `音符` tab） | `/api/lanes`、`/api/project` 的人声轨 |
| 卷帘 ruler / 段落 / 和弦 / 歌词 | `LaneView.vue` | `/api/lanes`（秒域）+ 播放头跟着音频走 |
| 日志抽屉 `[扒谱]` / `[歌词]` | `LogDrawer.vue` | SSE `step` / `log` / `lyrics` / 阶段收尾 |
| `下载 ▾` | `ProjectHead.vue` 的下拉 | `/api/jobs/{id}/files/*` + `/download` |
| `在编辑器中打开` | 去掉（原型的「编辑」页本就没有内容） | — |
| `模拟：回到未扒谱` | 去掉（真任务不需要演示开关） | — |

---

## 6. 关键实现决定

1. **卷帘的秒由后端算。** 秒↔tick 的唯一真源是小节表（分段线性映射），那套逻辑在
   `timeline.Timeline` 里验证过；TS 里再抄一份就是埋第二个真相。所以 `/lanes` 直接给秒，
   前端只做 `总时长 → 像素宽` 的线性缩放。
2. **波形在服务端算。** 1200 个桶：ffmpeg 解成 4kHz 单声道，分桶取绝对值最大，几十毫秒，
   结果按桶数缓存在任务目录（`peaks-1200.json`）。前端用 canvas 画，不用 DOM。
3. **音频必须支持 Range。** 没有 206，浏览器拖动进度条就废了。范围解析在 `media.parse_range`，
   越界回 416 而不是悄悄给全量。
4. **歌词的"行"是派生结构。** 字才是真数据；行按换气间隙（>1.0s）或字数（>18）折出来，
   `runner.group_lyric_lines` 一个函数同时服务实时事件与落盘的 `lyrics.json`，两边一致。
5. **取消是协作式的。** 推理无法从外部中断，`Progress.check()` 在每次模型回调里查标志，
   命中就抛 `JobCancelled`。所以取消的生效点是"下一个解码步 / 下一段切片"。
6. **上传先落盘，但先不排队（门禁）。** 按 1MB 分块写，超限立刻中断并删掉半截文件；被拒的
   上传不留目录。建好工程状态是 `draft`（既没排队也没在跑），用户点第一个「开始」才
   `_queue.put`——`draft` 不是终态，所以 SSE 流可以在他选参数期间就挂着。
7. **进度只打补丁，产物重新拉。** SSE 事件原地更新阶段进度（进度条才连续动），
   阶段结束 / 终态时重拉一次快照（那才是落盘后的真相）。
8. **`static_ready` 是自检。** 前端没构建时服务照常起，根路径回一段纯文本告诉你去 build。
9. **播放头是独立组件。** `currentTime` 每秒更新好几次，让结果区直接读它会把几百个和弦块
   跟着重渲染，所以只有 `Playhead.vue` 订阅它。
10. **参数表只有一份。** `jobs.PARAM_SPECS` 同时喂给三个地方：`/api/env` 发给前端画面板、
    `validate_options` 校验 PATCH、`describe_change` 拼"谁改成了什么"的日志文案。
    前端不硬编码取值（`params.ts` 只做"行 + 补丁 + 只读判定"，有 11 条单测）。
11. **参数变了就重载模型，不偷偷混用。** `EngineProvider` / `AsrProvider` 各自按
    `(模型, 设备, 精度, 权重精度)` 缓存：配置和手上那台不一样就先 `release()` 再按新配置加载。
    小显存卡上这两档不会来回切；切了就把代价说在日志里（"扒谱参数变了，按新参数重新加载模型"）。
12. **"精度"只改计算精度。** 前端那个 `BF16 快速 / FP32 精确` 落到 `EngineConfig.dtype`
    （只影响 CUDA 上的计算）；**权重精度是启动开关**（`--engine-weights`），所以 4G 特供模式
    下前端选 fp32 也不会把显存撑爆——两件事分开，是这个设计的关键。

---

## 7. 怎么跑

```bat
:: 最省事：双击工作区根目录的 webui.bat（自检 + 自动构建前端 + 起服务 + 开浏览器）
webui.bat
webui.bat 8010 2      :: 首选端口 + 直接进「4G 特供」模式（跳过菜单）
```

**只有两个模式**（`scripts/launch.py` 的 `PROFILES`）：

| 模式 | 服务端开关 | 参数面板 |
|---|---|---|
| `1` 默认 | 无 | **可调**：精度 / 设备 / 范围 / 量化 / 识别模型 / 音源 / 语言 |
| `2` 4G 特供 | `--engine-weights bf16 --asr-max-vram 1GiB --asr-max-slice 30 --lock-params` | **锁定**（服务端定死，`PATCH /options` 回 409） |

> 原来还有"0.6B 识别模型"和"两个模型常驻"两档，已经去掉：前者现在就是参数面板上的
> 「模型」一栏（`--asr-model` 仍然留给运维用），后者（两个模型一起占显存）在 10G 卡上太危险。

启动器分两层，**这不是洁癖，是踩过的坑**：

- `webui.bat`（工作区根目录）：**只允许 ASCII**。cmd.exe 按**字节偏移**重读批处理文件，
  里面一旦出现多字节字符（哪怕是 `rem` 注释），行就会被切断、尾巴被当命令执行——实测报
  `'bf16' is not recognized as an internal or external command`。它只做三件事：
  `chcp 65001`、检查 `.venv\Scripts\python.exe`、把参数转交给下一层。
- `scripts/launch.py`：全部逻辑与中文输出（Python 在 Windows 控制台走 `WriteConsoleW`，
  任何代码页都不乱码）。它负责自检、前端缺失时 `npm run build`、**端口可用性检查**（首选端口
  被占或落在系统保留段就自动换一个，并说明被谁占了）、二选一的模式菜单、开浏览器、起服务。
- `chcp 65001` 仍然要留着：它让**子进程**（服务端）的 UTF-8 输出在同一窗口里也正常。

```powershell
# 一次性：装后端依赖 + 构建前端
.\.venv\Scripts\python.exe -m pip install -e .[web]
cd web; npm install; npm run build; cd ..

# 起服务（零参数 = 默认模式，参数在页面上选）
.\.venv\Scripts\python.exe -m sheetsage2_webui.webui        # http://127.0.0.1:8000

# 4G 特供：把参数锁死（等价于启动菜单里的 2）
.\.venv\Scripts\python.exe -m sheetsage2_webui.webui `
    --engine-weights bf16 --asr-max-vram 1GiB --asr-max-slice 30 --lock-params
```

> `--asr-max-vram` 走 accelerate 的 `device_map="auto"` + `max_memory`。实测（1.7B，45s 切片）：
> 预算 3GiB → 峰值显存 5996→**5039MiB**、转写 5.6→**10.5s**；预算 2GiB → **3982MiB** / 18.4s；
> **识别文本逐字一致**。两个坑：① `max_memory` 是**每个模型各自**的上限（ASR 与对齐器分别
> `from_pretrained`），预算必须小于单个模型的权重大小才会真的 offload；② 代价是线性变慢。
> 详见 `scripts/probe_asr_offload.py`。

### 最低配能到哪：4G 显存 + 16G 内存（两个阶段解耦）

扒谱和歌词**串行**跑（默认阶段之间释放模型），所以卡的下限取两者较大的那个，不是相加。
实测（RTX 3080 12G + 45s 切片；显存是 torch 分配峰值，另外还要给 CUDA 上下文/桌面留约 800MiB）：

| 阶段 | 配置 | 峰值显存 | 峰值内存(RSS) | 45s 用时 | 4G 够吗 |
|---|---|---|---|---|---|
| 扒谱 | 默认（fp32 权重） | 3379MiB | 4719MiB | 7.8s | ✗ |
| 扒谱 | `--engine-weights bf16` | **2088MiB** | 5907MiB | 6.3s | ✓ |
| 歌词 | 1.7B + `--asr-max-vram 1GiB` | **2265MiB** | 7880MiB | 31.1s | ✓ |
| 歌词 | 0.6B + `--asr-max-vram 1GiB` | **2332MiB** | 5756MiB | 9.6s | ✓ |
| 歌词 | 0.6B / 1.7B 全 GPU | 3601 / 5996MiB | 2726 / 4907MiB | 5.5 / 5.7s | ✗ |

```powershell
# 4G 卡的跑法：两个阶段都开省显存开关，再按显存把切片切小
.\.venv\Scripts\python.exe -m sheetsage2_webui.webui --engine-weights bf16 --asr-max-vram 1GiB --asr-max-slice 30
```

三条硬约束：

1. **不能 `--keep-models`**：同时常驻要按**相加**算（2088 + 2265 ≈ 4.4G），4G 卡放不下。
2. **扒谱阶段没有 offload 这条路**：SheetSage2 自己的 `from_pretrained` 只接受单个设备
   （`device_map="auto"` 会被它强转成 `cuda`，多设备直接 `ValueError`），只能靠 bf16 把权重减半
   （实测 3379 → 2088MiB，且**出谱签名与 fp32 完全一致**：100 音 / 34 唱 / 66 器 / 79 拍）。
   注意降精度必须在**加载之后**做：模型要先在 fp32 下合并 LoRA 适配器，加载期给 `torch_dtype`
   会直接报 "Merge adapters on CPU with float32 parameters"。
3. **16G 内存够**：最坏一档（1.7B + 1GiB 预算）RSS 7880MiB，加系统占用仍在 16G 以内。

代价是**慢**：45s 音频的歌词阶段，3080 上 1.7B + 1GiB 预算要 31s（全 GPU 5.7s）。
3050 4G 比 3080 慢数倍，所以"能跑"不等于"好用"——整首歌按 30s 切片会跑很久。


开发前端（HMR，`/api` 代理到 8000）：

```powershell
.\.venv\Scripts\python.exe -m sheetsage2_webui.webui --port 8000   # 终端 1
cd web; npm run dev                                                # 终端 2 -> 5173
```

改完前端**必须 `npm run build`**，否则后端服务的还是旧产物。

---

## 8. 验证

```powershell
# 1) HTTP 层：两阶段生命周期 / SSE / 卷帘 / 波形 / Range / 下载安全（假 runner，不碰模型）
.\.venv\Scripts\python.exe -m pytest tests -q

# 2) 真机端到端：真起服务、真跑两个模型、真下载 zip
.\.venv\Scripts\python.exe scripts\verify_webui.py
.\.venv\Scripts\python.exe scripts\verify_webui.py --transcribe-only     # 只跑扒谱（省时间）
.\.venv\Scripts\python.exe scripts\verify_webui.py --keep                # 留着，自己点页面

# 3) UI 冒烟：把构建产物放进 jsdom，对着真服务渲染一遍（要有一个跑完的工程）
.\.venv\Scripts\python.exe scripts\verify_webui.py --port 8123 --keep   # 终端 1
cd web; npm run smoke -- http://127.0.0.1:8123                          # 终端 2
```

**第 2 步的判据**：上传后**不自动开跑**（`draft` + 两个阶段 `idle`），点了「开始扒谱」之后
两个阶段才依次到 `done`；SSE 有序且以 `done` 收尾、`highlights` 里拍号/BPM/量化网格非空、
产物 ≥ 29 个、`/project` 有小节表与两条分轨、`/lanes` 的段落/和弦/人声音符/歌词行非空且
秒数落在曲长内、`/peaks` 有波形、`/audio` 的 206/416 正确、**人声音符真的填上了字**
（`assigned > 0` 且 `project.json` 里有 `lyric`）、zip 含 `project.json`/`report.json`/`lyrics.json`
且与单文件逐字节一致、路径穿越被拒。

中间两段分别验**干声规则**与**参数面板 / 段落切片**：

- 干声：第一次跑歌词 `source=mix`；把混音原曲**当作干声**传进去（内容不校验）→ 快照里有这条、
  旧歌词作废、`/stems/vocal/audio` 支持 Range；重跑歌词 `source=stem` 且 `source_file` 就是
  那个文件名；移除后回到 `idle` 并回退。
- 参数面板：`/api/env.params` 的两组键就是 `精度/设备/范围/量化` + `模型/音源/段落/语言`，
  语言那一行是 `widget=select` 且选项 > 4，「段落」默认 `vocal`；改语言 200、乱填语言 400、
  不认识的参数 400；把量化改成"不量化" → **扒谱与歌词都作废**；改回去再 `POST /chain`
  重跑一遍能回到 `done`（最后一步是为了让 UI 冒烟有个跑完的工程可渲染）。
- 段落：验证用的 45s 切片里前 30s 被模型判成前奏，所以「只跑唱段」应该只送 14.7s 去识别
  （`detail.sections` 求和），跳到的那段在 `detail.skipped_sections` 里是 `前奏`；
  改成「全部段落」再跑一遍 → `skipped_instrumental == 0` 且送去的音频变多（45.0s）；
  卷帘的段落标签是中文（`['前奏', '主歌']`）且没有相邻重复。

**第 3 步的判据**：jsdom 渲染出的 DOM 里有 2 条音轨、2 张阶段卡、和弦块、歌词块、"已完成"
状态，且控制台没有 Vue 报错或警告。这是唯一能盖住"运行期"的那一层——`vue-tsc` 只保证
类型和模板编译。

本机实测（45s 切片，《光芒》）：扒谱 24.03s（含加载 15.5s）、歌词 14.84s（含加载 10.3s），
34 字 / 3 行，挂字 34、没分到字 5、同音挤占 5，卷帘 29 个和弦 / 4 个段落 / 34 个人声音符。
把混音原曲当作干声传进去重跑歌词：3.3s（识别模型还驻留在显存里），`source=stem`。
