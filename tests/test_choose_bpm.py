# -*- coding: utf-8 -*-
"""choose_bpm.py 测试：速度八度校验与精细拟合。"""
import numpy as np

import choose_bpm as cb


# ---------- score_for ----------

def test_score_for_prefers_double_time_when_snare_is_1_beat():
    # 军鼓间隔 0.5s：@120bpm 是 1 拍（异常），@240bpm 是 2 拍（正常）
    s120 = cb.score_for(120, 0.5, 0.5)
    s240 = cb.score_for(240, 0.5, 0.5)
    assert s240 < s120

def test_score_for_prefers_half_time_when_snare_is_4_beats():
    # 军鼓间隔 2.0s：@120bpm 是 4 拍（异常），@60bpm 是 2 拍（正常）
    s120 = cb.score_for(120, 2.0, 2.0)
    s60 = cb.score_for(60, 2.0, 2.0)
    assert s60 < s120


# ---------- median_beat_interval ----------

def test_median_beat_interval_basic():
    assert cb.median_beat_interval([0.0, 0.5, 1.0]) == 0.5

def test_median_beat_interval_needs_three():
    assert cb.median_beat_interval([0.0, 0.5]) is None
    assert cb.median_beat_interval([0.0]) is None

def test_median_beat_interval_filters_same_cluster():
    # diff=[0.01, 0.99, 1.0] -> 过滤 0.01 -> [0.99, 1.0] -> 中位 0.995
    assert cb.median_beat_interval([0.0, 0.01, 1.0, 2.0]) == 0.995


# ---------- continuous_score / best_rot_continuous ----------

def test_continuous_score_aligned_beats_offset():
    times = np.array([0.0, 0.5, 1.0, 1.5, 2.0])
    pitches = np.array([35, 38, 35, 38, 35])
    s0 = cb.continuous_score(times, pitches, 120.0, rot_cells=0)
    s4 = cb.continuous_score(times, pitches, 120.0, rot_cells=4)
    assert s0 > s4

def test_best_rot_continuous_returns_zero_for_aligned():
    # kick 第 1 拍、snare 2/4 拍：rot=0 唯一最优（避免对称平局）
    times = np.array([0.0, 0.5, 1.5])
    pitches = np.array([35, 38, 38])
    rot, score = cb.best_rot_continuous(times, pitches, 120.0)
    assert rot == 0
    assert score > 0


# ---------- refine_by_tightness ----------

def test_refine_by_tightness_finds_correct_bpm():
    # 构造精确落在 100 BPM 16 分栅格上的时间
    beat = 60.0 / 100.0
    times = np.array([k * beat / 4.0 for k in range(64)])
    best_bpm, tight, improve = cb.refine_by_tightness(times, 100.0, span=0.05, step=0.0002)
    assert abs(best_bpm - 100.0) < 0.5
    assert tight < 0.05

def test_refine_by_tightness_needs_enough_notes():
    times = np.array([0.0, 0.3, 0.6])
    best_bpm, tight, improve = cb.refine_by_tightness(times, 100.0)
    assert best_bpm == 100.0
    assert tight is None


# ---------- aligned_pairs（P1-5 修复：时间/音高下标对齐）----------

def test_aligned_pairs_sorts_and_keeps_alignment():
    # 时间交错：kick 在 0.0/1.0，snare 在 0.5/1.5（per 按 pitch 分组）
    per = {38: np.array([0.5, 1.5]), 35: np.array([0.0, 1.0])}
    times, pitches = cb.aligned_pairs(per)
    assert list(times) == [0.0, 0.5, 1.0, 1.5]      # 时间整体排序
    assert list(pitches) == [35, 38, 35, 38]        # 每个时间对应正确音高

def test_aligned_pairs_rotation_correct_for_interleaved():
    # 交错律动：对齐后在正确 BPM 下 rot=0（旧错位会系统性偏）
    per = {38: np.array([0.5, 1.5, 2.5]), 35: np.array([0.0, 1.0, 2.0])}
    times, pitches = cb.aligned_pairs(per)
    rot, _ = cb.best_rot_continuous(times, pitches, 120.0)
    assert rot == 0
