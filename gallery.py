#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 styles/ 下所有风格渲染同一份内容，拼成一张对比总览图。

用途：README 里放一张图，让人一眼看到「同一份内容能有多少种完全不同的样子」。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_card  # noqa: E402

HERE = Path(__file__).resolve().parent
STYLES = sorted((HERE / "styles").glob("*.json"))
PAYLOAD = json.loads((HERE / "payloads" / "pinky.json").read_text(encoding="utf-8"))
OUT = HERE / "docs" / "styles-gallery.png"

CELL_W, CELL_H, LABEL_H = 236, 315, 34
COLS = 8
PAD = 28
TITLE_H = 96
BG = (18, 20, 26)
TEXT = (232, 238, 247)
DIM = (130, 140, 158)


def main() -> None:
    if not STYLES:
        raise SystemExit("styles/ 里没有风格")
    rows = (len(STYLES) + COLS - 1) // COLS
    sheet_w = PAD * 2 + COLS * CELL_W + (COLS - 1) * 8
    sheet_h = TITLE_H + PAD + rows * (CELL_H + LABEL_H + 10)
    sheet = Image.new("RGB", (sheet_w, sheet_h), BG)
    sd = ImageDraw.Draw(sheet)

    f_title = gh_card.load_font("cn_bold", 40)
    f_sub = gh_card.load_font("cn", 22)
    f_label = gh_card.load_font("cn", 18)

    sd.text((PAD, 26), "gh-zh-card · 同一份内容，%d 种风格" % len(STYLES),
            font=f_title, fill=TEXT)
    sd.text((PAD, 74), "python gh_card.py --style <id>   /   styles/*.json",
            font=f_sub, fill=DIM)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for i, sp in enumerate(STYLES):
            style = gh_card.load_style(sp.stem)
            png = tmp / f"{sp.stem}.png"
            gh_card.render(PAYLOAD, png, style)
            thumb = Image.open(png).convert("RGB").resize((CELL_W, CELL_H), Image.LANCZOS)

            col, row = i % COLS, i // COLS
            x = PAD + col * (CELL_W + 8)
            y = TITLE_H + PAD + row * (CELL_H + LABEL_H + 10)
            sheet.paste(thumb, (x, y))
            sd.rectangle([x, y, x + CELL_W - 1, y + CELL_H - 1], outline=(52, 58, 70))

            name = style.get("name_zh") or sp.stem
            cap = name if len(name) <= 9 else name[:8] + "…"
            tw = sd.textlength(cap, font=f_label)
            sd.text((x + (CELL_W - tw) / 2, y + CELL_H + 7), cap, font=f_label, fill=DIM)
            print(f"  [{i + 1}/{len(STYLES)}] {sp.stem}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(OUT, "PNG", optimize=True)
    jpg = OUT.with_suffix(".jpg")
    sheet.convert("RGB").save(jpg, "JPEG", quality=88, optimize=True)
    print()
    print("总览图 PNG:", OUT, "%.0f KB" % (OUT.stat().st_size / 1024))
    print("总览图 JPG:", jpg, "%.0f KB" % (jpg.stat().st_size / 1024), "← README 用这个")
    print("尺寸: %dx%d" % (sheet_w, sheet_h))


if __name__ == "__main__":
    main()