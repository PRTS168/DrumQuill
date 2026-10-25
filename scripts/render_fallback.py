# -*- coding: utf-8 -*-
"""Fallback drum-score renderer: aligned MIDI -> printable PDF (pure reportlab).

Used by run_pipeline.ps1 when MuseScore is missing or fails to render.
Renders a Songsterr-style drum sheet: 5-line percussion staff, bar lines,
4/4 time signature, metronome mark, drum symbols (BD/SD/HH/Tom/Cym),
whole-bar rests, per-bar numbers, drum legend, multi-page A4 layout.

Usage: python render_fallback.py <in.mid> <out.pdf> [bpm] [title]

Note: cairosvg is NOT usable on this machine (no cairo DLL), so this module
draws directly with reportlab instead of SVG->PDF conversion. svglib chain
is verified as a documented alternative (see docs/fallback_matrix.md).
"""
import os
import sys
import pretty_midi
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

# GM drum pitches we can display (others are ignored, matching gen_musicxml)
PITCH_NAME = {35: "底鼓 BD", 38: "军鼓 SD", 42: "踩镲 HH", 47: "嗵鼓 Tom", 49: "吊镲 Cym"}
X_PITCHES = {42, 46, 49, 51, 57}  # x notehead (cymbals/hihat)
BD_PITCH = 35

if len(sys.argv) < 3:
    print("usage: render_fallback.py <in.mid> <out.pdf> [bpm] [title]")
    sys.exit(2)
MIDI = sys.argv[1]
OUT = sys.argv[2]
try:
    BPM = float(sys.argv[3]) if len(sys.argv) > 3 else 156.0
except ValueError:
    print("usage: render_fallback.py <in.mid> <out.pdf> [bpm] [title]（bpm 需为数字）")
    sys.exit(2)
if not (BPM > 0):  # 挡住 0 / 负数 / NaN / inf，否则 bar_s 计算失真或除零
    print("错误：BPM 必须为正数（收到：%r）。" % BPM, file=sys.stderr)
    sys.exit(2)
TITLE = sys.argv[4] if len(sys.argv) > 4 else os.path.splitext(os.path.basename(MIDI))[0]

# ---- layout constants (points, A4 landscape) ----
PAGE = landscape(A4)          # 842 x 595
MARGIN_L, MARGIN_R = 46, 40
MARGIN_T, MARGIN_B = 46, 40
S = 9.0                       # staff line spacing
STAFF_H = 4 * S               # 5 lines
BARS_PER_LINE = 6
LINES_PER_PAGE = 5
line_width = PAGE[0] - MARGIN_L - MARGIN_R
bar_width = line_width / BARS_PER_LINE
LINE_STEP = STAFF_H + 46       # vertical distance between systems (room for labels)

# slot -> offset (points above bottom staff line y0). bottom line = 0.
SLOT_Y = {
    35: -0.6 * S,    # space just under the bottom line
    38: 2.0 * S,     # middle line (line 3)
    47: 3.0 * S,     # space above middle
    42: 4.0 * S,     # top line
    49: 5.0 * S,     # space above top line
}

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
CJK = "STSong-Light"

# ---- load notes ----
try:
    pm = pretty_midi.PrettyMIDI(MIDI)
except Exception as e:
    print("错误：无法读取 MIDI 文件 %s（%s: %s）。" % (MIDI, type(e).__name__, e), file=sys.stderr)
    sys.exit(1)
