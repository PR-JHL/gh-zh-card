#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gh-zh-card 自检 —— 把 SKILL.md 第 5 步的「打开 PNG 看一眼」变成可执行断言。

原来那一步只能靠人（或 agent）看图，问题是：

  · 44 套风格 × 每天几张卡，肉眼看完一遍不现实；
  · 内容是 agent 写的，写长了就会静默压到页脚数据条上（老版本真会这样）；
  · 「死黑」「豆腐块」「对比度不够」这类问题，盯着看也容易漏。

所以这里不看像素猜测，而是让 gh_card.render() 把每块文字的落点、颜色、
底色都记下来（gh_card.Audit），再对这些事实做断言。检查分三类：

  版面（error）  出界、压页脚、两块文字互相压住、单行超宽
  文案（error）  为了塞进版面被省略 / 被截断的字段 —— 该改文案，不是改版面
  可读性（warn） 字号被缩得太小、对比度偏低、深色卡没有明暗层次、缺字形（豆腐块）

用法（通常不直接调，走 `python gh_card.py ... --check`）：

    python card_check.py --payload payloads/pinky.json --style obsidian
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gh_card  # noqa: E402

# 渲染器自己会画的符号，不算「缺字形」
STRUCTURAL_CHARS = set("★—·›→「」『』（）·…")

# 底色很暗的卡，明暗跨度小于这个值就算「发闷」（除文字外几乎没有层次）
DARK_MEAN = 70
DEAD_BLACK_SPREAD = 8
# 文字与底色的对比度：低于 2.2 基本读不了，低于 3.0 只是偏淡（弱化标签是设计的一部分）
CONTRAST_ERROR = 2.2
CONTRAST_WARN = 3.0


@dataclass
class Finding:
    level: str          # error / warn / info
    code: str
    message: str
    detail: dict = field(default_factory=dict)


def _ratio(a, b) -> float:
    return gh_card.contrast_ratio(tuple(a), tuple(b))


