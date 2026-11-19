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
       python gh_card.py --payload payload.json --check
  B) 手动模式：自动抓数据，不带中文摘要，仅出"数据卡"
       python gh_card.py --fetch owner/repo --out examples/

渲染是「先排版、再落笔」：内容塞不下时先整体缩字号，再省次要区块，最后才截断并报告，
不会静默压到页脚数据条上。--check 会把版面/文案长度/可读性问题变成断言（见 card_check.py）。
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

# 竖向分区：正文只能待在这条线以上，页脚数据条以下归页脚
FOOT_LINE = H - PAD - 96 - 34       # 页脚分隔线
CONTENT_BOTTOM = FOOT_LINE - 24     # 正文最低边界
VERDICT_MIN_H = 96                  # 结论块最小高度（两行）
VERDICT_BOTTOM_LIMIT = FOOT_LINE - 6  # 结论块底边不许越过的线（页脚线留 6px）
FIT_SCALES = (1.0, 0.94, 0.88, 0.82, 0.76, 0.70)
FIT_SKIPS = ((), ("start",), ("start", "for_who"),
             ("start", "for_who", "pain3"), ("start", "for_who", "pain3", "pain2"),
             ("start", "for_who", "pain3", "pain2", "pain"))

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
    """按「英文整词 + 中文逐字 + 标点禁则」换行，避免把 Android 拆成 A/ndroid。

    单个词比整行还宽时（长 URL、长包名）按字符硬断 —— 否则它会直接冲出卡片右边界。
    """
    lines: list[str] = []
    for para in s.split("\n"):
        cur = ""
        for tok in _tokens(para):
            if text_w(draw, tok, font) > max_w:
                # 这一个词自己就放不下一行：先把当前行收掉，再把这个词逐字硬断
                if cur:
                    lines.append(cur)
                    cur = ""
                rest = tok
                while rest:
                    take = ""
                    for ch in rest:
                        if take and text_w(draw, take + ch, font) > max_w:
                            break
                        take += ch
                    lines.append(take)
                    rest = rest[len(take):]
                continue
            if not cur or text_w(draw, cur + tok, font) <= max_w:
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


