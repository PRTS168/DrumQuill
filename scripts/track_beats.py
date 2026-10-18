# -*- coding: utf-8 -*-
"""用 Beat This!（CPJKU）跟踪节拍与强拍，输出可直接当栅格用的拍点表。

为什么换掉"检测一个 BPM 再全曲套用"：
  · 普通音乐的速度并不恒定（渐慢、拖拍、段落变化），单一 BPM 会让后半段整体错位；
  · Beat This 直接给出每一拍的时间点与强拍，栅格随音乐走，不需要相位搜索/旋转修正；
  · 强拍就是小节线，小节数、拍号都从数据里来，而不是假设 4/4。

输出（默认写到 <outdir>/<song>.beats.json）：
{
  "bpm_median": 73.2, "meter": 4, "beats": [...], "downbeats": [...],
  "bars": [{"start": 0.0, "end": 1.640, "bpm": 73.2, "beats": [0.0, 0.82, ...]}],
  "duration": 287.4, "source": "beat_this"
}

用法：
  python track_beats.py <audio> <out.json> [--device cpu] [--fallback-librosa]
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def set_torch_home():
    """模型缓存定位：优先仓库内 models\\torch，其次用户设置的 TORCH_HOME。

    这样无论系统缓存目录怎么迁移，只要仓库里存过权重就能离线复用；
    两处都没有时才按 TORCH_HOME 下载。
    """
    local = os.path.join(ROOT, "models", "torch", "hub", "checkpoints")
    if os.path.isdir(local) and any(f.endswith(".ckpt") for f in os.listdir(local)):
        os.environ["TORCH_HOME"] = os.path.join(ROOT, "models", "torch")
        return
    torch_home = os.environ.get("TORCH_HOME") or os.path.join(ROOT, "models", "torch")
    try:
        os.makedirs(torch_home, exist_ok=True)
        os.environ.setdefault("TORCH_HOME", torch_home)
    except Exception:
        pass


def track_beat_this(audio, device="cpu"):
    set_torch_home()
    from beat_this.inference import File2Beats
    f2b = File2Beats(checkpoint_path="final0", device=device, dbn=False)
    beats, downbeats = f2b(audio)
    return [float(b) for b in beats], [float(b) for b in downbeats], "beat_this"


def track_librosa(audio):
    """兜底：librosa 只给拍，不给强拍；强拍按 4 拍推定。"""
    import librosa
    import numpy as np
    y, sr = librosa.load(audio, sr=22050, mono=True)
    _tempo, idx = librosa.beat.beat_track(y=y, sr=sr, trim=False, units="frames")
    beats = [float(t) for t in librosa.frames_to_time(idx, sr=sr)]
    if len(beats) < 8:
        return beats, [], "librosa"
    iv = float(np.median(np.diff(beats)))
    downbeats = beats[::4]
    return beats, [float(x) for x in downbeats], "librosa(%0.1f BPM)" % (60.0 / iv)


def build_bars(beats, downbeats, duration, meter=None):
    """按拍号整块切小节：每小节 meter 拍，起点对齐到第一个强拍所属的拍。"""
    import numpy as np
    if len(beats) < 8:
        return []
    beats_a = np.asarray(beats, dtype=float)
    if not meter:
        # 先看强拍间隔里最常出现几拍
        if len(downbeats) >= 3:
            db = np.asarray(downbeats, dtype=float)
            counts = []
            for i in range(len(db) - 1):
                inside = int(((beats_a >= db[i] - 1e-6) & (beats_a < db[i + 1] - 1e-6)).sum())
                if inside:
                    counts.append(inside)
            meter = int(round(float(np.median(counts)))) if counts else 4
        else:
            meter = 4
    meter = max(1, min(12, int(meter)))

    # 起点：第一个强拍在拍点里的序号（找不到就用 0）
    start_idx = 0
    if len(downbeats) >= 1:
        import bisect
        start_idx = min(bisect.bisect_left(list(beats_a), downbeats[0] - 0.05), len(beats_a) - 1)
        start_idx = max(0, start_idx)
    idx = start_idx
    bars = []
    while idx + meter <= len(beats_a):
        chunk = beats_a[idx:idx + meter]
        span = beats_a[idx + meter] - beats_a[idx] if idx + meter < len(beats_a) else (chunk[-1] - chunk[0] + np.median(np.diff(beats_a)))
        bar = {
            "start": round(float(chunk[0]), 4),
            "end": round(float(beats_a[idx + meter]) if idx + meter < len(beats_a) else float(chunk[-1] + (chunk[-1] - chunk[0]) / max(1, meter - 1)), 4),
            "bpm": round(60.0 * meter / span, 2) if span > 0 else 0.0,
            "beats": [round(float(b), 4) for b in chunk],
        }
        bars.append(bar)
        idx += meter

    # 尾部不足一小节的拍点：并入再加一个"不完整小节"（保持拍数真实，谱面会按整小节写）
    if idx < len(beats_a):
        chunk = beats_a[idx:]
        span = float(chunk[-1] - chunk[0]) + (float(chunk[-1] - chunk[0]) / max(1, len(chunk) - 1) if len(chunk) > 1 else 0.5)
        bars.append({
            "start": round(float(chunk[0]), 4),
            "end": round(float(chunk[0]) + (float(chunk[-1] - chunk[0]) + 0.5) if len(chunk) > 1 else float(chunk[0]) + 0.5, 4),
            "bpm": round(60.0 * len(chunk) / span, 2) if span > 0 else 0.0,
            "beats": [round(float(b), 4) for b in chunk],
        })
    return bars


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    audio, out = sys.argv[1], sys.argv[2]
    device = "cpu"
    for i, a in enumerate(sys.argv):
        if a == "--device" and i + 1 < len(sys.argv):
            device = sys.argv[i + 1]

    if not os.path.isfile(audio):
        print("[!] 音频不存在：%s" % audio)
        return 2

    beats, downbeats, source = [], [], ""
    try:
        beats, downbeats, source = track_beat_this(audio, device)
    except Exception as e:
        sys.stderr.write("[!] Beat This 不可用（%s），回退 librosa\n" % e)
    if len(beats) < 8:
        beats, downbeats, source = track_librosa(audio)

    if len(beats) < 8:
        print("[!] 拍点太少（%d），无法建立栅格" % len(beats))
        return 1

    import numpy as np
    iv = np.diff(beats)
    duration = beats[-1] + float(np.median(iv)) if len(beats) > 1 else 0.0
    if len(downbeats) < 2:
        downbeats = beats[::4]
    bars = build_bars(beats, downbeats, duration)
    _counts = [len(b["beats"]) for b in bars]
    meter = int(round(float(__import__("numpy").median(_counts)))) if _counts else 4
    # 每拍属于哪小节、小节内第几拍（按顺序分配，供对齐脚本直接使用）
    beat_bars, beat_positions = [], []
    i = 0
    for bi, bar in enumerate(bars):
        for k in range(len(bar["beats"])):
            if i < len(beats):
                beat_bars.append(bi)
                beat_positions.append(k)
                i += 1
    while len(beat_bars) < len(beats):
        beat_bars.append(max(0, len(bars) - 1))
        beat_positions.append(0)
    meter_counts = [len(b["beats"]) for b in bars]
    meter = int(round(float(np.median(meter_counts)))) if meter_counts else 4

    data = {
        "source": source,
        "bpm_median": round(float(60.0 / np.median(iv)), 2),
        "bpm_cv": round(float(np.std(iv) / np.mean(iv)), 4),
        "meter": meter,
        "bars": bars,
        "beat_bars": beat_bars,
        "beat_positions": beat_positions,
        "downbeats": [round(d, 4) for d in downbeats],
        "beats": [round(b, 4) for b in beats],
        "duration": round(duration, 3),
    }
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("拍 %d 个，强拍 %d 个，小节 %d 个，拍号 %d/4，中位速度 %.1f BPM（拍距变异 %.3f，来源 %s）"
          % (len(beats), len(downbeats), len(bars), meter, data["bpm_median"], data["bpm_cv"], source))
    print("saved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
