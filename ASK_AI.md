# 遇到问题，让 AI 帮你查

这个工具的环境是**随包给的**（便携 Python + 依赖 + 模型 + ffmpeg），所以绝大多数
「装不上 / 找不到库 / 版本不对」的经典问题都不会发生。剩下的问题基本是三类：
**解压不完整**、**显存/驱动**、**网络**（只有轻量包第一次安装时）。

下面这段话**直接整段复制**给任意 AI（ChatGPT / Claude / DeepSeek / 豆包 / 通义都行），
再把 `环境快照.txt` 的内容粘在后面：

---

```
我在 Windows 上用「SheetSage2 扒谱」这个整合包（离线音乐转乐谱工具，把歌
扒成 SVP/MIDI，可在钢琴卷帘上改音符与歌词），遇到了问题：

【我做了什么】
（例：解压后双击 start.bat / 拖进一首 mp3 / 点了开始扒谱）

【我看到什么】
（例：黑窗口报错原文 / 页面上的红字 / 进度卡在 47% 不动 / 浏览器打不开）

【环境快照】
（把 selfcheck.bat 生成的「环境快照.txt」全文粘在这里）

请帮我判断是哪一环出了问题，并给出**具体到命令和文件路径**的修复步骤。
如果需要我执行命令，请写成 Windows CMD 能直接粘贴的形式。
```

---

## 给 AI 的技术背景（这段也可以一起粘过去）

> 以下是这个包的结构，方便 AI 定位问题，**测试者不用看懂**。

**程序结构**

* 解压后的根目录记作 `<PKG>`。
* `start.bat` 是启动器，做三件事：找到解释器 → 调 `python -m app.server` → 结束时保持窗口。
  它**必须是纯 ASCII**（cmd.exe 按 GBK 解析 .bat，中文会变乱码当命令执行），
  所有中文输出都由 Python 打印。
* 解释器查找顺序：`<PKG>\runtime\python\python.exe` → `<PKG>\runtime\python\Scripts\python.exe`
  → `<PKG>\.venv\Scripts\python.exe`。全量包命中第一个。
* `runtime\python` 是**便携版 CPython 3.12.11**（来自 python-build-standalone，
  随包分发）。依赖直接装在它自己的 `Lib\site-packages` 里，`sys.prefix` 就是该目录，
  **因此整个文件夹可以随意搬动/改名**。uv 的 `Lib\EXTERNALLY-MANAGED` 标记已被删除，
  所以可以正常 `runtime\python\python.exe -m pip install ...`。
* `app\server.py` 是 FastAPI 服务，**只监听 127.0.0.1**，默认端口 8777
  （`settings.json` 可改；`settings.json` 不存在时用默认值）。
* 真正的推理在**子进程** `python -m app.worker` 里跑，父进程通过 stdout 的
  `@@P/@@R/@@E` 前缀行读取进度；「停止」用 `taskkill /T /F` 杀整棵进程树。

**路径解析**（`app\config.py`）——积分三级：环境变量 → 包内相对路径 → 开发机历史路径

| 资源 | 环境变量 | 包内位置 |
| --- | --- | --- |
| SheetSage2 权重 | `SHEETSAGE2_MODEL_DIR` | `models\SheetSage2` |
| MERT-v2-FullSong | `MERT_MODEL_DIR` | `models\MERT-v2-FullSong` |
| ffmpeg | `FFMPEG_BIN` | `runtime\ffmpeg\bin\ffmpeg.exe` |

必需的权重文件：`models\SheetSage2\model.safetensors`(218 MB) + `config.json`、
`models\MERT-v2-FullSong\model.safetensors`(2.4 GB)。缺任何一个，环境检测里那一条会
是**红叉（阻塞项）**，页面会禁止开始扒谱。

**设备选择**（`app\worker.py::pick_device`）——不需要用户操心

* `settings.json` 的 `default_device`：`auto`（默认）/ `cuda` / `cpu`。
* `auto`：`torch.cuda.is_available()` 为真就用 GPU，否则 CPU。
* 即使显式写了 `cuda` 但环境用不了，也会**降级 CPU 并在诊断里写明原因**。
* CPU 上自动把 `bf16` 换成 `fp32`（CPU 的 bf16 没有加速）。
* 模型加载阶段 GPU 抛异常时会**自动清理显存、退回 CPU 重试一次**，
  不会整个任务失败。
* 没有 N 卡**不是错误**：`envcheck` 里「CUDA 可用性」是黄色 warn，不是红色 fail。

