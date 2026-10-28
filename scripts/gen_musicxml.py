# -*- coding: utf-8 -*-
"""Drum MIDI → MusicXML（网格制谱版）。

用法：
  python gen_musicxml.py <in.mid> <out.musicxml> [bpm] [title] [markers_file] [bars_per_line] [grid]

与旧版的区别（旧版把"到下一个音符的间隔"当音符时值，导致 duration=961/1441、
type 与 duration 对不上、64 分碎片、小节被拆散）：

  1. 落点统一量到网格上（默认 16 分，可用 grid=32 更细）；
  2. 每个音符的时值只取合法值 {32分,16分,8分,4分}，type 由时值唯一决定 —— 不可能再对不上；
  3. 空隙用休止符补齐，每小节时值合计必须等于 1920（4/4, divisions=480），否则直接报错；
  4. 力度转成记谱信息：velocity≥112 标重音记号（>），军鼓/嗵鼓 velocity≤70 标鬼音括号；
  5. 鼓件只写 ADTOF 真正能分辨的 5 件，不做虚假映射。

输出前会打印校验结果，任何一条不满足都会以非零码退出。
"""
import os
import sys
from collections import defaultdict

import pretty_midi

MIDI = sys.argv[1] if len(sys.argv) > 1 else None
OUT = sys.argv[2] if len(sys.argv) > 2 else None
BPM = float(sys.argv[3]) if len(sys.argv) > 3 else 156.0
TITLE = sys.argv[4] if len(sys.argv) > 4 else "Drum Score"
MARKERS_FILE = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] not in ("", "-") else None
BARS_PER_LINE = int(sys.argv[6]) if len(sys.argv) > 6 else 4
GRID = int(sys.argv[7]) if len(sys.argv) > 7 else 16
PLAN_PATH = sys.argv[8] if len(sys.argv) > 8 and sys.argv[8] not in ("", "-") else os.environ.get("DRUM_BEAT_PLAN", "")
PLAN = None
if PLAN_PATH and os.path.isfile(PLAN_PATH):
    import json as _json
    try:
        with open(PLAN_PATH, encoding="utf-8") as _f:
            PLAN = _json.load(_f)
    except Exception as _e:
        print("[!] plan.json 读取失败（%s），退回固定 BPM 模式" % _e)
        PLAN = None

if not MIDI or not OUT:
    print("usage: gen_musicxml.py <in.mid> <out.musicxml> [bpm] [title] [markers] [bars_per_line] [grid]")
    sys.exit(2)

DIV = 480                     # 每四分音符 tick
BEAT_S = 60.0 / BPM
BAR_S = BEAT_S * 4
BAR_TICKS = DIV * 4           # 1920（plan 模式下按拍号重算）
METER = 4

# 网格单元（tick）
if GRID not in (4, 8, 16, 32):
    print("[!] grid 只支持 4/8/16/32，按 16 处理")
    GRID = 16
CELL = int(BAR_TICKS / GRID)  # 16 分 -> 120

# 合法时值：只保留 2 的幂次，type 与时值唯一对应
TYPE_OF = {1920: "whole", 960: "half", 480: "quarter", 240: "eighth", 120: "16th", 60: "32nd"}
NOTE_MAX = 480                # 打击音最多写到四分，靠休止符填空白
REST_ORDER = [960, 480, 240, 120, 60]

# GM 打击乐音高 -> (显示步, 显示八度, MuseScore 内部音高, 乐器名, 符干)
DRUM_MAP = {
    35: ("E", 4, 36, "Bass Drum", "down"),
    38: ("C", 5, 39, "Acoustic Snare", "up"),
    42: ("G", 5, 43, "Closed Hi-Hat", "up"),
    47: ("D", 5, 48, "Tom", "up"),
    49: ("A", 5, 50, "Crash Cymbal 1", "up"),
}
X_PITCHES = {42, 49}          # 踩镲/吊镲用 x 符头
GHOST_PITCHES = {38, 47}      # 可以打鬼音括号的鼓件

ACCENT_VEL = 112          # 兜底值；main() 里会按全曲力度分位数自校准
GHOST_VEL = 70


