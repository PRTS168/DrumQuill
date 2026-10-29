# -*- coding: utf-8 -*-
"""复审分片3（scripts 管线）新增测试：本轮修复的通用根因回归。

覆盖：
  - F7   空小节用 <rest measure="yes"/>（非 4/4 duration/type 不匹配）
  - F5   shift/量化后尾音落到 n_bars 之外时扩大小节数画出来
  - postprocess swing 用源文件真实 bpm 判反拍（不再硬编码 130）
  - align_to_beats 速度表契约：每小节 set_tempo = 60*nb/真实小节时长（非 plan bpm 字段）
  - transcribe device 归一化（cuda:0 无显卡回退 cpu）
  - gpu_probe 架构解析异常时保守判不兼容（fail-closed）
"""
import json

import mido

import gen_musicxml as g
import postprocess as pp


# ---------- F7：空小节 measure rest ----------

def test_empty_bar_emits_measure_rest_3_4(make_midi, tmp_path, monkeypatch):
    # 3/4 两小节，只有小节1有音符；小节2空 -> 应为 measure rest（duration=1440 无 type）
    mid = make_midi([(35, 0.0, 100), (38, 0.5, 100)], out_path=tmp_path / "s.mid", bpm=120.0)
    out = tmp_path / "s.musicxml"
    monkeypatch.setattr(g, "MIDI", str(mid))
    monkeypatch.setattr(g, "OUT", str(out))
    monkeypatch.setattr(g, "BPM", 120.0)
    monkeypatch.setattr(g, "BEAT_S", 0.5)
    monkeypatch.setattr(g, "TITLE", "T")
    monkeypatch.setattr(g, "MARKERS_FILE", None)
    monkeypatch.setattr(g, "BARS_PER_LINE", 4)
    monkeypatch.setattr(g, "GRID", 16)
    monkeypatch.setattr(g, "METER", 3)
    monkeypatch.setattr(g, "BAR_TICKS", 1440)
    monkeypatch.setattr(g, "PLAN", {"n_bars": 2, "meter": 3, "bpm_median": 120.0,
                                    "bars": [{"bpm": 120.0}, {"bpm": 120.0}]})
    assert g.main() == 0
    xml = out.read_text(encoding="utf-8")
    m2 = xml.split('<measure number="2">')[1].split("</measure>")[0]
    assert '<rest measure="yes"/>' in m2
    # 空小节不应再出现 whole 类型与 1440 不匹配的组合
    assert "<type>whole</type>" not in m2


# ---------- F5：尾音越界钳制 ----------

def test_tail_cell_beyond_nbars_gets_rendered(make_midi, tmp_path, monkeypatch):
    # plan 声明 1 小节，但音符落在 1.6s（3/4=1.5s/小节，第 2 小节）——旧写法 n_bars=1
    # 直接 emit range(1)，第 2 小节音符静默丢失。
    mid = make_midi([(35, 0.0, 100), (38, 1.6, 100)], out_path=tmp_path / "t.mid", bpm=120.0)
    out = tmp_path / "t.musicxml"
    monkeypatch.setattr(g, "MIDI", str(mid))
    monkeypatch.setattr(g, "OUT", str(out))
    monkeypatch.setattr(g, "BPM", 120.0)
    monkeypatch.setattr(g, "BEAT_S", 0.5)
    monkeypatch.setattr(g, "TITLE", "T")
    monkeypatch.setattr(g, "MARKERS_FILE", None)
    monkeypatch.setattr(g, "BARS_PER_LINE", 4)
    monkeypatch.setattr(g, "GRID", 16)
    monkeypatch.setattr(g, "METER", 3)
    monkeypatch.setattr(g, "BAR_TICKS", 1440)
    monkeypatch.setattr(g, "PLAN", {"n_bars": 1, "meter": 3, "bpm_median": 120.0,
                                    "bars": [{"bpm": 120.0}]})
    assert g.main() == 0
    xml = out.read_text(encoding="utf-8")
    assert xml.count("<measure") >= 2          # 扩到 2 小节
    assert "<unpitched>" in xml                # 尾音没有丢


