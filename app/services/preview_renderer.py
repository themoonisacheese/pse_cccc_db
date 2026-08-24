"""Server-side preview image renderer for CCCC embeddable pages.

Generates PNG preview images for clue, user, and sequence pages so they
can be embedded in SE chat. The images use the site's Tokyo Night-inspired
dark theme (matching style.css) and show the vital info from each page.

Used by the content-negotiated .png routes in main.py.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# ── Theme colors (matching style.css :root) ──────────────────
BG = (26, 27, 38)          # #1a1b26
BG_SOFT = (31, 35, 53)     # #1f2335
BG_CARD = (36, 40, 59)     # #24283b
FG = (192, 202, 245)       # #c0caf5
FG_DIM = (86, 95, 137)     # #565f89
ACCENT = (122, 162, 247)   # #7aa2f7
GREEN = (158, 206, 106)    # #9ece6a
RED = (247, 118, 142)      # #f7768e
YELLOW = (224, 175, 104)  # #e0af68
ORANGE = (255, 158, 100)  # #ff9e64
BORDER = (42, 46, 63)      # #2a2e3f

# ── Fonts ────────────────────────────────────────────────────
_FONT_SANS = "DejaVuSans.ttf"
_FONT_SANS_BOLD = "DejaVuSans-Bold.ttf"
_FONT_MONO = "DejaVuSansMono.ttf"


def _font(size, bold=False, mono=False):
    if mono:
        return ImageFont.truetype(_FONT_MONO, size)
    return ImageFont.truetype(_FONT_SANS_BOLD if bold else _FONT_SANS, size)


def _text_width(draw, text, font):
    return draw.textbbox((0, 0), text, font=font)[2]


def _wrap_text(text, font, draw, max_width):
    words = text.split()
    lines = []
    current = []
    for word in words:
        test = " ".join(current + [word])
        if _text_width(draw, test, font) <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _truncate(text, max_len):
    return text[:max_len] + "\u2026" if len(text) > max_len else text


def _draw_card(draw, x, y, w, h):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=BG_CARD, outline=BORDER)


def _draw_stat_card(draw, x, y, w, h, value, label, value_font, label_font):
    _draw_card(draw, x, y, w, h)
    cx = x + w // 2
    vb = value_font.getbbox(value)
    vw = vb[2] - vb[0]
    vh = vb[3] - vb[1]
    draw.text((cx - vw // 2, y + 12), value, fill=ACCENT, font=value_font)
    lb = label_font.getbbox(label)
    lw = lb[2] - lb[0]
    draw.text((cx - lw // 2, y + 12 + vh + 6), label, fill=FG_DIM, font=label_font)


def render_clue(clue, solution_use_count=0):
    W, H = 800, 400
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    title = f"Clue #{clue.legacy_number or clue.id}"
    draw.text((24, 20), title, fill=FG, font=_font(28, bold=True))

    if clue.clue_date:
        ds = clue.clue_date.strftime("%Y-%m-%d") if isinstance(clue.clue_date, date) else str(clue.clue_date)
        dw = _text_width(draw, ds, _font(14))
        draw.text((W - 24 - dw, 26), ds, fill=FG_DIM, font=_font(14))

    draw.line([(24, 64), (W - 24, 64)], fill=BORDER, width=1)

    y = 80
    cf = _font(18)
    for line in _wrap_text(clue.clue_text or "(no clue text)", cf, draw, W - 48)[:6]:
        draw.text((24, y), line, fill=FG, font=cf)
        y += 26

    y += 8
    mf = _font(13)
    tags = []
    if clue.clue_length:
        tags.append(f"{clue.clue_length} chars")
    if clue.clues_by_author_so_far:
        tags.append(f"#{clue.clues_by_author_so_far} by author")
    if clue.clues_by_solver_so_far:
        tags.append(f"#{clue.clues_by_solver_so_far} by solver")
    tx = 24
    for tag in tags:
        tw = _text_width(draw, tag, mf)
        draw.rounded_rectangle([tx, y, tx + tw + 16, y + 22], radius=11, fill=BG_CARD, outline=BORDER)
        draw.text((tx + 8, y + 3), tag, fill=FG_DIM, font=mf)
        tx += tw + 24

    y += 40
    cw = (W - 48 - 32) // 3
    lf = _font(12)
    vf = _font(16, bold=True)

    _draw_card(draw, 24, y, cw, 80)
    draw.text((36, y + 10), "AUTHOR", fill=FG_DIM, font=lf)
    draw.text((36, y + 30), _truncate(clue.author or "\u2014", 20), fill=ACCENT, font=vf)

    sx = 24 + cw + 16
    _draw_card(draw, sx, y, cw, 80)
    draw.text((sx + 12, y + 10), "SOLVER", fill=FG_DIM, font=lf)
    draw.text((sx + 12, y + 30), _truncate(clue.solver or "\u2014", 20), fill=GREEN if clue.solver else FG_DIM, font=vf)

    sx2 = sx + cw + 16
    _draw_card(draw, sx2, y, cw, 80)
    draw.text((sx2 + 12, y + 10), "SOLUTION", fill=FG_DIM, font=lf)
    if clue.solution:
        draw.text((sx2 + 12, y + 30), _truncate(clue.solution, 20), fill=YELLOW, font=vf)
        if clue.answer_length:
            draw.text((sx2 + 12, y + 56), f"{clue.answer_length} letters", fill=FG_DIM, font=_font(12))
    else:
        draw.text((sx2 + 12, y + 30), "Not yet solved", fill=FG_DIM, font=_font(14))

    draw.text((24, H - 28), "cccc.poggers.website", fill=FG_DIM, font=_font(12))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_user_profile(username, authored, solved, rank, max_streak, current_streak, max_day_streak, first_date, last_date):
    W, H = 800, 400
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    draw.text((24, 20), username, fill=FG, font=_font(28, bold=True))

    if first_date:
        tenure = f"Active {first_date} \u2192 {last_date}"
        tw = _text_width(draw, tenure, _font(14))
        draw.text((W - 24 - tw, 26), tenure, fill=FG_DIM, font=_font(14))

    draw.line([(24, 64), (W - 24, 64)], fill=BORDER, width=1)

    y = 84
    cw = (W - 48 - 32) // 3
    ch = 80

    _draw_stat_card(draw, 24, y, cw, ch, f"{authored:,}", "Clues Authored", _font(28, bold=True), _font(13))
    _draw_stat_card(draw, 24 + cw + 16, y, cw, ch, f"#{rank}" if rank else "\u2014", "Leaderboard Rank", _font(28, bold=True), _font(13))
    _draw_stat_card(draw, 24 + (cw + 16) * 2, y, cw, ch, f"{solved:,}", "Clues Solved", _font(28, bold=True), _font(13))

    y2 = y + ch + 16
    _draw_stat_card(draw, 24, y2, cw, ch, str(max_streak), "Biggest Streak", _font(28, bold=True), _font(13))
    _draw_stat_card(draw, 24 + cw + 16, y2, cw, ch, str(current_streak), "Current Streak", _font(28, bold=True), _font(13))
    _draw_stat_card(draw, 24 + (cw + 16) * 2, y2, cw, ch, str(max_day_streak), "Biggest Day Streak", _font(28, bold=True), _font(13))

    draw.text((24, H - 28), "cccc.poggers.website", fill=FG_DIM, font=_font(12))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_sequence(seq, clues):
    W, H = 800, 400
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    name = seq.name or "Unnamed sequence"
    draw.text((24, 20), _truncate(name, 40), fill=FG, font=_font(24, bold=True))

    type_label = "Author" if seq.seq_type == "author" else ("Tag" if seq.seq_type == "tag" else "Theme")
    bf = _font(12, bold=True)
    bw = _text_width(draw, type_label, bf)
    bx = 24 + _text_width(draw, _truncate(name, 40), _font(24, bold=True)) + 12
    bc = ACCENT if seq.seq_type == "author" else (ORANGE if seq.seq_type == "tag" else GREEN)
    draw.rounded_rectangle([bx, 26, bx + bw + 16, 48], radius=11, fill=BG_CARD, outline=bc)
    draw.text((bx + 8, 29), type_label, fill=bc, font=bf)

    y = 56
    parts = [f"{len(clues)} clue{'s' if len(clues) != 1 else ''}"]
    if seq.author:
        parts.append(f"by {seq.author}")
    draw.text((24, y), " \u00b7 ".join(parts), fill=FG_DIM, font=_font(14))

    draw.line([(24, 82), (W - 24, 82)], fill=BORDER, width=1)

    y = 96
    hf = _font(12)
    rf = _font(14)
    sf = _font(14, mono=True)

    draw.text((24, y), "#", fill=FG_DIM, font=hf)
    draw.text((80, y), "Clue", fill=FG_DIM, font=hf)
    draw.text((560, y), "Author", fill=FG_DIM, font=hf)
    draw.text((680, y), "Solution", fill=FG_DIM, font=hf)
    y += 20

    for i, c in enumerate(clues[:8]):
        if i % 2 == 0:
            draw.rectangle([20, y, W - 20, y + 24], fill=BG_SOFT)
        draw.text((24, y + 2), str(c.legacy_number or "\u2014"), fill=FG_DIM, font=rf)
        draw.text((80, y + 2), _truncate(c.clue_text or "", 55), fill=FG, font=rf)
        draw.text((560, y + 2), _truncate(c.author or "\u2014", 14), fill=ACCENT, font=rf)
        draw.text((680, y + 2), _truncate(c.solution or "(not solved)", 14), fill=YELLOW if c.solution else FG_DIM, font=sf)
        y += 24

    if len(clues) > 8:
        draw.text((24, y + 4), f"\u2026 and {len(clues) - 8} more", fill=FG_DIM, font=_font(13))

    draw.text((24, H - 28), "cccc.poggers.website", fill=FG_DIM, font=_font(12))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
