#!/usr/bin/env python3
"""Render all public overlay states without a controller, capture device or RTMP."""

import argparse
from io import BytesIO
from pathlib import Path

from overlay_model import HEADLINES, OverlayState
from PIL import Image, ImageDraw
from renderer import font, render_overlay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sheet = Image.new("RGB", (1040, 3 * 342), (238, 240, 244))
    examples = [
        ("waiting", None),
        ("switching", "Chicago Blackhawks at Detroit Red Wings"),
        ("recovering", "Chicago Blackhawks at Detroit Red Wings"),
        ("standby", None),
        ("long-title", "An unusually long championship event title with two teams and extra coverage details"),
        ("hidden", None),
    ]
    for index, (example, title) in enumerate(examples):
        state = "switching" if example == "long-title" else example
        data = OverlayState("Dillflix 2" if index % 2 else "Dillflix 1", state, HEADLINES.get(state, ""), title, state != "hidden")
        frame = Image.new("RGB", (1920, 1080), (38, 44, 52))
        draw = ImageDraw.Draw(frame)
        for x in range(0, 1920, 240):
            draw.line((x, 0, x, 1079), fill=(47, 53, 61))
        for y in range(0, 1080, 180):
            draw.line((0, y, 1919, y), fill=(47, 53, 61))
        card = render_overlay(data)
        if card:
            overlay = Image.open(BytesIO(card))
            frame.paste(overlay, (48, 1080 - 42 - overlay.height), overlay)
        frame.save(args.output / f"{example}-1080p.png")
        frame.resize((320, 240), Image.Resampling.LANCZOS).save(args.output / f"{example}-240p.png")
        x, y = (index % 2) * 520 + 10, (index // 2) * 342 + 10
        sheet.paste(frame.resize((500, 281), Image.Resampling.LANCZOS), (x, y))
        ImageDraw.Draw(sheet).text((x, y + 291), example.replace("-", " ").title(), font=font(18), fill=(23, 28, 36))
    sheet.save(args.output / "all-states.png")
    print(f"Preview saved to {args.output.resolve()}")


if __name__ == "__main__":
    main()
