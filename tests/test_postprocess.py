# -*- coding: utf-8 -*-
"""postprocess.py 测试：去重 / 最短时值 / 摇摆。"""
import pytest

import postprocess as pp


def _note(pitch, start, end, vel=100, channel=0):
    return {"pitch": pitch, "start": start, "end": end,
            "velocity": vel, "channel": channel}


# ---------- dedupe ----------

def test_dedupe_removes_close_duplicate_keeps_louder():
    notes = [_note(35, 0.0, 0.05, vel=100), _note(35, 0.02, 0.07, vel=120)]
    out, removed = pp.dedupe(notes, 50)
    assert removed == 1
    assert len(out) == 1
    assert out[0]["velocity"] == 120

def test_dedupe_keeps_first_when_first_louder():
    notes = [_note(35, 0.0, 0.05, vel=120), _note(35, 0.02, 0.07, vel=100)]
    out, removed = pp.dedupe(notes, 50)
    assert removed == 1
    assert out[0]["velocity"] == 120

def test_dedupe_disabled_at_zero():
    notes = [_note(35, 0.0, 0.05), _note(35, 0.02, 0.07)]
    out, removed = pp.dedupe(notes, 0)
    assert removed == 0
    assert len(out) == 2

def test_dedupe_keeps_different_pitches():
    notes = [_note(35, 0.0, 0.05), _note(38, 0.02, 0.07)]
    out, removed = pp.dedupe(notes, 50)
    assert removed == 0
    assert len(out) == 2


# ---------- min_length ----------

def test_min_length_extends_short_note():
    notes = [_note(35, 0.0, 0.01)]
    out, changed = pp.min_length(notes, 30)
    assert changed == 1
    assert out[0]["end"] == pytest.approx(0.03)

def test_min_length_does_not_overshoot_next_same_pitch():
    notes = [_note(35, 0.0, 0.01), _note(35, 0.03, 0.05)]
    out, changed = pp.min_length(notes, 30)
    assert changed == 2
    assert out[0]["end"] <= 0.03 - 0.001 + 1e-9

def test_min_length_disabled_at_zero():
    notes = [_note(35, 0.0, 0.01)]
    out, changed = pp.min_length(notes, 0)
    assert changed == 0
    assert out[0]["end"] == 0.01


# ---------- swing ----------

def test_swing_moves_only_offbeat_sixteenths():
    notes = [_note(35, 0.25, 0.3), _note(38, 0.0, 0.1)]
    out, changed = pp.swing(notes, 0.5)
    assert changed == 1
    assert out[0]["start"] > 0.25     # 反拍右移
    assert out[1]["start"] == 0.0     # 正拍不动

def test_swing_disabled_at_zero():
    notes = [_note(35, 0.25, 0.3)]
    out, changed = pp.swing(notes, 0)
    assert changed == 0
    assert out[0]["start"] == 0.25


# ---------- write_midi 冒烟 ----------

def test_write_midi_roundtrip(make_midi, tmp_path):
    notes = [_note(35, 0.0, 0.1, vel=110), _note(38, 0.5, 0.6, vel=100)]
    out = tmp_path / "pp.mid"
    pp.write_midi(notes, str(out), bpm=120.0)
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(str(out))
    got = [(n.pitch, round(n.start, 3), n.velocity) for i in pm.instruments for n in i.notes]
    assert got == [(35, 0.0, 110), (38, 0.5, 100)]
