# 目录结构

仓库根即项目根。所有入口脚本都从自身位置推导根路径，也可以用环境变量 `DRUM_AI_ROOT` 覆盖。

```
DrumQuill/
├── start.bat                    双击入口：检查环境 → 启动界面
├── 启动扒谱.bat                 同 start.bat（保留给旧快捷方式）
├── 一键扒谱.bat                 把音频拖到图标上直接开始
├── run_pipeline.ps1             六步管线主脚本
├── config.json.example          配置示例（复制为 config.json 使用）
├── requirements.txt             Python 运行时依赖
├── README.md / CHANGELOG.md / LICENSE
├── bootstrap/                   环境检查与安装
│   ├── ensure.ps1               检测依赖，缺什么装什么，然后启动
│   ├── install.ps1              安装器命令行入口（可逐项跳过）
│   ├── download.ps1             多镜像下载（断点续传）
│   └── silent-launch.vbs        无窗口启动
├── app/                         界面与服务
│   ├── app.py                   Flask 服务：上传/状态/取消/历史/产物/PDF 预览/模型管理
│   ├── index.html               单页前端
│   ├── launcher.py              EXE 启动器：端口探测 → 服务就绪 → pywebview 窗口
│   ├── installer.py             组件化安装器（能力清单、向导、单组件安装、多镜像回退）
│   ├── models_registry.py       分离模型注册表：下载、校验、切换、删除
│   ├── gpu_detect.py            显卡探测与决策：算力→CUDA 版本、显存→段长、失败阶梯
│   └── start_server.ps1         源码模式启动脚本
├── scripts/                     管线各步骤
│   ├── detect_bpm.py            ① 速度检测
│   ├── transcribe.py            ③ 鼓点转录（逐类阈值 + 力度）
│   ├── fix_tempo_tick.py        ④ 写入 BPM 与 tick 精度
│   ├── fix_align.py             ④ 相位对齐 + 软量化
│   ├── gen_musicxml.py          ④ 生成 MusicXML
│   ├── postprocess.py           规则引擎：去重、最短时值、摇摆量化
│   ├── render_fallback.py       无 MuseScore 时的 reportlab 直绘 PDF
│   ├── midi_to_wav.py           无 ffmpeg / MuseScore 时的 WAV 合成
│   └── gpu_probe.py             分离前的 GPU 探针
├── docs/                        结构、回退矩阵、第三方许可
├── build_spec/DrumQuill.spec    PyInstaller 打包配置
├── songs/                       输入音频          （内容不入库）
├── stems/                       分离中间产物      （内容不入库）
├── midis/                       转录与校正后 MIDI （内容不入库）
├── output/                      成品              （内容不入库）
├── models/                      分离模型权重      （首次运行自动下载）
└── sound/                       鼓声音色库        （首次运行自动下载）
```

几点说明：

- `songs/ midis/ stems/ output/ models/ sound/` 目录本身入库（各带 `.gitkeep` 占位），内容不入库。
- `config.json` 是每台机器自己的配置，不入库；仓库只提供 `config.json.example`。
- 数据目录可以改：设置里改「数据目录」，或写进 `config.json` 的 `dataDir`，
  四个子目录会跟着搬。
- 运行期文件（`app\job.log`、`app\service.log`、`app\task_state.json` 等）都在 `.gitignore` 里。
