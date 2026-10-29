# 第三方组件许可证声明

本仓库自身代码以 **MIT 许可证**发布（见根目录 LICENSE）。以下第三方组件随本工具使用，
各自有独立许可证，**使用本工具即视为同时遵守下列全部条款**。

> ⚠️ **最需要注意：ADTOF 权重为 CC BY-NC-SA 4.0（署名-非商业性使用-相同方式共享），
> 禁止任何商用场景。** 若你把本工具用于公司产品、付费服务、商业课程等场景，请先替换
> 为允许商用的转录模型，否则构成侵权。

## 1. ADTOF-pytorch 及其权重 —— CC BY-NC-SA 4.0（非商用！）

- 代码：https://github.com/xavriley/ADTOF-pytorch （本仓库锁定提交 85c192e）
- 预训练权重：随 ADTOF 发布，用于"鼓音频→MIDI"转录。
- 许可证：**Creative Commons Attribution-NonCommercial-ShareAlike 4.0（CC BY-NC-SA 4.0）**。
  - 允许：非商业的学习、研究、个人使用。
  - 禁止：任何直接或间接商用（含企业内部商业流程、付费产品/课程）。
  - 衍生须同许可共享。
- 来源说明：以 ADTOF 官方仓库/权重发布页声明为准；如上游更新条款，以其最新声明为准。

## 2. Demucs（htdemucs v4 权重与代码）—— MIT

- 代码：https://github.com/facebookresearch/demucs （本仓库 pip 包 demucs==4.1.0）
- htdemucs 权重（本地缓存于 models/demucs_local，约 84 MB）：Facebook Research 以 MIT 发布。
- 许可证全文：https://github.com/facebookresearch/demucs/blob/main/LICENSE

## 3. MuseScore 4 —— GPL-3.0（仅作外部 CLI 调用）

- 官网：https://musescore.org ；本工具只在命令行调用 `MuseScore4.exe`
  完成 MusicXML→PDF 与 MIDI→音频渲染，**未链接/修改 MuseScore 源码，不构成衍生作品**。
- 因此本工具整体无需按 GPL-3.0 开源；用户机器上需自行安装 MuseScore 4（免费）。
- 未安装 MuseScore 时管线自动回退到内置渲染器（reportlab 直绘 PDF，不经过 SVG→PDF 转换）。

## 4. MS Basic.sf3 音色库 —— MIT

- 位置：本工具 sound/MS_Basic.sf3（约 51 MB），与 MuseScore 4 自带音色库同源
  （MuseScore 4 安装目录 sound/MS Basic_License.md 原文已核实）。
  整合包不含 sf3（可选组件；安装器可在装有 MuseScore 4 的机器上从其目录复制）。
- 谱系：FluidR3（Frank Wen, 2000-2002, MIT）→ FluidR3Mono（Michael Cowgill, 2014-17, MIT）
  → MuseScore_General（S. Christian Collins, 2018-19, MIT）→ MS Basic。
- 许可：MIT，允许商用、修改与再分发，须保留版权与许可声明。

## 5. ffmpeg / ffprobe —— GPL-3.0（整合包随附 gyan.dev essentials 构建）

- 官网：https://ffmpeg.org ；本工具用它做 WAV→MP3（libmp3lame）与音频时长探测（ffprobe）。
- 整合包随附的 ffmpeg.exe / ffprobe.exe 为 https://www.gyan.dev 的 essentials_build
  （构建配置含 --enable-gpl，实测自报 GPL-3.0；此前本文误标 LGPL，已纠正）。
- 依 GPL-3.0 第 4 条，随包附许可证全文（tools\ffmpeg\bin\LICENSE）与源码获取说明
  （tools\ffmpeg\bin\FFMPEG-SOURCE.txt）：源码自 https://ffmpeg.org/download.html 获取，
  构建配置可用 `ffmpeg -version` 核对。
- 仅外部命令调用，不链接。

## 6. 核心 Python 库（pip 依赖）

| 包 | 许可证 |
|---|---|
| Flask / Werkzeug | BSD-3-Clause |
| pywebview / pythonnet | BSD-3 / MIT（pythonnet: MIT） |
| librosa | ISC |
| pretty_midi / mido | MIT |
| numpy / scipy / numba | BSD-3-Clause |
| soundfile / audioread / soxr | MIT / MIT / LGPL-2.1+ |
| matplotlib | PSF-based（BSD 兼容） |
| torch / torchaudio | BSD-3-Clause（+其他第三方条款，见 torch 源码 NOTICE） |
| reportlab | BSD-3-Clause |
| CairoSVG / svglib / tinycss2 / cssselect2 / webcolors | LGPL-3.0（CairoSVG）/ MIT（svglib 历史为 MIT，2.x 以其 setup.py 声明为准）/ BSD / BSD / BSD |
| requests / urllib3 / certifi | Apache-2.0 / MIT / MPL-2.0 |
| PyYAML / tqdm / pillow / lxml | MIT / MPL-2.0 / HPND / BSD-3 |

以上均为各自官方声明；个别小传递依赖（如 einops、julius、lameenc）为 MIT。
如发现某条与上游最新声明不符，以上游仓库 LICENSE 文件为准。

## 7. 未能完全核实的条目

- CairoSVG 2.9.1 与 svglib 2.3.0 的具体许可证：以两个包 `python -m pip show`
  及各自仓库元数据为准（CairoSVG 历史为 LGPL-3.0；svglib 历史为 MIT/LGPL 混合，
  上述"以实际为准"）。
- ADTOF 权重若上游后续更换许可协议（例如改为 Apache-2.0），商用禁令同步解除，
  请复查 ADTOF 官方发布页。

## 8. Python 3.10 运行时（整合包 runtime\python）—— PSF License

- 整合包自带 CPython 3.10.11 完整运行时，按 Python Software Foundation License 再分发；
  官方 LICENSE.txt 已随包保留（runtime\python\LICENSE.txt）。