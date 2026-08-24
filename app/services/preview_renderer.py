"""Server-side preview image renderer for CCCC embeddable pages.

Generates compact PNG preview images for clue, user, and sequence pages so
they can be embedded in SE chat. SE chat displays images at ~300x150, so we
render at 600x300 (2:1 aspect ratio) and pack text densely to maximize
legibility at that small display size.

Uses the site's Tokyo Night-inspired dark theme (matching style.css).
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
ORANGE = (255, 158, 100)   # #ff9e64
BORDER = (42, 46, 63)      # #2a2e3f

# ── Fonts ────────────────────────────────────────────────────
_FONT_SANS = "DejaVuSans.ttf"
_FONT_SANS_BOLD = "DejaVuSans-Bold.ttf"
_FONT_MONO = "DejaVuSansMono.ttf"

# ── Canvas dimensions ────────────────────────────────────────
# SE chat displays at ~300x150; we render at 2x for sharpness.
W, H = 600, 300
PAD = 16  # outer padding


def _font(size: int, bold=False, mono=False) -> ImageFont.FreeTypeFont:
    if mono:
        return ImageFont.truetype(_FONT_MONO, size)
    return ImageFont.truetype(_FONT_SANS_BOLD if bold else _FONT_SANS, size)


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return draw.textbbox((0, 0), text, font=font)[2]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """Word-wrap text to fit max_width. Returns list of lines."""
    words = text.split()
    lines, current = [], []
    for word in words:
        test = " ".join(current + [word])
        if _tw(draw, test, font) <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _trunc(text: str, draw: ImageDraw.ImageDraw, font, max_width: int) -> str:
    """Truncate text with ellipsis to fit max_width."""
    if _tw(draw, text, font) <= max_width:
        return text
    ellipsis = "\u2026"
    while text and _tw(draw, text + ellipsis, font) > max_width:
        text = text[:-1]
    return text + ellipsis if text else ""


def _card(draw, x, y, w, h, fill=BG_CARD, outline=BORDER):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=6, fill=fill, outline=outline)


def _pill(draw, x, y, text, font, fill=BG_CARD, outline=BORDER, text_color=FG_DIM):
    """Draw a rounded pill badge. Returns the width consumed."""
    tw = _tw(draw, text, font)
    pw = tw + 12
    draw.rounded_rectangle([x, y, x + pw, y + font.size + 6], radius=(font.size + 6) // 2,
                           fill=fill, outline=outline)
    draw.text((x + 6, y + 1), text, fill=text_color, font=font)
    return pw


def _footer(draw):
    """Draw the site name in the bottom-right corner."""
    fb = _font(10)
    tw = _tw(draw, "cccc.poggers.website", fb)
    draw.text((W - PAD - tw, H - PAD - 12), "cccc.poggers.website", fill=FG_DIM, font=fb)


# ── Clue preview ─────────────────────────────────────────────

def render_clue(clue, solution_use_count=0) -> bytes:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # ── Header: small "Clue #N" left, date right ──
    title = f"Clue #{clue.legacy_number or clue.id}"
    draw.text((PAD, PAD), title, fill=FG_DIM, font=_font(13, bold=True))

    if clue.clue_date:
        ds = clue.clue_date.strftime("%Y-%m-%d") if isinstance(clue.clue_date, date) else str(clue.clue_date)
        dw = _tw(draw, ds, _font(11))
        draw.text((W - PAD - dw, PAD + 2), ds, fill=FG_DIM, font=_font(11))

    draw.line([(PAD, PAD + 22), (W - PAD, PAD + 22)], fill=BORDER, width=1)

    # ── Clue text — the main content, takes ~half the image ──
    y = PAD + 30
    cf = _font(18)
    lines = _wrap(draw, clue.clue_text or "(no clue text)", cf, W - 2 * PAD)
    max_clue_lines = 5
    for line in lines[:max_clue_lines]:
        draw.text((PAD, y), line, fill=FG, font=cf)
        y += 24
    if len(lines) > max_clue_lines:
        draw.text((PAD, y), "\u2026", fill=FG_DIM, font=cf)
        y += 24

    # ── Solution — monospaced green, fills space between clue text and footer ──
    y += 8
    if clue.solution:
        sol_font = _font(20, bold=True, mono=True)
        sol_text = clue.solution
        if _tw(draw, sol_text, sol_font) <= W - 2 * PAD:
            sw = _tw(draw, sol_text, sol_font)
            draw.text(((W - sw) // 2, y), sol_text, fill=GREEN, font=sol_font)
        else:
            sol_font = _font(16, bold=True, mono=True)
            sol_lines = _wrap(draw, sol_text, sol_font, W - 2 * PAD)[:2]
            for sl in sol_lines:
                sw = _tw(draw, sl, sol_font)
                draw.text(((W - sw) // 2, y), sl, fill=GREEN, font=sol_font)
                y += 22
        if clue.answer_length:
            y += 4
            tag = f"{clue.answer_length} letters"
            tf = _font(10)
            tw = _tw(draw, tag, tf)
            draw.text(((W - tw) // 2, y), tag, fill=FG_DIM, font=tf)
    else:
        draw.text((PAD, y), "Not yet solved", fill=FG_DIM, font=_font(14))

    # ── Meta pills (compact, above the footer) ──
    pill_y = H - PAD - 52
    mf = _font(9)
    tags = []
    if clue.clue_length:
        tags.append(f"{clue.clue_length} chars")
    if clue.clues_by_author_so_far:
        tags.append(f"#{clue.clues_by_author_so_far} by author")
    if clue.clues_by_solver_so_far:
        tags.append(f"#{clue.clues_by_solver_so_far} by solver")
    if solution_use_count and solution_use_count > 1:
        tags.append(f"solution {solution_use_count}\u00d7")
    tx = PAD
    for tag in tags:
        pw = _pill(draw, tx, pill_y, tag, mf)
        tx += pw + 6

    # ── Footer: author and solver, both blue, slightly larger ──
    fy = H - PAD - 24
    pf = _font(15, bold=True)
    lf = _font(9)

    # Author (left)
    draw.text((PAD, fy), "AUTHOR", fill=FG_DIM, font=lf)
    draw.text((PAD, fy + 12), _trunc(clue.author or "\u2014", draw, pf, 240), fill=ACCENT, font=pf)

    # Solver (right)
    solver_text = clue.solver or "\u2014"
    solver_disp = _trunc(solver_text, draw, pf, 240)
    sw = _tw(draw, solver_disp, pf)
    draw.text((W - PAD - sw, fy), "SOLVER", fill=FG_DIM, font=lf)
    draw.text((W - PAD - sw, fy + 12), solver_disp, fill=ACCENT, font=pf)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── User profile preview ──────────────────────────────────────

def render_user_profile(username, authored, solved, rank,
                        max_streak, current_streak, max_day_streak,
                        first_date, last_date) -> bytes:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # ── Header: username + tenure ──
    draw.text((PAD, PAD), _trunc(username, draw, _font(22, bold=True), W - 2 * PAD), fill=FG, font=_font(22, bold=True))

    if first_date:
        tenure = f"Active {first_date} \u2192 {last_date}"
        tw = _tw(draw, tenure, _font(11))
        draw.text((W - PAD - tw, PAD + 6), tenure, fill=FG_DIM, font=_font(11))

    draw.line([(PAD, PAD + 30), (W - PAD, PAD + 30)], fill=BORDER, width=1)

    # ── 2 rows of 3 stat cards ──
    card_w = (W - 2 * PAD - 16) // 3
    card_h = 56
    y1 = PAD + 38
    y2 = y1 + card_h + 8

    def _stat(x, y, value, label):
        _card(draw, x, y, card_w, card_h)
        vf = _font(20, bold=True)
        lf = _font(9)
        draw.text((x + 8, y + 6), label, fill=FG_DIM, font=lf)
        draw.text((x + 8, y + 22), _trunc(value, draw, vf, card_w - 16), fill=ACCENT, font=vf)

    _stat(PAD, y1, f"{authored:,}", "AUTHORED")
    _stat(PAD + card_w + 8, y1, f"#{rank}" if rank else "\u2014", "RANK")
    _stat(PAD + (card_w + 8) * 2, y1, f"{solved:,}", "SOLVED")

    _stat(PAD, y2, str(max_streak), "MAX STREAK")
    _stat(PAD + card_w + 8, y2, str(current_streak), "CURRENT STREAK")
    _stat(PAD + (card_w + 8) * 2, y2, str(max_day_streak), "MAX DAY STREAK")

    _footer(draw)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Sequence preview ──────────────────────────────────────────

def render_sequence(seq, clues) -> bytes:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # ── Header: name + type badge ──
    name = seq.name or "Unnamed sequence"
    nf = _font(18, bold=True)
    name_disp = _trunc(name, draw, nf, W - 2 * PAD - 80)
    draw.text((PAD, PAD), name_disp, fill=FG, font=nf)

    # Type badge
    type_label = "Author" if seq.seq_type == "author" else ("Tag" if seq.seq_type == "tag" else "Theme")
    bc = ACCENT if seq.seq_type == "author" else (ORANGE if seq.seq_type == "tag" else GREEN)
    bf = _font(9, bold=True)
    bx = PAD + _tw(draw, name_disp, nf) + 8
    _pill(draw, bx, PAD + 4, type_label, bf, fill=BG_CARD, outline=bc, text_color=bc)

    # Meta line
    y = PAD + 26
    parts = [f"{len(clues)} clue{'s' if len(clues) != 1 else ''}"]
    if seq.author:
        parts.append(f"by {seq.author}")
    draw.text((PAD, y), " \u00b7 ".join(parts), fill=FG_DIM, font=_font(11))

    draw.line([(PAD, y + 16), (W - PAD, y + 16)], fill=BORDER, width=1)

    # ── Member clue table ──
    y = y + 22
    hf = _font(9)
    rf = _font(11)
    sf = _font(11, mono=True)

    # Column positions
    c_num = PAD
    c_clue = PAD + 36
    c_author = W - PAD - 140
    c_solution = W - PAD - 60
    clue_w = c_author - c_clue - 8

    # Header row
    draw.text((c_num, y), "#", fill=FG_DIM, font=hf)
    draw.text((c_clue, y), "Clue", fill=FG_DIM, font=hf)
    draw.text((c_author, y), "Author", fill=FG_DIM, font=hf)
    draw.text((c_solution, y), "Solution", fill=FG_DIM, font=hf)
    y += 14

    row_h = 18
    max_rows = (H - PAD - y - 20) // row_h
    for i, c in enumerate(clues[:max_rows]):
        ry = y + i * row_h
        if i % 2 == 0:
            draw.rectangle([PAD - 2, ry, W - PAD + 2, ry + row_h - 1], fill=BG_SOFT)
        draw.text((c_num, ry + 1), str(c.legacy_number or "\u2014"), fill=FG_DIM, font=rf)
        draw.text((c_clue, ry + 1), _trunc(c.clue_text or "", draw, rf, clue_w), fill=FG, font=rf)
        draw.text((c_author, ry + 1), _trunc(c.author or "\u2014", draw, rf, 70), fill=ACCENT, font=rf)
        sol_text = c.solution or "(unsolved)"
        draw.text((c_solution, ry + 1), _trunc(sol_text, draw, sf, 56), fill=YELLOW if c.solution else FG_DIM, font=sf)

    if len(clues) > max_rows:
        draw.text((PAD, y + max_rows * row_h + 2), f"\u2026 and {len(clues) - max_rows} more",
                  fill=FG_DIM, font=_font(10))

    _footer(draw)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
