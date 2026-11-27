#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令出卡：文案体检 -> 渲染 -> 版面自检 -> 落一份卡片档案。

为什么要这个文件
----------------
出一次卡原来是三步三条命令：

    python copy_check.py p.json          # 文案 vs 数据
    python gh_card.py --payload p.json --offline --out out/ --md --caption
    python gh_card.py --payload p.json --offline --out out/ --check   # 又渲一遍

每一步都可以漏：漏了体检，编出来的数字就发出去了；漏了自检，压坏版面的图也发出去了。
而「漏掉的那一步」恰恰是出事的那一步。这里把它串成一道闸门，只有**全部过了**才说「可以发」。

顺序是有讲究的：
  · **文案体检在前，而且它不过就不渲染** —— 文案错的时候连图都不该存在，
    否则总会有人顺手把那张图发出去。
  · 版面自检在后 —— 它必须先有图；不过的时候图留着（要看得见才知道怎么改），
    但档案里写 `publishable: false`，退出码 1。

顺带解决另一件事：**这张卡的数字是什么时候的**。以前只有 payload 里有 `stats_fetched_at`，
26 份真实 payload 里一份都没有 —— 也就是说「这张卡上的星数现在还对不对」这个问题，
事后谁都回答不了。现在每张卡旁边落一份 `<name>.card.json`，记下数字、来源时间、
风格、尺寸和三个文件的 sha256。以后要复核、要重渲染、要查「当时是多少星」，都看它。

用法
----
    python card_build.py daily/2026-11-25/xcl-dev__xcl.json --style obsidian
    python card_build.py p.json --out out/ --name 01-slug --online      # 顺便核对实时数字
    python card_build.py p.json --json                                  # 机器可读的结论
    python card_build.py --selftest                                     # 自带用例

退出码：0 可以发；1 不能发（原因见输出）；2 用法/环境问题。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import card_check  # noqa: E402
import copy_check  # noqa: E402
import gh_card  # noqa: E402

SCHEMA = 1


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_dict(f) -> dict:
    """copy_check.Finding 用 __slots__、card_check.Finding 是 dataclass —— 两种都要能序列化。"""
    if hasattr(f, "as_dict"):
        return f.as_dict()
    return {k: v for k, v in vars(f).items()}


