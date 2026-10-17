# -*- coding: utf-8 -*-
"""MuseScore-safe drum MusicXML generator - v3 (standard positions + markers + layout).
Usage: python gen_musicxml.py <in.mid> <out.musicxml> [bpm] [title] [markers_file] [bars_per_line]

v3 additions:
- section markers (Intro/1st Verse...) as above-staff words
- forced line break every bars_per_line bars (default 5, like Songsterr)

Positions (from MuseScore 4.7.5 roundtrip authority):
- display: BD=E4, SD=C5, HH=G5, Tom=D5, Cym=A5 (x notehead for HH/cym)
- stems: BD down, others up
- multiple score-instruments, midi-unpitched = MuseScore internal pitch (GM+1)
"""
import sys
import pretty_midi

MIDI = sys.argv[1] if len(sys.argv) > 1 else None
OUT = sys.argv[2] if len(sys.argv) > 2 else None
BPM = float(sys.argv[3]) if len(sys.argv) > 3 else 156.0
TITLE = sys.argv[4] if len(sys.argv) > 4 else "Drum Score"
if not MIDI or not OUT:
    print("usage: gen_musicxml.py <in.mid> <out.musicxml> [bpm] [title]")
    sys.exit(2)
MARKERS_FILE = sys.argv[5] if len(sys.argv) > 5 else None
BARS_PER_LINE = int(sys.argv[6]) if len(sys.argv) > 6 else 5

# load markers: "17:1st Verse" per line
markers = {}
if MARKERS_FILE:
    for line in open(MARKERS_FILE, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            b, txt = line.split(":", 1)
            try:
                markers[int(b)] = txt.strip()
            except ValueError:
                pass
DIV = 480
BEAT_S = 60.0 / BPM
BAR_S = BEAT_S * 4
BAR_TICKS = DIV * 4

pm = pretty_midi.PrettyMIDI(MIDI)
notes = sorted((n.pitch, n.start) for inst in pm.instruments for n in inst.notes)

# pitch -> (display_step, display_octave, musescore_internal_pitch, instrument_name, stem)
DRUM_MAP = {
    35: ("E", 4, 36, "Bass Drum", "down"),       # GM BD -> MuseScore 36
    38: ("C", 5, 39, "Acoustic Snare", "up"),
    42: ("G", 5, 43, "Closed Hi-Hat", "up"),
    47: ("D", 5, 48, "Low-Mid Tom", "up"),
    49: ("A", 5, 50, "Crash Cymbal 1", "up"),
}
X_PITCHES = {42, 46, 49, 51, 57}

def display_pos(pitch):
    if pitch in DRUM_MAP:
        return DRUM_MAP[pitch][0], DRUM_MAP[pitch][1]
    return "C", 5

def tick_of(t):
    return int(round((t / BEAT_S) * DIV))

def ttype(d):
    if d >= 450:
        return "half"
    if d >= 210:
        return "quarter"
    if d >= 105:
        return "eighth"
    if d >= 45:
        return "16th"
    if d >= 22:
        return "32nd"
    return "64th"

bars = {}
for pitch, start in notes:
    b = int(start // BAR_S)
    t = tick_of(start - b * BAR_S)
    t = max(0, min(t, BAR_TICKS - 1))
    bars.setdefault(b, {}).setdefault(t, []).append(pitch)
n_bars = max(bars.keys()) + 1
print("bars:", n_bars, "notes:", len(notes))

xml = []
xml.append('<?xml version="1.0" encoding="UTF-8"?>')
xml.append('<score-partwise version="3.1">')
xml.append('  <work><work-title>%s</work-title></work>' % TITLE)
xml.append('  <identification><creator type="composer">Demucs + ADTOF transcription</creator></identification>')
xml.append('  <part-list>')
xml.append('    <score-part id="P1">')
xml.append('      <part-name>Drum Kit</part-name>')
# one score-instrument per drum (like MuseScore's own export)
for pitch, (step, octv, ip, name, stem) in sorted(DRUM_MAP.items()):
    iid = "P1-I%d" % ip
    xml.append('      <score-instrument id="%s">' % iid)
    xml.append('        <instrument-name>%s</instrument-name>' % name)
    xml.append('        </score-instrument>')
for pitch, (step, octv, ip, name, stem) in sorted(DRUM_MAP.items()):
    iid = "P1-I%d" % ip
    xml.append('      <midi-instrument id="%s">' % iid)
    xml.append('        <midi-channel>10</midi-channel>')
    xml.append('        <midi-program>1</midi-program>')
    xml.append('        <midi-unpitched>%d</midi-unpitched>' % ip)
    xml.append('        </midi-instrument>')
xml.append('    </score-part>')
xml.append('  </part-list>')
xml.append('  <part id="P1">')

for b in range(n_bars):
    xml.append(f'    <measure number="{b + 1}">')
    # line break every BARS_PER_LINE bars (matching Songsterr row layout)
    if b > 0 and (b % BARS_PER_LINE == 0):
        xml.append('      <print new-system="yes"/>')
    # section marker
    if (b + 1) in markers:
        xml.append('      <direction placement="above">')
        xml.append('        <direction-type><words font-weight="bold">%s</words></direction-type>' % markers[b + 1])
        xml.append('      </direction>')
    if b == 0:
        xml.append('      <attributes>')
        xml.append(f'        <divisions>{DIV}</divisions>')
        xml.append('        <time><beats>4</beats><beat-type>4</beat-type></time>')
        xml.append('        <clef><sign>percussion</sign></clef>')
        xml.append('      </attributes>')
        xml.append('      <sound tempo="%.1f"/>' % BPM)
        xml.append('      <direction placement="above">')
        xml.append('        <direction-type><metronome><per-minute>%.0f</per-minute></metronome></direction-type>' % BPM)
        xml.append('        <sound tempo="%.1f"/>' % BPM)
        xml.append('      </direction>')
    events = sorted(bars.get(b, {}).items())
    for i, (t, pitches) in enumerate(events):
        if i + 1 < len(events):
            dur = max(1, events[i + 1][0] - t)
        else:
            dur = max(1, BAR_TICKS - t)
        ty = ttype(dur)
        for j, pitch in enumerate(sorted(set(pitches))):
            if pitch not in DRUM_MAP:
                continue
            step, octv, ip, name, stem = DRUM_MAP[pitch]
            iid = "P1-I%d" % ip
            ch = "<chord/>" if j > 0 else ""
            xh = "<notehead>x</notehead>" if pitch in X_PITCHES else ""
            xml.append('      <note>')
            if ch:
                xml.append('        ' + ch)
            xml.append('        <unpitched><display-step>%s</display-step><display-octave>%d</display-octave></unpitched>' % (step, octv))
            xml.append(f'        <duration>{dur}</duration>')
            xml.append('        <instrument id="%s"/>' % iid)
            xml.append('        <voice>1</voice>')
            xml.append(f'        <type>{ty}</type>')
            xml.append('        <stem>%s</stem>' % stem)
            if xh:
                xml.append('        ' + xh)
            xml.append('      </note>')
    xml.append('    </measure>')

xml.append('  </part>')
xml.append('</score-partwise>')

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(xml))
print("saved:", OUT)
