# -*- coding: utf-8 -*-
"""align_to_beats.py 测试：拍先验与整拍相位校验。"""
import json
import sys

import pretty_midi
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


def test_load_beats_fallback_marks_leading_pickup(tmp_path):
    # 旧格式文件（无 beat_bars）：bars 只覆盖强拍之后的拍，开头的弱起记 -1
    beats_json = {
        "beats": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5],
        "bars": [
            {"start": 1.0, "end": 3.0, "bpm": 120.0, "beats": [1.0, 1.5, 2.0, 2.5]},
            {"start": 3.0, "end": 5.0, "bpm": 120.0, "beats": [3.0, 3.5, 4.0, 4.5]},
        ],
        "meter": 4,
        "bpm_median": 120.0,
    }
    p = tmp_path / "beats.json"
    p.write_text(json.dumps(beats_json), encoding="utf-8")
    _beats, _db, _bars, _meter, _bpm, beat_bar, beat_k = ab.load_beats(str(p))
    assert list(beat_bar) == [-1, -1, 0, 0, 0, 0, 1, 1, 1, 1]
    assert list(beat_k) == [-1, -1, 0, 1, 2, 3, 0, 1, 2, 3]


# ---------- best_beat_shift：同槽多音高 ----------

def test_best_beat_shift_multi_pitch_same_slot():
    # kick+hat 同槽 + 单音高军鼓槽：旧实现拍位按槽记、音高按音高 extend，
    # zip 把 hat 记到军鼓的拍位上、军鼓被截断，选出错误相位（shift=0）；
    # 修复后 (拍,音高) 严格配对 → 应平移 1 拍。
    cells = {0: {35: 100, 42: 90}, 960: {38: 100}}
    shift, base, best = ab.best_beat_shift(cells, meter=4, bar_ticks=1920)
    assert shift == 1
    assert best > base


def test_best_beat_shift_multi_pitch_stays_when_aligned():
    # kick+hat 落 1/3 拍、军鼓落 2/4 拍（0-based 0/2 与 1/3）：相位已正确，不平移
    cells = {
        0: {35: 100, 42: 90},
        480: {38: 100},
        960: {35: 100, 42: 90},
        1440: {38: 100},
    }
    shift, base, best = ab.best_beat_shift(cells, meter=4, bar_ticks=1920)
    assert shift == 0


# ---------- main()：弱起拍跳过 ----------

def test_main_skips_notes_on_pickup_beats(tmp_path, monkeypatch, make_midi):
    # beats.json 用 -1 标记弱起拍：落在弱起上的音符不写入栅格，强拍后的照常落格
    beats = [round(i * 0.5, 4) for i in range(10)]
    beats_json = {
        "beats": beats,
        "downbeats": [1.0],
        "meter": 4,
        "bpm_median": 120.0,
        "bars": [
            {"start": 1.0, "end": 3.0, "bpm": 120.0, "beats": beats[2:6]},
            {"start": 3.0, "end": 5.0, "bpm": 120.0, "beats": beats[6:10]},
        ],
        "beat_bars": [-1, -1, 0, 0, 0, 0, 1, 1, 1, 1],
        "beat_positions": [-1, -1, 0, 1, 2, 3, 0, 1, 2, 3],
    }
    bj = tmp_path / "song.beats.json"
    bj.write_text(json.dumps(beats_json), encoding="utf-8")
    # 起音：0.05s 落在弱起拍（应跳过）；1.05s 落在第 1 小节第 1 拍；2.55s 落在第 1 小节第 4 拍
    in_mid = make_midi([(38, 0.05, 100), (35, 1.05, 100), (38, 2.55, 100)], bpm=120.0)
    out_mid = tmp_path / "out.mid"
    plan_json = tmp_path / "plan.json"
    monkeypatch.setattr(sys, "argv",
                        ["align_to_beats.py", str(in_mid), str(bj), str(out_mid), str(plan_json)])
    assert ab.main() == 0

    plan = json.loads(plan_json.read_text(encoding="utf-8"))
    assert plan["skipped_pickup"] == 1
    assert plan["cells_out"] == 2

    pm = pretty_midi.PrettyMIDI(str(out_mid))
    starts = sorted(round(n.start, 3) for n in pm.instruments[0].notes)
    assert starts == [0.0, 1.5]   # tick 0 与 tick 1440，按 120 BPM 换算
