# 评测记录

记录 DrumQuill 的评测方法、公开数据来源与实测结果，全部可复现。
评测代码在 `build_work\eval_gmd.py`（不入发布包，仓库内 `build_work` 被忽略）。

## 一、评测数据

**有真值的鼓演奏：Groove MIDI Dataset（GMD）**

- 来源：Magenta / Google，CC BY 4.0，1150 条人类鼓手演奏 MIDI（8 位鼓手、多种风格与速度）
- 为什么用它：MIDI 是精确真值；把同一个 MIDI 渲染成音频，就得到"音频 + 逐音符真值"的配对，
  不依赖任何商业录音
- 渲染：`fluidsynth -ni -r 44100 -g 0.9 -F out.wav sound\MS_Basic.sf3 in.mid`
  （fluidsynth 严格按 MIDI 自带 tempo 播放，因此音频与真值同一时间轴）

**真实录音：4 首本地曲目**（评估网格、记谱与小节结构，不参与逐音符精度打分）

## 二、方法与指标

对每条演奏：

1. 用 `scripts\transcribe.py`（默认阈值）转录渲染音频 → 预测 MIDI
2. 与真值按鼓类比较起音：±50ms 容差、先搜索全局最优时间偏移（±3s 粗搜 + 5ms 细搜）
3. 类别映射：底鼓 35/36，军鼓 38/40，踩镲 42/44/46，吊镲 49/51/52/55/57/59，嗵鼓 41/43/45/47/48/50
4. 另外跑完整链路：Beat This 拍点 → 按拍对齐 → 生成 MusicXML → 结构校验

## 三、结果（10 条人类演奏，固定随机种子抽样）

逐类微平均 F1：**中位 0.887，均值 0.867，范围 0.53–0.99**

| 类 | 中位 F1 | 说明 |
| --- | :---: | --- |
| 底鼓 | **0.997** | 稳定准确 |
| 军鼓 | **0.899** | 稳定准确 |
| 踩镲 | 0.375 | 双峰：踩镲为主的段落 1.00，ride/开镲为主的段落 0.00 |
| 嗵鼓 | 0.384 | 真值分布在 41–50 多个音高，模型只输出一种 |
| 吊镲 | **0.000** | 真值含 ride（51）、china（52）等，模型只输出 crash（49） |

结构校验：10 条全部通过（每小节时值合计、时值合法性、type 一致性）。

## 四、结论

1. **底鼓/军鼓已经够用**：接近 1.0，与真实录音上的结果一致（0.894/0.921 的微平均主要被镲片拖低）。
2. **镲片是明确的短板，且属于模型边界**：ADTOF 只输出"吊镲"一类，无法区分 ride 与 crash，
   更无法表达开/闭踩镲。评测里 ride 为主的段落 cymbal F1 直接是 0 —— 这不是参数能修的。
3. **嗵鼓同理**：单一音高无法覆盖高/中/落地嗵的分工。
4. 结构与网格链路稳定：所有样本每小节时值合计正确、无非法时值、无碎片音符。

## 五、复现

```powershell
# 1. 取 GMD（约 3MB，MIDI only）
curl -L -o eval\gmd.zip https://storage.googleapis.com/magentadata/datasets/groove/groove-v1.0.0-midionly.zip
tar -xf eval\gmd.zip -C eval\gmd

# 2. 跑评测（需要 venv、fluidsynth、sound\MS_Basic.sf3）
python build_work\eval_gmd.py --limit 10
# 明细：build_work\eval_gmd\gmd_report.json
```

## 六、已知的评测局限

- 渲染音色来自 MS Basic 音源，与真实录音存在音色差异；不过底鼓/军鼓的结果与真实录音一致，
  说明结论方向可信。
- 抽样只有 10 条，全部是 GMD 的独奏鼓；不含"混音分离"环节的误差（分离误差单独在
  `docs\structure.md` 与 README 的分离模型对比中给出）。
- 大样本（Slakh2100 全量、MDB Drums）需要较大下载量，当前网络条件下未纳入。