**依赖**

* `requirements-base.txt`：69 个纯 PyPI 依赖（不含 torch）。
* torch 版本按显卡分：有 N 卡 `torch==2.10.0+cu130`（源 `https://download.pytorch.org/whl/cu130`），
  没 N 卡 `torch==2.10.0+cpu`（源 `.../whl/cpu`）。
* **torch 版本下限是 2.2，建议 ≥2.6**：
  * `transformers 4.57.6` 声明 `torch>=2.2`；`accelerate 1.15.0` 声明 `torch>=2.0.0`；
  * 运行时是 CPython 3.12，torch 从 **2.2.0** 起才有 cp312 的 Windows wheel；
  * numpy 钉在 2.5，**torch < 2.3 与 numpy 2.x 有已知 C-API 不兼容**；
  * **实测通过**：`2.6.0+cpu` 与 `2.10.0`（cu130 GPU/CPU）。
  * 这套代码里**没有任何 torch 版本断言**，所以版本不匹配通常表现为
    `ImportError`/算子报错，而不是"版本不支持"的友好提示 —— 遇到怪异的
    `RuntimeError: ... not implemented for 'BFloat16'` / numpy 相关报错时优先怀疑这里。
* **torchaudio 必须与 torch 版本完全一致**。
* 换版本：
  `runtime\python\python.exe tools\install_deps.py --torch-version 2.6.0 --torch-index https://download.pytorch.org/whl/cpu`，
  或装完后 `runtime\python\python.exe -m pip install torch==X torchaudio==X --index-url <源>`。
  注意 **cu130 源只有 torch ≥2.9**，装更老的 CUDA 版要配 `.../cu124|cu126|cu128`。
* 轻量包的安装器是 `tools\install_deps.py`，可重入：已装好的会跳过。
  常用参数：`--device cpu|gpu`、`--torch-version`、`--torch-spec`、`--torch-index <镜像>`、
  `--hf-endpoint https://hf-mirror.com`。
* 模型来自 `m-a-p/SheetSage2` 与 `m-a-p/MERT-v2-FullSong`（HuggingFace）。

**运行时常见检查命令**（在包根目录的 CMD 里跑）

```bat
runtime\python\python.exe -c "import torch;print(torch.__version__, torch.cuda.is_available())"
runtime\python\python.exe tools\env_report.py
runtime\python\python.exe -m pip list
```

**已知会踩的坑**

1. **ffmpeg 必须在 PATH 上** —— 这是最容易误判的一条。SheetSage2 的 vendored 代码
   `models\SheetSage2\audio_sheetsage2.py::load_audio` 里写死了
   `shutil.which("ffmpeg")` 和 `subprocess.run(["ffmpeg", ...])`，**只认 PATH**，
   不会用 `app\config.py::ffmpeg_path()` 解析出来的路径。
   所以 `app\config.py` 在 **import 期**会调用 `ensure_ffmpeg_on_path()`，把随包
   ffmpeg 的目录接上 PATH；`envcheck` 也改成以"PATH 可见性"为准。
   如果还是报 `RuntimeError: FFmpeg is required to read audio files`，说明
   `runtime\ffmpeg\bin\ffmpeg.exe` 真的不在了（常见原因：杀毒软件删除），
   重新解压或设环境变量 `FFMPEG_BIN` 指向任意 ffmpeg.exe 即可。
2. 直接删 `.wav` 的输入没问题，但 `mp3` / `flac` 走 ffmpeg 解码——ffmpeg 被删了就会失败。
3. 服务重启会丢任务注册表（内存态），旧任务不能恢复，要重新扒。
4. 同时只能跑**一个**任务（串行，避免显存爆掉）；重复提交会返回 409。
5. 试听是**客户端 Web Audio 离线渲染**，不依赖系统声卡驱动；没声音先看浏览器是否静音。
6. 超过 5 分钟的长歌、男女合唱的逐音归属都还在测试中，可能不准。

---

## 实在不行：最小化复现

把这几件事按顺序记录发出来，比「它不工作」有用一百倍：

1. `selfcheck.bat` 生成的 `环境快照.txt`
2. 你用的哪首歌（时长、格式、是不是纯音乐/DJ/干声）
3. 黑窗口里**最后 20 行**原文
4. `output\<任务目录>\summary.json`
5. 哪一步不对：没开始 / 进度卡住 / 报错 / 结果不对（音高、歌词、和弦…）
