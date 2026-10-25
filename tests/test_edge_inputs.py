# -*- coding: utf-8 -*-
"""边界输入鲁棒性回归：空/近空/损坏 MIDI、单音符、超长曲、BPM 极端值 20/400。

三个被测脚本都是 CLI 形态（模块级读取 sys.argv），按仓库约定用 subprocess
直调 .venv-test 的 python 执行脚本主流程，断言退出码与产物。

与脚本内注释一致的约定语义：
  - fix_tempo_tick / midi_to_wav：0 音符 → 明确失败（stderr 中文提示 + exit 1）；
  - render_fallback：兜底渲染器「有输入就出谱」，0 音符 → 1 小节全休止 PDF（exit 0）。
"""
import os
import subprocess
import sys
import wave

import pytest
from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
FIX_TEMPO = os.path.join(SCRIPTS_DIR, "fix_tempo_tick.py")
MIDI2WAV = os.path.join(SCRIPTS_DIR, "midi_to_wav.py")
RENDER_FB = os.path.join(SCRIPTS_DIR, "render_fallback.py")

try:
    import reportlab  # noqa: F401
    _HAS_REPORTLAB = True
except Exception:
    _HAS_REPORTLAB = False  # render_fallback 是可选兑底渲染器；环境缺它时相关用例跳过

_VENV_PY = os.path.join(PROJECT_ROOT, ".venv-test", "Scripts", "python.exe")
PY = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable

