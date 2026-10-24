# -*- coding: utf-8 -*-
"""fix_align.py 测试：相位对齐与参数解析。"""
import fix_align as fa


# ---------- dominant_phase ----------

def test_dominant_phase_finds_exact_phase():
    # 返回"扫描序中第一个达到最大命中数的相位"；断言落在容差覆盖范围且全量命中
    times = [0.1, 0.6, 1.1, 1.6]     # period=0.5s，真实相位 0.1
    tol = 0.05 * 0.5                  # 0.025s
    step = 0.5 / 400                  # 1.25ms 扫描粒度
    ph = fa.dominant_phase(times, 0.5, tol_ratio=0.05)
    assert abs(ph - 0.1) <= tol + step + 1e-9
    for t in times:
        r = min(abs((t - ph) % 0.5), 0.5 - abs((t - ph) % 0.5))
        assert r <= tol + 1e-9

def test_dominant_phase_zero_phase():
    times = [0.0, 0.5, 1.0, 1.5]
    ph = fa.dominant_phase(times, 0.5)
    assert ph == 0.0

def test_dominant_phase_empty():
    assert fa.dominant_phase([], 0.5) == 0.0

def test_dominant_phase_tolerates_jitter():
    times = [0.10, 0.49, 1.02, 1.51]  # 轻微抖动仍在 tol 内
    ph = fa.dominant_phase(times, 0.5, tol_ratio=0.25)
    assert abs(ph - 0.0) < 0.01 or abs(ph - 0.1) < 0.02


# ---------- parse_args ----------

def test_parse_args_positional_and_flags():
    a = fa.parse_args(["fix_align.py", "in.mid", "out.mid", "120", "--auto", "--tol", "0.3"])
    assert a["in"] == "in.mid"
    assert a["out"] == "out.mid"
    assert a["bpm"] == 120.0
    assert a["auto"] is True
    assert a["tol"] == 0.3

def test_parse_args_defaults():
    a = fa.parse_args(["fix_align.py", "in.mid", "out.mid", "90"])
    assert a["tol"] == 0.25
    assert a["grid"] == 16
    assert a["quant"] is True
    assert a["offset"] is None

def test_parse_args_no_quant():
    a = fa.parse_args(["fix_align.py", "in.mid", "out.mid", "90", "--no-quant"])
    assert a["quant"] is False