def fit_value(draw, s: str, kind: str, max_w: int, start: int = 40,
              min_size: int = 20) -> tuple[str, ImageFont.FreeTypeFont]:
    """页脚数值：先缩字号；缩到底还放不下就截断加省略号。

    宁可字短一点，也不能让「Mozilla Public License 2.0」压到隔壁的「今天」上。
    """
    font = load_font(kind, start)
    size = start
    while size > min_size and text_w(draw, s, font) > max_w:
        size -= 1
        font = load_font(kind, size)
    if text_w(draw, s, font) <= max_w:
        return s, font
    font = load_font(kind, min_size)
    if text_w(draw, s, font) <= max_w:
        return s, font
    cut = s
    while cut and text_w(draw, cut + "…", font) > max_w:
        cut = cut[:-1]
    return ((cut.rstrip() + "…") if cut else "…"), font


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
    """1234 -> 1.2k，123456 -> 123k，1234567 -> 1.2M。

    以前只做 k，六位数会被写成 1234.6k（数位读起来是错的）。
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 100_000:
        return f"{n / 1000:.0f}k"
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
        # 记下这批数字是什么时候抓的：卡片发出去之后还能回答「当时是多少」
        "stats_fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------- 渲染
def vertical_gradient(w: int, h: int, top, bot) -> Image.Image:
    """竖向渐变：只算一列 h 个像素，再横向拉伸到 w。

    早期版本逐像素写 w*h 个点（1080x1440 = 155 万次赋值），单张图要好几秒，
    而渐变本身横向是恒定的，所以拉伸结果完全等价、快两个数量级。
    """
    base = Image.new("RGB", (1, h))
    px = base.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
    return base.resize((w, h), Image.NEAREST)


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


# ---------------------------------------------------------------- 布局审计
# 卡片是「先算后画」：把要画的东西排成 ops、量出总高度，塞不下就整体缩字号或
# 省略次要区块，最后才落笔。以前是边走边画，内容一长就静默压到页脚数据条上。

SKIP_LABELS = {
    "start": "「怎么开始」区块",
    "for_who": "「适合谁」区块",
    "pain3": "第 3 条痛点",
    "pain2": "第 2 条痛点",
    "pain": "「它能解决什么」整块",
}


class Audit:
    """记录一次渲染的几何与色彩事实，交给 card_check.py 断言。

    render() 里每画一块文字都会在这里留一条记录：落在哪、什么颜色、
    底下的背景是什么颜色。有了它，「有没有压到页脚」「对比度够不够」
    这类原来只能靠肉眼的问题，就能被程序判定。
    """

    def __init__(self) -> None:
        self.texts: list[dict] = []
        self.rects: list[dict] = []
        self.frames: dict = {}
        self.bg_stats: dict = {}
        self.scale: float = 1.0
        self.dropped: list[str] = []
        self.truncated: list[dict] = []

    def add_text(self, d, img, section, xy, text, font, fill, max_w=None, bg=None) -> None:
        box = d.textbbox(xy, text, font=font)
        if bg is None:
            cx = min(max((box[0] + box[2]) // 2, 0), W - 1)
            cy = min(max((box[1] + box[3]) // 2, 0), H - 1)
            bg = img.getpixel((cx, cy))
        self.texts.append({
            "section": section,
            "text": text,
            "bbox": [int(v) for v in box],
            "color": [int(v) for v in fill],
            "bg": [int(v) for v in bg],
            "font_path": getattr(font, "path", "") or "",
            "size": int(getattr(font, "size", 0) or 0),
            "max_w": max_w,
            "_font": font,
        })

    def to_json(self) -> dict:
        return {
            "scale": self.scale,
            "dropped": self.dropped,
            "truncated": self.truncated,
            "frames": {k: list(v) if isinstance(v, tuple) else v
                       for k, v in self.frames.items()},
            "bg_stats": self.bg_stats,
            "texts": [{k: v for k, v in t.items() if not k.startswith("_")}
                      for t in self.texts],
            "rects": self.rects,
        }


# ---------------------------------------------------------------- 调色板
def _palette(style: dict) -> dict:
    """把 styles/<id>.json 解析成一组 RGB；顺带处理 accent 当正文色的可读性兜底。"""
    pal = style["palette"]
    BG_TOP, BG_BOT = rgb(pal["bg_top"]), rgb(pal["bg_bottom"])
    CARD, CARD_HI = rgb(pal["surface"]), rgb(pal["surface_alt"])
    TEXT, DIM, FAINT = rgb(pal["text"]), rgb(pal["text_dim"]), rgb(pal["text_faint"])
    ACCENT, ACCENT2 = rgb(pal["accent"]), rgb(pal["accent_2"])
    GREEN, LINE = rgb(pal["good"]), rgb(pal["line"])
    # accent 用作正文色时可能糊在背景里（浅色主题的荧光黄、粉色尤其明显）。
    # 对比不够就退回正文色，装饰块仍然用原 accent —— 保留风格，但保证读得清。
    ACCENT_TEXT = ACCENT if contrast_ratio(ACCENT, BG_BOT) >= 4.0 else TEXT
    ACCENT2_TEXT = ACCENT2 if contrast_ratio(ACCENT2, CARD_HI) >= 3.0 else TEXT
    # 命令块的首行用 good 色（绿色）当「可执行」的记号；浅色卡上它会糊成一片，
    # 所以同样要兜底成正文色。
    GOOD_TEXT = GREEN if contrast_ratio(GREEN, CARD_HI) >= 3.0 else TEXT
    return {
        "bg_top": BG_TOP, "bg_bot": BG_BOT, "card": CARD, "card_hi": CARD_HI,
        "text": TEXT, "dim": DIM, "faint": FAINT,
        "accent": ACCENT, "accent_2": ACCENT2, "good": GREEN, "line": LINE,
        "accent_text": ACCENT_TEXT, "accent2_text": ACCENT2_TEXT,
        "good_text": GOOD_TEXT,
    }


def _bg_stats(img: Image.Image) -> dict:
    """背景的明暗层次：用来判「死黑卡」（除文字外几乎没有明暗过渡）。"""
    hist = img.convert("L").histogram()
    total = sum(hist) or 1
    mean = sum(i * c for i, c in enumerate(hist)) / total

    def pct(p: float) -> int:
        acc, target = 0, total * p / 100.0
        for i, c in enumerate(hist):
            acc += c
            if acc >= target:
                return i
        return 255

    return {"mean": round(mean, 1), "p5": pct(5), "p95": pct(95),
            "levels": sum(1 for c in hist if c)}


# ---------------------------------------------------------------- 排版（只测量）
def plan_content(d, payload: dict, style: dict, scale: float = 1.0,
                 skip: tuple = ()) -> tuple[list[dict], int]:
    """算出页脚之上所有绘制指令；返回 (ops, 内容最底 y)。只测量，不落笔。"""
    C = _palette(style)
    typ = style.get("type") or {}
    title_scale = float(typ.get("title_scale") or 1.0)
    lead_mul = float(typ.get("lead") or 1.34)

    S = lambda v: int(round(v * scale))
    F = lambda kind, size: load_font(kind, max(11, S(size)))
    ops: list[dict] = []

    def text(section, tx, ty, s, font, fill, max_w=None, bg=None):
        ops.append({"kind": "text", "section": section, "xy": (int(tx), int(ty)),
                    "text": s, "font": font, "fill": fill, "max_w": max_w, "bg": bg})

    def rect(section, box, radius, fill):
        ops.append({"kind": "rect", "section": section,
                    "box": tuple(int(v) for v in box), "radius": radius, "fill": fill})

    def note(field, dropped: str):
        ops.append({"kind": "note", "field": field, "dropped": dropped})

    x = PAD
    y = 66

    # 顶部标签 pill（保持原样：文字 + 胶囊底）
    f_pill = F("cn", 24)
    label = "GitHub 中文速览"
    pw = text_w(d, label, f_pill) + 36
    ph = f_pill.size + 18
    rect("pill", (x, y, x + pw, y + ph), ph // 2, C["card_hi"])
    text("pill", x + 18, y + 9 - 2, label, f_pill, C["accent_text"], bg=C["card_hi"])
    y += S(58)

    # owner/repo
    f_mono_s = F("mono", 26)
    text("owner", x, y, f"{payload['owner']}/{payload['repo']}", f_mono_s, C["faint"])
    y += S(52)

    # 中文名（大字，最多 2 行）
    name_zh = payload.get("name_zh") or payload["repo"]
    font_name, lines, lead = fit_block(
        d, name_zh, "cn_bold", W - PAD * 2, 2,
        S(int(78 * title_scale)), S(int(50 * title_scale)), lead_mul)
    for ln in lines:
        text("name", x, y, ln, font_name, C["text"])
        y += lead
    y += S(14)

    # 一句话定位（左边有 26px 的强调竖条 + 缩进，所以可用宽度要扣掉）
    tagline = payload.get("tagline", "")
    if tagline:
        tw = W - PAD * 2 - 26
        font_t, lines_t, lead_t = fit_block(
            d, tagline, "cn_bold", tw, 3, S(42), S(30), lead_mul + 0.04)
        rect("tagline_bar", (x, y + 4, x + 6, y + lead_t * len(lines_t) - 12),
             3, C["accent"])
        tx = x + 26
        for ln in lines_t:
            text("tagline", tx, y, ln, font_t, C["accent_text"], max_w=tw)
            y += lead_t
        y += S(34)

    # 它能解决什么
    pains = [p for p in (payload.get("pain") or []) if p][:3]
    if "pain3" in skip:
        pains = pains[:2]
    if "pain2" in skip:
        pains = pains[:1]
    if "pain" in skip:
        pains = []
    if pains:
        f_sec = F("cn_bold", 26)
        text("section", x, y, "它能解决什么", f_sec, C["faint"])
        y += S(54)
        f_b = F("cn", 33)
        for p in pains:
            rect("pain_dot", (x + 2, y + 14, x + 12, y + 24), 5, C["good"])
            lines_p = wrap_cjk(d, p, f_b, W - PAD * 2 - 44)
            for ln in lines_p:
                text("pain", x + 36, y, ln, f_b, C["text"], max_w=W - PAD * 2 - 44)
                y += S(48)
            y += S(10)
        y += S(18)

    # 适合谁
    for_who = "" if "for_who" in skip else payload.get("for_who", "")
    if for_who:
        text("section", x, y, "适合谁", f_sec, C["faint"])
        y += S(50)
        f_w = F("cn", 31)
        lines_w = wrap_cjk(d, for_who, f_w, W - PAD * 2 - 48)
        if len(lines_w) > 3:
            note("for_who", "".join(lines_w[3:]))
            lines_w = lines_w[:3]
        block_h = len(lines_w) * S(46) + S(32)
        rect("for_who_card", (x, y, W - PAD, y + block_h), 18, C["card"])
        ty = y + S(16)
        for ln in lines_w:
            text("for_who", x + 24, ty, ln, f_w, C["text"], bg=C["card"])
            ty += S(46)
        y += block_h + S(30)

    # 怎么开始（可以是命令，也可以是一句人话）
    start = "" if "start" in skip else (payload.get("start") or payload.get("install", ""))
    if start:
        is_cmd = bool(start) and not has_cjk(start)
        f_cmd = F("mono" if is_cmd else "cn", 27)
        shown = ("$ " + start) if is_cmd else start
        lines_c = wrap_cjk(d, shown, f_cmd, W - PAD * 2 - 40)
        if len(lines_c) > 3:
            note("start", "".join(lines_c[3:]))
            lines_c = lines_c[:3]
        block_h = len(lines_c) * S(40) + S(28)
        rect("start_card", (x, y, W - PAD, y + block_h), 16, C["card_hi"])
        ty = y + S(14)
        for i, ln in enumerate(lines_c):
            text("start", x + 20, ty, ln, f_cmd,
                 (C["good_text"] if (is_cmd and i == 0) else C["text"]),
                 max_w=W - PAD * 2 - 40, bg=C["card_hi"])
            ty += S(40)
        y += block_h + S(26)

    return ops, int(y)


def fit_content(d, payload: dict, style: dict) -> tuple[list[dict], float, tuple, int]:
    """从最舒展的排版开始试，塞不下就整体缩字号，再不行才省次要区块。

    返回 (ops, 字号倍率, 省略的区块, 正文流占到的 y)。
    """
    need_verdict = bool(payload.get("verdict"))
    # 结论块最低要求：结论可以贴着正文放（老排版就这样），但不能压到页脚线
    reserve = VERDICT_MIN_H if need_verdict else 0
    for skip in FIT_SKIPS:
        for scale in FIT_SCALES:
            ops, bottom = plan_content(d, payload, style, scale, skip)
            if bottom + reserve <= VERDICT_BOTTOM_LIMIT:
                return ops, scale, skip, bottom
    ops, bottom = plan_content(d, payload, style, FIT_SCALES[-1], FIT_SKIPS[-1])
    return ops, FIT_SCALES[-1], FIT_SKIPS[-1], bottom


def plan_verdict(d, payload: dict, style: dict, top: int) -> tuple[list[dict], dict]:
    """结论块：在正文下方剩余空间里居中；放不下就缩字号，再不行才截断并报告。"""
    verdict = payload.get("verdict", "")
    ops: list[dict] = []
    if not verdict:
        return ops, {}
    C = _palette(style)
    x = PAD
    foot_line = FOOT_LINE
    # 老排版的口径：结论可以贴到正文底下，但它的底边不许越过页脚线上方 14px
    avail = foot_line - 24 - top
    f_v = load_font("cn", 22)
    lines_v: list[str] = []
    step = 31
    for size in range(30, 21, -1):
        f = load_font("cn", size)
        step = int(size * 1.45)
        lv = wrap_cjk(d, verdict, f, W - PAD * 2 - 52)
        if len(lv) * step + 34 <= avail:
            f_v, lines_v = f, lv
            break
    else:
        f_v = load_font("cn", 22)
        step = int(22 * 1.45)
        lines_v = wrap_cjk(d, verdict, f_v, W - PAD * 2 - 52)

    info: dict = {}
    if len(lines_v) > 5:
        info = {"field": "verdict", "dropped": "".join(lines_v[5:])}
        lines_v = lines_v[:5]
    block_h = len(lines_v) * step + 34
    # 塞不进「正文底 → 页脚线」之间时，从结论尾部删行（而不是压到数据条上）
    budget = VERDICT_BOTTOM_LIMIT - top
    while len(lines_v) > 1 and block_h > budget:
        lines_v = [*lines_v[:-1]]
        info = {"field": "verdict", "dropped": "".join(lines_v[len(lines_v) - 1:])}
        block_h = len(lines_v) * step + 34
    top_limit = top + 24
    bottom_limit = foot_line - 24 - block_h
    by = min(bottom_limit, max(top_limit, top + max(0, (bottom_limit - top) // 2)))
    by = max(by, top)                                    # 内容短时贴着正文放
    by = min(by, VERDICT_BOTTOM_LIMIT - block_h)         # 绝不越过页脚线
    by = max(by, 0)
    ops.append({"kind": "rect", "section": "verdict_card", "radius": 18,
                "box": (x, by, W - PAD, by + block_h), "fill": C["card"]})
    ops.append({"kind": "rect", "section": "verdict_bar", "radius": 3,
                "box": (x, by + 10, x + 5, by + block_h - 10), "fill": C["accent_2"]})
    ty = by + 17
    for ln in lines_v:
        ops.append({"kind": "text", "section": "verdict", "xy": (x + 26, ty),
                    "text": ln, "font": f_v, "fill": C["text"],
                    "max_w": W - PAD * 2 - 52, "bg": C["card"]})
        ty += step
    return ops, info


def _paint_ops(img: Image.Image, d, ops: list[dict], audit: Audit) -> None:
    for op in ops:
        if op["kind"] == "rect":
            d.rounded_rectangle(list(op["box"]), radius=op["radius"], fill=op["fill"])
            audit.rects.append({"section": op["section"], "box": list(op["box"]),
                                "radius": op["radius"], "fill": list(op["fill"])})
        elif op["kind"] == "text":
            audit.add_text(d, img, op["section"], op["xy"], op["text"],
                           op["font"], op["fill"], op["max_w"], op["bg"])
            d.text(op["xy"], op["text"], font=op["font"], fill=op["fill"])


def render(payload: dict, out_path: Path, style: dict | None = None,
           audit: Audit | None = None) -> Path:
    style = style or DEFAULT_STYLE
    C = _palette(style)
    audit = audit if audit is not None else Audit()

    img = build_background(style)
    d = ImageDraw.Draw(img)
    audit.bg_stats = _bg_stats(img)
    audit.frames = {
        "canvas": (0, 0, W, H),
        "safe": (PAD - 2, 0, W - PAD + 2, CONTENT_BOTTOM),
        "content_bottom": CONTENT_BOTTOM,
        "foot_line": FOOT_LINE,
        "data_bar": (0, FOOT_LINE + 1, W, H - PAD + 6),
        "credit": (0, H - 40, W, H),
    }

    # 正文：先排版、再落笔；落笔时每块文字的位置都记进 audit
    ops, scale, skip, flow_bottom = fit_content(d, payload, style)
    audit.scale = scale
    audit.dropped = [SKIP_LABELS[s] for s in skip]
    _paint_ops(img, d, ops, audit)
    for op in ops:
        if op["kind"] == "note":
            audit.truncated.append({"field": op["field"], "dropped": op["dropped"]})

    # 结论（底部对齐，填掉内容与页脚之间的空）
    vops, vinfo = plan_verdict(d, payload, style, flow_bottom)
    _paint_ops(img, d, vops, audit)
    if vinfo:
        audit.truncated.append(vinfo)

    # ---- 底部数据条 ----
    foot_y = H - PAD - 96
    d.line([(PAD, FOOT_LINE), (W - PAD, FOOT_LINE)], fill=C["line"], width=2)
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
        val, font_v = fit_value(d, val, "cn_bold", col_w - 44)
        size_v = font_v.size
        vy = foot_y + (40 - size_v)
        audit.add_text(d, img, "data", (cx, vy), val, font_v, C["text"], col_w - 44)
        d.text((cx, vy), val, font=font_v, fill=C["text"])
        audit.add_text(d, img, "data_label", (cx, foot_y + 54), lbl, f_lbl, C["faint"])
        d.text((cx, foot_y + 54), lbl, font=f_lbl, fill=C["faint"])

    # 右上角话题标签
    topics = [t for t in (payload.get("topics") or []) if t][:3]
    if topics:
        f_t = load_font("mono", 20)
        total_w = sum(text_w(d, "#" + t, f_t) + 16 for t in topics) + 16 * (len(topics) - 1)
        tx = W - PAD - total_w
        for t in topics:
            tw = text_w(d, "#" + t, f_t) + 16
            th = f_t.size + 12
            d.rounded_rectangle([tx, 66, tx + tw, 66 + th], radius=th // 2,
                                fill=C["card_hi"])
            audit.rects.append({"section": "topics", "box": [tx, 66, tx + tw, 66 + th],
                                "radius": th // 2, "fill": list(C["card_hi"])})
            audit.add_text(d, img, "topics", (tx + 8, 66 + 6 - 2), "#" + t,
                           f_t, C["accent2_text"], bg=C["card_hi"])
            d.text((tx + 8, 66 + 6 - 2), "#" + t, font=f_t, fill=C["accent2_text"])
            tx += tw + 16

    # 署名：卡片被转发时链接跟着走 —— 这是这个工具唯一的自然传播入口
    if (style.get("decor") or {}).get("credit", True):
        f_cr = load_font("cn", 15)
        credit = "gh-zh-card · github.com/PR-JHL/gh-zh-card"
        cw = text_w(d, credit, f_cr)
        # 把弱化色再往背景压 45%，保证不抢内容
        credit_col = tuple(int(c + (C["bg_bot"][i] - c) * 0.45) for i, c in enumerate(C["faint"]))
        audit.add_text(d, img, "credit", ((W - cw) // 2, H - 32), credit, f_cr, credit_col)
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
    ap.add_argument("--save", action="store_true",
                    help="把 --fetch 抓到的真数据（含抓取时间）写回 --payload 文件")
    ap.add_argument("--style", default="obsidian",
                    help="风格 id（styles/<id>.json）或风格 JSON 的路径，默认 obsidian")
    ap.add_argument("--style-dir", action="append", default=[],
                    help="额外风格目录，可重复；用来加载 design-cards 里的风格库")
    ap.add_argument("--no-credit", action="store_true",
                    help="不画底部署名（默认画：卡片被转发时带上项目链接）")
    ap.add_argument("--check", action="store_true",
                    help="渲染后自检：版面 / 文案长度 / 可读性，有问题退出码 1")
    ap.add_argument("--audit-json", help="把自检明细（每块文字的位置、对比度）写成 JSON")
    ap.add_argument("--list-styles", action="store_true",
                    help="列出 styles/ 里所有可用风格，然后退出")
    args = ap.parse_args()

    if args.list_styles:
        rows_seen: list[tuple[str, str, str]] = []
        known: set[str] = set()
        for base in [Path(p) for p in args.style_dir] + STYLE_DIRS:
            if not Path(base).is_dir():
                continue
            for f in sorted(Path(base).glob("*.json")):
                if f.stem in known:
                    continue
                try:
                    st = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                known.add(f.stem)
                rows_seen.append((f.stem,
                                  st.get("name_zh") or st.get("name") or "",
                                  st.get("family") or ""))
        print(f"{'id':24}{'中文名':16}{'家族'}")
        for sid, nm, fam in rows_seen:
            print(f"{sid:24}{nm:16}{fam}")
        print(f"\n共 {len(rows_seen)} 套。用法：--style <id>")
        return

    payload: dict = {}
    if args.payload:
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    if args.fetch and not args.offline:
        fresh = fetch_repo(args.fetch)
        # 以前的写法是 {**fresh, **payload}：payload 里的旧数字会把刚抓到的真数据盖掉，
        # 于是「重新抓一次」根本不会更新星数。统计字段必须让真数据赢，文案字段保持不动。
        for k, v in fresh.items():
            if k == "stats_fetched_at" or k not in payload:
                payload[k] = v
            elif k in ("stars", "forks", "language", "license", "updated"):
                payload[k] = v
        if args.save:
            if not args.payload:
                ap.error("--save 需要同时指定 --payload（要写回哪个文件）")
            Path(args.payload).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"已把真数据写回: {args.payload}（stars={payload.get('stars')}, "
                  f"抓取时间 {payload['stats_fetched_at']}）")
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
    audit = Audit()
    png = render(payload, out_dir / f"{stem}.png", style, audit=audit)
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

    rc = 0
    if args.check or args.audit_json:
        import card_check
        findings = card_check.check(audit, payload)
    if args.check:
        print()
        print(card_check.report(findings, audit))
        rc = 1 if any(f.level == "error" for f in findings) else 0
    if args.audit_json:
        Path(args.audit_json).write_text(json.dumps({
            "findings": [f.__dict__ for f in findings],
            "audit": audit.to_json(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"自检明细: {args.audit_json}")
    if rc:
        sys.exit(rc)


if __name__ == "__main__":
    main()