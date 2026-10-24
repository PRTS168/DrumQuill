# -*- coding: utf-8 -*-
"""速度八度校验：判断检测到的 BPM 是不是被算成了 2 倍（或一半）。

思路：鼓组的军鼓踩在 2、4 拍，所以**相邻军鼓的间隔通常在 2 拍左右**；
底鼓间隔多在 1~2 拍。检测到的 BPM 若偏快一倍，军鼓间隔就会算成 4 拍（很少见），
偏慢一倍则会算成 1 拍（等于每拍都在打军鼓，也不符合常规律动）。

用法：
  python choose_bpm.py <in.mid> <detected_bpm> [--json]
输出：选取后的 BPM 与判据明细（标准输出一行 JSON）。
"""
import json
import sys

import numpy as np
import pretty_midi

DIV = 480


def load_ticks(path):
    pm = pretty_midi.PrettyMIDI(path)
    per = {}
    for inst in pm.instruments:
        for n in inst.notes:
            per.setdefault(int(n.pitch), []).append(float(n.start))
    return {k: np.array(sorted(v)) for k, v in per.items()}


def aligned_pairs(per):
    """把 {pitch: [times]} 展开成 (times, pitches)，整体按时间排序，
    保证时间与音高下标严格对齐（旧代码时间全局排序、音高按分组展开，下标错位）。"""
    pairs = sorted((t, p) for p, v in per.items() for t in v)
    return (np.array([t for t, _ in pairs]),
            np.array([p for _, p in pairs]))


def median_beat_interval(times, bpm_dummy=None):
    """以秒为单位的相邻间隔中位数。"""
    if len(times) < 3:
        return None
    d = np.diff(times)
    d = d[d > 0.02]          # 丢掉同簇重复
    if len(d) == 0:
        return None
    return float(np.median(d))


def score_for(bpm, snare_iv, kick_iv):
    """军鼓间隔越接近 2 拍、底鼓越接近 1~2 拍，分数越低越好。"""
    beat = 60.0 / bpm
    s = 0.0
    if snare_iv:
        s += abs(snare_iv / beat - 2.0) * 1.0
    if kick_iv:
        kb = kick_iv / beat
        s += abs(kb - 1.5) * 0.5
    return round(s, 3)


# ---- 连续律动评分（用于 BPM 精细拟合，避免半格翻转）----
DIV, CELL, BAR = 480, 120, 1920
KICK, SNARE, HI, TOM, CYMBAL = 35, 38, 42, 47, 49
# 每个鼓件"期望落点"（十六分格），权重按惯例给
TARGETS = {
    KICK: {0: 1.2, 8: 1.0, 4: 0.4, 12: 0.4},
    SNARE: {4: 1.0, 12: 1.0},
    CYMBAL: {0: 1.8, 8: 0.5},
    HI: {0: 0.5, 2: 0.5, 4: 0.5, 6: 0.5, 8: 0.5, 10: 0.5, 12: 0.5, 14: 0.5},
    TOM: {14: 0.3, 15: 0.3, 0: 0.3, 1: 0.3},
}
SIGMA = 0.35          # 以十六分格为单位


def continuous_score(times, pitches, bpm, rot_cells=0):
    """返回整首歌的连续律动分（越大越好）。"""
    if len(times) == 0:
        return 0.0
    t = times / (60.0 / bpm) * DIV
    g = ((t - rot_cells * CELL) % BAR) / CELL        # 小数格位置 0..16
    total = 0.0
    for pos, p in zip(g, pitches):
        targets = TARGETS.get(int(p))
        if not targets:
            continue
        best = 0.0
        for target, w in targets.items():
            d = abs((pos - target + 8) % 16 - 8)      # 环绕距离
            best = max(best, w * pow(2.718281828, -(d * d) / (2 * SIGMA * SIGMA)))
        total += best
    return total


def best_rot_continuous(times, pitches, bpm):
    scores = [(continuous_score(times, pitches, bpm, r), r) for r in range(16)]
    scores.sort(reverse=True)
    return scores[0][1], scores[0][0]


