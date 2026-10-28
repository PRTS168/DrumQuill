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
        # 兜底：bars 从第一个强拍开始切块，只覆盖其后的拍；开头的弱起拍不属于
        # 任何小节，记 -1。不按时间过滤，避免边界抖动导致错位。
        covered = sum(len(b["beats"]) for b in bars)
        n_lead = max(0, len(beats) - covered)
        beat_bar = [-1] * n_lead
        beat_k = [-1] * n_lead
        for bi, bar in enumerate(bars):
            for k in range(len(bar["beats"])):
                if len(beat_bar) >= len(beats):
                    break
                beat_bar.append(bi)
                beat_k.append(k)
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
            b = ((tick - r * DIV) % bar_ticks) // DIV
            for p in slot:
                # 槽内每个音高都要配上自己的拍位：同槽多音高（kick+hat）是鼓点常态；
                # 旧写法拍位按槽记、音高按音高 extend，zip 会错配且截断。
                beat_of_note.append(b)
                pitches.append(p)
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
    skipped_pickup = 0
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
        if bi < 0:
            # 弱起拍（第一小节之前的拍）不属于任何小节：跳过，不写入栅格
            skipped_pickup += 1
            continue
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

    # 输出对齐 MIDI（mido 直写）：音符放在等距 tick 上（每小节 bar_ticks），
    # 每小节起点写一条 set_tempo（该小节的真实 bpm）。任何尊重速度事件的渲染器
    # （MuseScore / pretty_midi / DAW）都能精确还原真实时间线。旧写法把"真实秒数"
    # 放到单一 initial_tempo 的平直时钟上：速度波动的歌里合成音频与拍点网格必然
    # 扭曲，动态谱与音乐对不上（实测 272s 曲内漂 ~1.3s，波动大的歌更甚）。
    import mido
    TPB = DIV                    # 每拍 480 tick，与格线同一量纲
    ref_bpm = bpm if bpm > 0 else 120.0
    mid = mido.MidiFile(ticks_per_beat=TPB)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    events = []                  # (abs_tick, order, msg)：同 tick 时 set_tempo 先于音符
    events.append((0, 1, mido.MetaMessage("time_signature", numerator=meter, denominator=4)))
    # 速度事件：每小节一条，值由"该小节实际拍数 × 60 / 小节真实时长"推导 ——
    # 这样任何尊重速度表的渲染器，逐小节时长都与真实拍点网格严丝合缝（误差 ~0）。
    # 不用 plan 里的 bpm 字段：它是拍间隔均值，与整小节跨度有 ~20ms/小节的系统差。
    tick = 0
    for bi, b in enumerate(bars):
        nb = len(b.get("beats") or []) or meter
        dur = float(b.get("end") or 0.0) - float(b.get("start") or 0.0)
        bpm_bar = (60.0 * nb / dur) if dur > 0.05 else ref_bpm
        if bpm_bar <= 0:
            bpm_bar = ref_bpm
        if bi > 0:
            events.append((tick, 0, mido.MetaMessage("set_tempo", tempo=max(1, mido.bpm2tempo(bpm_bar)))))
        tick += nb * DIV
    note_len_ticks = max(1, int(round(DIV * 0.1 * ref_bpm / 60.0)))   # 约 0.1s 的音符长度
    for abs_tick in sorted(cells):
        for pitch, vel in sorted(cells[abs_tick].items()):
            events.append((abs_tick, 2, mido.Message("note_on", channel=9, note=pitch, velocity=vel)))
            events.append((abs_tick + note_len_ticks, 3, mido.Message("note_on", channel=9, note=pitch, velocity=0)))
    events.sort(key=lambda e: (e[0], e[1]))
    last_tick = 0
    for tick, _order, msg in events:
        track.append(msg.copy(time=tick - last_tick))
        last_tick = tick
    mid.save(out_mid)

    plan = {
        "meter": meter,
        "bpm_median": bpm,
        "n_bars": len(bars),
        "bars": [{"index": i + 1, "start": b["start"], "end": b["end"],
                  "bpm": b["bpm"], "beats": len(b["beats"])} for i, b in enumerate(bars)],
        "notes_in": len(notes),
        "cells_out": len(cells),
        "clamped_tail": clamped,
        "skipped_pickup": skipped_pickup,
    }
    with open(plan_json, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)

    print("对齐完成：%d 个起音 -> %d 个格位置，%d 小节，%d/4，尾部越界 %d"
          % (len(notes), len(cells), len(bars), meter, clamped))
    print("saved:", out_mid, "|", plan_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
