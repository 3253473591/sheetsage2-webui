# ROCm 加速接入调研（与现有 CUDA / torch 共存）

> 结论先行：**能接，而且比预期近**——AMD 已经为 Windows 发官方 ROCm 轮子（`torch 2.9.1+rocm7.2.1` / cp312 / win_amd64），本项目代码对 `cuda` 的依赖很浅（ROCm 下 `torch.cuda.*` 照样可用）。
> 真正要准备的不是"改算法"，而是**三件事**：① 确认目标卡在官方支持列表；② 把 torch 拆成"两套环境 + 安装清单分支"，因为**同一个 venv 里塞不下 CUDA 与 ROCm 两份 torch**；③ 在 AMD 卡上重跑一遍验收（数值/显存/耗时）。
>
> 本文调研时间：2026-10-23。版本号以 AMD 仓库清单为准（带日期），下文链接部分被 Cloudflare 挡过（429），以仓库目录为事实来源。

---

## 1. 先厘清"共存"的三种含义

| 理解 | 能不能 | 做法 |
|---|---|---|
| A. 同一个 `.venv` 里同时装 CUDA 版和 ROCm 版 torch | **不能** | 两者包名都叫 `torch`，各自带 `libtorch_cuda` / `libtorch_hip` 与不同的 `c10` 后端；装第二个会覆盖第一个。一个进程只能有一个后端。 |
| B. 同一台机器、同一个项目，N 卡走 CUDA、A 卡走 ROCm，启动时选 | **能** | 两个环境：`.venv`（cu130）+ `.venv-rocm`（rocm7.2.1），`webui.bat` / `launch.py` 按参数或探测结果挑解释器。**推荐这条路。** |
| C. 一份安装包自动适配两种卡 | 能，但要多做一层 | 安装期探测显卡 → 选清单分支（见 §6.2）。代价：内测包体积/清单要分叉，`tools/bootstrap.py` 的 `manifest["torch"]` 结构要改。 |

> 顺带说明：机器上同时插 N 卡和 A 卡，也还是只能二选一（一个 torch 二进制只编了一种后端）。`HIP_VISIBLE_DEVICES` / `CUDA_VISIBLE_DEVICES` 只控制可见设备，不切换后端。

---

## 2. 官方轮子的现状（这是全部判断的地基）

### 2.1 Windows（本项目的主战场）

