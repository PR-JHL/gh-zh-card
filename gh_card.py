#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gh-card —— 把任何 GitHub 仓库变成一张中文速览卡（1080x1440，可直接发小红书/公众号/X）
MIT License.

设计原则：
  1. 数字必须是真的 —— 统计信息只从 GitHub API 拿，不允许手填假数据。
  2. 中文文案由 agent（LLM）写，脚本只负责渲染 —— 所以它是"agent skill"而不是"一个 API 封装"。
  3. 零额外依赖 —— 只用 requests + Pillow（大多数 Python 环境自带）。

两种用法：
  A) agent 模式（推荐）：agent 写一份 payload.json，脚本只渲染
       python gh_card.py --payload payload.json
  B) 手动模式：自动抓数据，不带中文摘要，仅出"数据卡"
       python gh_card.py --fetch owner/repo --out examples/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("需要 Pillow：pip install pillow")

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# ---------------------------------------------------------------- 主题
W, H = 1080, 1440
PAD = 76

# ---------------------------------------------------------------- 默认风格
# 真正的风格在 styles/<id>.json 里；这里只是兜底，保证没有 style 文件也能出图。
DEFAULT_STYLE = {
    "id": "wandering-earth",
    "name": "Wandering Earth",
    "palette": {
        "bg_top": "#05090F", "bg_bottom": "#0F2230",
        "surface": "#132636", "surface_alt": "#1A3546",
        "text": "#E9F5FF", "text_dim": "#82A6BE", "text_faint": "#6B8EA6",
        "accent": "#4FC3F7", "accent_2": "#FF5A4E", "accent_3": "#FFB020",
        "good": "#5BE0C4", "line": "#22455C",
    },
    "decor": {
        "grid": 0.11, "scanlines": 0.04, "corner_marks": True,
        "frame": True, "top_stripe": "accent_2", "glow": 0.20, "vignette": 0.26,
    },
    "type": {"title_scale": 1.0, "lead": 1.34, "mono_install": True},
}

STYLE_DIRS = [
    Path(__file__).resolve().parent / "styles",
    Path(__file__).resolve().parent.parent / "design-cards" / "styles",
]

FONT_FILES = {
    "cn_bold": [
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ],
    "cn": [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ],
    "mono": [
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/CascadiaMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ],
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def load_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key in _font_cache:
        return _font_cache[key]
    for path in FONT_FILES[kind]:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size, index=0)
                _font_cache[key] = font
                return font
            except OSError:
                continue
    try:
        fallback = ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1 不接受 size
        fallback = ImageFont.load_default()
    _font_cache[key] = fallback
    return fallback


# ---------------------------------------------------------------- 文本工具
def text_w(draw: ImageDraw.ImageDraw, s: str, font: ImageFont.FreeTypeFont) -> int:
    return int(draw.textlength(s, font=font))


NO_LINE_START = set("、。，．）］】」』〉》！？：；%”’·…～/")
NO_LINE_END = set("（［【「『〈《“‘")


def _tokens(s: str) -> list[str]:
    """把字符串切成「不可再分」的排版单元：中文单字，英文/数字整词，标点单个。"""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch.isascii() and (ch.isalnum() or ch in "-_.+#@'"):
            j = i
            while j < n and s[j].isascii() and (s[j].isalnum() or s[j] in "-_.+#@'"):
                j += 1
            out.append(s[i:j])
            i = j
        else:
            out.append(ch)
            i += 1
    return out


def wrap_cjk(draw, s: str, font, max_w: int) -> list[str]:
    """按「英文整词 + 中文逐字 + 标点禁则」换行，避免把 Android 拆成 A/ndroid。"""
    lines: list[str] = []
    for para in s.split("\n"):
        cur = ""
        for tok in _tokens(para):
            if text_w(draw, cur + tok, font) <= max_w or not cur:
                cur += tok
                continue
            # 标点不出现在行首：宁可行尾稍微超一点
            if tok and tok[0] in NO_LINE_START:
                cur += tok
                continue
            # 行尾是开括号：把上一字一起带下去
            carry = ""
            if cur and cur[-1] in NO_LINE_END:
                carry, cur = cur[-1], cur[:-1]
            lines.append(cur)
            cur = carry + tok
        lines.append(cur)
    return [ln for ln in lines] or [""]


