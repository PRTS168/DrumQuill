# -*- coding: utf-8 -*-
"""gen_musicxml.py 测试：网格制谱的核心纯函数 + 端到端冒烟。"""
import pytest

import gen_musicxml as g


# ---------- legal_below ----------

def test_legal_below_returns_largest_legal():
    assert g.legal_below(480, 480) == 480
    assert g.legal_below(240, 480) == 240
    assert g.legal_below(100, 480) == 60
    assert g.legal_below(0, 480) == 60  # 兜底

def test_legal_below_respects_cap():
    assert g.legal_below(1000, 240) == 240
    assert g.legal_below(1000, 60) == 60


# ---------- rest_chain ----------

def test_rest_chain_sums_to_total():
    out, left = g.rest_chain(1920)
    assert left == 0
    assert sum(out) == 1920
    assert all(d in (960, 480, 240, 120, 60) for d in out)

def test_rest_chain_single_and_zero():
    assert g.rest_chain(120) == ([120], 0)
    assert g.rest_chain(0) == ([], 0)

def test_rest_chain_leftover_for_unfillable():
    out, left = g.rest_chain(10)
    assert out == []
    assert left == 10


# ---------- quantize ----------

def test_quantize_grid_positions():
    # 120 BPM：BEAT_S=0.5s，CELL=120tick（16 分格）
    cells = g.quantize([(35, 0.0, 100), (38, 0.5, 90), (42, 0.25, 80)])
    assert cells[(0, 0)] == {35: 100}
    assert cells[(0, 480)] == {38: 90}
    assert cells[(0, 240)] == {42: 80}

def test_quantize_merges_same_cell_keep_max_velocity():
    cells = g.quantize([(35, 0.0, 100), (35, 0.02, 120)])
    assert cells[(0, 0)] == {35: 120}

def test_quantize_filters_non_drum_pitches():
    cells = g.quantize([(60, 0.0, 100), (35, 0.0, 90)])
    assert 60 not in cells[(0, 0)]
    assert cells[(0, 0)] == {35: 90}


# ---------- 非 4/4 拍号（P1-4 修复：先定 METER/BAR_TICKS 再 quantize）----------

def test_quantize_respects_3_4_bar_ticks(monkeypatch):
    # 3/4 每小节 1440 tick：1.5s（第 4 拍，1440 tick）应进入小节 1
    monkeypatch.setattr(g, "BAR_TICKS", 1440)
    cells = g.quantize([(35, 0.0, 100), (35, 1.5, 100)])
    assert (0, 0) in cells
    assert (1, 0) in cells          # 第 4 拍在 3/4 下属小节 1
    assert (0, 1440) not in cells

def test_quantize_4_4_default_keeps_4th_beat_in_bar0():
    # 对照：默认 4/4（1920），1.5s（1440 tick）仍在小节 0
    cells = g.quantize([(35, 1.5, 100)])
    assert (0, 1440) in cells


# ---------- groove / rotation ----------

def test_best_rotation_zero_for_canonical_groove():
    # 底鼓在第 1 拍、军鼓在 2/4 拍、踩镲均匀 8 分 —— 标准律动，相位 0 应最优
    raw = [(35, 0.0, 100)]
    for t in (0.25, 0.75, 1.25, 1.75):
        raw.append((42, t, 90))
    raw.append((38, 0.5, 100))
    raw.append((38, 1.5, 100))
    rot, scores = g.best_rotation(raw)
    assert rot == 0
    assert scores[0][0] >= scores[-1][0]

def test_continuous_groove_aligned_beats_offset():
    raw_aligned = [(35, 0.0, 100), (38, 0.5, 100), (38, 1.5, 100)]
    raw_offset = [(35, 0.0625, 100), (38, 0.5625, 100), (38, 1.5625, 100)]  # 偏 1/2 格
    s_aligned = g.continuous_groove(raw_aligned, 0)
    s_offset = g.continuous_groove(raw_offset, 0)
    assert s_aligned > s_offset


# ---------- build_measures / validate ----------