def refine_bpm(times, pitches, bpm, span=0.008, step=0.0002):
    """在 ±span 范围内微调 BPM，返回 (最佳BPM, 分数, 相位)。"""
    best = (bpm, best_rot_continuous(times, pitches, bpm)[1],
            best_rot_continuous(times, pitches, bpm)[0])
    n = int(span / step)
    for k in range(-n, n + 1):
        b = bpm * (1.0 + k * step)
        rot, sc = best_rot_continuous(times, pitches, b)
        if sc > best[2] + 1e-9:
            best = (b, rot, sc)
    return best


def tightness(times, bpm):
    """平均"到最近十六分格"的偏差（以格为单位，越小越贴）。随机起音约 0.25。"""
    t = times / (60.0 / bpm) * DIV / CELL
    frac = np.abs(t - np.round(t))
    return float(np.mean(frac))


def refine_by_tightness(times, bpm, span=0.05, step=0.0002):
    """在 ±span 内按贴合度细扫，返回 (最佳BPM, 贴合度, 相对改善)。"""
    if len(times) < 8:
        return bpm, None, 0.0
    base = tightness(times, bpm)
    best_b, best_v = bpm, base
    n = int(span / step)
    for k in range(-n, n + 1):
        b = bpm * (1.0 + k * step)
        v = tightness(times, b)
        if v < best_v:
            best_b, best_v = b, v
    improve = (base - best_v) / base if base > 0 else 0.0
    return best_b, best_v, improve


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    mid, bpm = sys.argv[1], float(sys.argv[2])
    want_json = "--json" in sys.argv
    per = load_ticks(mid)
    snare_iv = median_beat_interval(per.get(38, []))
    kick_iv = median_beat_interval(per.get(35, []))

    cands = []
    for factor, label in ((0.5, "half"), (1.0, "keep"), (2.0, "double")):
        b = bpm * factor
        cands.append({
            "bpm": round(b, 2),
            "factor": factor,
            "label": label,
            "score": score_for(b, snare_iv, kick_iv),
            "snare_beats": round(snare_iv / (60.0 / b), 2) if snare_iv else None,
            "kick_beats": round(kick_iv / (60.0 / b), 2) if kick_iv else None,
        })
    cands.sort(key=lambda c: c["score"])
    best = cands[0]
    # 只有明显更好时才切换，避免抖动
    keep = next(c for c in cands if c["label"] == "keep")
    chosen = best if best["score"] + 0.35 < keep["score"] else keep

    # 精细拟合：用"起音贴栅格度"在 ±5% 内细扫（librosa 检测常偏 1~3%）
    # 时间与音高整体按时间排序对齐（旧代码下标错位导致 rotation 系统性偏差）
    all_times, all_pitches = aligned_pairs(per)
    refined_bpm, tight, improve = refine_by_tightness(all_times, chosen["bpm"])
    if improve > 0.12 and abs(refined_bpm - chosen["bpm"]) > 0.05:
        detail = "（起音贴合度 %.3f，微调 %+.2f%%）" % (tight, (refined_bpm / chosen["bpm"] - 1) * 100)
        rot, _sc = best_rot_continuous(all_times, all_pitches,
                                       refined_bpm)
    else:
        refined_bpm = chosen["bpm"]
        detail = ""
        rot, _sc = best_rot_continuous(all_times, all_pitches, refined_bpm)

    out = {
        "detected": bpm,
        "chosen": round(refined_bpm, 2),
        "label": chosen["label"] + ("+refit" if detail else ""),
        "refit": detail,
        "rotation": rot,
        "snare_interval_s": round(snare_iv, 3) if snare_iv else None,
        "kick_interval_s": round(kick_iv, 3) if kick_iv else None,
        "candidates": cands,
    }
    if want_json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print("检测 %.2f BPM；军鼓间隔 %.0f ms（%.2f 拍 @%.2f）"
              % (bpm, (snare_iv or 0) * 1000, (snare_iv or 0) / (60.0 / bpm), bpm))
        for c in cands:
            print("  %-6s %7.2f BPM  军鼓 %.2f 拍  底鼓 %.2f 拍  分 %.2f"
                  % (c["label"], c["bpm"], c["snare_beats"] or 0, c["kick_beats"] or 0, c["score"]))
        print("采用 %.2f BPM（%s）%s" % (out["chosen"], chosen["label"], detail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
