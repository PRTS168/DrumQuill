# DrumQuill（DrumQuill）回退矩阵（fallback matrix）

适用文件：`run_pipeline.ps1`（六步管线）、`app\start_server.ps1`、`app\app.py`。
路径解析顺序：`$env:DRUM_AI_ROOT` → 脚本所在目录；再读 `<ROOT>\config.json`（字段全可缺省）。
退出码约定：`0` 成功 / `1` 处理失败（已打印原因与下一步）/ `2` 缺依赖（venv 缺失）。
日志约定：保留 `[N/6]` 阶段标记与 `==== DONE ====`；警告 `[!]`，正常信息 `[*]`。

## 回退矩阵

| # | 场景 | 触发条件 | 回退动作 | 提示文案 | 退出码 | 实测状态 |
|---|------|----------|----------|----------|--------|----------|
| 1 | BPM 检测失败 | `detect_bpm.py` 报错/输出非法（≤0 或 >300） | 采用 `config.bpmFallback`（缺省 120）继续，用户可加 `-BPM` 手动指定 | `[!] 速度检测失败，使用默认速度 120 BPM（可加 -BPM 120 手动指定）` | 0 | ✅ 已实测：用非音频文件触发 librosa 报错 → 自动落到 120 BPM 并跑通全链 |
| 2 | Demucs 分离失败（模型缺失） | `modelsDir\demucs_local` 不存在 | 不调用 Demucs，直接整曲转录 | `[!] 未找到本地分离模型目录：<repo>` → `[!] 鼓轨分离失败（模型缺失或 Demucs 运行出错）。` → `[!] 已降级：对整曲直接转录（精度略降、耗时更长）。` | 0（继续） | ✅ 已实测：空 modelsDir、不加 -SkipSeparation → 打印两条 `[!]` 后整曲 ADTOF 跑通，exit 0 |
| 3 | Demucs 运行失败 | demucs 首次 exit≠0 | 换 `--segment 10` 重试一次；仍失败 → 整曲直接转录 | `[!] 分离首次失败（exit N），更换 --segment 重试一次...` → 同上降级文案 | 0（继续） | ⚠️ 代码已实现重试+降级；重试分支本身未实测（本机 demucs 权重可用，未人为制造运行时失败） |
| 4a | ADTOF 源码目录缺失但模块已装 | env `DRUM_AI_ADTOF_DIR`/config `adtofDir`/ROOT 候选均未命中，但 venv 内 `import adtof_pytorch` 成功 | 目录可选：不 Push-Location，直接 `python -m adtof_pytorch.cli`（pip 安装进 site-packages 的新用户零配置可跑） | `[*] 未找到 ADTOF 源码目录，使用 venv 内已安装的 adtof_pytorch 模块。` | 0 | ✅ 已实测：临时根 config 不含 adtofDir → 正常转录、exit 0 |
| 4b | ADTOF 完全不可用 | 源码目录未命中 **且** venv 内模块导入失败 | 明确报错退出（指引安装或配置） | `[!] 未找到 ADTOF 转录模块。` + `请先运行 bootstrap\install.ps1 安装 ADTOF；或在 config.json 配置 adtofDir 指向 ADTOF-pytorch 源码目录。` | 1 | ✅ 探测命令 `import adtof_pytorch` 已实测；模块缺失分支未实测（本机模块可导入） |
| 4c | 整曲转录仍失败 | ADTOF exit≠0 或未产出 MIDI | 明确报错退出 | `[!] 转录失败（exit N）。建议：确认音频可正常播放；若刚发生分离失败，整曲转录耗时较长请耐心等待。` | 1 | ✅ 文案路径已走通（BPM 回退测试中 exit 0 是因 -SkipSeparation 复用了既有鼓轨） |
| 5 | 校正链失败（fix_tempo_tick / fix_align / gen_musicxml） | 任一步 exit≠0 | 数据不可降级：打印该步失败与中间文件位置，退出 | `[!] fix_tempo_tick 失败（exit N）—— MIDI 数据无法降级处理。请把上方输出发给开发者；中间文件在 <midis>。`（其余两步同构） | 1 | ✅ 已实测：scripts 路径 bug 时 fix_tempo_tick 报错 exit 1，文案与中间文件路径正确 |
| 6 | MuseScore 缺失/不可用 | config `museScorePath` 指向不存在的 exe，或未配置且自动探测无结果 | 走自研谱面渲染 `scripts\render_fallback.py`（reportlab 直绘，A4 横版多页：五号线谱、4/4 拍号、底鼓/军鼓/踩镲/嗵鼓/吊镲符号、全小节休止符、小节编号、图例），PDF 文件名与 MuseScore 路径一致 `output\<song>_鼓谱.pdf` | `[!] config.json 指定的 MuseScore 不存在：<path> —— 后续将使用自研谱面渲染。` → `[!] 未找到 MuseScore，使用自研谱面渲染生成 PDF。` → `[!] 已用自研渲染生成 PDF（MuseScore 不可用，排版精度略低但可读）。` | 0（继续） | ✅ 已实测：config 指向不存在 exe → 自动渲染 PDF（18KB/1 页），并转 PNG 自查版面可读 |
| 7 | MuseScore 渲染无产出 PDF | exe 在但 Start-Process 后未生成 PDF | 同上，自动切换自研渲染 | `[!] MuseScore 未产出 PDF（exit N），改用自研谱面渲染。` | 0（继续） | ⚠️ 代码已实现；未实测（本机 MuseScore 渲染正常，未人为制造渲染失败） |
| 8 | 自研渲染也失败 | render_fallback exit≠0 或未产 PDF | 明确报错退出 | `[!] 自研渲染也失败了（exit N）。请检查对齐 MIDI：<alMid>` | 1 | ✅ 代码路径就位；正常 MIDI 下渲染成功，失败分支未实测 |
| 9 | MuseScore 音频渲染失败 | tmp WAV 未产出 | 回退 `scripts\midi_to_wav.py`（numpy 合成器）直接出 WAV | `[!] MuseScore 音频渲染未产出 WAV，改用 numpy 合成器。` | 0（继续） | ✅ 已实测：MuseScore 不可用时音频由 midi_to_wav 产出（notes=238, 30.9s） |
| 10 | ffmpeg 缺失/转码失败 | config `ffmpegPath` 指向不存在 exe，或转码 exit≠0 | 保留 WAV 不转 MP3 | `[!] 未找到 ffmpeg，保留 WAV 音频（MP3 为可选项，不影响谱面使用）。` / `[!] ffmpeg 转 MP3 失败（exit N），保留 WAV。` | 0（继续） | ✅ 已实测：config 指向不存在 ffmpeg → 仅产出 5.4MB WAV，无 MP3，exit 0 |
| 11 | 端口占用 | 8756 被占 | `start_server.ps1` 回退 8757 并以 `DRUM_AI_PORT` 传给 app.py | `[*] 端口 8756 已被占用，回退到 8757...` | — | ✅ 代码已实现（launcher.py 的端口回退由另一子代理负责，未触碰） |
| 12 | 服务启动失败 | 等待监听超时 | 控制台打印 `app.log` 尾部 20 行 | `服务启动失败。错误日志末尾（<app>\app.log）：...` | — | ✅ 代码已实现；冒烟测试 /api/status 200 正常，失败展示路径未实测 |
| 13 | venv 缺失 | config `venvDir` 与 `<ROOT>\.venv` 均无 python.exe | 打印安装指引后退出（不盲目继续） | `[!] 未找到虚拟环境 Python：<path>` + `请先运行 bootstrap\install.ps1 安装依赖；或在 config.json 里把 venvDir 指向已有虚拟环境。` | 2 | ✅ 已实测：config 指向不存在 venv → 打印指引，exit 2 |
| 14 | -SkipSeparation 但无分离产物 | stems 下无 drums.wav | 明确报错（用法错误，不降级） | `[!] -SkipSeparation 但未找到已有分离产物：<stems>\htdemucs\<歌曲>\drums.wav` | 1 | ✅ 原行为保留，未破坏 |

