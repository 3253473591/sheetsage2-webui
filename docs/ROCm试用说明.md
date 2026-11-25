# A 卡（ROCm）试用说明

> 给帮忙测试的网友看的，一页就够。目标是**先跑通一首歌**，速度慢一点没关系。详细背景见 [`ROCm加速调研.md`](ROCm加速调研.md)。

## 0. 需要什么

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 64 位 |
| 显卡 | AMD RX 7000 / 9000 系，或 Radeon PRO（在 [AMD 兼容列表](https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2/docs/compatibility/) 里） |
| 驱动 | AMD Adrenalin 最新版（装完重启一次） |
| 磁盘 | 额外约 6 GB（`.venv-rocm` 3.8 GB + AMD 轮子 1.8 GB，轮子装完可删） |
| 网络 | 第一次要能连 `repo.radeon.com`（约 2 GB，可断点续传） |

> **国内没有 `repo.radeon.com` 的镜像**：清华/中科大都没镜像它，南大镜像的是 PyTorch 官方 whl（其中的 rocm 轮子是 **Linux** 版，Windows 用不了）。所以这 2 GB 只能直连 AMD。太慢的话按下面的「手动下载」做。

### 下载太慢 / 总是断？

脚本会把 4 个文件的 URL 打出来。拿别的工具（浏览器、IDM、或者能上外网的机器）下好，放进项目根的 `wheels-rocm\` 目录，文件名**照下面的写**，然后重跑 `01_install.bat rocm`——脚本会校验大小、跳过已下好的：

```
rocm_sdk_core-7.2.1-py3-none-win_amd64.whl                    645 MB
rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl        490 MB
torch-2.9.1+rocm7.2.1-cp312-cp312-win_amd64.whl               821 MB
torchaudio-2.9.1+rocm7.2.1-cp312-cp312-win_amd64.whl          0.5 MB
```

URL 前缀统一是 `https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/`，注意 URL 里的 `+` 要写成 `%2B`（本地文件名用 `+`，别照抄 `%2B`）。

> RDNA2 及更老的卡（RX 6000 系）官方不在列表里，**大概率跑不起来**；想试也可以，但失败是常态，别当成 bug。

## 1. 装

解压内测包，双击 **`03_AMD_ROCm.bat`**（等价于在命令行跑 `01_install.bat rocm`）。它会：

1. 复制现有 `.venv` → `.venv-rocm`（原环境不动，随时能回退）；
2. 下载 AMD 的 4 个轮子（约 1.8 GB，断了重跑会接着下）；
3. 把 CUDA 版 torch 换成 `torch 2.9.1+rocm7.2.1`；
4. 自检并写 `.env-backend`。

看到 `ROCm step done` 就好了。**没装完也不要紧**：重跑同一条命令会继续；彻底乱了就删掉 `.venv-rocm` 重来。

## 2. 跑

双击 **`00_start.bat`**。启动横幅里应该出现：

```
[1/3] 解释器 OK        .venv-rocm\Scripts\python.exe
      GPU 后端：AMD ROCm（AMD Radeon RX 7900 XTX，hip=7.2.1）
```

然后页面上：

1. 顶栏预检应该写「检测到显卡（**AMD ROCm**）」；如果写的是 CPU，先别跑，把横幅发回来；
2. 拖一首**短一点**的歌（3~4 分钟），点「自动提取」；
3. 第一次跑可能**多花几分钟**（MIOpen 在 autotune），之后会快；
4. 跑完能导出 SVP、能进 SynthV 播放就算通过。

## 3. 要发回什么

出问题的话，这几样最有价值：

1. 控制台窗口**最后 40 行**（尤其是 `hip`、`HSA`、`MIOpen`、`out of memory` 这类字眼）；
2. 页面顶栏「导出诊断包」的 zip（里面有 `env.json`，会带 `backend.kind` 和 `hip`）；
3. 这个命令的输出：

```powershell
.\.venv-rocm\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.hip, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

## 4. 已知限制（不是 bug）

- **torch 比 N 卡版低一个小版本**（2.9.1 vs 2.10.0）：Windows 侧 AMD 目前最高就这一版；
- **不开 flash-attn**：用 sdpa，结果一致，速度取决于卡；
- **bf16**：卡不支持时自动落 fp16（横幅/日志里能看到实际精度）；
- 首跑慢：MIOpen 第一次要建立算子库缓存。

## 5. 回退

删掉 `.venv-rocm\` 和 `.env-backend` 两个东西，`00_start.bat` 就回到 `.venv`。或者临时 `set SHEETSAGE_BACKEND=cuda` 再启动。
