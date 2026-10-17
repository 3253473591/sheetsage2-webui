# SheetSage2 WebUI (V2) — 推理集成 + WebUI

把 [m-a-p/SheetSage2](https://huggingface.co/m-a-p/SheetSage2) 的推理接进本项目：
**能稳定调用、能验证结果**，并且有一个"丢音频 → 选参数 → 开始识别 → 下载产物"的本地 WebUI
（Vue 3 + Vite + TypeScript 前端，FastAPI 后端；参数面板照 `静态页面.html` 原型做成可调的）。

> 模型权重许可是 **CC BY-NC 4.0（非商业）**，商用前需要另行确认授权。

## 先读这个

- **[`docs/交接-下一次对话.md`](docs/交接-下一次对话.md)** —— 唯一入口：现状、怎么跑、未决问题、下一步
- [`docs/健壮性保障评估.md`](docs/健壮性保障评估.md) —— **面向"不懂技术的用户"的保障措施评估**（幂等 / 乐观锁 / 原子写 / 僵尸任务 / 报错可读性，P0~P2 共 22 条缺口 + 落地顺序）
- [`docs/WebUI规格.md`](docs/WebUI规格.md) —— **WebUI 的实现规格**（API、任务状态机、SSE、阶段表、参数面板、段落切片）
- [`docs/WebUI问题清单.md`](docs/WebUI问题清单.md) —— **WebUI 的问题与改进流水账**（发现新问题记这里）
- [`README-internal-test.md`](README-internal-test.md) —— **内测说明**（给同好的话术：怎么跑、怎么反馈、已知的坑）
- [`docs/SVP导出调研.md`](docs/SVP导出调研.md) —— **SVP 导出的选型调研 + 落地记录**（用 LibreSVIP，不自写 `.svp` 序列化）
- [`docs/打包分发待办.md`](docs/打包分发待办.md) —— **待办**：轻量包 + 首次运行在线下载（下载清单已核实）
- [`docs/BPM算法规格.md`](docs/BPM算法规格.md) —— BPM 的实现规格
- [`docs/音符量化规格.md`](docs/音符量化规格.md) —— 量化的实现规格
- [`docs/Vocal2Midi-对照与借鉴.md`](docs/Vocal2Midi-对照与借鉴.md) —— 参考项目对照与可借鉴项
- [`docs/SheetSage2-集成调研.md`](docs/SheetSage2-集成调研.md) —— 最全的调研与实测

## 现状

| 能力 | 状态 |
|---|---|
| 模型加载（CUDA / CPU，bf16 / fp32，本地权重优先） | ✅ 已验证 |
| 全曲转写（滑窗 + 重叠前缀） | ✅ 10 首样例全部跑通 |
| 产物完整性与自洽性检查 | ✅ 15 个文本产物 + 6 个 MIDI |
| 确定性 / 重载一致 / 离线加载 / CPU 退路 | ✅ 已验证 |
| 一键流水线（零参数 → tick 域工程 + 报告） | ✅ 已验证 |
| 歌词识别（Qwen3-ASR + 逐字对齐，按区间切片） | ✅ 已验证 |
| 填词（逐字时间戳 → 音符） | ✅ 已验证（3 首，看不到的字 29 / 18 / 233，量化后不再恶化） |
| 音符时值量化（DP 吸网格，口径见 `docs/音符量化规格.md`） | ✅ 已验证（13 工程 / 26 轨道，最大位移 0.86s → 0.12s，0 失败） |
| **扒谱音高修正**（旋律默认降一个八度；和弦轨不动） | ✅ 已验证（真机 A/B：音符整齐 −12，tick / 小节表 / 和弦逐项不变）——见 [`docs/WebUI问题清单.md`](docs/WebUI问题清单.md) §1 |
| **WebUI**（丢音频 → 选参数 → 扒谱 + 歌词识别 → 下载产物） | ✅ 已验证（原型 `静态页面.html` 接真数据；真机端到端 + 49 项 HTTP 层测试 + 29 项前端单测 + jsdom 冒烟） |
| **导出 SVP**（Synthesizer V Studio 工程，带变速 + 歌词；人声/乐器/和弦可勾） | ✅ 已验证（真机导出 + 往返自洽；见 [`docs/SVP导出调研.md`](docs/SVP导出调研.md) §5） |
| WebUI 的**编辑**页 | ⛔ 未做（导航上留了位置，原型的「编辑」页本就没有内容） |

**BPM 有两个来源，必须交叉校验**：模型不把速度当预测任务（8 个 prompt 字段里没有 tempo），
但它自己的 ABC 生成器会用**首尾两点法**算一个值写进 `score.abc` 的 `Q:1/4=` —— 这才是"模型
原始产出的 BPM"。`beat.lab` 的时间戳只有 0.01s 精度（104 BPM 的真拍长 0.576923s 被存成 0.58s，
中位间隔法会读成 103.45），且歌曲结尾的散拍会拖偏端点法，所以 `tempo.reconcile()` 以**节拍网格的
最小二乘拟合**为准，`Q` 值作独立校验，不一致就报警：

| 歌曲 | 模型 `Q:1/4=` | 网格拟合 | |
|---|---|---|---|
| 光芒 | 104 | **104.000** | 一致 |
| 朋友的酒 | 120 | **119.995** | 一致 |
| 相思 | 98 | **100.001** | 不一致（结尾散拍拖偏 Q 值） |

速度另有吸附（取整/半整数）和恒速化重排两层干预，见 `scripts/tempo_report.py` 与
[`docs/SheetSage2-集成调研.md` §5.1](docs/SheetSage2-集成调研.md)。

详细验证结果见 [`reports/验证报告.md`](reports/验证报告.md)，
接口与环境调研见 [`docs/SheetSage2-集成调研.md`](docs/SheetSage2-集成调研.md)。

## 目录结构

```
sheetsage2-webui V2/
├─ src/sheetsage2_webui/       集成层（本次的核心代码）
│   ├─ bootstrap.py            进程级环境准备（OpenMP 开关、ffmpeg、UTF-8 输出）
│   ├─ engine.py               模型加载 + transcribe 封装 + 显存不足退回 CPU
│   ├─ tempo.py                BPM 的鲁棒估计、吸附、恒速化干预、拍号与弱起、交叉校验
│   ├─ timeline.py             秒 ↔ tick 的分段线性映射（不依赖 BPM）
│   ├─ quantize.py             音符时值量化：吸到网格（整数 DP）
│   ├─ sections.py             段落标签：英文 → 中文 + 合并相邻同名（纯函数）
│   ├─ asr.py                  歌词识别：Qwen3-ASR + 逐字对齐，按区间切片
│   ├─ lyrics.py               把逐字时间戳填到音符上
│   ├─ pipeline.py             一键流水线：转写 + 自动裁决 + tick 域工程 + 量化 + 报告
│   ├─ result.py               产物解析、自洽性检查、结果摘要
│   ├─ service.py              给上层用的单一入口（转写 + 摘要 + 体检）
│   ├─ cli.py                  命令行入口
│   └─ webui/                  WebUI 后端（FastAPI）
│       ├─ app.py              路由：工程 / 两阶段 / SSE 进度 / 卷帘 / 媒体 / 下载
│       ├─ jobs.py             两阶段任务：阶段状态机、事件流、队列、打包
│       ├─ runner.py           真实实现：pipeline.run + Qwen3-ASR + 填词 + 歌词分行
│       ├─ lanes.py            卷帘数据（tick → 秒，唯一真源仍是小节表）
│       ├─ media.py            波形包络 + Range 解析
│       ├─ __main__.py         python -m sheetsage2_webui.webui
│       └─ static/             前端构建产物（`npm run build` 生成）
├─ web/                        前端（Vue 3 + Vite + TypeScript）
│   ├─ src/App.vue             顶栏 / 侧栏 / 主区 / 日志抽屉
│   ├─ src/components/         侧栏 / 工程头 / 音轨波形 / 阶段卡 / 卷帘 / 日志
│   ├─ src/composables/        useStudio（状态机 + SSE）、useAudio（播放器）
│   ├─ src/api.ts              与后端的 HTTP + SSE 契约
│   └─ smoke.mjs               UI 冒烟：构建产物 + jsdom + 真服务
├─ models/                     本地权重：SheetSage2 / MERT-v2-FullSong / Qwen3-ASR-* / Qwen3-ForcedAligner-0.6B
├─ samples/                    待转写的样例音频
├─ scripts/verify.py           验证脚本：跑通并给出可复现证据
├─ scripts/verify_webui.py     WebUI 端到端验证：真起服务、真跑两个模型、真下载 zip
├─ scripts/tempo_report.py     BPM 报告与干预
├─ scripts/quantize_real_check.py  量化在 output/ 全部真实工程上的效果表
├─ scripts/lyric_diagnose.py   填词质量诊断（丢掉 / 挤占 / 匹配方式分布）
├─ scripts/check_pipeline_contract.py  固定约定：raw_* 必须保留、填词基准随字/音而变
├─ scripts/probe_ref_align.py  对照实验：参考歌词喂对齐器能不能消零长度
├─ scripts/asr_demo.py         按已扒工程的区间跑歌词识别
├─ scripts/probe_asr.py        环境兼容性探针
├─ scripts/probe_romaji.py     日语罗马音探针（结论：模型不支持，只能本地后处理）
├─ output/<歌名>/              每次转写的完整产物 + summary.json
├─ reports/                    验证报告、卷帘预览图
├─ docs/                       接口与环境调研
└─ *.flac                      待转写的样例音频
```

## 环境

**本项目用自己独立的环境**（不共享 Anaconda base 的 site-packages）：`.venv` 是一个
**独立 venv**，torch / transformers / fastapi / libresvip 全部装在里面，
和 base 里的 conda / streamlit 这些互不影响。

本机实测可用的组合：

| 项 | 值 | 说明 |
|---|---|---|
| Python | **3.12.11** | 用 `uv` 提供；**不能用 3.13**——torch 的本地轮子只有 `cp312` |
| torch / torchaudio | **2.10.0+cu130** | 来自本地轮子 `E:\torch\CU130+PY312\`（`cp312-cp312-win_amd64`） |
| GPU 驱动 | 591.86（CUDA 13.1） | cu130 的轮子要这个级别；`torch.cuda.is_available()` 实测 True（RTX 3080） |
| transformers | **4.57.6** | 与 `qwen-asr` 要求的版本一致，两个模型共存于一个环境 |
| 其它 | huggingface-hub 0.36.2、numpy 2.5、scipy 1.18、soundfile、pretty_midi、mido、mir_eval | |
| ffmpeg | `D:\ffmpeg\bin` | `bootstrap.py` 会自动挂到 PATH |
| 模型权重 | `models/`（本地优先） | SheetSage2 / MERT-v2-FullSong / Qwen3-ASR-1.7B·0.6B / Qwen3-ForcedAligner-0.6B |

```powershell
# 建环境（Python 3.12 由 uv 提供；torch 用本地轮子，不从网上拉）
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install --python .\.venv\Scripts\python.exe `
    "E:\torch\CU130+PY312\torch-2.10.0+cu130-cp312-cp312-win_amd64.whl" `
    "E:\torch\CU130+PY312\torchaudio-2.10.0+cu130-cp312-cp312-win_amd64.whl"

# 项目本体 + WebUI + SVP 导出 + 歌词识别（[web] / [svp] 见 pyproject.toml）
uv pip install --python .\.venv\Scripts\python.exe -e ".[web,svp]" `
    "transformers==4.57.6" qwen-asr pytest requests psutil mir_eval nagisa sox

# 自检：应该打印 cuda 13.0 / True
.\.venv\Scripts\python.exe -c "import torch,libresvip,transformers; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
```

> 换台机器时 **torch 的轮子路径是外部依赖**（`E:\torch\CU130+PY312\`），没有就去
> [pytorch.org](https://pytorch.org) 按 CUDA 版本挑对应的 cu 轮子；`cp312` 这一条别搞错。
>
> `bootstrap.py` 会设置 `KMP_DUPLICATE_LIB_OK=TRUE`。独立环境里通常没这个冲突，
> 但指向 conda 的 `libiomp5md.dll` 时仍需要它（不设会在 `import torch` 直接以
> `OMP: Error #15` 中止）。

## 用法

转写：

```powershell
.\.venv\Scripts\python.exe -m sheetsage2_webui.cli "凤凰传奇 - 光芒.flac" --out-root output

# 常用参数
--device cuda|cpu|auto     # 默认 auto
--dtype bf16|fp32          # 默认 bf16（仅 CUDA 生效，CPU 强制 fp32）
--preset default|paper     # default 用 overlap=200 / lookahead=100
--max-seconds 30           # 只转前 30 秒，调试用
--local-files-only         # 只用本地缓存，不联网

# BPM 干预
--bpm 104                  # 手动指定速度，记录模型网格相对它的漂移
--bpm-snap integer         # 把估计速度吸附到整数 / half 半整数
```

验证（这是"结果可信"的依据）：

```powershell
.\.venv\Scripts\python.exe scripts\verify.py
.\.venv\Scripts\python.exe scripts\verify.py --skip-slow   # 跳过重载/离线/CPU
.\.venv\Scripts\python.exe scripts\verify.py --reuse       # 复用已有产物
```

单元测试（不加载模型，只测产物解析规则）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

在代码里调用：

```python
from sheetsage2_webui import SheetSage2Engine, EngineConfig

engine = SheetSage2Engine(EngineConfig(device="auto", dtype="bf16")).load()
result = engine.transcribe("song.flac", "output/song")
print(result["abc"][:200], result["melody_notes"], result["peak_gpu_mib"])
```

## WebUI（工作台：丢音频 → 选参数 → 扒谱 + 歌词识别 → 下载产物）

界面就是工作区根目录那份 `静态页面.html` 原型接到真数据上：侧栏工程列表、音轨真波形与播放、
两张阶段卡（扒谱 SheetSage2 / 歌词识别 Qwen3-ASR）、结果卷帘（小节线 / 段落 / 和弦 / 歌词）、
日志抽屉。规格与"原型元素 → 组件 → 数据来源"的对照表见
[`docs/WebUI规格.md`](docs/WebUI规格.md)。

**最省事：双击工作区根目录的 `webui.bat`**（自检环境 → 没构建过前端就自动构建 → 起服务 →
开浏览器；启动时选默认 / 4G 特供两种模式）。

```bat
webui.bat                   :: 首选 8000，启动时选模式
webui.bat 8010 2            :: 换端口 + 直接进「4G 特供」模式
```

> 端口只是**首选**：被别的程序占着、或落在系统保留段（`WinError 10013`）时会自动换一个，
> 并把"被谁占了"打出来（例：`! 端口 8000 被 Manager.exe（PID 5972）占用，自动改用 64316`）。
> `webui.bat` 本身**只允许 ASCII**：cmd.exe 按字节偏移重读批处理，里面出现中文会把行切断
> 当命令执行；中文全在 `scripts/launch.py` 里打印（Python 走 WriteConsoleW，任何代码页都不乱码）。

手动来一遍也行：

```powershell
# 一次性：装后端依赖 + 构建前端
#   [web] = WebUI 后端（fastapi/uvicorn/python-multipart）
#   [svp] = 导出 SVP（libresvip）；不装只是导不了 SVP，其余照常
.\.venv\Scripts\python.exe -m pip install -e .[web,svp]
cd web; npm install; npm run build; cd ..

# 起服务 -> http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m sheetsage2_webui.webui
```

- **下载默认是 SVP**：工程头主按钮「下载 SVP」→ 弹窗勾选要装进去的内容
  （**人声默认勾 / 乐器默认关 / 和弦默认关**）；整包 zip 与单个产物退到旁边小箭头 ▾ 的菜单里。
  导出的是 **Synthesizer V Studio 工程，带变速（逐小节 tempo）与歌词**。
  - **和弦一轨只能有一个音**：SVP 的轨是单声部的，而 `chords.mid` 每个和弦有 4 个同时发声的音，
    所以勾和弦会自动**拆成 4 轨**（第 1 轨是低音）。
  - 乐器轨 / 和弦轨在 SVP 里只能是"唱歌轨"（SV 只有唱歌轨与音频轨两种），会用默认音节
    （中文 `啦` / 日语 `ラ` / 其它 `la`）被唱出来，当参考旋律用。
  - 接口层保持原语义：`GET /api/jobs/{id}/download` 不带参数**仍然是整包 zip**，
    导 SVP 走 `?kind=svp&parts=...`。选型与实测数字见 [`docs/SVP导出调研.md`](docs/SVP导出调研.md)。
- **丢音频不立刻开跑（门禁）**：上传只把工程建出来（状态 `draft`，两个阶段都 `idle`），
  参数面板这时才露出来；用户点「开始扒谱」/「开始识别歌词」才进队列。想一条龙就走
  「⏵ 自动提取」（走后端 `/chain`）。**一次拖两个文件也行**——认名字（干声/vocal/人声）优先，
  否则第一个当混音原曲、第二个当干声。
- **参数面板照着原型做成可调的**（`PATCH /api/jobs/{id}/options`，取值表在 `/api/env.params`）：
  - 扒谱：**精度** BF16 快速 / FP32 精确（= 计算精度 `dtype`，只影响 CUDA 上的计算；
    权重精度仍是启动开关 `--engine-weights`）、**设备** 自动·GPU / CPU、
    **范围** 整首 / 前 30 秒 / 前 60 秒、**量化** 128 分三连网格 / 不量化；
  - 歌词：**模型** 1.7B / 0.6B（可选项来自 `models/Qwen3-ASR-*` 的兄弟目录）、
    **音源** 干声（推荐）/ 混音原曲、**段落** 只跑唱段 / 全部段落、
    **语言** 下拉框 自动/中文/英语/日语/粤语/韩语。

  比原型多的几处：**量化**（128 分三连网格 / 不量化）、**段落**（只跑唱段 / 全部段落）、
  **语言下拉框**（原型只有 4 个语言按钮）。
  改了参数 = 换了口径，**旧结果作废**：
  改扒谱参数会让谱面与歌词一起退回"待开始"，改歌词参数只作废歌词；对应阶段正在跑时改不了（409）。
- **旋律音高修正不做成参数**：模型的旋律整体高一个八度（和弦轨是准的），一律降一个八度就完了
  ——虚拟歌姬本来也不会按原调调音，没必要让用户选（命令行调试仍可用 `--pitch-shift`）。
- **4G 特供模式把参数锁死**（`--lock-params`）：服务端定死安全组合，面板只读，
  `PATCH /options` 一律 409。默认模式下参数全部可调。
- **歌词音源：选干声但没传就自动回退。** 干声那行可以直接上传 / 更换 / 移除——**内容不校验**，
  故意把混音原曲（甚至别的歌）传进去也允许；界面上只写一句"本次音源：…"。
- **工作流是 扒谱 → 歌词，按钮有门禁**：扒谱进行中「开始识别歌词」置灰；没扒谱就点会提示
  "请先完成扒谱"；「⏵ 自动提取」是唯一的串联入口（走后端 `/chain`）；有谱但没填词的工程直接放开。
- **两个阶段可以连着跑，也可以单独跑/重跑/停止**；重跑扒谱或换干声都会让已识别的歌词作废
  （音符位置 / 音源变了）。
- **两个模型轮流用显存**：切到歌词阶段前释放 SheetSage2（10G 卡上 0.5G + 6.3G 一起待着太危险），
  代价是下首歌重跑扒谱要重新加载（本机实测 14.7s）；显存富裕就加 `--keep-models`。
- **显存不够也想两个模型常驻**：`--asr-max-vram 3GiB` 让歌词识别把放不下的层留在内存里
  （实测 1.7B：峰值显存 5996→5039MiB、转写 5.6→10.5s、**识别文本逐字一致**）。
- **4G 显存 + 16G 内存在两阶段解耦时能跑**：`--engine-weights bf16`（扒谱 3379→2088MiB，
  出谱签名与 fp32 一致）+ `--asr-max-vram 1GiB`（歌词 2265~2332MiB）+ `--asr-max-slice 30`，
  下限约 2.3GB；但不能 `--keep-models`（相加 4.4G）。详见
  [`docs/WebUI规格.md`](docs/WebUI规格.md) §7 与 `scripts/probe_asr_offload.py`。

开发前端（HMR，`/api` 自动代理到 8000）：

```powershell
.\.venv\Scripts\python.exe -m sheetsage2_webui.webui --port 8000   # 终端 1
cd web; npm run dev                                                # 终端 2 -> http://127.0.0.1:5173
```

改完前端要 `npm run build` 才会进到后端静态目录；后端没找到前端产物时会照常起服务，
根路径回一段提示告诉你去 build。

```powershell
# 真机端到端（真起服务 + 真跑两个模型 + 真下载 zip，45s 切片约 40s）
.\.venv\Scripts\python.exe scripts\verify_webui.py
.\.venv\Scripts\python.exe scripts\verify_webui.py --transcribe-only   # 只跑扒谱
.\.venv\Scripts\python.exe scripts\verify_webui.py --keep              # 验证完留着，自己点页面

# UI 冒烟：构建产物 + jsdom + 真服务（要有一个跑完的工程）
cd web; npm run smoke -- http://127.0.0.1:8123
```

**工程 = `<项目根>/output/<歌名>/`**（和命令行跑出来的完全同形状）：产物（`project.json` 等 30 个）
直接躺在工程根，WebUI 自己的东西在 `job.json` + `.webui/`（`input/` 原音频、`peaks-*.json` 波形缓存、
`export/` 缓存的 `.svp`、`bundle.zip` 懒生成）。**启动时扫描 `output/` 重建列表**，所以重启不丢工程；
命令行走过的目录也会出现在侧栏里（标记「已有产物」，只读：能看卷帘 / 下载 / 导 SVP，不能重跑）。

## 一键流水线（WebUI 的接入点）

用户不调任何参数：丢音频 → 出 tick 域工程 + 报告。所有判断自动完成并写进 `report.json`。

```powershell
.\.venv\Scripts\python.exe -m sheetsage2_webui.pipeline "song.flac" --out output/song
```

```python
from sheetsage2_webui.pipeline import run

payload = run("song.flac", "output/song")
payload["decisions"]   # 拍号 / 弱起 / 速度 / 来源 / 是否恒速 / 调性 / 往返误差 / 量化统计
payload["project"]     # tick 域工程：小节表 + tempo_map + 调性 + 段落 + 和弦 + 音符 + 量化
payload["report"]      # 每个决策的依据
```

可选覆盖：`--bpm`、`--pickup actual|padded`、`--tempo-mode section|measure|single`、
`--no-quantize`、`--simplicity 0~5`。其中**量化开/关**（`--no-quantize`）已经在 WebUI 的参数
面板上放开（「量化」一行）；其余几项只在命令行可用。

**时间真源是小节表，不是 BPM**：

```
playback.json.measures[] = [{start, end, score_start, score_end}, ...]
        ↓ 逐小节分段线性
秒 ←──────────────────────────────→ tick（PPQ 1920）
```

恒速曲、变速曲、弱起小节走同一套代码，实测往返误差 ~1e-14 s。BPM 只用于显示和写
`set_tempo`。**速度表默认按段落拟合再合并**——歌里的速度变化几乎总在段落边界上，
`皮囊` 因此从 36 个结构段塌成 3 个速度段，其余歌曲各 1 段：

| 歌曲 | 速度段 |
|---|---|
| 光芒 | 104.002 |
| 朋友的酒 | 119.995 |
| 相思 | 100.001 |
| 皮囊 | 124.311 → **109.994**（2:52–3:47）→ 124.069 |

**音符时值量化默认开启**：tick 域的音符会吸到网格（`quantize.smart_quantize_musical`，
整数 DP），这样卷帘画得出格子、DAW 也导得进去。量化**只动 tick**，
小节表仍是唯一时间真源。真实工程实测（13 工程 / 26 轨道 / 6927 音）：

| 工程 | 拍号 | 量化前在网格上 | 量化后 | 平均位移 / 最大 |
|---|---|---|---|---|
| 忘川风华录（人声） | 4/4 | 98.3% | **100%** | 0.000s / 0.007s |
| 光芒（人声） | 4/4 | 1.2% | **100%** | 0.002s / 0.007s |
| 皮囊（人声） | 4/4 | 42.6% | **100%** | 0.004s / 0.017s |
| 壁上观（人声） | 4/4 | 8.8% | **100%** | 0.004s / 0.010s |
| 巴拉莱卡（人声） | 2/4 | 34.9% | **100%** | 0.002s / 0.010s |
| 相思（人声） | 6/8 | 54.2% | **100%** | 0.002s / 0.010s |

26 条轨道全部成功，没有一条量化失败；**最大位移从 0.86s 降到 0.028s**（位移统计写在
`project.json` 的 `quantize_shift_seconds` 里）。口径有三处要点：

1. **位移按"一拍"归一，不是按小节**——按小节归一时位移代价被摊薄 16 倍，节拍阶梯会把
   本来就在网格上的音也整体推走（《旷古回响》656 个音里 350 个、最大 1 拍）。
2. **网格步长 = 一拍 / 96**（PPQ1920 时 20 tick），与拍号无关。写成"一小节/32"时
   6/8 会算出 180 tick，与模型 onset 的 60 tick 格子对不上（《相思》128/214 个音
   因此被搬 ≥180 tick）。
3. **阶梯认三连音 + 单音位移上限 40 tick**。只做前两条时，DP 会"挑最好的档"而不是
   "就近吸附"：《巴拉莱卡（The Rod）.mid》569 个音里 **479 个被搬走**、三连音全被
   抹平。加上三连音两档（1/3 拍 ↔ 8 分、1/6 拍 ↔ 16 分）与位移上限之后，
   直线 8/16/32 分与三连音音型**全部原样通过**。

算法规格与实测见 [`docs/音符量化规格.md`](docs/音符量化规格.md)
（§3 口径、§7 网格与 SynthV SNAP 对照、§8 三连音、§9 位移上限）。

量化会把量化前的值一并存进音符（`raw_tick` / `raw_dur`），**填词对齐必须用它们**：
逐字时间戳是沿模型自己的音符时间轴产生的，量化把 tick 吸到网格后音符的秒位置就变了，
字号时间戳随之对不上——实测"看不到的字"《相思》18 → 42、《光芒》29 → 60，
改用 `raw_*` 后完全恢复到 18 / 29。

## 歌词识别（Qwen3-ASR + 逐字对齐）

扒谱完成后，用 SheetSage2 的 `structure.lab` 段落切音频，逐段跑 Qwen3-ASR，
再用 Qwen3-ForcedAligner 拿逐字时间戳。切片是为了**省显存**（峰值随长度线性增长，
约 10MB/s）并绕开对齐器的 5 分钟上限。

**切片是按"识别出来的段落"切的**，不是按固定秒数硬切——段落边界本来就是音乐上的自然切点，
切在字中间更糟。`asr.sections_from_lab()` 依次做五件事：

1. **标签翻中文**：`sections.SECTION_LABELS`（`verse→主歌`、`pre-chorus→预副歌`…，
   认不出来的原样保留，不猜）；
2. **合并相邻同名段**：模型的标签是**按乐句**给的，`structure.lab` 里到处是 `verse verse` /
   `chorus chorus`，不合并卷帘上就是一串重复块（实测《2020原唱》28 行 → 10 段）；
3. **跳过前奏 / 间奏 / 尾奏**（参数「段落」= 只跑唱段，默认）：这些段落一般不唱，
   实测省掉 25% 左右的音频（《光芒》195.2s → 146.4s、《壁上观》284.0s → 211.3s）；
4. **打包**：相邻短段攒到 `--asr-max-slice`（默认 90s，4G 模式 30s）以内；
5. **超长再切开**，丢掉 < 8s 的尾巴。

段落的边界与"跳过了哪几段"都记在 `stages.lyrics.detail` 和 `lyrics.json` 里。
`structure.lab` 本身**一个字节都不改**（它是模型原始产物），中文只出现在我们工程化之后的
`project.json` / 卷帘 / 日志里；速度拟合的输入也照旧用原始行（见 `docs/BPM算法规格.md`）。

```powershell
# 先装（依赖 transformers==4.57.6，与 SheetSage2 完全重合，不会冲突）
.\.venv\Scripts\python.exe -m pip install qwen-asr

.\.venv\Scripts\python.exe scripts\asr_demo.py "凤凰传奇_-_光芒"
.\.venv\Scripts\python.exe scripts\asr_demo.py "凤凰传奇_-_光芒" --model Qwen3-ASR-1.7B
```

```python
from sheetsage2_webui.asr import AsrConfig, AsrEngine, sections_from_lab

text = Path("output/song/structure.lab").read_text("utf-8")
sections = sections_from_lab(text, skip_instrumental=True)   # 跳过前奏/间奏/尾奏
engine = AsrEngine(AsrConfig(asr_model="models/Qwen3-ASR-0.6B",
                             aligner_model="models/Qwen3-ForcedAligner-0.6B"))
run = engine.transcribe_sections("samples/song.flac", sections)
engine.release()        # 用完把显存还回去
print(run.text, run.chars[:3], [(s.label, s.seconds) for s in sections])
```

实测（《光芒》，3 个切片）：0.6B 峰值 3904MiB、1.7B 峰值 6299MiB，转写都约 25s。
**1.7B 多花 2.4GB 换来专有名词全对**（布达拉宫 / 喜马拉雅山 / 格桑花），默认建议 1.7B。

## 把歌词填到音符上

`lyrics.assign_lyrics()` 按逐字时间戳往音符上挂字：

1. 一个字的时间窗是 `[s, e]`，挂到**起点落在 `[s − 容差, e)` 内的第一个音**；
   没有就挂到重叠最多的那个音。
2. 同一个字范围内的**后续**音符写转音标记：中文/日文 `-`，英文后续音节 `+`。
3. 没对上的音保持空串，丢掉的字进报告。

**顺序很关键**：必须先把所有字的"头"定下来，再补转音标记。否则处理第一个字时就会
贪心地把下一个音标成 `-`——实测《光芒》开头会变成 `曾 - 天 空`，"经"被吃掉。

```powershell
# 一条命令：扒好谱 → 跑歌词识别 → 填进 project.json
.\.venv\Scripts\python.exe scripts\asr_demo.py "凤凰传奇_-_光芒" --model Qwen3-ASR-1.7B --fill
```

```python
from sheetsage2_webui.lyrics import fill_project

result = fill_project(project, run.chars, track_id="vocal")
# result["fill"] -> {assigned, melisma, unmatched_notes, dropped, ...}
```

## 本地权重（models/）

`models/` 下放本地权重后**优先使用**，仓库 id 只是兜底；`HF_HUB_OFFLINE=1` 下验证过
可以完全离线加载（SheetSage2 0.22GB + MERT 2.36GB + Qwen 三件套）。

> 一个坑：transformers 4.57.6 对**本地目录**走 `trust_remote_code` 时，**只复制当前那一步
> 需要的那几个 `.py` 加它的直接相对导入**，不会递归。运行到后面再 import 别的模块就会
> `FileNotFoundError`（实测缺 `chord_spelling_sheetsage2.py`）。`bootstrap.sync_remote_code_cache()`
> 会在加载前把本地目录的 `.py` 全量补齐到 `HF_MODULES_CACHE/transformers_modules/<名字>/`。
> 用仓库 id 加载没有这个问题，因为那时的缓存是整个仓库。

上层服务直接调用的单一入口：

```python
from sheetsage2_webui.service import transcribe_to_directory

payload = transcribe_to_directory("song.flac", "output/song", model=engine)
```

---

## 打内测包（分发给同好）

给"没装过 Python、也没装过 ffmpeg"的人用的**解压即用**包：随包带**便携 CPython 3.12 + 裁剪版
静态 ffmpeg**，torch 与模型首次运行时按 `downloads.json` 直链下载（支持续传 + sha256）。

```powershell
.\.venv\Scripts\python.exe scripts\build_alpha_package.py     # -> dist\SheetSage2-Studio-alpha3.zip
```

打出来的包 **24.9 MB**（3616 个文件，解压 65.0 MB），内测者拿到后：

1. 解压到无中文路径、留 20 GB 空间；
2. 双击 `webui.bat` —— 首次运行自动建环境、下 torch（1.8 GB）与模型
   （**默认最小集 6.0 GB**；**1.7B 识别模型是可选**，+4.5 GB，随时 `setup.bat --with-1.7b` 补）；
3. 浏览器自动打开。

细节、实测记录与还没做的部分见 [`docs/打包分发待办.md`](docs/打包分发待办.md) §5；
给内测者的话术见 [`README-internal-test.md`](README-internal-test.md)。

> **ffmpeg 是随包的**：`bootstrap.find_ffmpeg()` 的顺序是
> **环境变量 → 随包自带 → PATH → 开发机固定路径**（本机的 ffmpeg 在 `D:\ffmpeg\bin`，
> 和项目不在一起，所以开发机那两条只是兜底）。
