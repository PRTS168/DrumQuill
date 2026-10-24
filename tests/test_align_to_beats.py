# -*- coding: utf-8 -*-
"""align_to_beats.py 测试：拍先验与整拍相位校验。"""
import json

import pytest

import align_to_beats as ab


# ---------- beat_prior ----------

def test_beat_prior_rewards_snare_on_2_and_4():
    # 0-based beat 1,3 -> 1-based 2,4（4/4 军鼓标准落点）
    s = ab.beat_prior([1, 3], [38, 38], meter=4)
    assert s == pytest.approx(2.4)

def test_beat_prior_penalizes_snare_on_1_and_3():
    s = ab.beat_prior([0, 2], [38, 38], meter=4)
    assert s < 0

def test_beat_prior_kick_on_one():
    s = ab.beat_prior([0], [35], meter=4)
    assert s == pytest.approx(1.0)


# ---------- best_beat_shift ----------

def test_best_beat_shift_finds_one_beat_shift():
    # 军鼓落在 0-based beat 0、2（1/3 拍）：整体后移 1 拍才符合 2/4 拍
    cells = {0: {38: 100}, 960: {38: 100}}
    shift, base, best = ab.best_beat_shift(cells, meter=4, bar_ticks=1920)
    assert shift == 1
    assert best > base

def test_best_beat_shift_keeps_zero_when_already_good():
    cells = {480: {38: 100}, 1440: {38: 100}}
    shift, base, best = ab.best_beat_shift(cells, meter=4, bar_ticks=1920)
    assert shift == 0


# ---------- load_beats ----------

def test_load_beats_fallback_assigns_beats_in_order(tmp_path):
    beats_json = {
        "beats": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5],
        "bars": [
            {"start": 0.0, "end": 2.0, "bpm": 120.0, "beats": [0.0, 0.5, 1.0, 1.5]},
            {"start": 2.0, "end": 4.0, "bpm": 120.0, "beats": [2.0, 2.5, 3.0, 3.5]},
        ],
        "meter": 4,
        "bpm_median": 120.0,
    }
    p = tmp_path / "beats.json"
    p.write_text(json.dumps(beats_json), encoding="utf-8")
    beats, downbeats, bars, meter, bpm, beat_bar, beat_k = ab.load_beats(str(p))
    assert meter == 4
    assert list(beat_bar) == [0, 0, 0, 0, 1, 1, 1, 1]
    assert list(beat_k) == [0, 1, 2, 3, 0, 1, 2, 3]
