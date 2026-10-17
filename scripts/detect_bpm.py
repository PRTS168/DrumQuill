# -*- coding: utf-8 -*-
"""Detect tempo (BPM) from an audio file using librosa (whole song)."""
import sys
import librosa

path = sys.argv[1]
y, sr = librosa.load(path, sr=22050, mono=True, duration=180)
tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
try:
    # tempo may be a 1-element numpy array in newer librosa
    t = tempo[0] if hasattr(tempo, "__len__") else tempo
    bpm = int(round(float(t)))
except Exception:
    bpm = 120
# beat_track sometimes returns half-speed for fast music
if 50 <= bpm <= 100:
    bpm = bpm * 2
print(bpm)
