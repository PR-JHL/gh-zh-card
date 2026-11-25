#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归自检：styles/ 下每套风格都渲染一遍，逐张跑 card_check。

加了新风格、改了排版、调了字体之后跑这个，确认没有哪套风格被改坏。

用法：
    python selftest.py                    # 全部风格 × 8 份 payload（约 3 分钟）
    python selftest.py --quick            # 只跑 6 套代表风格（十来秒）
    python selftest.py --sheet docs/long-copy.jpg   # 顺便拼一张长文案对比图
    python selftest.py --json report.json

判定标准：
    · 版面错误（出界 / 压页脚 / 文字互相压住 / 单行超宽）→ 失败
    · 文案被省略或截断 → 只统计（长文案样本本来就是用来测降级的）
    · 可读性 warn / info → 只统计
    · tofu 样本必须被 card_check 报出豆腐块，报不出也算失败（自检本身失效）
    · thin 样本（只有名字 + 一句定位）必须被报出 THIN_CARD，报不出同样算失败
    · messy 样本（payload 类型全写错）必须被就地纠正并报出 PAYLOAD_FIXED
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import card_check  # noqa: E402
import gh_card  # noqa: E402

LAYOUT_ERRORS = {"TEXT_OUT_OF_SAFE", "FOOTER_OVERLAP", "TEXT_OVERLAP",
                 "TEXT_ON_BLOCK", "LINE_TOO_WIDE"}

QUICK_STYLES = ["obsidian", "wandering-earth", "letterpress-news", "neo-brutalism",
                "lofoten-aurora", "swiss-international"]

# 豆腐块样本：微软雅黑没有韩文/emoji 字形，正好用来验证自检抓不抓得到
TOFU_PAYLOAD = {
    "_note": "自检用的缺字形样本：한국어 / 🙂 在微软雅黑里没有字形",
    "owner": "demo",
    "repo": "glyph-test",
    "url": "https://github.com/demo/glyph-test",
    "name_zh": "字形测试",
    "tagline": "한국어와 🙂 混排的测试文案：字体缺字形时应该渲染成豆腐块",
    "pain": ["这一条是正常的汉字，用来做对照"],
    "for_who": "跑 selftest 的人",
    "start": "python selftest.py",
    "verdict": "看到这条说明自检跑起来了。",
    "stars": 1,
    "language": "Python",
    "license": "MIT",
    "updated": "2026-11-18T02:00:00Z",
    "topics": ["selftest"],
}

SHEET_STYLES = ["obsidian", "wandering-earth", "letterpress-news", "neo-brutalism",
                "lofoten-aurora", "sakura-wafu", "swiss-international", "steampunk"]

# 极端样本：每一项都长到离谱（连 license 都写全称），用来压排版引擎的兜底路径
EXTREME_PAYLOAD = {
    "_note": "自检用的极端样本：连页脚许可证都是全称，专门压「最后一档降级」",
    "owner": "too-long",
    "repo": "everything-is-too-long",
    "url": "https://github.com/too-long/everything-is-too-long",
    "name_zh": "这一行项目名本身就长得离谱而且故意写成两行以上看看会发生什么标题超长测试",
    "tagline": "这一段定位语写成了四行还不止，因为要测试排版引擎在极端情况下会不会把整张卡片压成一张谁也读不下去的废纸",
    "pain": [
        "第一条痛点写得特别长，长到需要占据四行甚至五行版面，目的就是把正文区域彻底撑爆，"
        "看它还能不能守住页脚那条线",
        "第二条痛点同样写得特别长，长到需要占据四行甚至五行版面，目的就是把正文区域彻底撑爆，"
        "看它还能不能守住页脚那条线",
        "第三条痛点同样写得特别长，长到需要占据四行甚至五行版面，目的就是把正文区域彻底撑爆，"
        "看它还能不能守住页脚那条线",
    ],
    "for_who": "一个人维护非常多服务、每天被无数杂事打断、想把所有重复操作都固定成一条命令、"
               "并且还希望这条命令能自己解释清楚每一步在干什么的后端工程师",
    "start": "python -m everything.cli --bootstrap --environment=production "
             "--config-dir=~/.everything --verbose --dry-run --no-color "
             "--log-level=debug --timeout=3600",
    "verdict": "结论也写得非常长，长到需要四五行的位置，因为这个样本存在的唯一目的就是让排版引擎"
               "必须做取舍：到底是从字号开始缩，还是直接省掉区块，还是把结论截断，每一种选择都会"
               "在自检报告里留下记录，方便人回来判断这张卡还能不能发。",
    "stars": 9999999,
    "language": "TypeScript",
    "license": "Mozilla Public License 2.0",
    "updated": "2026-11-18T02:00:00Z",
    "topics": ["extreme", "layout", "stress"],
}


