# -*- coding: utf-8 -*-
"""把转录出的音符对齐到"真实拍栅格"（Beat This 的拍点），而不是全曲一个 BPM。

做法：
  1. 每个起音先找它落在哪一拍（beats[i] .. beats[i+1]）；
  2. 在该拍内部按局部位置量化到 16 分（sub = round(比例 × 4)）；
  3. 输出 MIDI：tick = 小节起点 + 拍内序号×480 + sub×120（divisions=480/拍）；
  4. 同时输出 plan.json：每小节的真实起止时间与速度，供 gen_musicxml 写速度映射。

这样速度漂移、轻重拍、非 4/4 都能跟着走，不需要全曲 BPM 常数与相位搜索。

用法：
  python align_to_beats.py <in.mid> <beats.json> <out.mid> <plan.json>
"""
import json
import sys

import numpy as np
import pretty_midi

DIV = 480            # 每拍 tick
SUB = 4              # 每拍 4 个十六分
CELL = DIV // SUB    # 120 tick


def load_beats(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    beats = np.array(d.get("beats") or [], dtype=float)
    bars = d.get("bars") or []
    beat_bar = d.get("beat_bars")
    beat_k = d.get("beat_positions")
    if not beat_bar or not beat_k:
        # 兜底：按小节顺序依次分配拍（不按时间过滤，避免边界抖动导致错位）
        beat_bar, beat_k, i = [], [], 0
        for bi, bar in enumerate(bars):
            for k in range(len(bar["beats"])):
                if i < len(beats):
                    beat_bar.append(bi)
                    beat_k.append(k)
                    i += 1
        while len(beat_bar) < len(beats):
            beat_bar.append(len(bars) - 1)
            beat_k.append(0)
    return (beats,
            np.array(d.get("downbeats") or [], dtype=float),
            bars,
            int(d.get("meter") or 4),
            float(d.get("bpm_median") or 120.0),
            np.array(beat_bar, dtype=int),
            np.array(beat_k, dtype=int))


KICK_P, SNARE_P, HI_P, TOM_P, CYMBAL_P = 35, 38, 42, 47, 49


def beat_prior(beat_of_note, pitches, meter):
    """给定"每拍属于小节第几拍"，用鼓类先验打分。"""
    score = 0.0
    for k, p in zip(beat_of_note, pitches):
        b = (k % meter) + 1                      # 1-based 拍号
        if p == SNARE_P:
            score += 1.2 if (meter >= 4 and b in (2, 4)) else (1.0 if (meter < 4 and b == 2) else -0.3)
        elif p == KICK_P:
            score += 1.0 if b == 1 else (0.8 if (meter >= 4 and b == 3) else 0.1)
        elif p == CYMBAL_P:
            score += 1.5 if b == 1 else -0.2
        elif p == TOM_P:
            score += 0.2 if b in (3, 4) else 0.0
    return score


def best_beat_shift(cells, meter, bar_ticks):
    """在整拍平移里选先验最优的相位；cells: {abs_tick: {pitch: vel}}。"""
    best = (None, -1e9)
    base = None
    for r in range(meter):
        beat_of_note, pitches = [], []
        for tick, slot in cells.items():
            local = (tick - r * DIV) % bar_ticks
            beat_of_note.append(local // DIV)
            pitches.extend(slot.keys())
        sc = beat_prior(beat_of_note, pitches, meter)
        if r == 0:
            base = sc
        if sc > best[1]:
            best = (r, sc)
    # 只有明显更好才改（0.5 分以上，约等于几十个音符的证据）
    if base is not None and best[1] - base < 0.5:
        return 0, base, best[1]
    return best[0], base, best[1]


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    in_mid, beats_json, out_mid, plan_json = sys.argv[1:5]

    beats, _downbeats, bars, meter, bpm, beat_bar, beat_k = load_beats(beats_json)
    if len(beats) < 8 or len(bars) < 1:
        print("[!] 拍点不足，无法对齐")
        return 1

    # 每小节起始 tick（按拍数累加）
    bar_start_tick = []
    tick = 0
    for bar in bars:
        bar_start_tick.append(tick)
        tick += DIV * len(bar["beats"])
    n_beats = len(beats)

    pm = pretty_midi.PrettyMIDI(in_mid)
    notes = [(float(n.start), int(n.pitch), int(n.velocity or 100))
             for inst in pm.instruments for n in inst.notes]
    if not notes:
        print("[!] 输入 MIDI 没有音符")
        return 1

    cells = {}          # abs_tick -> {pitch: velocity}
    clamped = 0
    for ts, pitch, vel in notes:
        if ts < beats[0]:
            idx, frac = 0, 0.0
        elif ts >= beats[-1]:
            idx, frac = n_beats - 1, 1.0
            clamped += 1
        else:
            idx = int(np.searchsorted(beats, ts, side="right") - 1)
            nxt = beats[idx + 1] if idx + 1 < len(beats) else beats[idx]
            frac = (ts - beats[idx]) / (nxt - beats[idx]) if nxt > beats[idx] else 0.0
            idx = min(idx, n_beats - 1)
        sub = int(round(frac * SUB))
        if sub >= SUB:
            idx = min(idx + 1, n_beats - 1)
            sub = 0
        bi = int(beat_bar[idx])
        abs_tick = int(bar_start_tick[bi] + int(beat_k[idx]) * DIV + sub * CELL)
        slot = cells.setdefault(abs_tick, {})
        slot[pitch] = max(slot.get(pitch, 0), vel)

    # 整拍相位校验（修正"军鼓落在 1、3 拍"这类半小节错位）
    bar_ticks = DIV * meter
    shift, base_score, best_score = best_beat_shift(cells, meter, bar_ticks)
    if shift:
        moved = {}
        for tick, slot in cells.items():
            new_tick = tick - shift * DIV
            moved[new_tick] = slot
        cells = moved
        print("整拍相位校验：按鼓类先验平移 %d 拍（先验分 %.1f -> %.1f）" % (shift, base_score, best_score))
    else:
        print("整拍相位校验：Beat This 的强拍相位通过（先验分 %.1f）" % base_score)

    if cells:
        lo = min(cells)
        if lo < 0:
            pad = (-lo + bar_ticks - 1) // bar_ticks * bar_ticks
            cells = {k + pad: v for k, v in cells.items()}

    new_pm = pretty_midi.PrettyMIDI(initial_tempo=bpm if bpm > 0 else 120.0)
    inst = pretty_midi.Instrument(program=1, is_drum=True)
    for abs_tick in sorted(cells):
        for pitch, vel in sorted(cells[abs_tick].items()):
            # 秒数必须按同一参考速度换算，否则下游按另一个 BPM 反算 tick 会整体拉伸
            sec_per_beat = 60.0 / (bpm if bpm > 0 else 120.0)
            start = abs_tick / float(DIV) * sec_per_beat
            inst.notes.append(pretty_midi.Note(velocity=vel, pitch=pitch,
                                               start=start, end=start + 0.1))
    new_pm.instruments.append(inst)
    new_pm.write(out_mid)

    plan = {
        "meter": meter,
        "bpm_median": bpm,
        "n_bars": len(bars),
        "bars": [{"index": i + 1, "start": b["start"], "end": b["end"],
                  "bpm": b["bpm"], "beats": len(b["beats"])} for i, b in enumerate(bars)],
        "notes_in": len(notes),
        "cells_out": len(cells),
        "clamped_tail": clamped,
    }
    with open(plan_json, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)

    print("对齐完成：%d 个起音 -> %d 个格位置，%d 小节，%d/4，尾部越界 %d"
          % (len(notes), len(cells), len(bars), meter, clamped))
    print("saved:", out_mid, "|", plan_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