def fit_single(draw, s: str, kind: str, max_w: int, start: int, min_size: int = 18):
    """单行文本自动缩字号直到塞得下。"""
    size = start
    while size > min_size and text_w(draw, s, load_font(kind, size)) > max_w:
        size -= 1
    return load_font(kind, size), size


def fit_block(draw, s: str, kind: str, max_w: int, max_lines: int,
              start: int, min_size: int, lead: float = 1.32):
    """从大字号往小缩，直到塞进 max_lines 行。"""
    size = start
    while size >= min_size:
        font = load_font(kind, size)
        lines = wrap_cjk(draw, s, font, max_w)
        if len(lines) <= max_lines:
            return font, lines, int(size * lead)
        size -= 2
    font = load_font(kind, min_size)
    lines = wrap_cjk(draw, s, font, max_w)[:max_lines]
    return font, lines, int(min_size * lead)


def has_cjk(s: str) -> bool:
    """mono 等西文字体没有中文字形，混排会出豆腐块，必须先判一下。"""
    return any("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f"
               or "\uff00" <= ch <= "\uffef" for ch in s)


def fmt_num(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 10000:
        return f"{n / 1000:.1f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_age(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "1 天前"
    if days < 30:
        return f"{days} 天前"
    if days < 365:
        return f"{days // 30} 个月前"
    return f"{days // 365} 年前"


# ---------------------------------------------------------------- 抓数据
def fetch_repo(slug: str) -> dict:
    if requests is None:
        raise SystemExit("抓取数据需要 requests：pip install requests")
    if "/" not in slug:
        raise SystemExit(f"仓库格式应为 owner/repo，收到：{slug}")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "gh-card"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_err: Exception | None = None
    r = None
    for attempt in range(3):  # 代理偶尔会被重置，重试两次基本都能过
        try:
            r = requests.get(f"https://api.github.com/repos/{slug}",
                             headers=headers, timeout=25)
            break
        except Exception as e:  # noqa: BLE001 - 网络层什么都可能抛
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    if r is None:
        raise SystemExit(
            f"连不上 GitHub API（{type(last_err).__name__}）。\n"
            f"如果你在公司代理后面，设一个环境变量再试：HTTPS_PROXY=http://...\n"
            f"或者先用 --offline 模式，把统计数字写进 payload（须来自 --fetch 或网页核实）。"
        )
    if r.status_code == 404:
        raise SystemExit(f"仓库不存在或无权限：{slug}")
    if r.status_code == 403:
        raise SystemExit("GitHub API 限流了。设置 GITHUB_TOKEN 环境变量可提高到 5000 次/小时。")
    r.raise_for_status()
    d = r.json()
    lic = (d.get("license") or {}).get("spdx_id")
    if not lic or lic == "NOASSERTION":
        lic = "未标注"
    return {
        "owner": d["owner"]["login"],
        "repo": d["name"],
        "stars": d.get("stargazers_count", 0),
        "forks": d.get("forks_count", 0),
        "language": d.get("language") or "—",
        "license": lic,
        "updated": d.get("pushed_at"),
        "topics": (d.get("topics") or [])[:4],
        "description": d.get("description") or "",
        "url": d.get("html_url"),
    }


# ---------------------------------------------------------------- 渲染
def vertical_gradient(w: int, h: int, top, bot) -> Image.Image:
    img = Image.new("RGB", (w, h), top)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        col = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = col
    return img


def pill(draw, x: int, y: int, text: str, font, fg, bg, padx=18, pady=9) -> int:
    w = text_w(draw, text, font) + padx * 2
    h = font.size + pady * 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=bg)
    draw.text((x + padx, y + pady - 2), text, font=font, fill=fg)
    return x + w