def load_fixtures() -> list[tuple[str, dict, str]]:
    """(名字, payload, 说明)"""
    out = []
    for name, rel, note in [
        ("pinky", "payloads/pinky.json", "正常文案"),
        ("guizang", "payloads/guizang-game-skill.json", "正常文案"),
        ("stress", "payloads/stress-long.json", "每项都超长"),
        ("longid", "payloads/edge-long-id.json", "owner/repo 长到 66 字符"),
        ("longtop", "payloads/edge-long-topics.json", "三个话题都超长"),
        ("thin", "payloads/edge-thin.json", "只有名字 + 一句定位 + 一句结论"),
        ("messy", "payloads/messy-types.json", "类型全写错（pain 是字符串、stars 是 null…）"),
    ]:
        p = HERE / rel
        if p.exists():
            out.append((name, json.loads(p.read_text(encoding="utf-8")), note))
    out.append(("tofu", TOFU_PAYLOAD, "缺字形样本"))
    out.append(("extreme", EXTREME_PAYLOAD, "每一项都长到离谱"))
    return out


def check_one(payload: dict, style: dict, out_png: Path):
    audit = gh_card.Audit()
    gh_card.render(payload, out_png, style, audit=audit)
    return card_check.check(audit, payload), audit


def make_sheet(payload: dict, path: Path) -> None:
    from PIL import Image, ImageDraw

    cell_w, cell_h, label_h, pad, cols = 236, 315, 30, 24, 4
    rows = (len(SHEET_STYLES) + cols - 1) // cols
    title_h = 74
    sheet = Image.new("RGB", (pad * 2 + cols * cell_w + (cols - 1) * 8,
                              title_h + pad + rows * (cell_h + label_h + 8)), (18, 20, 26))
    sd = ImageDraw.Draw(sheet)
    sd.text((pad, 20), "gh-zh-card · 超长文案 → 自动缩放，版面不压页脚", font=gh_card.load_font("cn_bold", 32), fill=(235, 240, 248))
    sd.text((pad, 60), "旧版会压到页脚数据条上；现在整体缩字号，仍放不下才省略区块并在 --check 里报错",
            font=gh_card.load_font("cn", 18), fill=(132, 142, 160))

    with tempfile.TemporaryDirectory() as td:
        for i, sid in enumerate(SHEET_STYLES):
            style = gh_card.load_style(sid)
            png = Path(td) / f"{sid}.png"
            findings, audit = check_one(payload, style, png)
            thumb = Image.open(png).convert("RGB").resize((cell_w, cell_h), Image.LANCZOS)
            col, row = i % cols, i // cols
            x = pad + col * (cell_w + 8)
            y = title_h + pad + row * (cell_h + label_h + 8)
            sheet.paste(thumb, (x, y))
            sd.rectangle([x, y, x + cell_w, y + cell_h], outline=(52, 58, 70))
            bad = [f for f in findings if f.level == "error"]
            name = style.get("name_zh") or sid
            cap = f"{name} · {audit.scale:.0%}" + ("" if not bad else f" · {len(bad)} 错")
            sd.text((x + 4, y + cell_h + 6), cap[:22], font=gh_card.load_font("cn", 16),
                    fill=(230, 120, 110) if bad else (140, 150, 168))

    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, "JPEG", quality=90, optimize=True)
    print(f"\n长文案对比图: {path} ({path.stat().st_size // 1024} KB)")