def _area(box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _inter(a, b) -> list[int]:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else []


def _visible(texts: list[dict]) -> list[dict]:
    """页脚数据条和署名有自己的位置约定，不参与正文的版面断言。"""
    return [t for t in texts if t["section"] not in ("data", "data_label", "credit")]


# ---------------------------------------------------------------- 缺字形
def _glyph_sig(font, ch: str):
    mask = font.getmask(ch, mode="L")
    return mask.size, bytes(mask)


def missing_glyphs(audit) -> dict[str, list[str]]:
    """用 .notdef 参照法找豆腐块：字体里没有的字符，画出来和未分配码位长得一样。"""
    cache: dict[tuple, tuple] = {}
    found: dict[str, list[str]] = {}

    def notdef(font):
        key = (id(font), None)
        if key not in cache:
            try:
                cache[key] = _glyph_sig(font, "\ue000")
            except Exception:  # noqa: BLE001 - 字体对象千奇百怪，查不了就当没有
                cache[key] = None
        return cache[key]

    for t in audit.texts:
        font = t.get("_font")
        if font is None:
            continue
        base = notdef(font)
        if base is None:
            continue
        for ch in sorted(set(t["text"])):
            if ch.isspace() or ch in STRUCTURAL_CHARS:
                continue
            key = (id(font), ch)
            if key not in cache:
                try:
                    cache[key] = _glyph_sig(font, ch)
                except Exception:  # noqa: BLE001
                    cache[key] = None
            sig = cache[key]
            if sig is not None and sig == base:
                found.setdefault(ch, [])
                if t["section"] not in found[ch]:
                    found[ch].append(t["section"])
    return found


# ---------------------------------------------------------------- 断言
def check(audit, payload: dict | None = None) -> list[Finding]:
    out: list[Finding] = []
    frames = audit.frames or {}
    foot_line = frames.get("foot_line", gh_card.FOOT_LINE)
    data_bar = frames.get("data_bar", (0, foot_line + 1, gh_card.W, gh_card.H - gh_card.PAD + 6))
    safe = frames.get("safe", (gh_card.PAD - 2, 0, gh_card.W - gh_card.PAD + 2, gh_card.CONTENT_BOTTOM))
    content = _visible(audit.texts)

    # 1) 正文出界 / 压页脚线
    for t in content:
        b = t["bbox"]
        if b[0] < safe[0] - 4 or b[2] > safe[2] + 4 or b[1] < 0:
            out.append(Finding("error", "TEXT_OUT_OF_SAFE",
                               f"{t['section']} 区块的文字超出左右安全边距：{t['text'][:16]!r}",
                               {"section": t["section"], "bbox": b}))
        if b[3] > gh_card.VERDICT_BOTTOM_LIMIT + 2:
            out.append(Finding("error", "TEXT_OUT_OF_SAFE",
                               f"{t['section']} 区块越过了正文底线 {gh_card.VERDICT_BOTTOM_LIMIT}px：{t['text'][:16]!r}",
                               {"section": t["section"], "bbox": b}))
        if _inter(b, data_bar):
            out.append(Finding("error", "FOOTER_OVERLAP",
                               f"{t['section']} 区块压到页脚数据条：{t['text'][:16]!r}",
                               {"section": t["section"], "bbox": b}))

    # 2) 两块文字互相压住
    seen: set[tuple] = set()
    for i, a in enumerate(content):
        for b in content[i + 1:]:
            if a["section"] == b["section"]:
                continue
            overlap = _inter(a["bbox"], b["bbox"])
            if overlap and _area(overlap) > 16:
                key = tuple(sorted((a["section"], b["section"])))
                if key in seen:
                    continue
                seen.add(key)
                out.append(Finding("error", "TEXT_OVERLAP",
                                   f"「{a['section']}」和「{b['section']}」两块文字重叠",
                                   {"sections": list(key), "overlap": overlap}))

    # 3) 文字压在别的区块卡片上（不是它自己的底）
    for t in content:
        for r in audit.rects:
            if r["section"] in ("pill", "topics", "pain_dot"):
                continue
            if t["section"] in (r["section"], r["section"].replace("_card", "")):
                continue
            overlap = _inter(t["bbox"], r["box"])
            if overlap and _area(overlap) > 0.5 * _area(t["bbox"]):
                out.append(Finding("error", "TEXT_ON_BLOCK",
                                   f"{t['section']} 的文字压在「{r['section']}」色块上：{t['text'][:16]!r}",
                                   {"section": t["section"], "block": r["section"]}))
                break

    # 4) 单行超宽；容忍一个标点的「禁则」溢出（换行规则允许行尾多一个标点）
    for t in content:
        mw = t.get("max_w")
        if not mw:
            continue
        tol = max(12, t.get("size", 12))
        width = t["bbox"][2] - t["bbox"][0]
        if width > mw + tol:
            out.append(Finding("error", "LINE_TOO_WIDE",
                               f"{t['section']} 有一行比排版宽度宽 {width - mw}px：{t['text'][:16]!r}",
                               {"section": t["section"], "bbox": t["bbox"], "max_w": mw}))

    # 5) 为了塞进版面而省略 / 截断的内容
    for label in audit.dropped:
        out.append(Finding("error", "COPY_DROPPED",
                           f"内容超长，已省略{label}。别硬挤 —— 把文案改短。",
                           {"dropped": label}))
    for note in audit.truncated:
        field_name = note.get("field", "?")
        dropped = note.get("dropped", "")
        out.append(Finding("error", "COPY_TRUNCATED",
                           f"「{field_name}」有 {len(dropped)} 个字没排进去（{dropped[:18]!r}…）。"
                           f"请删减该字段。",
                           {"field": field_name, "dropped_chars": len(dropped)}))

    # 6) 字号被缩得太小
    scale = float(audit.scale or 1.0)
    if scale < 0.8:
        out.append(Finding("warn", "TYPE_TOO_SMALL",
                           f"为了塞进版面，整体字号缩到了 {scale:.0%}；正文已经偏小，建议缩短文案。",
                           {"scale": scale}))
    elif scale < 1.0:
        out.append(Finding("info", "TYPE_SCALED",
                           f"为了塞进版面，整体字号缩到 {scale:.0%}。", {"scale": scale}))

    # 7) 对比度（用渲染时真实采样的底色，不是猜的）
    for t in content:
        if t["section"] == "credit":
            continue  # 署名是故意压暗的装饰，不参与可读性门槛
        ratio = _ratio(t["color"], t["bg"])
        if ratio < CONTRAST_ERROR:
            out.append(Finding("error", "LOW_CONTRAST",
                               f"{t['section']} 的字几乎看不清（对比度 {ratio:.1f}:1）：{t['text'][:16]!r}",
                               {"section": t["section"], "ratio": round(ratio, 2)}))
        elif ratio < CONTRAST_WARN:
            out.append(Finding("warn", "LOW_CONTRAST",
                               f"{t['section']} 对比度偏低（{ratio:.1f}:1）：{t['text'][:16]!r}",
                               {"section": t["section"], "ratio": round(ratio, 2)}))

    # 8) 缺字形（豆腐块）
    miss = missing_glyphs(audit)
    if miss:
        chars = "".join(miss)
        out.append(Finding("error", "GLYPH_MISSING",
                           f"字体里没有这些字，会渲染成豆腐块：{chars}"
                           f"（出现在 {'、'.join(sorted({s for v in miss.values() for s in v}))}）",
                           {"chars": chars}))

    # 9) 深色卡是不是「发闷」：整张图除了字几乎没有明暗过渡（只是提示，不是错误）
    st = audit.bg_stats or {}
    if st and st.get("mean", 255) < DARK_MEAN and (st.get("p95", 0) - st.get("p5", 0)) < DEAD_BLACK_SPREAD:
        out.append(Finding("info", "FLAT_BACKGROUND",
                           f"深色卡的明暗跨度只有 {st.get('p95') - st.get('p5')}/255（p5={st.get('p5')} → "
                           f"p95={st.get('p95')}），容易显得发闷。可调 decor.glow / vignette，"
                           f"或拉开 bg_top 与 bg_bottom 的明度。",
                           dict(st)))

    # 10) 页脚四项互相贴住
    vals = [t for t in audit.texts if t["section"] == "data"]
    vals.sort(key=lambda t: t["bbox"][0])
    for a, b in zip(vals, vals[1:]):
        gap = b["bbox"][0] - a["bbox"][2]
        if gap < 8:
            out.append(Finding("warn", "FOOTER_TIGHT",
                               f"页脚「{a['text']}」和「{b['text']}」贴得太近（间距 {gap}px）",
                               {"gap": gap}))

    # 10) 文案超长时，顺手把当前字数也算出来 —— 提示「删多少」比只说「太长了」有用
    if payload and any(f.code.startswith("COPY_") for f in out):
        pains = [len(p) for p in (payload.get("pain") or []) if p]
        lens = {"tagline": len(payload.get("tagline") or ""),
                "pain": pains,
                "for_who": len(payload.get("for_who") or ""),
                "start": len(payload.get("start") or payload.get("install") or ""),
                "verdict": len(payload.get("verdict") or "")}
        out.append(Finding("info", "LENGTH_HINT",
                           f"当前字数：tagline {lens['tagline']}（建议 ≤34）、"
                           f"pain {pains}（每条 ≤38）、for_who {lens['for_who']}（≤30）、"
                           f"verdict {lens['verdict']}（≤50）",
                           lens))

    order = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: (order.get(f.level, 3), f.code))
    return out