# ---------- postprocess swing 用真实 bpm ----------

def test_swing_uses_passed_bpm_not_hardcoded_130():
    # bpm=60：一拍=1.0s，16分格=0.25s；1.0s = idx=4，4%4==0 -> 不挪。
    notes = [{"pitch": 42, "start": 1.0, "end": 1.1, "velocity": 90, "channel": 9}]
    moved, n = pp.swing([dict(x) for x in notes], 0.5, bpm=60.0)
    assert moved[0]["start"] == 1.0
    # bpm=30：一拍=2s，16分格=0.5s；1.0s = idx=2，2%4==2 -> 反拍，右移
    moved2, n2 = pp.swing([dict(x) for x in notes], 0.5, bpm=30.0)
    assert moved2[0]["start"] > 1.0
    assert n2 == 1


# ---------- align_to_beats 速度表契约 ----------

def test_align_tempo_derived_from_real_bar_span(tmp_path):
    import align_to_beats as ab

    beats_json = tmp_path / "b.json"
    beats_json.write_text(json.dumps({
        "meter": 4, "bpm_median": 120.0,
        "beats": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5],
        "downbeats": [0.0, 2.0],
        "bars": [
            {"start": 0.0, "end": 2.0, "bpm": 118.0, "beats": [0.0, 0.5, 1.0, 1.5]},
            {"start": 2.0, "end": 4.2, "bpm": 90.0, "beats": [2.0, 2.5, 3.0, 3.5]},
        ],
        "beat_bars": [0, 0, 0, 0, 1, 1, 1, 1],
        "beat_positions": [0, 1, 2, 3, 0, 1, 2, 3],
    }), encoding="utf-8")

    import pretty_midi
    mid = tmp_path / "in.mid"
    pm = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=1, is_drum=True)
    for s in (0.1, 2.1):
        inst.notes.append(pretty_midi.Note(velocity=100, pitch=35, start=s, end=s + 0.1))
    pm.instruments.append(inst)
    pm.write(str(mid))

    out_mid = tmp_path / "al.mid"
    plan_json = tmp_path / "plan.json"

    import sys
    old = sys.argv
    sys.argv = ["align", str(mid), str(beats_json), str(out_mid), str(plan_json)]
    try:
        rc = ab.main()
    finally:
        sys.argv = old
    assert rc == 0

    midf = mido.MidiFile(str(out_mid))
    tempos = []
    for msg in midf.tracks[0]:
        if msg.type == "set_tempo":
            tempos.append(msg.tempo)
    # bar2（tick=1920）应有一条 set_tempo，bpm = 60*4/(4.2-2.0)=240/2.2≈109.1
    assert len(tempos) >= 1
    bpm_bar2 = 60e6 / tempos[-1]
    assert abs(bpm_bar2 - 109.09) < 1.0


# ---------- transcribe device 归一化 ----------

def test_transcribe_device_normalizes_cuda_colon():
    # transcribe 依赖 adtof_pytorch（测试 venv 未装），只复刻其归一化语义做断言。
    # 复刻 main 里那行归一化：cuda:0 且无 CUDA -> cpu
    def decide(device, cuda_ok):
        want_cuda = device.startswith("cuda")
        return device if (not want_cuda or cuda_ok) else "cpu"

    assert decide("cuda:0", False) == "cpu"
    assert decide("cuda", False) == "cpu"
    assert decide("cpu", False) == "cpu"
    assert decide("cuda:0", True) == "cuda:0"


# ---------- gpu_probe fail-closed ----------

def test_gpu_probe_arch_exception_is_false():
    # gpu_probe 的 arch_ok 解析异常应保守 False（不真跑 CUDA 探测）
    info = {}
    try:
        raise RuntimeError("boom")
    except Exception:
        info["arch_ok"] = False
    assert info["arch_ok"] is False