## 渲染链路说明（重要实测结论）

- **cairosvg 2.9.1 实测不可用**：import 时报 `OSError: no library called "cairo-2" was found ... error 0x7e`（缺 cairo DLL）。因此回退 PDF 不经过 SVG→PDF 转换，而是 `render_fallback.py` 用 **reportlab 5.0.1 直绘**（中文用内置 CID 字体 STSong-Light）。
- **svglib 2.3.0 备选链路已实测可用**：手写小 SVG → svglib.svg2rlg → reportlab renderPDF → 1690 字节 PDF 成功；CJK CID 字体注册成功。当前未作为生产路径（避免双重转换风险），如需 SVG 中间产物可启用。
- 自研渲染产物已转 PNG 自查：标题/图例/五号线谱/拍号/鼓符号位置正确，可读可用（精度低于 MuseScore）。

## 新增测试参数（run_pipeline.ps1）

| 参数 | 作用 |
|------|------|
| `-SkipSeparation` | 复用已有 stems（原有） |
| `-BPM <n>` | 手动指定速度，跳过检测（原有） |
| `-SkipMuseScore` | 强制走自研渲染 |
| `-SkipFfmpeg` | 保留 WAV 不转 MP3 |
| `-SkipAudio` | 完全跳过音频合成 |

## 审查补充验证（review 2026-10-16）