def legal_below(limit, cap):
    """不超过 limit（且不超过 cap）的最大合法时值。"""
    for d in (480, 240, 120, 60):
        if d <= limit and d <= cap:
            return d
    return 60


def rest_chain(total):
    """把空隙拆成合法休止符。"""
    out = []
    left = total
    for d in REST_ORDER:
        while left >= d:
            out.append(d)
            left -= d
    return out, left


def load_notes(path):
    pm = pretty_midi.PrettyMIDI(path)
    raw = []
    for inst in pm.instruments:
        for n in inst.notes:
            raw.append((int(n.pitch), float(n.start), int(n.velocity or 100)))
    return raw


def quantize(raw):
    """绝对时间 -> (小节, 小节内 tick) -> {pitch: velocity}。"""
    cells = defaultdict(dict)          # (bar, tick) -> {pitch: vel}
    for pitch, start, vel in raw:
        if pitch not in DRUM_MAP:
            continue
        abs_tick = int(round(start / BEAT_S * DIV / CELL)) * CELL
        if abs_tick < 0:
            abs_tick = 0
        bar, tick = divmod(abs_tick, BAR_TICKS)
        slot = cells[(bar, tick)]
        slot[pitch] = max(slot.get(pitch, 0), vel)
    return cells


# 律动先验：用在"小节旋转"打分上（鼓组常见落点）
KICK, SNARE, HI, TOM, CYMBAL = 35, 38, 42, 47, 49


SIGMA_CELL = 0.35
TARGETS = {
    KICK: {0: 1.2, 8: 1.0, 4: 0.4, 12: 0.4},
    SNARE: {4: 1.0, 12: 1.0},
    CYMBAL: {0: 1.8, 8: 0.5},
    HI: {0: 0.5, 2: 0.5, 4: 0.5, 6: 0.5, 8: 0.5, 10: 0.5, 12: 0.5, 14: 0.5},
    TOM: {14: 0.3, 15: 0.3, 0: 0.3, 1: 0.3},
}


def continuous_groove(raw, rot_cells):
    """连续律动评分：按"到期望格的加权重合度"累加，避免半格翻转。"""
    import math
    grid = BAR_TICKS / float(CELL)
    total = 0.0
    for pitch, start, _vel in raw:
        if pitch not in DRUM_MAP:
            continue
        pos = ((start / BEAT_S * DIV - rot_cells * CELL) % BAR_TICKS) / CELL
        targets = TARGETS.get(pitch)
        if not targets:
            continue
        best = 0.0
        for target, w in targets.items():
            d = abs((pos - target + grid / 2.0) % grid - grid / 2.0)
            best = max(best, w * math.exp(-(d * d) / (2.0 * SIGMA_CELL * SIGMA_CELL)))
        total += best
    return total


def best_rotation(raw):
    """在 min(16, 4*METER) 个相位里选律动最优的旋转；平局取更小 rot（确定性）。"""
    n_phases = min(16, 4 * METER)
    scores = [(round(continuous_groove(raw, r), 3), r) for r in range(n_phases)]
    scores.sort(key=lambda sr: (-sr[0], sr[1]))
    return scores[0][1], scores


