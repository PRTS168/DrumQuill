# -*- coding: utf-8 -*-
"""Render drum MIDI into a playable single-track drum audio (WAV).
Synthesized drum kit (kick/snare/hihat/tom/cymbal) - local, no external libs.

Usage: python midi_to_wav.py <in.mid> <out.wav> [bpm] [gain]
"""
import sys
import numpy as np
import pretty_midi

MIDI = sys.argv[1]
OUT = sys.argv[2]
BPM = float(sys.argv[3]) if len(sys.argv) > 3 else 156.0
GAIN = float(sys.argv[4]) if len(sys.argv) > 4 else 0.9
SR = 44100

pm = pretty_midi.PrettyMIDI(MIDI)
# tempo from midi if present
if pm.estimate_tempo() > 0:
    BPM = pm.estimate_tempo()
duration = max(n.end for inst in pm.instruments for n in inst.notes) + 1.0
total = int(duration * SR)
buf = np.zeros(total, dtype=np.float64)

def env_exp(n, tau):
    return np.exp(-np.arange(n) / (tau * SR))

def syn_kick(n):
    # low sine sweep 120->45 Hz + click
    t = np.arange(n) / SR
    f = 120 * np.exp(-t * 18) + 42
    ph = 2 * np.pi * np.cumsum(f) / SR
    return (np.sin(ph) * env_exp(n, 0.18) + 0.25 * np.random.randn(n) * env_exp(n, 0.008)) * 1.4

def syn_snare(n):
    t = np.arange(n) / SR
    osc = np.sin(2 * np.pi * (190 * np.exp(-t*8) + 160) * t) * env_exp(n, 0.16)
    noise = np.random.randn(n) * env_exp(n, 0.10)
    return (osc + 0.8 * noise) * 1.0

def syn_hh(n, open_=False):
    tau = 0.14 if open_ else 0.045
    noise = np.random.randn(n)
    # highpass-ish: diff twice (keep length)
    noise = np.diff(noise, 2, prepend=np.zeros(2)) / 4
    return noise * env_exp(n, tau) * 0.9

def syn_tom(n, base=150):
    t = np.arange(n) / SR
    f = base * np.exp(-t * 6)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * env_exp(n, 0.22) * 1.1

def syn_cym(n):
    noise = np.random.randn(n)
    noise = np.diff(noise, 4, prepend=np.zeros(4)) / 6
    t = np.arange(n) / SR
    shimmer = 1 + 0.4 * np.sin(2 * np.pi * 213 * t) * env_exp(n, 0.3)
    return noise * env_exp(n, 0.65) * shimmer * 0.8

def pitch_sound(pitch):
    n_sec = {35: 0.30, 38: 0.28, 42: 0.10, 47: 0.35, 49: 0.9}
    n = int(n_sec.get(pitch, 0.3) * SR)
    if pitch == 35:
        return syn_kick(n)
    if pitch == 38:
        return syn_snare(n)
    if pitch == 42:
        return syn_hh(n)
    if pitch == 47:
        return syn_tom(n)
    if pitch == 49:
        return syn_cym(n)
    return np.zeros(n)

count = 0
for inst in pm.instruments:
    for note in inst.notes:
        start = int(note.start * SR)
        snd = pitch_sound(note.pitch)
        end = min(start + len(snd), total)
        buf[start:end] += snd[:end - start]
        count += 1

# normalize + fade in/out
peak = np.abs(buf).max()
if peak > 0:
    buf = buf / peak * GAIN
fade = int(0.02 * SR)
buf[:fade] *= np.linspace(0, 1, fade)
buf[-fade:] *= np.linspace(1, 0, fade)

# stereo (slight width) -> interleave
left = buf
right = np.roll(buf, int(0.0004 * SR))
pcm = np.empty(len(buf) * 2, dtype=np.int16)
pcm[0::2] = (left * 32767).astype(np.int16)
pcm[1::2] = (right * 32767).astype(np.int16)

import wave
with wave.open(OUT, "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
print("saved:", OUT, "notes:", count, "dur: %.1fs bpm: %.0f" % (duration, BPM))