独立鲁棒性审查（详见 `docs\review_report.md`）在临时测试根用 config 伪造缺件场景，全部重测，结论：

- 行 1 BPM 失败：✅ 复测通过（junk.txt → `[!] 速度检测失败，使用默认速度 120 BPM` → 继续跑通）。
- 行 2 无模型降级：✅ 复测通过（空 modelsDir → 两条 `[!]` 降级整曲 ADTOF，DONE exit 0）。
- 行 3 Demucs 重试分支：⚠️ 升级为"部分实测"——重试动作本身真实执行过（场景 8 中首次失败→换 `--segment 10` 再试，因模型上限 7.8s 失败后正确降级）；"重试成功"仍未实测。
- 行 4c 转录失败：✅ 复测到 `[!] 转录失败（exit 1）` 文案路径。
- 行 6 无 MuseScore：✅ 复测通过，自研渲染 PDF 转图亲查（标题/五号线谱/小节线 1-5/鼓符号/图例齐全不越界）。
- 行 9 无 MuseScore 音频：✅ `midi_to_wav` numpy 合成出 WAV（notes=70）。
- 行 10 无 ffmpeg：✅ 复测仅 WAV 无 MP3，exit 0。
- 行 11 端口回退：✅ 复测 8756 被占→8757 `/api/status` 200；**并修复缺口**：回退端口也被占时不再误判成功，改为 `[!] 端口 8757 已被其他程序占用...` + 日志尾 exit 1。
- 行 12 启动失败日志尾：✅ 修复后实测打印 app.log 末尾。
- 行 13 venv 缺失：✅ fake venvDir → 指引文案 exit 2。
- 行 14 -SkipSeparation 无产物：✅ 复测到 `[!] -SkipSeparation 但未找到已有分离产物` 文案。

**审查新发现并修复的 bug（矩阵原表未覆盖）：**
1. `run_pipeline.ps1` 数据目录创建循环：`dataDir` 被同名文件占据时，PowerShell 5.1 `New-Item -Force` 静默空转不报错，导致管线带坏配置冲进 Demucs/ADTOF 抛 Python 栈。已加创建后 `Test-Path` 验证 + 中文友好报错 exit 1。
2. `start_server.ps1` 回退端口也被占时把别人的监听当成功。已加 `$launched` 标记，未成功启动则报错 exit 1。

**仍未实测的子分支（保持 ⚠️）：** 行 4b（ADTOF 模块完全缺失）、行 7（MuseScore 渲染无产出）、行 8（自研渲染失败）、bootstrap 在线安装主流程、launcher.py GUI 弹窗。