# 子进程 stderr 里的中文按 utf-8 输出，避免 Windows 管道下的编码歧义
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def run_script(script, *args):
    return subprocess.run(
        [PY, script] + [str(a) for a in args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=ENV, cwd=PROJECT_ROOT, timeout=300,
    )


def write_midi(path, notes, bpm=120.0, ppq=480):
    """用 mido 造边界 MIDI。notes: [(pitch, start_beat, velocity)]。"""
    mid = MidiFile(ticks_per_beat=ppq)
    trk = MidiTrack()
    mid.tracks.append(trk)
    trk.append(MetaMessage("set_tempo", tempo=bpm2tempo(bpm), time=0))
    events = []
    for pitch, start_beat, vel in notes:
        on = int(round(start_beat * ppq))
        events.append((on, 1, pitch, vel))            # 同 tick 时 note_off 先于 note_on
        events.append((on + ppq // 8, 0, pitch, 0))   # 时值 1/8 拍
    events.sort(key=lambda e: (e[0], e[1]))
    last = 0
    for tick, kind, pitch, vel in events:
        trk.append(Message("note_on" if kind else "note_off", note=pitch,
                           velocity=vel if kind else 0, channel=9, time=tick - last))
        last = tick
    trk.append(MetaMessage("end_of_track", time=0))
    mid.save(str(path))
    return path


def note_on_ticks(path):
    """读回 MIDI，返回各 note_on 的绝对 tick（按出现顺序）。"""
    mid = MidiFile(str(path))
    out = []
    for trk in mid.tracks:
        t = 0
        for msg in trk:
            t += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                out.append(t)
    return out


def tempo_values(path):
    mid = MidiFile(str(path))
    return [m.tempo for trk in mid.tracks for m in trk if m.type == "set_tempo"]


# ---------- 缺参 / 非法参数（usage 错误，退出码 2） ----------

@pytest.mark.parametrize("script", [FIX_TEMPO, MIDI2WAV, RENDER_FB])
def test_missing_args_exit_2_with_usage(script):
    r = run_script(script)
    assert r.returncode == 2
    assert "usage" in (r.stdout + r.stderr).lower()


@pytest.mark.parametrize("script", [FIX_TEMPO, MIDI2WAV, RENDER_FB])
def test_non_numeric_bpm_exit_2(tmp_path, script):
    src = write_midi(tmp_path / "x.mid", [(35, 0.0, 100)])
    r = run_script(script, src, tmp_path / "o.out", "abc")
    assert r.returncode == 2


@pytest.mark.parametrize("script", [FIX_TEMPO, RENDER_FB])
def test_bpm_zero_or_nonpositive_rejected(tmp_path, script):
    src = write_midi(tmp_path / "x.mid", [(35, 0.0, 100)])
    r = run_script(script, src, tmp_path / "o.out", 0)
    assert r.returncode == 2
    assert "BPM" in r.stderr


def test_fix_tempo_tick_bpm_below_settempo_range_rejected(tmp_path):
    # BPM=1 → set_tempo 需要 6000 万微秒，超出 mido 3 字节范围，必须拒绝而非写出坏 MIDI
    src = write_midi(tmp_path / "x.mid", [(35, 0.0, 100)])
    r = run_script(FIX_TEMPO, src, tmp_path / "o.mid", 1)
    assert r.returncode == 2
    assert "set_tempo" in r.stderr


# ---------- 损坏 MIDI（三个脚本都要可理解失败，退出码 1） ----------

@pytest.mark.parametrize("script", [FIX_TEMPO, MIDI2WAV, RENDER_FB])
def test_corrupt_midi_fails_with_chinese_message(tmp_path, script):
    src = tmp_path / "corrupt.mid"
    src.write_bytes(b"this is not a midi file")
    ext = ".mid" if script is FIX_TEMPO else (".wav" if script is MIDI2WAV else ".pdf")
    stem = os.path.splitext(os.path.basename(script))[0]
    out = tmp_path / (stem + "_out" + ext)
    r = run_script(script, src, out, 120)
    assert r.returncode == 1
    assert "无法读取" in r.stderr
    assert not out.exists()


# ---------- ① 空 / 0 音符 MIDI ----------

def test_fix_tempo_tick_empty_midi_fails_clearly(tmp_path):
    src = write_midi(tmp_path / "empty.mid", [])
    dst = tmp_path / "out.mid"
    r = run_script(FIX_TEMPO, src, dst, 120)
    assert r.returncode == 1
    assert "没有音符" in r.stderr
    assert "saved:" not in r.stdout          # 不产出空产物
    assert not dst.exists()


def test_midi_to_wav_empty_midi_fails_clearly(tmp_path):
    src = write_midi(tmp_path / "empty.mid", [])
    dst = tmp_path / "out.wav"
    r = run_script(MIDI2WAV, src, dst, 120)
    assert r.returncode == 1
    assert "没有音符" in r.stderr
    assert not dst.exists()


@pytest.mark.skipif(not _HAS_REPORTLAB, reason="render_fallback 需要 reportlab（可选兑底渲染器），环境缺失时跳过")
def test_render_fallback_empty_midi_degrades_to_one_rest_bar(tmp_path):
    src = write_midi(tmp_path / "empty.mid", [])
    dst = tmp_path / "out.pdf"
    r = run_script(RENDER_FB, src, dst, 120)
    assert r.returncode == 0, r.stderr
    assert "notes=0 bars=1" in r.stdout
    assert dst.exists() and dst.stat().st_size > 1000


# ---------- ② 近空 / 单音符（estimate_tempo 少于 2 音符抛 ValueError 的回归） ----------

def test_fix_tempo_tick_single_note_ok(tmp_path):
    src = write_midi(tmp_path / "one.mid", [(35, 1.0, 100)])  # 文件 120bpm → 0.5s
    dst = tmp_path / "out.mid"
    r = run_script(FIX_TEMPO, src, dst, 120)
    assert r.returncode == 0, r.stderr
    assert "saved:" in r.stdout
    # 重写后 0.5s × (480*120/60) = tick 480
    assert note_on_ticks(dst) == [480]
    assert tempo_values(dst) == [bpm2tempo(120)]


def test_midi_to_wav_single_note_ok(tmp_path):
    src = write_midi(tmp_path / "one.mid", [(38, 0.0, 100)])
    dst = tmp_path / "out.wav"
    r = run_script(MIDI2WAV, src, dst, 120)
    assert r.returncode == 0, r.stderr
    with wave.open(str(dst), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getframerate() == 44100
        # duration = 音符结束(0.0625s) + 1.0s 尾巴 → 约 1.06s
        assert 44000 <= w.getnframes() <= 50000


@pytest.mark.skipif(not _HAS_REPORTLAB, reason="render_fallback 需要 reportlab（可选兑底渲染器），环境缺失时跳过")
def test_render_fallback_single_note_ok(tmp_path):
    src = write_midi(tmp_path / "one.mid", [(35, 0.0, 100)])
    dst = tmp_path / "out.pdf"
    r = run_script(RENDER_FB, src, dst, 120)
    assert r.returncode == 0, r.stderr
    assert "notes=1 bars=1" in r.stdout
    assert dst.exists() and dst.stat().st_size > 1000


def test_midi_to_wav_unknown_pitch_only_ok(tmp_path):
    # 只有不属于鼓组映射的音高（如 60）：合法输入，合成静音样本不崩
    src = write_midi(tmp_path / "u.mid", [(60, 0.0, 100)])
    dst = tmp_path / "o.wav"
    r = run_script(MIDI2WAV, src, dst, 120)
    assert r.returncode == 0, r.stderr
    assert dst.exists() and dst.stat().st_size > 10000


# ---------- render_fallback notes[-1] 回归：最晚小节按最大起音时间 ----------

@pytest.mark.skipif(not _HAS_REPORTLAB, reason="render_fallback 需要 reportlab（可选兑底渲染器），环境缺失时跳过")
def test_render_fallback_last_bar_uses_latest_onset_not_highest_pitch(tmp_path):
    # 吊镲(49)在 0s，底鼓(35)在 32 拍（文件 120bpm → 16.0s）；脚本 BPM 参数 60
    # → 每小节 4s，底鼓起音在第 5 小节开头。旧代码 notes[-1][1] 取排序后最后一项
    # （pitch 49 的 0.0s），小节数只算 round(16.0625/4)=4，第 5 小节被丢；
    # 修复后按最大起音 16.0//4+1=5 取 5 小节。
    src = write_midi(tmp_path / "tail.mid", [(49, 0.0, 100), (35, 32.0, 100)])
    dst = tmp_path / "out.pdf"
    r = run_script(RENDER_FB, src, dst, 60)
    assert r.returncode == 0, r.stderr
    assert "bars=5" in r.stdout
    assert dst.exists() and dst.stat().st_size > 1000


# ---------- ③ BPM 极端值 20 / 400 ----------

def test_fix_tempo_tick_extreme_bpm_20_and_400(tmp_path):
    for bpm in (20, 400):
        src = write_midi(tmp_path / ("e%d.mid" % bpm), [(35, 0.0, 100), (38, 2.0, 90)])
        dst = tmp_path / ("o%d.mid" % bpm)
        r = run_script(FIX_TEMPO, src, dst, bpm)
        assert r.returncode == 0, r.stderr
        assert tempo_values(dst) == [bpm2tempo(bpm)]
        # 第 2 个音符在文件里为 1.0s；按脚本 BPM 换算 tick
        expected = int(round(1.0 * 480 * bpm / 60.0))
        assert note_on_ticks(dst) == [0, expected]


def test_midi_to_wav_extreme_bpm_20_and_400(tmp_path):
    for bpm in (20, 400):
        src = write_midi(tmp_path / ("e%d.mid" % bpm), [(35, 0.0, 100), (42, 1.0, 90)])
        dst = tmp_path / ("o%d.wav" % bpm)
        r = run_script(MIDI2WAV, src, dst, bpm)
        assert r.returncode == 0, r.stderr
        assert dst.exists() and dst.stat().st_size > 10000
        with wave.open(str(dst), "rb") as w:
            assert w.getframerate() == 44100 and w.getnchannels() == 2


def test_render_fallback_extreme_bpm_20_and_400(tmp_path):
    # 文件 120bpm：两音符起音 0s / 2.0s。BPM 20 → 每小节 12s → 1 小节；
    # BPM 400 → 每小节 0.6s → 2.0//0.6+1 = 4 小节。
    src = write_midi(tmp_path / "x.mid", [(35, 0.0, 100), (38, 4.0, 90)])
    for bpm, bars in ((20, 1), (400, 4)):
        dst = tmp_path / ("o%d.pdf" % bpm)
        r = run_script(RENDER_FB, src, dst, bpm)
        assert r.returncode == 0, r.stderr
        assert ("bars=%d" % bars) in r.stdout
        assert dst.exists() and dst.stat().st_size > 1000


# ---------- ③ 超长曲 ----------

def test_fix_tempo_tick_long_song_30min_ok(tmp_path):
    # (38, 3600拍) @ 文件 120bpm = 1800s；脚本 BPM 120 → tick 1800*960
    src = write_midi(tmp_path / "long.mid", [(35, 0.0, 100), (38, 3600.0, 90)])
    dst = tmp_path / "out.mid"
    r = run_script(FIX_TEMPO, src, dst, 120)
    assert r.returncode == 0, r.stderr
    assert note_on_ticks(dst) == [0, 1800 * 960]


def test_midi_to_wav_long_song_5min_ok(tmp_path):
    # (38, 600拍) @ 120bpm = 300s，缓冲约 265MB，应正常合成
    src = write_midi(tmp_path / "long.mid", [(35, 0.0, 100), (38, 600.0, 90)])
    dst = tmp_path / "out.wav"
    r = run_script(MIDI2WAV, src, dst, 120)
    assert r.returncode == 0, r.stderr
    with wave.open(str(dst), "rb") as w:
        assert abs(w.getnframes() / 44100.0 - 301.0625) < 0.1


def test_midi_to_wav_overlong_rejected_before_synth(tmp_path):
    # 2000s > 30 分钟上限：在分配大缓冲前明确拒绝（exit 1 + 中文提示）
    src = write_midi(tmp_path / "huge.mid", [(35, 0.0, 100), (38, 4000.0, 90)])
    dst = tmp_path / "o.wav"
    r = run_script(MIDI2WAV, src, dst, 120)
    assert r.returncode == 1
    assert "上限" in r.stderr
    assert not dst.exists()


def test_render_fallback_long_song_30min_ok(tmp_path):
    # 1800s @ BPM 20（每小节 12s）→ 尾音符起音正好在第 151 小节边界 → 151 小节
    src = write_midi(tmp_path / "long.mid", [(35, 0.0, 100), (38, 3600.0, 90)])
    dst = tmp_path / "out.pdf"
    r = run_script(RENDER_FB, src, dst, 20)
    assert r.returncode == 0, r.stderr
    assert "notes=2 bars=151" in r.stdout
    assert dst.exists() and dst.stat().st_size > 10000
