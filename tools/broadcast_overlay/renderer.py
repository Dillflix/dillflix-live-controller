"""Render public copy into a small transparent card, only when its state changes."""

import os
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from overlay_model import HEADLINES, clean_text
from PIL import Image, ImageDraw, ImageFont


@lru_cache(maxsize=16)
def font(size):
    candidates = [
        os.environ.get("DILLFLIX_OVERLAY_FONT", ""),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/seguisb.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    raise RuntimeError("Install fonts-dejavu-core or set DILLFLIX_OVERLAY_FONT")


def ellipsize(text, face, width):
    if face.getlength(text) <= width:
        return text
    while text and face.getlength(text.rstrip() + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap_title(text, face, width):
    words = text.split()
    lines = []
    while words and len(lines) < 2:
        line = words.pop(0)
        while words and face.getlength(line + " " + words[0]) <= width:
            line += " " + words.pop(0)
        if len(lines) == 1 and words:
            line += " " + " ".join(words)
            words.clear()
        lines.append(ellipsize(line, face, width))
    return lines


def render_overlay(state, *, width=780, height=144):
    if not state.visible or state.state not in HEADLINES:
        return None
    if not 16 <= width <= 16384 or not 16 <= height <= 16384:
        raise ValueError("Invalid overlay dimensions")
    # Render a fixed HD card and scale the resulting raster exactly as the
    # capture pipeline does. No extra per-quality compositing passes.
    canvas = Image.new("RGBA", (780, 144))
    draw = ImageDraw.Draw(canvas)
    title = clean_text(state.event_title or "", 300)
    headline = HEADLINES[state.state]
    name = clean_text(state.name, 48)
    header = name.upper() + (" · " + headline.upper() if title else "")
    body = title or headline
    header_font, title_font = font(22), font(32)
    lines = wrap_title(body, title_font, 724)
    panel_height = 144 if len(lines) == 2 else 112
    top = 144 - panel_height
    draw.rounded_rectangle((0, top, 779, 143), radius=7, fill=(13, 17, 24, 209), outline=(220, 228, 239, 25))
    draw.line((2, top + 8, 2, 135), fill=(230, 29, 69, 255), width=4)
    draw.text((26, top + 17), ellipsize(header, header_font, 724), font=header_font, fill=(188, 203, 221, 255), anchor="lt")
    for index, line in enumerate(lines):
        draw.text((26, top + 54 + index * 39), line, font=title_font, fill=(255, 255, 255, 255), anchor="lt")
    if (width, height) != canvas.size:
        canvas = canvas.resize((width, height), Image.Resampling.LANCZOS)
    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()