def build_measures(cells):
    """把格子整理成"每小节一个事件列表"，并给出每个事件的时值与休止。"""
    bars = defaultdict(list)
    for (bar, tick), pitches in cells.items():
        bars[bar].append((tick, pitches))

    measures = {}
    stats = {"notes": 0, "accents": 0, "ghosts": 0}
    for bar, events in bars.items():
        events.sort(key=lambda e: e[0])
        items = []          # [(kind, tick, dur, pitches_or_None, type)]

        # 小节开头到第一击之间的空隙（前导休止符），少这一步每小节都会缺拍
        lead = events[0][0]
        if lead > 0:
            rests, leftover = rest_chain(lead)
            if leftover:
                raise SystemExit("[!] 小节 %d 前导休止无法用合法时值填满（残留 %d）" % (bar + 1, leftover))
            cur = 0
            for r in rests:
                items.append(("rest", cur, r, None, TYPE_OF[r]))
                cur += r

        for i, (tick, pitches) in enumerate(events):
            next_tick = events[i + 1][0] if i + 1 < len(events) else BAR_TICKS
            gap = max(60, next_tick - tick)
            dur = legal_below(gap, NOTE_MAX)
            items.append(("note", tick, dur, pitches, TYPE_OF[dur]))
            stats["notes"] += len(pitches)
            for _p, v in pitches.items():
                if v >= ACCENT_VEL:
                    stats["accents"] += 1
                if v <= GHOST_VEL:
                    stats["ghosts"] += 1
            rest_start = tick + dur
            rests, leftover = rest_chain(max(0, next_tick - rest_start))
            if leftover:
                raise SystemExit("[!] 小节 %d tick %d 处休止符无法用合法时值填满（残留 %d）"
                                 % (bar + 1, tick, leftover))
            cur = rest_start
            for r in rests:
                items.append(("rest", cur, r, None, TYPE_OF[r]))
                cur += r
        measures[bar] = items
    return measures, stats


def validate(measures):
    """硬校验：每小节时值合计、合法时值、type 一致。"""
    problems = []
    allowed = set(TYPE_OF)
    for bar, items in measures.items():
        total = sum(d for _k, _t, d, _p, _ty in items)
        if total != BAR_TICKS:
            problems.append("小节 %d 时值合计 %d ≠ %d" % (bar + 1, total, BAR_TICKS))
        for kind, tick, dur, _p, ty in items:
            if dur not in allowed:
                problems.append("小节 %d 出现非法时值 %d" % (bar + 1, dur))
            if TYPE_OF.get(dur) != ty:
                problems.append("小节 %d 时值 %d 与记谱类型 %s 不一致" % (bar + 1, dur, ty))
            if dur < 60:
                problems.append("小节 %d 出现 64 分音符碎片" % (bar + 1))
    return problems


def load_markers(path):
    markers = {}
    if not path:
        return markers
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and ":" in line:
                b, txt = line.split(":", 1)
                try:
                    markers[int(b)] = txt.strip()
                except ValueError:
                    pass
    except OSError:
        pass
    return markers


