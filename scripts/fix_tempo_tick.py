# -*- coding: utf-8 -*-
"""Regenerate drum MIDI at given BPM with sorted event writing (handles overlap).
Usage: python fix_tempo_tick.py <in.mid> <out.mid> [bpm]
"""
import sys
import pretty_midi
import mido
from mido import MidiFile, MidiTrack, MetaMessage, Message

SRC = sys.argv[1] if len(sys.argv) > 1 else None
DST = sys.argv[2] if len(sys.argv) > 2 else None
BPM = float(sys.argv[3]) if len(sys.argv) > 3 else 156.0
if not SRC or not DST:
    print("usage: fix_tempo_tick.py <in.mid> <out.mid> [bpm]")
    sys.exit(2)
PPQ = 480
TICKS_PER_SEC = PPQ * BPM / 60.0
TEMPO_US = int(round(60_000_000 / BPM))

pm = pretty_midi.PrettyMIDI(SRC)
notes = sorted(
    (n.pitch, n.start, n.end, int(n.velocity))
    for inst in pm.instruments
    for n in inst.notes
)
print("source notes:", len(notes), "max end s:", round(max(n[2] for n in notes), 2))

def t(s):
    return int(round(s * TICKS_PER_SEC))

events = []
for pitch, start, end, vel in notes:
    on = t(start)
    off = max(on + 1, t(end))
    events.append((on, 0, "on", pitch, vel))
    events.append((off, 1, "off", pitch, 0))
events.sort(key=lambda e: (e[0], e[1]))

mid = MidiFile(type=1, ticks_per_beat=PPQ)
tr0 = MidiTrack()
tr0.append(MetaMessage("track_name", name="Drums", time=0))
tr0.append(MetaMessage("set_tempo", tempo=TEMPO_US, time=0))
tr0.append(MetaMessage("time_signature", numerator=4, denominator=4, time=0))
tr0.append(MetaMessage("end_of_track", time=0))
mid.tracks.append(tr0)

tr1 = MidiTrack()
tr1.append(Message("program_change", program=0, channel=9, time=0))
last = 0
for tick, _, kind, pitch, vel in events:
    tr1.append(Message("note_on" if kind == "on" else "note_off",
                       note=pitch, velocity=vel, channel=9, time=tick - last))
    last = tick
tr1.append(MetaMessage("end_of_track", time=0))
mid.tracks.append(tr1)

mid.save(DST)

cur = 0
max_abs = 0
n_on = 0
for msg in mido.MidiFile(DST).tracks[1]:
    cur += msg.time
    if msg.type == "note_on":
        n_on += 1
    if msg.type == "note_off":
        max_abs = max(max_abs, cur)
dur_s = max_abs / TICKS_PER_SEC
print("saved:", DST)
print("note_on count:", n_on, "| duration @%g bpm:" % BPM, round(dur_s, 1), "s | bars:", round(dur_s * BPM / 60 / 4, 1))
