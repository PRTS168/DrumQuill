# -*- coding: utf-8 -*-
"""把转录结果对齐到节拍栅格（相位对齐 + 软吸附），比原来的"硬吸附到 16 分"准得多。

用法：
  fix_align.py <in.mid> <out.mid> <bpm> [--auto|--offset 秒] [--tol 0.25] [--grid 16] [--no-quant]

背景（实测，参考谱 = 人工扒的 60s+90-150s 两段）：
  · 原来"先把第一击对齐到 0、再把所有音符硬吸到 16 分"会把逐类微平均 F1 从 0.897 打到 0.832
    （-6.5 点）——因为这首曲子的实际演奏并不严格落在 148 BPM 的 16 分栅格上，
    硬吸等于把每个音符平均挪 25ms、最多挪 50ms。
  · 改成"相位对齐 + 只吸附偏离栅格 <25% 十六分 的音符"后：F1 与不量化完全一致（0.897/0.858），
    而绝大多数音符（实测 55~75%）落在整数栅格上，谱面照样干净。
  · 容差可用 --tol 调（0 = 完全不量化）；--grid 默认 16（十六分音符）。

打印：相位、吸附比例、平均/最大位移，便于排查"谱面为什么歪"。
"""
import sys

import numpy as np
import pretty_midi


def parse_args(argv):
    args = {"in": None, "out": None, "bpm": 0.0, "offset": None,
            "auto": False, "tol": 0.25, "grid": 16, "quant": True}
    pos = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--auto":
            args["auto"] = True
        elif a == "--no-quant":
            args["quant"] = False
        elif a in ("--tol", "--grid", "--offset"):
            i += 1
            val = float(argv[i])
            args[a[2:]] = val
        else:
            pos.append(a)
        i += 1
    if len(pos) >= 3:
        args["in"], args["out"], args["bpm"] = pos[0], pos[1], float(pos[2])
    return args


def dominant_phase(times, period, tol_ratio=0.25, steps=400):
    """找出让最多音符落在栅格上的相位（0..period）。"""
    t = np.asarray(times, dtype=float)
    if t.size == 0:
        return 0.0
    best_ph, best_cnt = 0.0, -1
    for i in range(steps):
        ph = i * period / steps
        r = np.abs((t - ph) % period)
        r = np.minimum(r, period - r)
        cnt = int((r <= tol_ratio * period).sum())
        if cnt > best_cnt:
            best_ph, best_cnt = ph, cnt
    return best_ph


def main(argv):
    a = parse_args(argv)
    if not a["in"] or not a["out"] or not a["bpm"]:
        sys.stderr.write(__doc__)
        return 2
    bpm = float(a["bpm"])
    period = 60.0 / bpm * 4.0 / float(a["grid"])  # 一个栅格步长（秒）

    pm = pretty_midi.PrettyMIDI(a["in"])
    notes = [(inst, n) for inst in pm.instruments for n in inst.notes]
    if not notes:
        sys.stderr.write("输入 MIDI 没有音符\n")
        return 1
    onsets = np.array([n.start for _i, n in notes], dtype=float)

    # 1) 相位对齐：让栅格线落在音符最密的位置，而不是"第一击 = 0"
    phase0 = dominant_phase(onsets, period, tol_ratio=min(0.5, max(0.1, a["tol"])))
    if a["auto"] or a["offset"] is None:
        offset = phase0
    else:
        offset = float(a["offset"])
    print("grid: bpm=%.2f 1/%d=%.1fms  phase=%.1fms  offset=%.1fms"
          % (bpm, int(a["grid"]), period * 1000, phase0 * 1000, offset * 1000))

    # 2) 软吸附：只在偏离栅格 < tol*步长 时吸附，其余保留原时间（保住准确度）
    moved, shifts, residuals = 0, [], []
    for _inst, n in notes:
        t = n.start - offset
        if t < 0:
            t = 0.0
        k = round(t / period)
        target = k * period
        d = target - t
        residuals.append(abs(d))
        if a["quant"] and abs(d) <= a["tol"] * period:
            n.start = target
            t = target
            moved += 1
            shifts.append(abs(d))
        else:
            n.start = t
        n.end = n.start + 0.1
    pm.write(a["out"])

    res_ms = np.array(residuals) * 1000
    print("notes=%d  snapped=%d (%.0f%%)  mean_shift=%.1fms  max_shift=%.1fms  "
          "median_residual=%.1fms  max_residual=%.1fms"
          % (len(notes), moved, 100.0 * moved / max(1, len(notes)),
             (np.mean(shifts) * 1000 if shifts else 0.0),
             (np.max(shifts) * 1000 if shifts else 0.0),
             float(np.median(res_ms)), float(np.max(res_ms))))
    print("saved:", a["out"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
