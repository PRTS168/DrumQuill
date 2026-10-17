# DrumQuill

把一首歌变成可以打印的鼓谱。

DrumQuill 在本地跑完一整套流程：从混音里分出鼓轨，把每次敲击识别成音符，排成鼓谱导出 PDF。
同一份结果还能导出 MusicXML 和 MIDI，并合成一段只有鼓的音频用于对照。音频不出本机。

Windows 10 / 11 · MIT · 主产物是 PDF 鼓谱

![主界面](docs/images/ui.png)

## 它做什么

一首 4 分钟的歌，大约这样用：

1. 把音频拖进窗口（MP3 / WAV / FLAC / OGG / M4A / AAC）。
2. 选要什么产物、在哪算：完整扒谱 / 只要谱子 / 只要音频 / 只要鼓声，分离设备可选自动、CPU、GPU。
3. 等进度跑完。有 GPU 通常几分钟，纯 CPU 会慢一些。
4. 在完成页看鼓谱预览、试听鼓声、导出或另存。

不想要界面也可以直接跑命令行：`run_pipeline.ps1 -SongPath <音频>`，或用「一键扒谱.bat」把文件拖到图标上。

## 特色

**鼓谱是主角。** 完成页第一屏就是乐谱预览，可以翻页、缩放、系统程序打开、另存为 PDF，
不用先去找文件。音频和 MusicXML 排在后面。

**产物是可编辑的。** 除 PDF 外还给出 MusicXML 与 MIDI。谱面不满意时，
用 MuseScore 打开 MusicXML 改完再导出，比在图片上标注实在。

**按机器给建议。** 内置四个 Demucs 分离模型，界面按显存给推荐并标注实测效果，
模型可以下载、切换、删除；没有 NVIDIA 显卡就不建议装 GPU 组件，不会白下两个多 G。

**缺什么补什么。** 首次启动的安装向导按能力划组件（转录引擎、分离模型、排版、音频），
可以只装需要的；之后在设置里单独补装或重装某一个组件。

**产物能管。** 历史记录里每一首歌都能看到成品与中间产物：
试听、查看 PDF、下载、在文件夹中定位、删除单个文件或整首清空。

**出错有退路。** GPU 不可用时自动回落 CPU；MuseScore 缺失时用内置渲染兜底；
转录器异常时回退官方 CLI；显卡显存不够时按段长 7→5→3 递减重试。
每一步的降级都会写进运行日志，不会静默失败。

## 安装

**方式一：绿色版（推荐）**

下载 Release 里的压缩包，解压到任意目录，双击 `DrumQuill.exe`。
首次启动会打开安装向导，按需勾选组件；中途关掉也没关系，下次打开会接着装。

**方式二：源码运行**

需要 Windows 与 Python 3.9+：

```powershell
# 双击 start.bat，或手动：
powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap\ensure.ps1   # 检查/安装依赖
powershell -NoProfile -ExecutionPolicy Bypass -File app\start_server.ps1    # 启动 Web 界面
```

依赖装在仓库内的 `.venv`，不污染系统 Python；下载走国内镜像，支持断点续传。

**方式三：拖拽**

