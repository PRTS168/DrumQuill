# -*- coding: utf-8 -*-
"""Render drum MIDI into a playable single-track drum audio (WAV).
Synthesized drum kit (kick/snare/hihat/tom/cymbal) - local, no external libs.

Usage: python midi_to_wav.py <in.mid> <out.wav> [bpm] [gain]

退出码约定：2 = 参数/用法错误；1 = 输入内容无法处理。
空/无音符 MIDI 的语义（约定）：与 fix_tempo_tick 一致，选择「明确失败」而非
产出一段静音 WAV 掩盖转录失败——stderr 打印中文错误并以退出码 1 结束，
交给 run_pipeline.ps1 的失败提示链路（该处按非零退出码报「音频合成失败」）。
"""
import sys
import numpy as np
import pretty_midi

if len(sys.argv) < 3:
    print("usage: midi_to_wav.py <in.mid> <out.wav> [bpm] [gain]")
    sys.exit(2)
MIDI = sys.argv[1]
OUT = sys.argv[2]
try:
    BPM = float(sys.argv[3]) if len(sys.argv) > 3 else 156.0
    GAIN = float(sys.argv[4]) if len(sys.argv) > 4 else 0.9
except ValueError:
    print("usage: midi_to_wav.py <in.mid> <out.wav> [bpm] [gain]（bpm/gain 需为数字）")
    sys.exit(2)
SR = 44100

try:
    pm = pretty_midi.PrettyMIDI(MIDI)
except Exception as e:
    print("错误：无法读取 MIDI 文件 %s（%s: %s）。" % (MIDI, type(e).__name__, e), file=sys.stderr)
    sys.exit(1)
if sum(len(inst.notes) for inst in pm.instruments) == 0:
    print("错误：MIDI 中没有音符（0 个），无法合成预览音频；请检查上游转录输出是否为空：%s" % MIDI, file=sys.stderr)
    sys.exit(1)
# tempo from midi if present（pretty_midi 的 estimate_tempo 在音符少于 2 个时
# 直接抛 ValueError，必须兜底回退命令行 bpm；顺带缓存一次，避免 O(n) 估算重复调用）
try:
    est = pm.estimate_tempo()
except Exception:
    est = 0.0
if est > 0:
    BPM = est
duration = max(n.end for inst in pm.instruments for n in inst.notes) + 1.0
# 超长曲保护：合成缓冲约需 duration*44100*20 字节（float64 主缓冲 + np.roll 副本
# + int16 交错，约 0.85 MB/秒）。30 分钟以上的预览音频对本地工具已无意义且逼近
# 内存上限，按通用时长上限提前拒绝（与具体歌曲无关），避免 MemoryError 裸崩。
if duration > 1800.0:
    print("错误：MIDI 音频时长 %.1f 分钟，超出本地预览合成上限（30 分钟）；请检查输入 MIDI 是否异常。" % (duration / 60.0), file=sys.stderr)
    sys.exit(1)
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
