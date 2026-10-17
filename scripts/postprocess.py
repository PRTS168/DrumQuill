# -*- coding: utf-8 -*-
"""识别结果后处理（规则引擎）——给 AI 转录结果加一层"乐理大脑"。

用法：
  python postprocess.py <in.mid> <out.mid> [--dedupe-ms 50] [--min-len-ms 30] [--swing 0]

做的三件事（都可关掉，参数为 0 即不生效）：
  1. 抖动去重：同一音高在 dedupe-ms 内重复触发 → 只保留力度最大的那个
     （鼓手不可能 50ms 内连击同一鼓面两次，这类"抽搐"多半是模型误报）
  2. 最短时值：把短于 min-len-ms 的音符拉长（不越过下一个同音高的起点）
     （ADTOF 偶尔给出几毫秒的碎片音符，排版时会变成奇怪的符尾）
  3. 摇摆量化：把 8 分反拍（16 分网格里 index%4==2 的位置）整体延后，
     恢复"shuffle/swing"的呼吸感（swing=0 不处理；0.3~0.6 比较自然）

输出末行给出统计，方便在管线日志里看到"到底修了多少"。
"""
import sys
import collections

try:
    import pretty_midi
    from mido import MidiFile, MidiTrack, MetaMessage, Message
except Exception as e:  # pragma: no cover
    print("need pretty_midi + mido: %s" % e)
    sys.exit(2)

PPQ = 480
SIXTEENTH = PPQ // 4          # 120 ticks


def load_notes(path):
    pm = pretty_midi.PrettyMIDI(path)
    notes = []
    for inst in pm.instruments:
        for n in inst.notes:
            notes.append({"pitch": n.pitch, "start": n.start, "end": n.end,
                          "velocity": n.velocity, "channel": inst.program})
    notes.sort(key=lambda x: (x["start"], x["pitch"]))
    return pm, notes


def dedupe(notes, dedupe_ms):
    """同一音高在 dedupe_ms 内重复 -> 保留力度大的一个。"""
    if dedupe_ms <= 0:
        return notes, 0
    gap = dedupe_ms / 1000.0
    out, removed = [], 0
    last = {}
    for n in notes:
        prev = last.get(n["pitch"])
        if prev is not None and (n["start"] - prev["start"]) < gap:
            if n["velocity"] > prev["velocity"]:
                prev["velocity"] = n["velocity"]      # 保留更响的
                prev["end"] = max(prev["end"], n["end"])
            removed += 1
            continue
        out.append(n)
        last[n["pitch"]] = n
    return out, removed


def min_length(notes, min_len_ms):
    """把过短的音符拉长到 min_len_ms（不越过下一个同音高起点）。"""
    if min_len_ms <= 0:
        return notes, 0
    floor = min_len_ms / 1000.0
    nxt = {}
    changed = 0
    for i in range(len(notes) - 1, -1, -1):
        n = notes[i]
        limit = nxt.get(n["pitch"])
        end = n["end"]
        if end - n["start"] < floor:
            target = n["start"] + floor
            if limit is not None:
                target = min(target, limit - 0.001)
            if target > end:
                n["end"] = target
                changed += 1
        nxt[n["pitch"]] = n["start"]
    return notes, changed


def swing(notes, amount):
    """把 8 分反拍延后：16 分网格 index%4==2 的位置整体右移。"""
    if amount <= 0:
        return notes, 0
    delay = max(0.0, min(1.0, amount)) * (SIXTEENTH * 2 / 3.0)   # 最多延后 2/3 个 16 分
    changed = 0
    for n in notes:
        tick = int(round(n["start"] * (PPQ * 130 / 60.0)))  # 粗估；只用来判断"反拍"
        idx = int(round(tick / float(SIXTEENTH)))
        if idx % 4 == 2:
            n["start"] += delay          # 只挪起音，保持时值不被压扁
            if n["end"] <= n["start"]:
                n["end"] = n["start"] + 0.005
            changed += 1
    return notes, changed


def write_midi(notes, out_path, bpm):
    ticks_per_sec = PPQ * bpm / 60.0
    events = []
    for n in notes:
        on = int(round(n["start"] * ticks_per_sec))
        off = max(on + 1, int(round(n["end"] * ticks_per_sec)))
        events.append((on, 0, "on", n["pitch"], max(1, min(127, int(n["velocity"])))))
        events.append((off, 1, "off", n["pitch"], 0))
    events.sort(key=lambda e: (e[0], e[1]))

    mid = MidiFile(type=1, ticks_per_beat=PPQ)
    tr0 = MidiTrack()
    tr0.append(MetaMessage("track_name", name="Drums", time=0))
    tr0.append(MetaMessage("set_tempo", tempo=int(round(60000000 / bpm)), time=0))
    tr0.append(MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    tr0.append(MetaMessage("end_of_track", time=0))
    mid.tracks.append(tr0)

    tr1 = MidiTrack()
    tr1.append(Message("program_change", program=0, channel=9, time=0))
    last = 0
    for tick, _order, kind, pitch, vel in events:
        tr1.append(Message("note_on" if kind == "on" else "note_off",
                           note=pitch, velocity=vel, channel=9, time=tick - last))
        last = tick
    tr1.append(MetaMessage("end_of_track", time=0))
    mid.tracks.append(tr1)
    mid.save(out_path)


def main():
    if len(sys.argv) < 3:
        print("usage: postprocess.py <in.mid> <out.mid> [--dedupe-ms N] [--min-len-ms N] [--swing X] [--bpm N]")
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    opts = {"dedupe_ms": 50.0, "min_len_ms": 30.0, "swing": 0.0, "bpm": 120.0}
    i = 3
    while i < len(sys.argv) - 1:
        k = sys.argv[i]
        v = sys.argv[i + 1]
        if k == "--dedupe-ms":
            opts["dedupe_ms"] = float(v)
        elif k == "--min-len-ms":
            opts["min_len_ms"] = float(v)
        elif k == "--swing":
            opts["swing"] = float(v)
        elif k == "--bpm":
            opts["bpm"] = float(v)
        i += 2

    pm, notes = load_notes(src)
    # 从源文件里读回速度（写回时保持一致）
    try:
        _t, tempi = pm.get_tempo_changes()
        if len(tempi):
            opts["bpm"] = float(tempi[0])
    except Exception:
        pass

    before = len(notes)
    notes, n_dup = dedupe(notes, opts["dedupe_ms"])
    notes, n_len = min_length(notes, opts["min_len_ms"])
    notes, n_swing = swing(notes, opts["swing"])
    if not notes:
        print("后处理：输入没有音符，跳过（照原样复制）")
        import shutil
        shutil.copyfile(src, dst)
        return 0

    write_midi(notes, dst, opts["bpm"])
    print("后处理：%d -> %d 音符 | 去重 %d | 拉长 %d | 摇摆 %d（swing=%.2f, 去重阈值 %.0fms）"
          % (before, len(notes), n_dup, n_len, n_swing, opts["swing"], opts["dedupe_ms"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
