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


# ---------- P0 修复：rot 位移按全曲取模回卷，音符数守恒（4/4 半拍律动 rot>0） ----------

# 4/4 半拍律动整体拖后一个十六分格（rot=1 能对齐），且 bar0/bar1 各有 cell0 音符：
# 旧回卷只补一根小节线，bar0 cell0 与 bar1 cell0 会同时落到 (0,1800) 互相覆盖。
HALF_TIME_2BARS = [
    (49, 0.0, 100),                                   # bar0 cell0：crash
    (35, 0.125, 100), (42, 0.125, 100),               # bar0 cell1：底鼓+踩镲
    (42, 0.375, 100), (42, 0.625, 100), (42, 0.875, 100),
    (38, 1.125, 100), (42, 1.125, 100),               # bar0 cell9（第 3 拍）：军鼓+踩镲
    (42, 1.375, 100), (42, 1.625, 100), (42, 1.875, 100),
    (35, 2.0, 100),                                   # bar1 cell0：底鼓（回卷撞键对）
    (35, 2.125, 100), (42, 2.125, 100),
    (42, 2.375, 100), (42, 2.625, 100), (42, 2.875, 100),
    (38, 3.125, 100), (42, 3.125, 100),
    (42, 3.375, 100), (42, 3.625, 100), (42, 3.875, 100),
]


def test_best_rotation_prefers_rot_over_zero_for_dragged_half_time(monkeypatch):
    monkeypatch.setattr(g, "BEAT_S", 0.5)
    rot, scores = g.best_rotation(HALF_TIME_2BARS)
    assert rot > 0                       # 拖后一格的半拍律动不应停在 rot=0
    assert rot == scores[0][1]           # 返回的是最高分相位
    best = max(s for s, _r in scores)
    assert rot == min(r for s, r in scores if s == best)   # 平局取更小 rot
    assert len(scores) == 16             # 4/4 搜索范围 = min(16, 4*4)


def test_e2e_half_time_rot_conserves_notes_and_keeps_first_bar(
        make_midi, tmp_path, monkeypatch):
    mid = make_midi(HALF_TIME_2BARS, out_path=tmp_path / "song.mid", bpm=120.0)
    out = tmp_path / "song.musicxml"
    monkeypatch.setattr(g, "MIDI", str(mid))
    monkeypatch.setattr(g, "OUT", str(out))
    monkeypatch.setattr(g, "BPM", 120.0)
    monkeypatch.setattr(g, "BEAT_S", 0.5)
    monkeypatch.setattr(g, "TITLE", "Half Time")
    monkeypatch.setattr(g, "MARKERS_FILE", None)
    monkeypatch.setattr(g, "BARS_PER_LINE", 4)
    monkeypatch.setattr(g, "GRID", 16)
    monkeypatch.setattr(g, "PLAN", None)
    assert g.main() == 0
    xml = out.read_text(encoding="utf-8")
    # 音符数守恒：22 个输入音符一个不丢（旧代码会覆盖掉 1 个）
    assert xml.count("<unpitched>") == 22
    m1 = xml.split('<measure number="1">')[1].split("</measure>")[0]
    # 首小节保留完整内容：10 个本小节音符 + bar1 cell0 回卷来的底鼓
    assert m1.count("<unpitched>") == 11
    assert m1.count('<instrument id="P1-I36"/>') == 2   # 两记底鼓都在
    assert m1.count('<instrument id="P1-I39"/>') == 1   # 军鼓也在


# ---------- P1 修复：PLAN（拍栅格）分支不做相位搜索，rot=0 不位移 ----------

def test_e2e_plan_3_4_skips_phase_search(make_midi, tmp_path, monkeypatch):
    # 3/4 华尔滋整体晚一拍（若错误地做相位搜索会选中 rot=4，全谱晚一拍）
    waltz = []
    for bar in range(2):
        base = bar * 1.5                       # 120 BPM、3/4：每小节 1.5s
        waltz += [(35, base + 0.5, 100),       # 第 2 拍底鼓
                  (38, base + 1.0, 100)]       # 第 3 拍军鼓
    mid = make_midi(waltz, out_path=tmp_path / "waltz.mid", bpm=120.0)
    out = tmp_path / "waltz.musicxml"
    monkeypatch.setattr(g, "MIDI", str(mid))
    monkeypatch.setattr(g, "OUT", str(out))
    monkeypatch.setattr(g, "BPM", 120.0)
    monkeypatch.setattr(g, "BEAT_S", 0.5)
    monkeypatch.setattr(g, "TITLE", "Waltz")
    monkeypatch.setattr(g, "MARKERS_FILE", None)
    monkeypatch.setattr(g, "BARS_PER_LINE", 4)
    monkeypatch.setattr(g, "GRID", 16)
    monkeypatch.setattr(g, "METER", 3)
    monkeypatch.setattr(g, "BAR_TICKS", 1440)
    monkeypatch.setattr(g, "PLAN", {"n_bars": 2, "meter": 3, "bpm_median": 120.0,
                                    "bars": [{"bpm": 120.0}, {"bpm": 120.0}]})

    def _boom(_raw):
        raise AssertionError("PLAN 模式不得做相位搜索")

    monkeypatch.setattr(g, "best_rotation", _boom)
    assert g.main() == 0
    xml = out.read_text(encoding="utf-8")
    assert xml.count("<unpitched>") == 4              # 音符数守恒
    assert "<beats>3</beats>" in xml
    m1 = xml.split('<measure number="1">')[1].split("</measure>")[0]
    lead_rest = m1.find("<rest/><duration>480</duration>")
    first_note = m1.find("<unpitched>")
    # rot=0：第 2 拍的底鼓保持在原位（小节头有 480 tick 前导休止），而非被搬到第 1 拍
    assert 0 <= lead_rest < first_note


# ---------- P1 修复：2/4 相位搜索范围 min(16, 4*METER)，平局取更小 rot ----------

def test_best_rotation_2_4_tie_picks_smallest_rot(monkeypatch):
    monkeypatch.setattr(g, "METER", 2)
    monkeypatch.setattr(g, "BAR_TICKS", 960)
    monkeypatch.setattr(g, "BEAT_S", 0.5)
    # 每格一个踩镖：任意相位得分完全相同 -> 纯平局
    raw = [(42, c * 0.125, 90) for c in range(8)]
    rot, scores = g.best_rotation(raw)
    assert len(scores) == 8                 # 搜索范围 = min(16, 4*2)，不再超出一个小节
    assert scores[0][0] == scores[1][0]     # 确实是平局
    assert rot == 0                         # 平局取更小 rot（旧代码 sort(reverse=True) 会取 7）
