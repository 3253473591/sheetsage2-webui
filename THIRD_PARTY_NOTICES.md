# 第三方组件与许可 / Third-party notices

本仓库**只包含本项目原创的代码**（MIT，见 `LICENSE`）。下面的组件各自归原作者所有，
许可条款依然约束你对它们的使用。**本仓库不分发模型权重，也不分发 ffmpeg 二进制。**

> ⚠️ **一句话结论**：模型权重（SheetSage2、MERT-v2-FullSong）是
> **CC BY-NC 4.0 —— 仅限非商业用途**。
> 自己学习、研究、扒自己的歌没问题；**不能拿去卖、不能用于商业发行**。

---

## 1. 模型权重（不在本仓库内）

| 组件 | 来源 | 许可 | 获取方式 |
| --- | --- | --- | --- |
| **SheetSage2** | https://huggingface.co/m-a-p/SheetSage2 | **CC BY-NC 4.0** | `python tools/download_models.py` |
| **MERT-v2-FullSong** | https://huggingface.co/m-a-p/MERT-v2-FullSong | **CC BY-NC 4.0** | 同上 |

* 两个仓库都是 **gated**：需要先在 Hugging Face 页面上点同意，再 `huggingface-cli login`。
* SheetSage2 由 **M-A-P（Multimodal Art Projection）** 随 **YuE2** 项目发布，
  基于 MERT-v2-FullSong 主干 + 适配器，加载时自动合并。
  项目页：https://map-yue2.github.io/ ｜ 主仓库：https://github.com/multimodal-art-projection/YuE
* **署名要求**：使用时应标明 **MERT2**、模型名及其来源仓库
  （`m-a-p/MERT-v2-30s`、`m-a-p/MERT-v2-FullSong`）。
* 权重自带的许可原文随权重一起下载到 `models/*/LICENSE.txt`。

**所以：本项目不是"拿来就能商用"的开源项目。** 代码是 MIT，模型不是。

---

## 2. 运行时与可执行文件（不在本仓库内）

| 组件 | 许可 | 说明 |
| --- | --- | --- |
| FFmpeg | **GPLv3** | 项目**不分发** ffmpeg。请自行安装并把 `ffmpeg` 放进 `PATH`，或设环境变量 `FFMPEG_BIN` 指向任意 `ffmpeg.exe`。 |
| CPython / PyTorch / torchaudio | PSF / BSD-3-Clause | 由你用 `pip` 自行安装 |
| uv（可选） | MIT / Apache-2.0 | 仅用于快速建 `.venv` |

打包成整合包再分发时，`packaging/licenses/` 下已备好 `FFmpeg-GPLv3.txt`；
GPLv3 二进制再分发时**必须一并保留许可文件**。

---

## 3. 前端与音色（**在本仓库内**）

| 组件 | 版本 | 许可 | 位置 |
| --- | --- | --- | --- |
| abcjs | 6.7.0（basic） | **MIT** | `web/vendor/abcjs/` |
| FluidR3_GM 大钢琴采样 | Frank Wen | **CC BY 3.0 US** | `web/vendor/soundfont/` |

* abcjs 版权归 Paul Rosen / Gregory Dyke（https://abcjs.net），MIT 全文见
  `packaging/licenses/abcjs-MIT.txt`。
* 钢琴采样来自 Paul Rosen 的 `midi-js-soundfonts`（FluidR3_GM），
  原始署名与出处见 `web/vendor/soundfont/ATTRIBUTION.md`。
* **再分发本项目时必须保留上面两条署名**——这是这两个组件许可的硬要求。

---

## 4. Python 依赖

`requirements-base.txt` 里的 60+ 个包（FastAPI、uvicorn、transformers、librosa、
numpy、scipy、pretty_midi、mido……）各自保留原许可，绝大多数是 MIT / BSD / Apache-2.0。
需要逐项核对时：

```bat
.venv\Scripts\python.exe -m pip list
```

再按包名去 PyPI 页面查 `License` 字段。

---

## 5. 免责

本工具用于**音频分析**。请勿用它批量扒取他人受版权保护的作品并回头分发或商用。
扒谱结果（调性、和弦、音高）由模型自动推断，**不保证准确**，请以人耳和乐器为准。