def emit(measures, markers, n_bars):
    x = []
    _last_tempo = [None, 0]   # [上次速度, 上次所在小节]
    x.append('<?xml version="1.0" encoding="UTF-8"?>')
    x.append('<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 3.1 Partwise//EN" '
             '"http://www.musicxml.org/dtds/partwise.dtd">')
    x.append('<score-partwise version="3.1">')
    x.append('  <work><work-title>%s</work-title></work>' % TITLE)
    x.append('  <identification><creator type="composer">DrumQuill (Demucs + ADTOF)</creator></identification>')
    x.append('  <part-list>')
    x.append('    <score-part id="P1">')
    x.append('      <part-name>Drum Kit</part-name>')
    for pitch in sorted(DRUM_MAP):
        step, octv, ip, name, _stem = DRUM_MAP[pitch]
        x.append('      <score-instrument id="P1-I%d"><instrument-name>%s</instrument-name></score-instrument>'
                 % (ip, name))
    for pitch in sorted(DRUM_MAP):
        step, octv, ip, name, _stem = DRUM_MAP[pitch]
        x.append('      <midi-instrument id="P1-I%d"><midi-channel>10</midi-channel>'
                 '<midi-program>1</midi-program><midi-unpitched>%d</midi-unpitched></midi-instrument>'
                 % (ip, ip))
    x.append('    </score-part>')
    x.append('  </part-list>')
    x.append('  <part id="P1">')

    for bar in range(n_bars):
        items = measures.get(bar, [("rest", 0, BAR_TICKS, None, "whole")])
        x.append('    <measure number="%d">' % (bar + 1))
        if bar > 0 and bar % BARS_PER_LINE == 0:
            x.append('      <print new-system="yes"/>')
        if bar == 0:
            x.append('      <attributes>')
            x.append('        <divisions>%d</divisions>' % DIV)
            x.append('        <time><beats>%d</beats><beat-type>4</beat-type></time>' % METER)
            x.append('        <clef><sign>percussion</sign></clef>')
            x.append('      </attributes>')
            if not PLAN:
                x.append('      <direction placement="above"><direction-type>'
                         '<metronome><beat-unit>quarter</beat-unit><per-minute>%.0f</per-minute></metronome>'
                         '</direction-type><sound tempo="%.1f"/></direction>' % (BPM, BPM))
        if PLAN:
            try:
                bpm_bar = float(PLAN["bars"][bar]["bpm"])
            except Exception:
                bpm_bar = BPM
            prev = _last_tempo[0]
            last_bar = _last_tempo[1]
            # 阈值：可见标记只给"段落级"的速度变化；其余只作播放用
            show_all = str(os.environ.get("DRUM_SHOW_TEMPO", "")).lower() in ("1", "true", "yes")
            if prev is None:
                # 第 1 小节：可见的 ♩=xx
                x.append('      <direction placement="above"><direction-type>'
                         '<metronome><beat-unit>quarter</beat-unit>'
                         '<per-minute>%.0f</per-minute></metronome></direction-type>'
                         '<sound tempo="%.1f"/></direction>' % (bpm_bar, bpm_bar))
                _last_tempo[0] = bpm_bar
                _last_tempo[1] = bar
            else:
                drift = abs(bpm_bar - prev) / prev if prev else 0.0
                big = drift > (0.0005 if show_all else 0.015)
                long_gap = (bar - last_bar) >= 32 and drift > 0.004
                if big or long_gap:
                    if show_all:
                        # 需要看到所有变化时：可见的括号标记
                        x.append('      <direction placement="above"><direction-type>'
                                 '<metronome parentheses="yes"><beat-unit>quarter</beat-unit>'
                                 '<per-minute>%.0f</per-minute></metronome></direction-type>'
                                 '<sound tempo="%.1f"/></direction>' % (bpm_bar, bpm_bar))
                    else:
                        # 默认：只给播放引擎换速，谱面不留记号
                        x.append('      <sound tempo="%.1f"/>' % bpm_bar)
                elif not show_all:
                    # 每一小节都给播放引擎真实速度（不可见）：速度波动大的歌里少写任何一
                    # 小节，MuseScore 就会沿用旧速度渲染，合成音频与拍点网格累积漂移
                    # （实测 60s 曲内漂 9.5s，动态谱与音乐对不上）。谱面不受影响。
                    x.append('      <sound tempo="%.1f"/>' % bpm_bar)
                _last_tempo[0] = bpm_bar
                _last_tempo[1] = bar
        if (bar + 1) in markers:
            x.append('      <direction placement="above"><direction-type>'
                     '<words font-weight="bold">%s</words></direction-type></direction>' % markers[bar + 1])

        for kind, tick, dur, pitches, ty in items:
            if kind == "rest":
                x.append('      <note><rest/><duration>%d</duration><voice>1</voice>'
                         '<type>%s</type></note>' % (dur, ty))
                continue
            ordered = sorted(pitches.items(), key=lambda kv: kv[0])
            for j, (pitch, vel) in enumerate(ordered):
                step, octv, ip, _name, stem = DRUM_MAP[pitch]
                x.append('      <note>')
                if j > 0:
                    x.append('        <chord/>')
                x.append('        <unpitched><display-step>%s</display-step>'
                         '<display-octave>%d</display-octave></unpitched>' % (step, octv))
                x.append('        <duration>%d</duration>' % dur)
                x.append('        <instrument id="P1-I%d"/>' % ip)
                x.append('        <voice>1</voice>')
                x.append('        <type>%s</type>' % ty)
                if pitch in X_PITCHES:
                    x.append('        <notehead>x</notehead>')
                elif pitch in GHOST_PITCHES and vel <= GHOST_VEL:
                    x.append('        <notehead parentheses="yes">normal</notehead>')
                x.append('        <stem>%s</stem>' % stem)
                if vel >= ACCENT_VEL:
                    x.append('        <notations><articulations><accent/></articulations></notations>')
                x.append('      </note>')
        x.append('    </measure>')

    x.append('  </part>')
    x.append('</score-partwise>')
    return x