来源：[repo.radeon.com/rocm/windows/](https://repo.radeon.com/rocm/windows/) 与 [rocm-rel-7.2.1/](https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/)

| 发布 | 日期 | torch | 轮子 |
|---|---|---|---|
| 6.4.4 | 2025-09 | — | 早期预览 |
| 7.1.1 | 2025-11 | `2.9.0+rocmsdk20251116` | `cp312-win_amd64` |
| 7.2 | 2026-01 | `2.9.1+rocmsdk20260116` | `cp312-win_amd64` |
| **7.2.1** | **2026-03** | **`2.9.1+rocm7.2.1`** | **`cp312-win_amd64`** |

关键点：

1. **只有 `cp312`**——正好本项目锁的就是 Python 3.12（`docs/开发指南.md` §1），这条天生对齐。
2. 除了 `torch/torchaudio/torchvision`，还要装 AMD 的运行时包：`rocm_sdk_core`（约 640 MB）、`rocm_sdk_libraries_custom`（约 490 MB），编译/调试再加 `rocm_sdk_devel`（约 230 MB～2.4 GB）。**额外 2.5 GB 左右**（现有内测包要求 20 GB 空闲，够）。
3. 版本落差：本项目现在是 **torch 2.10.0+cu130**，Windows ROCm 侧最新只有 **2.9.1**。要接就得接受降一个小版本（本项目用的都是基础 API，理论上无痛，但要重跑 `transformers 4.57.6` + `qwen-asr` 的组合）。
4. 安装入口是 AMD 的官方索引/安装页：[Windows PyTorch 安装页](https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/install/installrad/windows/install-pytorch.html)、[AMD Software: PyTorch on Windows Edition 7.2 发布说明](https://www.amd.com/en/resources/support-articles/release-notes/RN-AMDGPU-WINDOWS-PYTORCH-7-2.html)。

### 2.2 Linux（若要"满血"）

[download.pytorch.org/whl/](https://download.pytorch.org/whl/) 下已有 `rocm7.0` / `rocm7.1` / `rocm7.2` / `rocm7.14` 四个索引，torch **2.11 / 2.12 / 2.13 / 2.14** 都有 `manylinux_2_28_x86_64` 轮子（cp310–cp315）。Linux 上可以**不低于**现有 2.10，这是 Windows 路线给不了的。

代价：整套部署要重做——`webui.bat`、`setup.bat`、`tools/bootstrap.py`（`runtime/python`、`ffmpeg.exe`）、`bootstrap.py` 里 `D:\ffmpeg\bin` 这类硬编码（`src/sheetsage2_webui/bootstrap.py:30-33`）都要有 Linux 版。

---

## 3. 必须先确认的三件事

1. **卡在不在支持列表**。Windows 侧官方矩阵限定型号（RX 7000 / 9000 系桌面卡、Radeon PRO、Ryzen AI 300 / Max 核显这一类）；RDNA2（gfx1030）及更老通常不在列。不在列就只能 `HSA_OVERRIDE_GFX_VERSION` 硬顶——**能跑是运气，不能跑是常态**，别把它当方案。矩阵入口：[Windows 兼容性](https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/compatibility/)。
2. **系统**。Windows 只能是 torch 2.9.1；要 2.10+ 只能 Linux。这个决定直接决定工作量（§7）。
3. **是否保留 N 卡路径**。保留 → 双环境 + 后端识别；整机切 AMD → 只换清单，代码改动可以更少。

---

## 4. 准备清单（Windows 原生路线）

| 项 | 要求 | 备注 |
|---|---|---|
| 显卡 | 在 AMD Windows 兼容列表内 | 第一优先级，先查 |
| 驱动 | 对应 ROCm 版本的 Adrenalin | 版本号按 AMD 安装页，别用老驱动硬试 |
| 系统 | Windows 10/11 64 位 | 与现在一致 |
| Python | **3.12**（cp312） | 项目本来就是 3.12，且"不能用 3.13" |
| VC 运行库 | MSVC 2015–2022 Redistributable | ROCm DLL 依赖 |
| 磁盘 | 额外 ~3 GB（torch 820 MB + rocm_sdk 约 1.1 GB + 解压） | 内测包 20 GB 预算仍够 |
| 网络 | 首次装要能到 `repo.radeon.com` | 国内可能慢，考虑镜像/离线轮子（`downloads.json` 那套下载器正好能复用） |
| 环境隔离 | **单独的 `.venv-rocm`** | 见 §1-B |

安装自检（期望：版本带 `rocm`、`hip` 有值、`is_available()` 为 True、卡名是 AMD）：

```powershell
.\.venv-rocm\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.cuda, torch.version.hip, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# 期望类似：2.9.1+rocm7.2.1 None 7.2.x True AMD Radeon RX 7900 XTX
```

> 注意 `torch.version.cuda` 会是 `None`——现有自检脚本（`tools/bootstrap.py:259-274`、`scripts/verify.py:199-221`）就是打它的，不改会显示成"CUDA None"让人以为坏了。

---

## 5. 代码依赖盘点：哪些天然成立、哪些必须动

本项目对 `cuda` 的用法基本都在 `torch.cuda.*` 这层命名空间里，而 ROCm 构建把 HIP 映射到同一个命名空间，所以**大部分代码不用改**：

| 位置 | 用法 | ROCm 下 |
|---|---|---|
| `engine.py:156-158` | `auto` → `"cuda" if torch.cuda.is_available()` | ✅ 成立（HIP 设备也报 True） |
| `engine.py:184` | `model.eval().to("cuda")` | ✅ 成立 |
| `engine.py:258-260` | `startswith("cuda")` 决定 bf16 | ✅ 成立（但见 §6 的 bf16 风险） |
| `engine.py:267-272` | OOM 退回 CPU | ⚠️ 文案判据 `"cuda error: out of memory"`，HIP 报错是 `HIP error: out of memory`；`"out of memory"` 子串能兜住，但建议补 `hip` |
| `engine.py:274-278` | `torch.cuda.empty_cache()` + `.to("cpu")` | ✅ 成立 |
| `asr.py:127/159` | 默认 `cuda:0` + `device_map="cuda:0"` | ✅ 成立（accelerate 认 `cuda:N`） |
| `asr.py:300-302` / `341-348` / `401-402` | `empty_cache` / 峰值显存 | ✅ 成立 |
| `preflight.py:40-51` | `cuda_available()` + 「检测到显卡（CUDA）」 | ⚠️ 能用，**文案/语义要改**（见下） |
| `errors.py:159-169` | 按 `cuda ...` 关键词归类错误 | ❌ 要补 `hip` 关键词 |
| `tools/bootstrap.py:259-274` | 自检打印 `torch.version.cuda` | ❌ 要加 `hip` 分支 |
| `downloads.json:23-34` | 单一 CUDA 轮子清单 | ❌ 要分叉 |
| `models/SheetSage2/modeling_mert2.py:187-196` | 默认 `sdpa`，可选 `flash_attention_2` | ✅ 用 sdpa 就好；**别开 flash-attn**（ROCm 上要源码编译，且要求 fp16/bf16） |
| `bootstrap.py:30-33` | `D:\ffmpeg\bin` 硬编码 | 仅 Linux 路线需要改 |

---

## 6. 需要改的地方（按优先级）

### 6.1 后端识别（新增，约 30 行）

`preflight.cuda_available()` 扩成能区分后端，例如：

```python
def gpu_backend() -> dict:   # {"kind": "cuda" | "rocm" | "cpu", "name": ...}
    import torch
    if not torch.cuda.is_available():
        return {"kind": "cpu", "name": None}
    kind = "rocm" if getattr(torch.version, "hip", None) else "cuda"
    return {"kind": kind, "name": torch.cuda.get_device_name(0)}
```

调用点：`webui/preflight.py:118-127`（横幅文案）、`webui/__main__.py:141-149`（`device_hint`）、`webui/jobs.py:111`（「设备」选项的 label，"auto|cuda|cpu" 用户可以接受，但页面上应该写"GPU（NVIDIA / AMD）"）、`eta.py:44`（只区分 cpu/非 cpu，**不用动**）。

### 6.2 安装清单分叉（约 20 行 + 一份新清单）

`downloads.json` 从 `"torch": [...]` 改成 `"torch": {"cuda": [...], "rocm": [...]}`，`tools/bootstrap.py:148-190`（`step_torch`）按 `--backend` / 探测结果挑分支。ROCm 分支要下 3～4 个轮子（torch / torchaudio / rocm_sdk_core / rocm_sdk_libraries_custom），`sha256` 与 `bytes` 照现有格式补。注意 `step_torch` 末尾那句"反过来 pip 会去 PyPI 拖 CPU 版 torch"的护栏（`tools/bootstrap.py:184-187`）在 ROCm 分支要保留同样逻辑。

### 6.3 错误分类与 OOM 判据（10 行）

`errors.py:160-169` 加 `"hip out of memory"`、`"hip error"`、`"miopen"`、`"hsa"` 等关键词；`engine.py:41` 补 `"hip out of memory"`。否则 AMD 上炸显存时，用户看到的不是「显存不足 + 下一步」，而是「未知错误」。

### 6.4 文案（零风险，但用户体验全在这）

- 「检测到显卡（CUDA）」→「检测到显卡（NVIDIA CUDA）」/「（AMD ROCm）」
- 「有 N 卡的话检查驱动」→ 按后端给建议
- 日志里的 `device=cuda` 在 ROCm 上其实是 HIP——建议日志/诊断包同时打 `torch.version.hip`，出问题时省半小时
- 诊断包 `env.json`（`webui/jobs.py:2010` 附近）建议加 `hip` 字段

### 6.5 依赖组合重验

`transformers==4.57.6` + `qwen-asr` + `torch 2.9.1` 这套没在 AMD 上跑过。`qwen-asr` 目前要求 transformers 4.57.6（`docs/开发指南.md` §1），与 torch 版本无关，大概率没事；但 `accelerate` 的 `device_map="auto"` + `max_memory`（4G 特供模式，`asr.py:150-157`）在 ROCm 上要实测。

---

## 7. 坑与风险（按严重度排）

| # | 风险 | 影响 | 应对 |
|---|---|---|---|
| 1 | **卡不在支持列表** | 直接不成立 | 先查矩阵；不在列表就换卡或走 Linux |
| 2 | **Windows 只有 torch 2.9.1** | 版本从 2.10 后退 | 接受；或走 Linux 拿 2.10+ |
| 3 | **bf16** | RDNA2 无 bf16 指令；RDNA3/4 有但吞吐不如 fp16 | 探测 `torch.cuda.is_bf16_supported()`，不支持就落 `fp16`（`engine.py:258-260` 目前只判 cpu/cuda） |
| 4 | **SDPA 的 flash 后端** | 部分 gfx 需 `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1`，否则回退 math/efficient：**慢但结果对** | 首跑对比 CPU 结果；慢就开环境变量再测 |
| 5 | **不要开 `flash_attention_2`** | 需源码编译 flash-attn，且要求 fp16/bf16 激活（`modeling_mert2.py:187-193` 会直接抛） | 固定 `sdpa` |
| 6 | **MIOpen 首次 autotune** | 第一次跑可能多花几分钟，让人以为卡死 | 设 `MIOPEN_FIND_MODE=FAST` + `MIOPEN_USER_DB_PATH` 缓存 |
| 7 | **显存口径不同** | `max_memory` / 峰值统计在 ROCm 上语义一致但数值不同；APU 是共享内存 | 4G 特供模式要重标定 `max_vram` 档位 |
| 8 | **数值差异** | bf16/fp16 在 HIP 上累加顺序不同，扒谱的 BPM/量化可能微小偏移 | 验收用容差（音符数、和弦序列、BPM ±1），不是逐字节比对 |
| 9 | **"cuda" 这个词误导** | 日志、报错、用户都在说 CUDA，实际是 HIP | 见 §6.4 |
| 10 | **ROCm runtime 初始化慢 / 崩溃栈难读** | Windows 上首帧加载明显更久 | 启动横幅加"正在初始化 AMD 运行时" |

---

## 8. 最小验收（怎样算接成功）

1. **环境**：`torch.version.hip` 有值、`is_available()` True、卡名对得上（§4 自检命令）。
2. **单模型**：`scripts/verify.py` 能过（它读 `torch.cuda.is_available()` + `get_device_name`，`scripts/verify.py:199-221`）。
3. **端到端**：同一首参考曲，N 卡（或 CPU）与 A 卡各跑一次，比：
   - 音符数 / 和弦序列 / 段落数 / BPM（容差内）
   - 峰值显存（`peak_gpu_mib`）与阶段耗时
   - SVP 导出能进 SynthV 唱（这个跟后端无关，但必须确认没被连带破坏）
4. **显存边界**：4G 特供模式（`max_vram` 档位）在 A 卡上不 OOM，OOM 时能正确退回 CPU 重跑（`engine.py:224-237`）。
5. **错误路径**：故意用小显存档位触发 OOM，确认横幅是「显存不足 + 下一步」而不是「未知错误」。

---

## 9. 不建议的路线

| 路线 | 为什么不 |
|---|---|
| `torch-directml` | 早已停更（torch 2.4 时代），且本项目模型走 transformers + accelerate，DML 下 `device_map` 与 bf16 都不顺 |
| ZLUDA | 非官方、版本绑死、只对特定 torch 版本有效，不适合做分发 |
| WSL2 里跑 ROCm | 能用（官方有 WSL 兼容矩阵），但多一层：GPU 直通受驱动限制、文件 IO 走 9p 更慢、本项目一堆 Windows 专用脚本还要再分叉。**当备选，不做主线。** |
| Windows 上硬上 `HSA_OVERRIDE_GFX_VERSION` 支持老卡 | 见 §3-1，属于碰运气 |

---

## 10. 结论与建议

- **要回答"能不能"**：能。AMD 官方 Windows 轮子（torch 2.9.1+rocm7.2.1 / cp312）与本项目的 Python 3.12 天然对齐，代码层面对 `cuda` 的依赖是"命名空间级"的，不是"CUDA 专有 API 级"的。
- **要回答"要准备什么"**：卡（在列表内）+ 驱动 + 一个独立的 `.venv-rocm` + 一份 ROCm 安装清单 + 6 处小改（后端识别、清单分叉、自检、错误分类、文案、bf16 探测）+ 一次 AMD 上的对照验收。
- **工作量估计**：只做"AMD 用户能跑"（Windows 原生、双环境）——**1～2 天**；要做到"和 N 卡同等体验"（含 Linux 满血版、打包分叉、CI）——**再加 3～5 天**。
- **建议先做**：拿一张支持列表内的卡，用**独立 venv 手工验证**（不碰现有 `.venv`），跑通 §8 的第 1～3 步再决定是否进代码。**在没验证过 SDPA 在目标 gfx 上的速度之前，不要承诺"加速"。**

---

## 11. 待确认

1. 目标机器是哪张 AMD 卡（型号 / gfx）？在不在 [Windows 兼容列表](https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/compatibility/)？
2. 目标是"只想让 A 卡也能用"，还是"A 卡要达到 N 卡相当速度"？后者基本要走 Linux。
3. 这台机器还要不要再跑 N 卡（决定 §1 的 B 还是 C）。
4. 是否接受为了 Windows 把 torch 从 2.10 降到 2.9.1。
