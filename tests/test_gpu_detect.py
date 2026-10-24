# -*- coding: utf-8 -*-
"""gpu_detect.py 测试：显卡识别与后端选择（全部纯函数，不碰真实 GPU）。"""
import gpu_detect as gd


# ---------- 名称 -> 计算能力 ----------

def test_name_to_cc_known_cards():
    assert gd._name_to_cc("NVIDIA GeForce RTX 4090") == (8, 9)
    assert gd._name_to_cc("NVIDIA A100 SXM4") == (8, 0)
    assert gd._name_to_cc("NVIDIA GeForce GTX 1060") == (6, 1)
    assert gd._name_to_cc("NVIDIA V100") == (7, 0)

def test_name_to_cc_unknown_returns_none():
    assert gd._name_to_cc("Intel UHD Graphics") is None
    assert gd._name_to_cc("") is None


# ---------- 驱动 -> CUDA 版本 ----------

def test_driver_to_cuda_mapping():
    assert gd._driver_to_cuda(580) == "13.0"
    assert gd._driver_to_cuda(570) == "12.8"
    assert gd._driver_to_cuda(550) == "12.4"
    assert gd._driver_to_cuda(520) == "11.8"

def test_driver_to_cuda_too_old_or_invalid():
    assert gd._driver_to_cuda(500) is None
    assert gd._driver_to_cuda("abc") is None
    assert gd._driver_to_cuda("") is None


# ---------- pick_backend ----------

def test_pick_backend_no_gpu():
    r = gd.pick_backend([])
    assert r["ok"] is False

def test_pick_backend_too_old_card():
    # Kepler 3.7 < 5.0：没有任何 torch CUDA 轮子
    r = gd.pick_backend([{"name": "TITAN", "vram_mb": 6144, "driver": "580", "cc": [3, 7]}])
    assert r["ok"] is False

def test_pick_backend_modern_card_no_driver_conservative():
    r = gd.pick_backend([{"name": "RTX 4090", "vram_mb": 24576, "driver": "", "cc": [8, 9]}])
    assert r["ok"] is True
    assert r["tag"] == "cu121"   # 拿不到驱动 -> 保守选 cu121

def test_pick_backend_pascal_with_driver():
    r = gd.pick_backend([{"name": "GTX 1060", "vram_mb": 6144, "driver": "560", "cc": [6, 1]}])
    assert r["ok"] is True
    assert r["tag"] == "cu124"

def test_pick_backend_missing_cc():
    r = gd.pick_backend([{"name": "NVIDIA GeForce RTX 4090", "vram_mb": 24576, "driver": "", "cc": None}])
    assert r["ok"] is False


# ---------- 显存 -> 段长 ----------

def test_best_segment_by_vram():
    assert gd.best_segment(2000) == 4
    assert gd.best_segment(4000) == 5
    assert gd.best_segment(7000) == 7
    assert gd.best_segment(99999) == 7

def test_segment_ladder_descending():
    assert gd.segment_ladder(4000) == [5, 3]
    assert gd.segment_ladder(2000) == [4, 3]
    assert gd.segment_ladder(8000, preferred=7) == [7, 5, 3]
    assert gd.segment_ladder(0) == [4, 3]

def test_segment_ladder_clamps_preferred():
    assert gd.segment_ladder(8000, preferred=8) == [7, 5, 3]
    assert gd.segment_ladder(8000, preferred=2) == [3]
