# -*- coding: utf-8 -*-
"""DrumQuill 测试公共设施。

关键点：scripts 下的模块是 CLI 风格脚本，模块级代码会读取 sys.argv
（如 gen_musicxml.py 的 MIDI/OUT/BPM）。在导入任何被测模块之前先把
sys.argv 固定为单元素，避免 pytest 自身的参数被误读成路径。
"""
import sys

sys.argv = ["pytest", "x.mid", "x.musicxml", "120"]

import pytest  # noqa: E402

# 动态定位项目根（conftest 位于 <root>/tests/），避免写死本机路径，CI 可直接复用
import os  # noqa: E402
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
APP_DIR = os.path.join(PROJECT_ROOT, "app")

for _p in (SCRIPTS_DIR, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture
def make_midi(tmp_path):
    """构造一个最小可用 MIDI 文件，返回写入函数。

    用法：
        write_midi(notes, out_path=None, bpm=120.0)
    notes: [(pitch, start_sec, velocity)]
    """

    def _write(notes, out_path=None, bpm=120.0):
        import pretty_midi

        if out_path is None:
            out_path = tmp_path / "test.mid"
        pm = pretty_midi.PrettyMIDI(initial_tempo=bpm)
        inst = pretty_midi.Instrument(program=1, is_drum=True)
        for pitch, start, vel in notes:
            inst.notes.append(
                pretty_midi.Note(velocity=vel, pitch=pitch, start=start, end=start + 0.1)
            )
        pm.instruments.append(inst)
        pm.write(str(out_path))
        return out_path

    return _write