notes = sorted((n.pitch, n.start, n.end) for inst in pm.instruments for n in inst.notes)
beat_s = 60.0 / BPM
bar_s = beat_s * 4
dur = pm.get_end_time()
# 空 MIDI（全休止）的语义（约定）：本脚本是 MuseScore 失败后的最后兜底渲染器，
# 「有输入就出谱」——0 音符按 1 小节全休止降级产出合法 PDF，退出 0，不算失败。
n_bars = max(1, int(round(dur / bar_s)))
if notes:
    # 最晚小节按全曲最大「起音时间」计；旧写法 notes[-1][1] 取的是按 (pitch,start)
    # 排序后的最后一项（最高音高的最后起音），吊镲早停、其余鼓件打到曲尾时会
    # 低估小节数，尾部音符画丢。
    last_start = max(s for _, s, _ in notes)
    n_bars = max(n_bars, int(last_start // bar_s) + 1)
print("fallback render: notes=%d bars=%d bpm=%g" % (len(notes), n_bars, BPM))


def bar_notes(b):
    lo, hi = b * bar_s, (b + 1) * bar_s
    return [(p, s, e) for (p, s, e) in notes if lo <= s < hi]


def draw_staff_lines(c, x_left, x_right, y0):
    for i in range(5):
        c.setLineWidth(0.7)
        c.line(x_left, y0 + i * S, x_right, y0 + i * S)


def draw_barline(c, x, y0):
    c.setLineWidth(1.0)
    c.line(x, y0, x, y0 + STAFF_H)


def draw_notehead(c, x, y, pitch):
    r = S * 0.42
    if pitch in X_PITCHES:
        c.setLineWidth(1.4)
        c.line(x - r, y - r * 0.8, x + r, y + r * 0.8)
        c.line(x - r, y + r * 0.8, x + r, y - r * 0.8)
    else:
        c.setFillColorRGB(0, 0, 0)
        c.setStrokeColorRGB(0, 0, 0)
        c.setLineWidth(1.0)
        c.ellipse(x - r, y - r * 0.62, x + r, y + r * 0.62, stroke=1, fill=1)


def draw_stem(c, x, y, pitch):
    r = S * 0.42
    c.setLineWidth(1.0)
    if pitch == BD_PITCH:
        c.line(x + r * 0.7, y, x + r * 0.7, y + 2.6 * S)   # BD: stem down
    else:
        c.line(x - r * 0.7, y, x - r * 0.7, y - 2.6 * S)   # others: stem up


def draw_ledger(c, x, y):
    c.setLineWidth(0.8)
    c.line(x - 0.85 * S, y, x + 0.85 * S, y)


def draw_whole_rest(c, x, y0):
    # small rectangle hanging below the 2nd line from top (simple convention)
    c.setFillColorRGB(0, 0, 0)
    c.rect(x, y0 + 3.1 * S, 1.6 * S, 0.45 * S, stroke=0, fill=1)


def draw_time_clef(c, x, y0):
    # percussion clef: two thick vertical bars
    c.setLineWidth(2.0)
    c.line(x, y0, x, y0 + STAFF_H)
    c.line(x + 3.5, y0, x + 3.5, y0 + STAFF_H)
    c.setLineWidth(0.7)
    # 4/4 time signature
    c.setFont(CJK, 12)
    c.drawCentredString(x + 11, y0 + 2.5 * S, "4")
    c.drawCentredString(x + 11, y0 + 0.9 * S, "4")


c = pdfcanvas.Canvas(OUT, pagesize=PAGE)

systems_total = (n_bars + BARS_PER_LINE - 1) // BARS_PER_LINE
page = 0
for si in range(systems_total):
    line_in_page = si % LINES_PER_PAGE
    if line_in_page == 0:
        if page > 0:
            c.showPage()
        page += 1
        # header on every page top
        c.setFont(CJK, 17)
        c.drawString(MARGIN_L, PAGE[1] - MARGIN_T - 6, "%s — 鼓谱" % TITLE)
        c.setFont(CJK, 10)
        c.setFillColorRGB(0.35, 0.35, 0.35)
        c.drawString(MARGIN_L, PAGE[1] - MARGIN_T - 22,
                     "自研渲染（MuseScore 不可用）· ♩=%g · 4/4 · 共 %d 小节 · 第 %d 页" % (int(BPM), n_bars, page))
        c.setFillColorRGB(0, 0, 0)
        if page == 1:
            # legend row
            lx = MARGIN_L
            ly = PAGE[1] - MARGIN_T - 40
            c.setFont(CJK, 9.5)
            for pitch, name in PITCH_NAME.items():
                draw_notehead(c, lx + 4, ly, pitch)
                c.drawString(lx + 14, ly - 3, name)
                lx += 95
    b0 = si * BARS_PER_LINE
    bars = list(range(b0, min(b0 + BARS_PER_LINE, n_bars)))
    # page 1 needs extra room for title/subtitle/legend; later pages tighter
    y0 = PAGE[1] - MARGIN_T - (112 if page == 1 else 78) - line_in_page * LINE_STEP

    # barlines + staff
    draw_staff_lines(c, MARGIN_L, MARGIN_L + bar_width * len(bars), y0)
    for j, b in enumerate(bars):
        x = MARGIN_L + j * bar_width
        draw_barline(c, x, y0)
        # bar number above staff
        c.setFont(CJK, 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawString(x + 3, y0 + STAFF_H + 5, str(b + 1))
        c.setFillColorRGB(0, 0, 0)
        # clef + time sig at start of first system
        if si == 0 and j == 0:
            draw_time_clef(c, x + 6, y0)
        # notes
        bn = bar_notes(b)
        if not bn:
            draw_whole_rest(c, x + bar_width * 0.5 - 0.8 * S, y0)
            continue
        for p, s, e in bn:
            if p not in SLOT_Y:
                continue
            rel = (s - b * bar_s) / bar_s
            nx = x + 12 + rel * (bar_width - 20)
            ny = y0 + SLOT_Y[p]
            draw_notehead(c, nx, ny, p)
            draw_stem(c, nx, ny, p)
            if p == BD_PITCH:
                draw_ledger(c, nx, y0)
    # closing barline
    draw_barline(c, MARGIN_L + bar_width * len(bars), y0)

c.save()
print("saved:", OUT, os.path.getsize(OUT), "bytes,", page, "pages")