def check_fit_ident() -> list[str]:
    """fit_ident 的三条路都得走通：原样 / 缩字号 / 中间省略。

    样本里只有「长到必须省略」那一档，所以这里直接对函数下断言 ——
    不然「缩字号」那条路没人走过，坏了也不会有人发现。
    三档样本按当前字体的实际宽度现造，换机器换字体也不会失效。
    """
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    max_w = gh_card.W - gh_card.PAD * 2
    unit = "acme-labs/repository-name-"
    s = unit
    while gh_card.text_w(d, s, gh_card.load_font("mono", 18)) <= max_w:
        s += unit
    mid = s[:-len(unit)]                      # 18px 放得下、26px 放不下
    cases = [("PR-JHL/repo-preflight", ""), (mid, "shrink"), (mid * 3, "ellipsis")]
    bad = []
    for text_in, want in cases:
        text, font, how = gh_card.fit_ident(d, text_in, "mono", max_w)
        width = gh_card.text_w(d, text, font)
        ok = how == want and width <= max_w
        if how == "ellipsis":
            ok = ok and "…" in text and text.startswith(text_in[:4]) and text.endswith(text_in[-4:])
        if not ok:
            bad.append(f"{len(text_in)} 字符的标识符：期望 {want or '原样'}，"
                       f"实际 {how or '原样'} / 宽 {width}px / {text}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="把所有风格都渲染一遍并自检")
    ap.add_argument("--quick", action="store_true", help="只跑 6 套代表风格")
    ap.add_argument("--sheet", help="拼一张长文案对比图到指定路径")
    ap.add_argument("--json", help="把结果写成 JSON")
    args = ap.parse_args()

    style_files = sorted((HERE / "styles").glob("*.json"))
    if args.quick:
        style_files = [HERE / "styles" / f"{s}.json" for s in QUICK_STYLES]
    if not style_files:
        raise SystemExit("styles/ 里没有风格")

    fixtures = load_fixtures()
    print(f"gh-zh-card selftest · {len(style_files)} 套风格 × {len(fixtures)} 份 payload")
    ident_bad = check_fit_ident()
    print(f"标识符行护栏 {'✓ 原样 / 缩字号 / 中间省略 三条路都对' if not ident_bad else '✗ ' + ident_bad[0]}")
    report = {"styles": len(style_files), "fixtures": [], "ok": not ident_bad}
    tmpdir = tempfile.TemporaryDirectory()
    try:
        for name, payload, note in fixtures:
            rows = []
            for sf in style_files:
                findings, audit = check_one(payload, gh_card.load_style(sf.stem),
                                            Path(tmpdir.name) / f"{name}-{sf.stem}.png")
                rows.append({"style": sf.stem, "scale": audit.scale,
                             "dropped": audit.dropped, "truncated": audit.truncated,
                             "findings": [f.__dict__ for f in findings]})
            errors = [f for r in rows for f in r["findings"] if f["level"] == "error"]
            layout = [f for f in errors if f["code"] in LAYOUT_ERRORS]
            copy = [f for f in errors if f["code"].startswith("COPY_")]
            glyphs = [f for f in errors if f["code"] == "GLYPH_MISSING"]
            warns = [f for r in rows for f in r["findings"] if f["level"] == "warn"]

            if name == "tofu":
                ok = bool(glyphs)
                print(f"{name:8} 缺字形护栏 {'✓ 自检抓到了' if ok else '✗ 没抓到（自检失效）'}")
            elif name == "thin":
                # 这张卡版面本身是干净的，唯一的错就是「太空」—— 自检必须自己说出来，
                # 否则「版面通过」就成了「内容很薄也放行」。
                thin = [f for r in rows for f in r["findings"] if f["code"] == "THIN_CARD"]
                ok = not layout and bool(thin)
                print(f"{name:8} 太空护栏 {'✓ 自检抓到了' if thin else '✗ 没抓到（自检失效）'}"
                      f"{'  · 还有 ' + str(len(layout)) + ' 个版面错误' if layout else ''}")
            elif name == "messy":
                # 这张卡的 payload 类型是坏的：渲染器必须自己救回来并说出来，
                # 而不是渲染出单字痛点、或者干脆崩掉。
                fixed = [f for r in rows for f in r["findings"] if f["code"] == "PAYLOAD_FIXED"]
                ok = not layout and bool(fixed)
                print(f"{name:8} 类型护栏 {'✓ 自检抓到了 ' + str(len(fixed)) + ' 处修正' if fixed else '✗ 没抓到（自检失效）'}"
                      f"{'  · 还有 ' + str(len(layout)) + ' 个版面错误' if layout else ''}")
            else:
                ok = not layout
                scaled = sum(1 for r in rows if r["scale"] < 1.0)
                tail = f"{scaled}/{len(rows)} 张自动缩了字号" if scaled else ""
                print(f"{name:8} {len(rows) - len({r['style'] for r in rows if any(f['code'] in LAYOUT_ERRORS for f in r['findings'])})}"
                      f"/{len(rows)} 套版面干净"
                      f"{'  · ' + tail if tail else ''}"
                      f"{'  · ' + str(len(copy)) + ' 条文案提示' if copy else ''}"
                      f"{'  · ' + str(len(warns)) + ' 条可读性提醒' if warns else ''}"
                      f"  {'✓' if ok else '✗'}")
                for r in rows:
                    bad = [f for f in r["findings"] if f["level"] == "error"
                           and (f["code"] in LAYOUT_ERRORS or f["code"] == "GLYPH_MISSING")]
                    for f in bad:
                        print(f"         {r['style']}: {f['code']} {f['message']}")
            if not ok:
                report["ok"] = False
            report["fixtures"].append({"name": name, "note": note, "ok": ok,
                                       "styles": rows})
    finally:
        tmpdir.cleanup()

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n明细: {args.json}")

    if args.sheet:
        make_sheet(json.loads((HERE / "payloads/stress-long.json").read_text(encoding="utf-8")),
                   Path(args.sheet))

    print()
    print("结果:", "通过" if report["ok"] else "失败")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())