def test_build_measures_bar_sums_to_full_bar():
    cells = {(0, 0): {35: 100}, (0, 480): {38: 100}}
    measures, stats = g.build_measures(cells)
    total = sum(d for _k, _t, d, _p, _ty in measures[0])
    assert total == 1920
    assert stats["notes"] == 2

def test_validate_accepts_wellformed_measures():
    measures = {0: [("note", 0, 480, {35: 100}, "quarter"),
                   ("rest", 480, 960, None, "half"),
                   ("note", 1440, 480, {38: 100}, "quarter")]}
    assert g.validate(measures) == []

def test_validate_catches_bad_total():
    measures = {0: [("note", 0, 480, {35: 100}, "quarter")]}  # 只有 480
    problems = g.validate(measures)
    assert any("时值合计" in p for p in problems)

def test_validate_catches_illegal_duration():
    measures = {0: [("note", 0, 77, {35: 100}, "quarter")]}
    problems = g.validate(measures)
    assert any("非法时值" in p for p in problems)

def test_validate_catches_type_mismatch():
    measures = {0: [("note", 0, 60, {35: 100}, "quarter")]}  # 60 = 32nd
    problems = g.validate(measures)
    assert any("不一致" in p for p in problems)


# ---------- calibrate ----------

def test_calibrate_sets_velocity_quantiles():
    saved = (g.ACCENT_VEL, g.GHOST_VEL)
    try:
        raw = [(35, i * 0.5, (i % 100) + 1) for i in range(100)]
        g.calibrate(raw)
        assert g.ACCENT_VEL == 89   # vels[100*0.88]=vels[88]=89（vels 为 1..100）
        assert g.GHOST_VEL == 9     # vels[100*0.08]=vels[8]=9
        assert g.ACCENT_VEL > g.GHOST_VEL
    finally:
        g.ACCENT_VEL, g.GHOST_VEL = saved

def test_calibrate_skips_tiny_input():
    saved = (g.ACCENT_VEL, g.GHOST_VEL)
    try:
        g.calibrate([(35, 0.0, 100)])
        assert g.ACCENT_VEL == saved[0]
    finally:
        g.ACCENT_VEL, g.GHOST_VEL = saved


# ---------- 端到端冒烟：MIDI -> MusicXML ----------

def test_e2e_midi_to_musicxml(make_midi, tmp_path, monkeypatch):
    mid = make_midi([(35, 0.0, 110), (42, 0.25, 90), (38, 0.5, 110),
                     (42, 0.75, 90), (42, 1.25, 90), (38, 1.5, 110)],
                    out_path=tmp_path / "song.mid", bpm=120.0)
    out = tmp_path / "song.musicxml"
    monkeypatch.setattr(g, "MIDI", str(mid))
    monkeypatch.setattr(g, "OUT", str(out))
    monkeypatch.setattr(g, "BPM", 120.0)
    monkeypatch.setattr(g, "TITLE", "Test Song")
    monkeypatch.setattr(g, "MARKERS_FILE", None)
    monkeypatch.setattr(g, "BARS_PER_LINE", 4)
    monkeypatch.setattr(g, "GRID", 16)
    monkeypatch.setattr(g, "PLAN", None)
    assert g.main() == 0
    assert out.exists()
    xml = out.read_text(encoding="utf-8")
    assert "<score-partwise" in xml
    assert "<measure number=\"1\">" in xml
    assert "<part-name>Drum Kit</part-name>" in xml
    # 4/4、120bpm、2 小节（最后音符在 1.5s = 第 2 小节内）
    assert "<beats>4</beats>" in xml
    # 音符确实被写出来了
    assert "<unpitched>" in xml

def test_e2e_empty_midi_fails(make_midi, tmp_path, monkeypatch):
    mid = make_midi([], out_path=tmp_path / "empty.mid", bpm=120.0)
    out = tmp_path / "empty.musicxml"
    monkeypatch.setattr(g, "MIDI", str(mid))
    monkeypatch.setattr(g, "OUT", str(out))
    monkeypatch.setattr(g, "PLAN", None)
    with pytest.raises(SystemExit):
        g.main()
    assert not out.exists()