def _rel_luminance(c) -> float:
    def ch(v: float) -> float:
        v = v / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b) -> float:
    la, lb = _rel_luminance(a), _rel_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def rgb(value) -> tuple:
    """'#RRGGBB' / '#RGB' / (r,g,b) -> (r,g,b)"""
    if isinstance(value, (tuple, list)):
        return tuple(int(v) for v in tuple(value)[:3])
    h = str(value).strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"颜色格式不对：{value!r}（要 #RRGGBB）")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def load_style(name: str | None, extra_dirs: list | None = None) -> dict:
    """--style 既可以是风格 id（styles/<id>.json），也可以直接是 JSON 路径。"""
    if not name:
        return json.loads(json.dumps(DEFAULT_STYLE))
    cand = Path(name)
    style = None
    if cand.suffix.lower() == ".json" and cand.exists():
        style = json.loads(cand.read_text(encoding="utf-8"))
    else:
        for base in list(extra_dirs or []) + STYLE_DIRS:
            f = Path(base) / f"{name}.json"
            if f.exists():
                style = json.loads(f.read_text(encoding="utf-8"))
                break
    if style is None:
        found: list[str] = []
        for base in list(extra_dirs or []) + STYLE_DIRS:
            if Path(base).is_dir():
                found += sorted(p.stem for p in Path(base).glob("*.json"))
        uniq = list(dict.fromkeys(found))
        raise SystemExit(f"找不到风格 {name!r}。可用：{', '.join(uniq) or '（无）'}")

    merged = json.loads(json.dumps(DEFAULT_STYLE))
    for part in ("palette", "decor", "type"):
        merged[part].update(style.get(part) or {})
    for k in ("id", "name", "name_zh", "mood", "motifs", "use_for"):
        if k in style:
            merged[k] = style[k]
    return merged


def build_background(style: dict) -> Image.Image:
    pal, dec = style["palette"], style.get("decor") or {}
    img = vertical_gradient(W, H, rgb(pal["bg_top"]), rgb(pal["bg_bottom"]))

    # 柔光：左上角起一层 accent 色漫射，深色卡不至于「死黑」
    glow = float(dec.get("glow") or 0)
    if glow > 0:
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ac = rgb(pal["accent"])
        cx, cy = W * 0.16, H * 0.02
        for scale, alpha in ((1.05, 0.42), (0.70, 0.55), (0.38, 0.72)):
            rx, ry = W * 0.95 * scale, H * 0.55 * scale
            ld.ellipse([cx - rx, cy - ry, cx + rx, cy + ry],
                       fill=(ac[0], ac[1], ac[2], int(255 * glow * alpha)))
        layer = layer.filter(ImageFilter.GaussianBlur(120))
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    # HUD 网格
    grid = float(dec.get("grid") or 0)
    if grid > 0:
        g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        lc = rgb(pal["line"])
        a = max(6, min(80, int(255 * grid * 1.9)))
        for gx in range(0, W, 54):
            gd.line([(gx, 0), (gx, H)], fill=(lc[0], lc[1], lc[2], a), width=1)
        for gy in range(0, H, 54):
            gd.line([(0, gy), (W, gy)], fill=(lc[0], lc[1], lc[2], a), width=1)
        img = Image.alpha_composite(img.convert("RGBA"), g).convert("RGB")

    # 扫描线
    scan = float(dec.get("scanlines") or 0)
    if scan > 0:
        s = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(s)
        a = max(4, min(46, int(255 * scan * 2.2)))
        for sy in range(0, H, 4):
            sd.line([(0, sy), (W, sy)], fill=(0, 0, 0, a), width=1)
        img = Image.alpha_composite(img.convert("RGBA"), s).convert("RGB")

    # 四角压暗
    vig = float(dec.get("vignette") or 0)
    if vig > 0:
        v = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        vd = ImageDraw.Draw(v)
        a = max(8, min(88, int(255 * vig * 0.85)))
        for cx, cy in ((0, 0), (W, 0), (0, H), (W, H)):
            rx, ry = W * 0.33, H * 0.22
            vd.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=(0, 0, 0, a))
        v = v.filter(ImageFilter.GaussianBlur(150))
        img = Image.alpha_composite(img.convert("RGBA"), v).convert("RGB")

    # 顶部色带
    stripe = dec.get("top_stripe") or "none"
    if stripe != "none" and stripe in pal:
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 5], fill=rgb(pal[stripe]))
        d.rectangle([0, 5, W, 6], fill=rgb(pal["line"]))
    return img


def draw_decor_over(draw, style: dict) -> None:
    """压在内容之上的装饰：外框、四角定位括号。"""
    pal, dec = style["palette"], style.get("decor") or {}
    if dec.get("frame"):
        draw.rectangle([12, 12, W - 13, H - 13], outline=rgb(pal["line"]), width=2)
    if dec.get("corner_marks"):
        ac = rgb(pal["accent"])
        seg, off = 46, 30
        for bx, by, sx, sy in ((off, off, 1, 1), (W - off, off, -1, 1),
                               (off, H - off, 1, -1), (W - off, H - off, -1, -1)):
            draw.line([(bx, by), (bx + sx * seg, by)], fill=ac, width=3)
            draw.line([(bx, by), (bx, by + sy * seg)], fill=ac, width=3)