# ---------------------------------------------------------------- 报告
def report(findings: list[Finding], audit=None) -> str:
    errors = [f for f in findings if f.level == "error"]
    warns = [f for f in findings if f.level == "warn"]
    infos = [f for f in findings if f.level == "info"]
    if not findings:
        return "自检通过：版面、文案长度、可读性都没有问题。"

    lines = []
    if errors:
        lines.append(f"自检发现 {len(errors)} 个必须修的问题：")
    elif warns:
        lines.append(f"自检通过（{len(warns)} 条提醒）：")
    else:
        lines.append("自检通过。")
    icon = {"error": "  [错误]", "warn": "  [提醒]", "info": "  [信息]"}
    for f in errors + warns + infos:
        lines.append(icon.get(f.level, "  [?]") + " " + f.message)
    if err := [f for f in errors if f.code.startswith("COPY_")]:
        lines.append("")
        lines.append(f"→ 有 {len(err)} 条是文案太长。缩短文案比调排版参数管用："
                     f"tagline ≤34 字、每条 pain ≤38 字、for_who ≤30 字、verdict ≤50 字。")
    if audit is not None and audit.scale != 1.0:
        lines.append("")
        lines.append(f"整体字号缩到 {audit.scale:.0%}（内容偏长时的自动降级）。"
                     f"正文底线 {gh_card.VERDICT_BOTTOM_LIMIT}px，页脚线 {gh_card.FOOT_LINE}px。")
    return "\n".join([ln for ln in lines if ln != ""])


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染一张卡并自检")
    ap.add_argument("--payload", required=True)
    ap.add_argument("--style", default="obsidian")
    ap.add_argument("--out", help="顺便把 PNG 存到这里")
    ap.add_argument("--json", help="把自检结果写成 JSON")
    args = ap.parse_args()

    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    payload.setdefault("owner", "unknown")
    payload.setdefault("repo", "unknown")
    payload.setdefault("url", f"https://github.com/{payload['owner']}/{payload['repo']}")
    style = gh_card.load_style(args.style)
    audit = gh_card.Audit()
    out_png = Path(args.out) if args.out else Path(args.payload).with_suffix(".check.png")
    gh_card.render(payload, out_png, style, audit=audit)
    findings = check(audit, payload)
    print(report(findings, audit))
    if args.json:
        Path(args.json).write_text(json.dumps({
            "findings": [f.__dict__ for f in findings],
            "audit": audit.to_json(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n自检明细: {args.json}")
    return 1 if any(f.level == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())