def calibrate(raw):
    """按全曲力度分布定阈值：约前 12% 算重音，后 8% 算鬼音。"""
    global ACCENT_VEL, GHOST_VEL
    vels = sorted(v for _p, _s, v in raw if v > 0)
    if len(vels) < 40:
        return
    ACCENT_VEL = vels[int(len(vels) * 0.88)]
    GHOST_VEL = vels[max(0, int(len(vels) * 0.08))]
    if ACCENT_VEL <= GHOST_VEL:
        ACCENT_VEL = GHOST_VEL + 1


def main():
    raw = load_notes(MIDI)
    if not raw:
        raise SystemExit("[!] 输入 MIDI 没有音符")
    if PLAN:
        # 必须先按真实拍号定小节长度、再量化：否则非 4/4 会按 4/4 错切小节边界。
        # CELL 是十六分音符固定长度（120 tick），不随拍号变化，无需重算。
        globals()["METER"] = int(PLAN.get("meter") or 4)
        globals()["BAR_TICKS"] = DIV * globals()["METER"]
        print("拍栅格模式：%d 小节，%d/4，中位速度 %.1f BPM（栅格来自真实拍点，不做相位搜索）"
              % (int(PLAN.get("n_bars") or 0), METER, float(PLAN.get("bpm_median") or 0)))
    calibrate(raw)
    cells = quantize(raw)
    if not cells:
        raise SystemExit("[!] 量化后没有任何可记谱的鼓件（检查音高映射）")
    # 全曲总长（小节数）在位移前定死：rot 回卷按整曲总长取模 wrap 到曲尾。
    # 整曲取模是双射，任意 rot 下每个输入音符恰好映射到唯一输出槽位（音符数守恒）；
    # 旧回卷只补一根小节线，小节0早期音符会与小节1同 tick 音符撞键互相覆盖（静默丢音）。
    n_bars = int(PLAN["n_bars"]) if PLAN else (max(cells)[0] + 1)
    if PLAN:
        # 拍栅格模式：小节与相位都来自真实拍点，不做相位搜索，rot=0 不位移
        # （与 run_pipeline「跳过固定 BPM 量化与小节相位搜索」的口径一致）
        rot = 0
    else:
        # 固定 BPM 模式：小节边界可能整体错位 -> 用鼓类先验在有限相位里校正
        rot, rot_scores = best_rotation(raw)
        if rot:
            total_ticks = n_bars * BAR_TICKS
            shifted = {}
            for (bar, tick), pitches in cells.items():
                abs_tick = (bar * BAR_TICKS + tick - rot * CELL) % total_ticks
                key = divmod(abs_tick, BAR_TICKS)
                slot = shifted.get(key)
                if slot is None:
                    shifted[key] = dict(pitches)
                else:
                    # 回卷后仍同键（如 plan 边界外溢出等极端情形）：合并进同一槽位，不丢音
                    for p, v in pitches.items():
                        slot[p] = max(slot.get(p, 0), v)
            cells = shifted
            print("bar rotation: %d 个十六分（%.0f ms），候选前 3：%s"
                  % (rot, rot * CELL * BEAT_S / DIV * 1000, rot_scores[:3]))
    measures, stats = build_measures(cells)

    problems = validate(measures)
    if problems:
        print("[!] 校验失败，未写出文件：")
        for p in problems[:12]:
            print("    - " + p)
        if len(problems) > 12:
            print("    …共 %d 条" % len(problems))
        return 1

    xml = emit(measures, load_markers(MARKERS_FILE), n_bars)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(xml) + "\n")

    print("bars: %d  notes: %d  accents: %d  ghosts: %d  grid: 1/%d  (accent>=%d, ghost<=%d)"
          % (n_bars, stats["notes"], stats["accents"], stats["ghosts"], GRID,
             ACCENT_VEL, GHOST_VEL))
    _meter = globals()["METER"]
    print("校验通过：每小节时值合计 %d，时值合法，type 与时值一致（%d 小节，%d/4%s）"
          % (globals()["BAR_TICKS"], n_bars, _meter, "，拍栅格模式" if PLAN else "，小节相位已按律动对齐"))
    print("saved:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
