# -*- coding: utf-8 -*-
"""track_beats.py 测试：小节切分与拍-小节分配（纯函数，不依赖音频与模型）。"""
import track_beats as tb


def _beats(n, step=0.5):
    return [round(i * step, 4) for i in range(n)]


# ---------- assign_beat_bars（弱起拍记 -1） ----------

def test_ten_beats_start_idx_two_two_full_bars():
    # 复现场景：10 拍、第一个强拍在第 2 拍（start_idx=2）、meter=4。
    # beat_bars 应为 [-1,-1,0,0,0,0,1,1,1,1]：前两拍是弱起（不属任何小节），
    # beats[2+k] → bar=k//4, pos=k%4。
    # 旧实现从 beats[0] 起顺序分配 → [0,0,0,0,1,1,1,1,1,1]，全体晚 2 拍且尾部被塞进最后一格。
    beats = _beats(10)
    bars = tb.build_bars(beats, [beats[2]], duration=5.0, meter=4)
    assert [len(b["beats"]) for b in bars] == [4, 4]
    assert bars[0]["beats"] == beats[2:6]
    beat_bars, beat_positions, pickup = tb.assign_beat_bars(beats, bars)
    assert pickup == 2
    assert beat_bars == [-1, -1, 0, 0, 0, 0, 1, 1, 1, 1]
    assert beat_positions == [-1, -1, 0, 1, 2, 3, 0, 1, 2, 3]


def test_no_pickup_when_first_beat_is_downbeat():
    beats = _beats(8)
    bars = tb.build_bars(beats, [beats[0]], duration=4.0, meter=4)
    beat_bars, beat_positions, pickup = tb.assign_beat_bars(beats, bars)
    assert pickup == 0
    assert beat_bars == [0, 0, 0, 0, 1, 1, 1, 1]
    assert beat_positions == [0, 1, 2, 3, 0, 1, 2, 3]


def test_tail_partial_bar_gets_own_bar_not_crammed():
    # 13 拍、start_idx=2：2 个整小节 + 1 个 3 拍的不完整小节；
    # 尾部拍点归自己的小节，不再被补齐循环塞进最后一个整小节。
    beats = _beats(13)
    bars = tb.build_bars(beats, [beats[2]], duration=6.5, meter=4)
    assert [len(b["beats"]) for b in bars] == [4, 4, 3]
    beat_bars, beat_positions, pickup = tb.assign_beat_bars(beats, bars)
    assert pickup == 2
    assert beat_bars == [-1, -1, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2]
    assert beat_positions == [-1, -1, 0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2]


def test_no_downbeat_means_no_pickup():
    # 没有强拍数据：从 beats[0] 开始切，没有弱起
    beats = _beats(8)
    bars = tb.build_bars(beats, [], duration=4.0, meter=4)
    beat_bars, beat_positions, pickup = tb.assign_beat_bars(beats, bars)
    assert pickup == 0
    assert beat_bars == [0, 0, 0, 0, 1, 1, 1, 1]


def test_bars_cover_beats_after_pickup():
    # 不变式：bars 恰好覆盖弱起之后的全部拍（含尾部不完整小节）
    for n in (8, 10, 11, 13, 17):
        beats = _beats(n)
        bars = tb.build_bars(beats, [beats[2]], duration=n * 0.5, meter=4)
        beat_bars, _pos, pickup = tb.assign_beat_bars(beats, bars)
        assert pickup == 2
        assert len(beat_bars) == n
        assert beat_bars[:2] == [-1, -1]
        assert all(b >= 0 for b in beat_bars[2:])