def build(payload_path: Path, style_id: str = "obsidian", out_dir: Path | None = None,
          name: str | None = None, online: bool = False, max_age: int | None = None,
          write: bool = True) -> dict:
    """出一张卡，返回结论字典（也写成 <name>.card.json）。"""
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    payload.setdefault("owner", "unknown")
    payload.setdefault("repo", "unknown")
    if "url" not in payload:
        payload["url"] = f"https://github.com/{payload['owner']}/{payload['repo']}"
    out_dir = Path(out_dir or Path(payload_path).parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = name or Path(payload_path).stem

    # ---- 1) 文案体检（不过就不渲染）
    live = None
    session = None
    if online:
        try:
            import requests

            session = requests.Session()
            session.trust_env = False
            session.headers.update({"Accept": "application/vnd.github+json",
                                    "User-Agent": "gh-zh-card-card-build"})
            live = copy_check.fetch_live(f"{payload['owner']}/{payload['repo']}", session)
        except Exception as e:  # noqa: BLE001 - 联网核对是加分项，不该拖垮出卡
            print(f"[提示] 没能联网核对数字（{type(e).__name__}），按离线结论继续", file=sys.stderr)
    copy_findings = copy_check.lint(payload, live=live, max_age=max_age)
    copy_errors = [f for f in copy_findings if f.level == "error"]

    result = {
        "schema": SCHEMA,
        "stem": stem,
        "payload": str(Path(payload_path)),
        "payload_sha256": _sha(Path(payload_path)),
        "style": {"id": style_id},
        "rendered_at": None,
        "files": {},
        "findings": {"copy": [_as_dict(f) for f in copy_findings], "layout": []},
        "publishable": False,
        "blocked_by": [f.code for f in copy_errors],
    }
    if copy_errors:
        result["reason"] = "文案体检没过，没有渲染（免得那张图被顺手发出去）"
        return result

    # ---- 2) 渲染 + 写 md / caption
    style = gh_card.load_style(style_id)
    result["style"]["name"] = style.get("name_zh") or style.get("id") or style_id
    audit = gh_card.Audit()
    png = out_dir / f"{stem}.png"
    gh_card.render(payload, png, style, audit=audit)
    md = out_dir / f"{stem}.md"
    md.write_text(gh_card.to_markdown(payload), encoding="utf-8")
    cap = out_dir / f"{stem}.caption.txt"
    cap.write_text(gh_card.to_caption(payload), encoding="utf-8")

    # ---- 3) 版面自检
    layout = card_check.check(audit, payload)
    result["findings"]["layout"] = [_as_dict(f) for f in layout]
    layout_errors = [f for f in layout if f.level == "error"]

    fetched_at = payload.get("stats_fetched_at")
    result.update({
        "rendered_at": _stamp(),
        "canvas": [gh_card.W, gh_card.H],
        "scale": audit.scale,
        "stats": {
            "stars": payload.get("stars"),
            "language": payload.get("language"),
            "license": payload.get("license"),
            "updated": payload.get("updated"),
            "fetched_at": fetched_at,
        },
        "stats_provenance": "fetched" if fetched_at else "unknown",
        "files": {suffix: {"path": str(p), "bytes": p.stat().st_size, "sha256": _sha(p)}
                  for suffix, p in (("png", png), ("md", md), ("caption", cap))},
        "publishable": not layout_errors,
        "blocked_by": [f.code for f in layout_errors],
    })
    if layout_errors:
        result["reason"] = "版面自检没过：图还在（要看得见才知道怎么改），但别发"
    else:
        result["reason"] = "可以发"
    if not fetched_at:
        result.setdefault("notes", []).append(
            "数字没有来源时间（payload 里没有 stats_fetched_at）：以后复核不了它准不准")

    manifest = out_dir / f"{stem}.card.json"
    result["manifest"] = str(manifest)
    if write:
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return result


# ---------------------------------------------------------------- 输出
ICON = {"error": "错误", "warn": "提醒", "info": "信息"}


def render_report(res: dict) -> str:
    copy_f = res["findings"]["copy"]
    layout_f = res["findings"]["layout"]
    lines = [f"card_build · {res['stem']} · {res['style'].get('name') or res['style']['id']}"]

    def line(tag: str, findings: list) -> str:
        n = {"error": 0, "warn": 0}
        for f in findings:
            n[f["level"]] = n.get(f["level"], 0) + 1
        return f"  {tag:8} {n['error']} 错误 / {n['warn']} 提醒"

    lines.append(line("文案体检", copy_f))
    for f in copy_f:
        if f["level"] in ("error", "warn"):
            lines.append(f"      [{ICON[f['level']]}] {f['code']}: {f['message']}")
    if res.get("rendered_at"):
        lines.append(line("版面自检", layout_f))
        for f in layout_f:
            if f["level"] == "error":
                lines.append(f"      [{ICON[f['level']]}] {f['code']}: {f['message']}")

    if res["files"]:
        lines.append("  写出      " + " · ".join(
            f"{k} {v['bytes'] / 1024:.0f}KB" for k, v in res["files"].items()))
        lines.append(f"  档案      {Path(res['manifest']).name}")
    prov = res.get("stats_provenance")
    if prov:
        stars = res["stats"].get("stars")
        when = res["stats"].get("fetched_at") or "无时间戳"
        lines.append(f"  数字      ★ {gh_card.fmt_num(stars)}，来源时间 {when}"
                     + ("（无法复核）" if prov == "unknown" else ""))
    for note in res.get("notes", []):
        lines.append(f"  注意      {note}")

    lines.append("")
    if res["publishable"]:
        lines.append("结论：可以发。")
    else:
        lines.append(f"结论：不能发 —— {res.get('reason', '有错误')}"
                     + (f"（{', '.join(res['blocked_by'])}）" if res["blocked_by"] else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- 自检
def selftest() -> int:
    import tempfile

    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {name:38} {'✓' if cond else '✗'}  {'' if cond else detail}")

    print("card_build 自检")
    bad_copy = {
        "owner": "demo", "repo": "bad", "name_zh": "坏文案",
        "tagline": "An MCP-based framework for phone automation",   # 没中文
        "pain": ["支持截图", "支持点击", "支持滚动"],
        "start": "$ pip install bad",
        "verdict": "绝对是最强的，颠覆行业。",
        "stars": 1, "updated": "2026-11-20T00:00:00Z",
    }
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out"
        out.mkdir()
        good = {"owner": "demo", "repo": "good", "name_zh": "好卡",
                "tagline": "把一段英文 README 压成一张中文速览卡",
                "pain": ["读三千字还没看到重点", "看完不知道适不适合自己", "数字没人核对"],
                "for_who": "每天刷热榜的人", "start": "python card_build.py p.json",
                "verdict": "先体检再出图，漏一步就不给发。",
                "stars": 1234, "language": "Python", "license": "MIT",
                "updated": "2026-11-24T00:00:00Z", "stats_fetched_at": "2026-11-25T00:00:00Z"}
        payloads = {"good": good, "bad": bad_copy}
        paths = {}
        for k, v in payloads.items():
            p = Path(td) / f"{k}.json"
            p.write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
            paths[k] = p

        good_res = build(paths["good"], "obsidian", out)
        check("好文案 -> 可以发", good_res["publishable"] and good_res["reason"] == "可以发")
        check("写出 png / md / caption / card.json",
              all(k in good_res["files"] for k in ("png", "md", "caption"))
              and Path(good_res["manifest"]).exists())
        check("档案记了数字来源", good_res["stats_provenance"] == "fetched"
              and good_res["stats"]["fetched_at"] == "2026-11-25T00:00:00Z")
        check("档案里有 sha256", len(good_res["files"]["png"]["sha256"]) == 64)

        bad_res = build(paths["bad"], "obsidian", out)
        check("文案有问题 -> 不能发", not bad_res["publishable"])
        check("文案不过就不出图", not (out / "bad.png").exists())
        check("拦它的原因有 E_BANNED_WORD", "E_BANNED_WORD" in bad_res["blocked_by"],
              str(bad_res["blocked_by"]))

        # 版面问题：这张卡能过文案（三观端正），但只给一句定位 —— 应该报 THIN_CARD（提醒，不拦）
        thin = {"owner": "demo", "repo": "thin", "name_zh": "薄卡",
                "tagline": "只有一句定位的卡", "verdict": "版面干净但内容太薄。",
                "stars": 5, "updated": "2026-11-24T00:00:00Z"}
        tp = Path(td) / "thin.json"
        tp.write_text(json.dumps(thin, ensure_ascii=False), encoding="utf-8")
        thin_res = build(tp, "obsidian", out)
        codes = [f["code"] for f in thin_res["findings"]["layout"]]
        check("太薄的卡：提醒但不拦", thin_res["publishable"] and "THIN_CARD" in codes, str(codes))
        check("太薄的卡会写进 notes？(数字无来源提示)",
              any("来源时间" in n for n in thin_res.get("notes", [])))

        # 类型写坏：渲染器就地修，档案记为可发，但 findings 里要留下痕迹
        messy = dict(good, pain="第一条；第二条；第三条", stars=None, verdict=["结论一", "结论二"])
        mp = Path(td) / "messy.json"
        mp.write_text(json.dumps(messy, ensure_ascii=False), encoding="utf-8")
        messy_res = build(mp, "obsidian", out)
        fixed = [f["code"] for f in messy_res["findings"]["layout"]]
        check("类型写坏：修好后可发", messy_res["publishable"], str(messy_res["blocked_by"]))
        check("类型写坏：档案里记了 PAYLOAD_FIXED", "PAYLOAD_FIXED" in fixed, str(fixed))
        check("stars 未知时写「—」而不是 None", messy_res["stats"]["stars"] is None
              and gh_card.fmt_num(messy_res["stats"]["stars"]) == "—")

        # 缺字段：文案体检拦住，不出图
        p2 = Path(td) / "nofield.json"
        p2.write_text(json.dumps({"owner": "d", "repo": "r", "name_zh": "没 tagline"},
                                 ensure_ascii=False), encoding="utf-8")
        nf = build(p2, "obsidian", out)
        check("缺 tagline -> 不能发且不出图",
              not nf["publishable"] and "E_FIELD_MISSING" in nf["blocked_by"]
              and not (out / "nofield.png").exists())

        # 报告可读性：结论那句话必须在
        text = render_report(good_res)
        check("报告里有结论行", "结论：可以发" in text)
        check("报告里点明了数字来源", "来源时间" in text)

        # 输出目录不存在时自己建（第一次出卡最常见的用法）
        deep = Path(td) / "a" / "b" / "c"
        deep_res = build(paths["good"], "obsidian", deep, name="deep")
        check("输出目录不存在也能出卡", deep_res["publishable"] and (deep / "deep.png").exists())

    print("\n结果:", "通过" if ok else "失败")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="一条命令出卡：体检 -> 渲染 -> 自检 -> 卡片档案")
    ap.add_argument("payload", nargs="?", help="payload.json")
    ap.add_argument("--style", default="obsidian", help="风格 id（默认 obsidian）")
    ap.add_argument("--out", help="输出目录（默认与 payload 同目录）")
    ap.add_argument("--name", help="输出文件名（默认取 payload 文件名）")
    ap.add_argument("--online", action="store_true", help="顺便打 API 核对文案里的数字")
    ap.add_argument("--max-age", type=int, help="数字超过 N 天就提醒（配合 --online）")
    ap.add_argument("--json", dest="as_json", action="store_true", help="输出机器可读的结论")
    ap.add_argument("--selftest", action="store_true", help="跑自带用例")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.payload:
        ap.error("需要 payload.json（或用 --selftest）")

    res = build(Path(args.payload), args.style, Path(args.out) if args.out else None,
                args.name, args.online, args.max_age)
    if args.as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(render_report(res))
    return 0 if res["publishable"] else 1


if __name__ == "__main__":
    sys.exit(main())