def render(payload: dict, out_path: Path, style: dict | None = None) -> Path:
    style = style or DEFAULT_STYLE
    pal = style["palette"]
    typ = style.get("type") or {}
    title_scale = float(typ.get("title_scale") or 1.0)
    lead_mul = float(typ.get("lead") or 1.34)

    # 局部遮蔽模块级默认色：渲染体里就不必到处传 theme
    BG_TOP, BG_BOT = rgb(pal["bg_top"]), rgb(pal["bg_bottom"])
    CARD, CARD_HI = rgb(pal["surface"]), rgb(pal["surface_alt"])
    TEXT, DIM, FAINT = rgb(pal["text"]), rgb(pal["text_dim"]), rgb(pal["text_faint"])
    ACCENT, ACCENT2 = rgb(pal["accent"]), rgb(pal["accent_2"])
    GREEN, LINE = rgb(pal["good"]), rgb(pal["line"])

    # accent 用作正文色时可能糊在背景里（浅色主题的荧光黄、粉色尤其明显）。
    # 对比不够就退回正文色，装饰块仍然用原 accent —— 保留风格，但保证读得清。
    ACCENT_TEXT = ACCENT if contrast_ratio(ACCENT, BG_BOT) >= 4.0 else TEXT
    ACCENT2_TEXT = ACCENT2 if contrast_ratio(ACCENT2, CARD_HI) >= 3.0 else TEXT

    img = build_background(style)
    d = ImageDraw.Draw(img)

    x = PAD
    y = 66
    f_pill = load_font("cn", 24)
    x_end = pill(d, x, y, "GitHub 中文速览", f_pill, ACCENT_TEXT, CARD_HI)
    y += 58

    # owner/repo
    f_mono_s = load_font("mono", 26)
    full = f"{payload['owner']}/{payload['repo']}"
    d.text((x, y), full, font=f_mono_s, fill=FAINT)
    y += 52

    # 中文名（大字，最多 2 行）
    name_zh = payload.get("name_zh") or payload["repo"]
    font_name, lines, lead = fit_block(
        d, name_zh, "cn_bold", W - PAD * 2, 2,
        int(78 * title_scale), int(50 * title_scale), lead_mul)
    for ln in lines:
        d.text((x, y), ln, font=font_name, fill=TEXT)
        y += lead
    y += 14

    # 一句话定位
    tagline = payload.get("tagline", "")
    if tagline:
        font_t, lines_t, lead_t = fit_block(
            d, tagline, "cn_bold", W - PAD * 2, 3, 42, 30, lead_mul + 0.04)
        # 左侧强调竖条
        d.rounded_rectangle([x, y + 4, x + 6, y + lead_t * len(lines_t) - 12],
                            radius=3, fill=ACCENT)
        tx = x + 26
        for ln in lines_t:
            d.text((tx, y), ln, font=font_t, fill=ACCENT_TEXT)
            y += lead_t
        y += 34

    # 它能解决什么
    pains = [p for p in (payload.get("pain") or []) if p][:3]
    if pains:
        f_sec = load_font("cn_bold", 26)
        d.text((x, y), "它能解决什么", font=f_sec, fill=FAINT)
        y += 54
        f_b = load_font("cn", 33)
        for p in pains:
            d.ellipse([x + 2, y + 14, x + 12, y + 24], fill=GREEN)
            lines_p = wrap_cjk(d, p, f_b, W - PAD * 2 - 44)
            for ln in lines_p:
                d.text((x + 36, y), ln, font=f_b, fill=TEXT)
                y += 48
            y += 10
        y += 18

    # 适合谁
    for_who = payload.get("for_who", "")
    if for_who:
        f_sec = load_font("cn_bold", 26)
        d.text((x, y), "适合谁", font=f_sec, fill=FAINT)
        y += 50
        f_w = load_font("cn", 31)
        lines_w = wrap_cjk(d, for_who, f_w, W - PAD * 2 - 48)[:3]
        block_h = len(lines_w) * 46 + 32
        d.rounded_rectangle([x, y, W - PAD, y + block_h], radius=18, fill=CARD)
        ty = y + 16
        for ln in lines_w:
            d.text((x + 24, ty), ln, font=f_w, fill=TEXT)
            ty += 46
        y += block_h + 30

    # 怎么开始（可以是命令，也可以是一句人话）
    start = payload.get("start") or payload.get("install", "")
    if start:
        is_cmd = bool(start) and not has_cjk(start)
        f_cmd = load_font("mono" if is_cmd else "cn", 27)
        shown = ("$ " + start) if is_cmd else start
        lines_c = wrap_cjk(d, shown, f_cmd, W - PAD * 2 - 40)[:3]
        block_h = len(lines_c) * 40 + 28
        d.rounded_rectangle([x, y, W - PAD, y + block_h], radius=16, fill=CARD_HI)
        ty = y + 14
        for i, ln in enumerate(lines_c):
            d.text((x + 20, ty), ln, font=f_cmd,
                   fill=(GREEN if (is_cmd and i == 0) else TEXT))
            ty += 40
        y += block_h + 26

    # ---- 结论（可选，底部对齐，填掉下半部的空） ----
    verdict = payload.get("verdict", "")
    foot_line = H - PAD - 96 - 34
    if verdict:
        avail = foot_line - 24 - y
        f_v = load_font("cn", 29)
        for size in range(30, 21, -1):
            f_v = load_font("cn", size)
            lines_v = wrap_cjk(d, verdict, f_v, W - PAD * 2 - 52)
            if len(lines_v) * int(size * 1.45) + 34 <= avail:
                break
        lines_v = lines_v[:5]
        block_h = len(lines_v) * int(f_v.size * 1.45) + 34
        by = foot_line - 24 - block_h
        by = max(by, y)
        d.rounded_rectangle([x, by, W - PAD, by + block_h], radius=18, fill=CARD)
        d.rounded_rectangle([x, by + 10, x + 5, by + block_h - 10], radius=3, fill=ACCENT2)
        ty = by + 17
        for ln in lines_v:
            d.text((x + 26, ty), ln, font=f_v, fill=TEXT)
            ty += int(f_v.size * 1.45)

    # ---- 底部数据条 ----
    foot_y = H - PAD - 96
    d.line([(PAD, foot_line), (W - PAD, foot_line)], fill=LINE, width=2)

    f_lbl = load_font("cn", 22)
    items = [
        ("★ " + fmt_num(payload.get("stars", 0)), "stars"),
        (str(payload.get("language") or "—"), "语言"),
        (str(payload.get("license") or "—"), "许可证"),
        (fmt_age(payload.get("updated")), "更新"),
    ]
    col_w = (W - PAD * 2) // len(items)
    for i, (val, lbl) in enumerate(items):
        cx = PAD + col_w * i
        font_v, size_v = fit_single(d, val, "cn_bold", col_w - 44, 40, 20)
        d.text((cx, foot_y + (40 - size_v)), val, font=font_v, fill=TEXT)
        d.text((cx, foot_y + 54), lbl, font=f_lbl, fill=FAINT)

    # 右上角话题标签
    topics = [t for t in (payload.get("topics") or []) if t][:3]
    if topics:
        f_t = load_font("mono", 20)
        total_w = sum(text_w(d, "#" + t, f_t) + 16 for t in topics) + 16 * (len(topics) - 1)
        tx = W - PAD - total_w
        for t in topics:
            tx = pill(d, tx, 66, "#" + t, f_t, ACCENT2_TEXT, CARD_HI, padx=8, pady=6) + 16

    # 署名：卡片被转发时链接跟着走 —— 这是这个工具唯一的自然传播入口
    if (style.get("decor") or {}).get("credit", True):
        f_cr = load_font("cn", 15)
        credit = "gh-zh-card · github.com/PR-JHL/gh-zh-card"
        cw = text_w(d, credit, f_cr)
        # 把弱化色再往背景压 45%，保证不抢内容
        credit_col = tuple(int(c + (BG_BOT[i] - c) * 0.45) for i, c in enumerate(FAINT))
        d.text(((W - cw) // 2, H - 32), credit, font=f_cr, fill=credit_col)

    draw_decor_over(d, style)
    img.save(out_path, "PNG", optimize=True)
    return out_path


# ---------------------------------------------------------------- 文案
def to_markdown(payload: dict) -> str:
    url = payload.get("url") or f"https://github.com/{payload['owner']}/{payload['repo']}"
    lines = [
        f"# {payload.get('name_zh', payload['repo'])}",
        "",
        f"> {payload.get('tagline', '')}",
        "",
    ]
    pains = payload.get("pain") or []
    if pains:
        lines += ["**它能解决什么**", ""]
        lines += [f"- {p}" for p in pains]
        lines += [""]
    if payload.get("for_who"):
        lines += [f"**适合谁**：{payload['for_who']}", ""]
    start = payload.get("start") or payload.get("install")
    if start:
        if has_cjk(start):
            lines += ["**怎么开始**", "", start, ""]
        else:
            lines += ["**怎么装**", "", "```bash", start, "```", ""]
    lines += [f"**仓库**：{url}", ""]
    lines += [
        f"★ {fmt_num(payload.get('stars', 0))} · {payload.get('language', '—')}"
        f" · {payload.get('license', '—')} · 更新于 {fmt_age(payload.get('updated'))}"
    ]
    return "\n".join(lines)


def to_caption(payload: dict) -> str:
    """给自媒体用的一段文案，可直接粘。"""
    url = payload.get("url") or f"https://github.com/{payload['owner']}/{payload['repo']}"
    pains = payload.get("pain") or []
    body = [
        f"{payload.get('tagline', '')}",
        "",
    ]
    if pains:
        body += [f"· {p}" for p in pains]
        body += [""]
    if payload.get("for_who"):
        body += [f"适合：{payload['for_who']}", ""]
    body += [f"仓库：{url}", ""]
    tags = ["#GitHub", "#开源项目", "#AI工具", "#程序员"]
    for t in (payload.get("topics") or [])[:3]:
        tags.append("#" + str(t).replace("-", "").replace("_", ""))
    body.append(" ".join(tags))
    return "\n".join(body)


# ---------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(description="把 GitHub 仓库渲染成中文速览卡")
    ap.add_argument("--payload", help="payload JSON 文件（agent 写好的完整文案）")
    ap.add_argument("--fetch", help="owner/repo，只抓真实数据，用于生成数据卡")
    ap.add_argument("--out", default="out", help="输出目录")
    ap.add_argument("--name", help="输出文件名（不含扩展名）")
    ap.add_argument("--md", action="store_true", help="同时输出 Markdown")
    ap.add_argument("--caption", action="store_true", help="同时输出自媒体文案")
    ap.add_argument("--print", action="store_true", dest="do_print", help="把 payload 打印出来")
    ap.add_argument("--offline", action="store_true",
                    help="不联网：统计数字须已写在 payload 里（用于离线/代理不通的环境）")
    ap.add_argument("--style", default="obsidian",
                    help="风格 id（styles/<id>.json）或风格 JSON 的路径，默认 obsidian")
    ap.add_argument("--style-dir", action="append", default=[],
                    help="额外风格目录，可重复；用来加载 design-cards 里的风格库")
    ap.add_argument("--no-credit", action="store_true",
                    help="不画底部署名（默认画：卡片被转发时带上项目链接）")
    args = ap.parse_args()

    payload: dict = {}
    if args.payload:
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    if args.fetch and not args.offline:
        payload = {**fetch_repo(args.fetch), **payload}
    if not payload:
        ap.error("需要 --payload 或 --fetch")

    payload.setdefault("owner", "unknown")
    payload.setdefault("repo", "unknown")
    if "url" not in payload:
        payload["url"] = f"https://github.com/{payload['owner']}/{payload['repo']}"

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.name or f"{payload['owner']}__{payload['repo']}".replace("/", "__")
    style = load_style(args.style, [Path(p) for p in args.style_dir])
    if args.no_credit:
        style.setdefault("decor", {})["credit"] = False
    png = render(payload, out_dir / f"{stem}.png", style)
    print(f"风格: {style.get('name_zh') or style.get('id')}")

    if args.do_print:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"卡片: {png}")
    if args.md:
        md = out_dir / f"{stem}.md"
        md.write_text(to_markdown(payload), encoding="utf-8")
        print(f"Markdown: {md}")
    if args.caption:
        cap = out_dir / f"{stem}.caption.txt"
        cap.write_text(to_caption(payload), encoding="utf-8")
        print(f"文案: {cap}")


if __name__ == "__main__":
    main()