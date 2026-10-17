# -*- coding: utf-8 -*-
"""ADTOF 转录（研究后重写版）：可配逐类阈值 + 用激活峰值强度做力度（velocity）。

和原来直接调 `python -m adtof_pytorch.cli` 的区别：
  1. 阈值可以逐类传（默认值是按实测 F1 调好的，见 models_registry.BUILTIN）；
  2. 力度不再恒为 100：取每个峰值的激活强度，按该类的分位数映射到 45~127，
     谱面上重音/轻击能看出来（原来所有音符一样粗，打出来像机器）；
  3. 一个进程内完成，省掉一次 python 冷启动与权重重复加载；
  4. 额外输出 `--report` JSON（每类音符数/平均力度），便于排查"谱子太密/太稀"。

用法：
  python transcribe.py --audio drums.wav --out raw.mid [--thresholds 0.08,0.16,0.40,0.10,0.34]
                       [--fps 100] [--report report.json] [--device cpu]
"""
import argparse
import json
import os
import sys

import numpy as np

# ADTOF 可能是源码目录，也可能 pip 装进了 venv
for cand in (os.environ.get("DRUM_AI_ADTOF_DIR"),
             os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adtof"),
             os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ADTOF-pytorch")):
    if cand and os.path.isdir(cand) and os.path.isdir(os.path.join(cand, "src", "adtof_pytorch")):
        sys.path.insert(0, cand)
        break

try:
    from adtof_pytorch import (calculate_n_bins, create_frame_rnn_model, get_default_weights_path,
                               load_audio_for_model, load_pytorch_weights)
except Exception as e:  # pragma: no cover
    sys.stderr.write("找不到 adtof_pytorch：%s\n" % e)
    raise

NAMES = ["kick", "snare", "tom", "hihat", "cymbal"]
LABELS_5 = [35, 38, 47, 42, 49]
# 出厂阈值 = 实测扫参结果（同一首参考谱，A 段调、B 段验证，微平均 F1 提升 2~4 点）
DEFAULT_THRESHOLDS = [0.08, 0.16, 0.40, 0.10, 0.34]


def pick_with_strength(act, thresholds, fps=100,
                       pre_avg=0.1, post_avg=0.01, pre_max=0.02, post_max=0.01, combine=0.02):
    """逐类峰值拾取，返回 {类名: [(时间, 强度)]}。

    逻辑与 ADTOF 的 NotePeakPickingProcessor 一致（减去前向均值 → 局部极大 → 阈值 → 合并），
    区别只是把峰值处的幅值一起带出来，用于映射力度。
    """
    out = {}
    pre_avg_n = int(round(pre_avg * fps))
    post_avg_n = int(round(post_avg * fps))
    pre_max_n = int(round(pre_max * fps))
    post_max_n = int(round(post_max * fps))
    combine_n = max(1, int(round(combine * fps)))

    for i, name in enumerate(NAMES):
        x = np.asarray(act[:, i], dtype=np.float32)
        if pre_avg_n or post_avg_n:
            k = np.ones(pre_avg_n + 1 + post_avg_n, dtype=np.float32)
            k /= k.size
            padded = np.pad(x, (pre_avg_n, post_avg_n), mode="edge")
            ma = np.convolve(padded, k, mode="valid").astype(np.float32, copy=False)
            proc = np.maximum(0.0, x - ma)
        else:
            proc = x
        if pre_max_n or post_max_n:
            size = pre_max_n + 1 + post_max_n
            padded = np.pad(proc, (pre_max_n, post_max_n), mode="edge")
            win = np.stack([padded[j:j + len(proc)] for j in range(size)], axis=0).max(axis=0)
            is_peak = (proc >= win) & (proc >= thresholds[i])
        else:
            is_peak = proc >= thresholds[i]
        idx = np.where(is_peak)[0]
        peaks = []
        if idx.size:
            group = [int(idx[0])]
            for j in idx[1:]:
                if j - group[-1] <= combine_n:
                    group.append(int(j))
                else:
                    best = max(group, key=lambda z: proc[z])
                    peaks.append((best / float(fps), float(proc[best])))
                    group = [int(j)]
            best = max(group, key=lambda z: proc[z])
            peaks.append((best / float(fps), float(proc[best])))
        out[name] = peaks
    return out


def velocities(per_class, lo=45, hi=127):
    """把峰值强度映射成力度：按每类自身 P95 归一化，避免不同类之间尺度打架。"""
    vel = {}
    for name, peaks in per_class.items():
        if not peaks:
            vel[name] = []
            continue
        strengths = np.array([s for _t, s in peaks], dtype=np.float64)
        p95 = float(np.percentile(strengths, 95)) or 1.0
        vs = np.clip(np.round(lo + (hi - lo) * np.clip(strengths / p95, 0.0, 1.0)), lo, hi).astype(int)
        vel[name] = [(t, int(v)) for (t, _s), v in zip(peaks, vs)]
    return vel


def write_midi(path, vel, note_len=0.1):
    import pretty_midi
    midi = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=1, is_drum=True)
    for i, name in enumerate(NAMES):
        pitch = LABELS_5[i]
        for t, v in vel.get(name, []):
            inst.notes.append(pretty_midi.Note(velocity=int(v), pitch=pitch,
                                               start=float(t), end=float(t + note_len)))
    inst.notes.sort(key=lambda n: n.start)
    midi.instruments.append(inst)
    midi.write(path)