把音频文件拖到「一键扒谱.bat」图标上，装好环境后直接开始扒谱，完成后成品在 `output\`。

## 产物

| 文件 | 内容 |
| --- | --- |
| `output\<歌名>_鼓谱.pdf` | 鼓谱，可用任意 PDF 阅读器打印 |
| `output\<歌名>.musicxml` | 乐谱交换格式，可在 MuseScore 里继续编辑 |
| `output\<歌名>_鼓谱音频.mp3` | 按谱合成的鼓声，用来对照转录是否准确 |
| `output\<歌名>_鼓声.mp3` | 从原曲分离出的纯鼓轨（只要鼓声模式） |
| `midis\<歌名>_<BPM>.mid` | 校正后的 MIDI |
| `stems\<模型>\<歌名>\drums.wav` | 分离出的鼓轨，中间产物 |

中间产物保留是为了能只重跑某一步（例如换分离模型后重新转录）。

## 效果

用人工扒的鼓谱做参考，在 150 秒素材上按"逐类起音微平均 F1"（每个鼓类单独比较，±50ms 容差，
先做全局相位对齐）衡量，当前版本的端到端结果：

| 环节 | 参考段（前 60s） | 验证段（90–150s） |
| --- | --- | --- |
| 分离 + 转录 | 0.894 | 0.921 |

分鼓类看：军鼓 0.96、底鼓 0.91、踩镲 0.88、吊镲 0.80、嗵鼓 0.74。

耗时参考（4 线程 CPU、4GB 显存显卡，一首 4 分钟的歌）：

| 设备 | 分离 | 转录 | 合计 |
| --- | --- | --- | --- |
| GPU | 约 1 分钟 | 约 30 秒 | 2–3 分钟 |
| CPU | 3–5 分钟 | 约 30 秒 | 5–8 分钟 |

**已知的准确度上限。** 转录引擎（ADTOF）只输出五类：底鼓、军鼓、嗵鼓、踩镲、吊镲。
它不区分开/闭踩镲，也没有叮叮镲（ride），所以 ride 段落常被记成吊镲或踩镲——
这是模型能力限制，调参无法解决。另外节拍栅格来自 BPM 估计而非拍点跟踪，
速度明显漂移的曲子，后半段可能出现小节错位。

## 设置里值得知道的几项

| 项 | 作用 |
| --- | --- |
| 作业模式 | 完整 / 只要谱子 / 只要音频 / 只要鼓声 |
| 分离设备 | 自动（有 CUDA 就用 GPU） / CPU / GPU |
| 分离模型 | 按显存推荐；镲片多的曲子可试 mdx_extra，代价是慢约 3 倍 |
| 转录阈值 | 五类各自的灵敏度，留空即按当前分离模型取实测最优值 |
| 量化吸附容差 | 音符对齐到十六分音符的容差，0 表示不量化 |
| 后处理 | 去抖去重、最短时值、摇摆量化（0–0.6） |

想把成品放到别的盘，改「数据目录」即可；`config.json` 保存全部设置。

## 常见问题

**端口被占。** 默认 8756，占用时自动改用 8757；想固定就改 `config.json` 的 `port`。

**下载很慢或中断。** 镜像优先、断点续传，关掉重开会在断点继续。

**转出来的鼓谱不准。** 先看是否用了 GPU 与推荐的分离模型；
再用 MuseScore 打开 MusicXML，对照原曲改鼓点后重新导出 PDF——目前这是最快达到"能拿去用"的路径。

**任务显示"已有任务在运行"但其实没有。** 看 `app\job.log` 与 `app\service.log`；
需要重置时删掉 `app\task_state.json` 再启动。

**换了新版本，打开像没更新。** 启动器会比对程序文件指纹，发现文件更新会自动重启后台服务；
必要时可在任务管理器结束 `pythonw.exe`（命令行含 `app.py`）后重新打开。

## 目录

```
app/           Flask 服务、前端页面、启动器、安装器、模型与显卡探测
bootstrap/     环境检查与安装脚本
scripts/       管线各步骤：测速、转录、对齐、MusicXML、渲染兜底、GPU 探针
docs/          结构说明、回退矩阵、第三方许可
build_spec/    PyInstaller 打包配置
run_pipeline.ps1   六步管线主脚本
```

更细的说明见 [docs/structure.md](docs/structure.md) 与 [docs/fallback_matrix.md](docs/fallback_matrix.md)。

## 致谢

- [Demucs](https://github.com/facebookresearch/demucs)（Meta AI Research，adefossez）——鼓轨分离模型
- [ADTOF-pytorch](https://github.com/xavriley/ADTOF-pytorch) 与 ADTOF 原论文作者——鼓点转录
- [MuseScore](https://musescore.org/)——MusicXML 排版与 PDF 渲染
- [FFmpeg](https://ffmpeg.org/)——音频转码
- [PyMuPDF](https://github.com/pymupdf/PyMuPDF)、[Flask](https://flask.palletsprojects.com/)、
  [pywebview](https://pywebview.flowrl.com/)、[pretty_midi](https://github.com/craffel/pretty-midi)、
  [librosa](https://librosa.org/)、[PyTorch](https://pytorch.org/)、[NumPy](https://numpy.org/)——基础设施
- [MS Basic 音色库](https://musescore.org/)——鼓声合成音色

## 依赖与许可

本项目代码以 MIT 许可发布，见 [LICENSE](LICENSE)。

运行时依赖见 [requirements.txt](requirements.txt)；第三方组件的版本、许可证与注意事项见
[docs/third_party_licenses.md](docs/third_party_licenses.md)。

**商用请注意**：ADTOF 的模型权重采用 CC BY-NC-SA 4.0，禁止商业使用；
Demucs 权重为 MIT，MuseScore 与 MS Basic 音色库遵循各自的许可。
将本工具用于商业用途前，请先替换或取得转录权重授权。