def thresholds_for_model(model):
    """按分离模型取实测调优阈值（app/models_registry.py 是唯一事实来源）。"""
    if not model:
        return list(DEFAULT_THRESHOLDS)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "app"))
    try:
        import models_registry
        cfg = {}
        cfg_path = os.path.join(root, "config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        return list(models_registry.thresholds_for(model, cfg))
    except Exception:
        return list(DEFAULT_THRESHOLDS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--thresholds", default="")
    ap.add_argument("--model", default="", help="分离模型名，用于查该模型的调优阈值")
    ap.add_argument("--fps", type=int, default=100)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--report", default="")
    ap.add_argument("--weights", default="")
    args = ap.parse_args()

    ths = thresholds_for_model(args.model)
    if args.thresholds.strip():
        parts = [p.strip() for p in args.thresholds.split(",") if p.strip()]
        if len(parts) != 5:
            sys.stderr.write("阈值需要 5 个数字（底鼓,军鼓,嗵鼓,踩镲,吊镲），收到 %d 个\n" % len(parts))
            return 2
        ths = [float(p) for p in parts]

    import torch
    device = args.device if (args.device != "cuda" or torch.cuda.is_available()) else "cpu"
    model = create_frame_rnn_model(calculate_n_bins())
    model.eval()
    w = args.weights or get_default_weights_path()
    if w and os.path.isfile(w):
        model = load_pytorch_weights(model, w, strict=False)
    model.to(device)

    x = load_audio_for_model(args.audio).to(device)
    with torch.no_grad():
        pred = model(x).cpu().numpy()
    if pred.ndim == 3:
        pred = pred[0]

    peaks = pick_with_strength(pred, ths, fps=args.fps)
    vel = velocities(peaks)
    write_midi(args.out, vel)

    counts = {k: len(v) for k, v in vel.items()}
    mean_vel = {k: (round(float(np.mean([v for _t, v in vals])), 1) if vals else 0.0)
                for k, vals in vel.items()}
    print("Wrote MIDI to %s" % os.path.abspath(args.out))
    print("[transcribe] 阈值 %s · 音符 %d（%s）· 平均力度 %s"
          % (ths, sum(counts.values()), counts, mean_vel))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"thresholds": ths, "counts": counts, "mean_velocity": mean_vel,
                       "fps": args.fps, "device": device}, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
