#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文案体检 —— 在渲染之前，把「文案里说的话」和「payload 里的数据」对一遍。

gh-zh-card 的设计原则第一条是「数字必须真」。但昨天的自检只管版面：卡片有没有排坏，
它管不了文案里那句「22 天涨到两万六千星」和同一张卡页脚上的「★ 27.6k」是不是一回事。

这个工具补的就是这一块，只做机械能判断的部分：

  数字     文案里的星数 / 推送天数 / 许可证，与 payload（或 --online 抓的实时数据）核对
  结构     缺字段、start 带 $、tagline 没中文、痛点写成功能清单、痛点条数不对
  夸大     「最强 / 史上最 / 颠覆 / 绝对」这类当场能被证伪的词
  长度     超预算时直接算给你看：这张卡会被缩到多少字号、会不会截断
  时效     payload 里的数字是什么时候抓的（--max-age），文案里的「今天」还算不算今天

它不判断「文案有没有说到点上」——那要读 README，机器做不了。

用法：
    python copy_check.py payloads/pinky.json                  # 单张
    python copy_check.py daily/2026-11-19/                    # 整个目录
    python copy_check.py collection/collection.json           # 合集
    python copy_check.py daily/2026-11-19/ --online           # 顺便抓实时数据核对
    python copy_check.py --selftest                           # 自检这个工具本身
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    sys.exit("需要 Pillow：pip install pillow")

import gh_card  # noqa: E402

# 预算：来自 SKILL.md「文案质量标准」
BUDGET = {"tagline": 34, "pain": 38, "for_who": 30, "verdict": 50}

# 当场能被证伪的夸大词。注意「天花板」「第一版」「唯一」这类要么是术语、要么常常是事实
# 陈述，不放进禁用表 —— 宁可漏报，也不要让真话被拦。
BANNED = ["最强", "最好", "史上最", "史无前例", "颠覆", "绝对", "万能", "完美",
          "无与伦比", "碾压", "必装", "堪称", "宇宙第一", "吊打"]

# 功能清单的典型开头（SKILL.md 的反面教材就是「支持截图 / 支持点击」）
FEATURE_VERBS = ("支持", "提供", "包含", "采用", "基于", "集成", "内置", "具有",
                 "能够", "可以", "实现了", "兼具")

CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}

STAR_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?\s*[kKmM万]?|[零一二两三四五六七八九十百千万亿]+)\s*(?:颗)?星")
PUSH_RE = re.compile(r"([0-9]+|[零一二两三四五六七八九十百]+)\s*天\s*(?:没|没有|未)\s*(?:推送|更新|提交|动)")
# 许可证名必须整词匹配：不然 example 里的 "mpl"、commit 里的 "mit" 都会被当成许可证
LICENSE_RE = re.compile(
    r"(?<![A-Za-z0-9])(MIT|Apache(?:-?2\.0)?|AGPL(?:-?3\.0)?|GPL(?:-?3\.0)?|BSD(?:-?3)?|"
    r"MPL(?:-?2\.0)?|Unlicense|未标注|无许可证|没有许可证|非开源|自定义)(?![A-Za-z0-9])",
    re.I)
REL_TIME_RE = re.compile(r"(今天|昨天|前天|本周|最近\s*(\d+)?\s*天)")


# ---------------------------------------------------------------- 数字解析
def parse_number(s: str) -> int | None:
    """把 '两万六千' / '32 万' / '26000' / '1.2k' 统一解析成整数。"""
    s = s.replace(" ", "")
    if not s:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM万]?)", s)
    if m:
        v = float(m.group(1))
        mul = {"": 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "万": 1e4}[m.group(2)]
        return int(round(v * mul))
    total = section = num = 0
    for ch in s:
        if ch.isdigit():
            num = num * 10 + int(ch)
        elif ch in CN_DIGIT:
            num = CN_DIGIT[ch]
        elif ch in CN_UNIT:
            unit = CN_UNIT[ch]
            if unit >= 10000:
                total += (section + num) * unit
                section, num = 0, 0
            else:
                section += (num or 1) * unit
                num = 0
        else:
            return None
    return total + section + num


def fmt_num(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


# ---------------------------------------------------------------- 数据结构
class Finding:
    __slots__ = ("level", "code", "message", "detail")

    def __init__(self, level: str, code: str, message: str, detail: dict | None = None):
        self.level, self.code, self.message = level, code, message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code,
                "message": self.message, "detail": self.detail}


def _copy_text(payload: dict) -> str:
    parts = [payload.get("tagline") or "", payload.get("for_who") or "",
             payload.get("start") or "", payload.get("verdict") or ""]
    parts += [p for p in (payload.get("pain") or []) if p]
    return " ".join(parts)


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - dt).days


# ---------------------------------------------------------------- 单项检查
def lint(payload: dict, live: dict | None = None,
         now: datetime | None = None, max_age: int | None = None) -> list[Finding]:
    now = now or datetime.now(timezone.utc)
    out: list[Finding] = []

    def add(level, code, msg, detail=None):
        out.append(Finding(level, code, msg, detail))

    # 0) 先过类型体检：payload 是 agent 写的，`"pain": "一句话"`、`"verdict": ["…"]`、
    #    `"stars": null` 都很常见。不先修形状，下面的规则会拿字符/列表去比较，
    #    要么误报，要么直接崩（`sequence item 3: expected str instance, list found`）。
    payload, p_fixes, p_blocked = gh_card.normalize_payload(payload)
    for n in p_fixes:
        add("info" if n.get("kind") == "typo" else "warn", "W_PAYLOAD_FIXED",
            n["message"] + "（渲染器也会这么修，但源头最好写对）", n)
    for n in p_blocked:
        add("error", "E_PAYLOAD_BROKEN", n["message"], n)

    text = _copy_text(payload)

    # 1) 基本字段
    for field in ("owner", "repo", "name_zh", "tagline"):
        if not str(payload.get(field) or "").strip():
            add("error", "E_FIELD_MISSING", f"缺字段 `{field}`，这张卡是残的", {"field": field})

    # 2) 夸大词
    hits = [w for w in BANNED if w in text]
    if hits:
        add("error", "E_BANNED_WORD",
            f"用了当场能被证伪的词：{'、'.join(hits)}（写出能被验证的结论，不写形容词）",
            {"words": hits})

    # 3) 结构：命令块不带 $，tagline 要有中文，痛点别写成功能清单
    start = str(payload.get("start") or payload.get("install") or "")
    if start.startswith("$"):
        add("error", "E_DOLLAR_START", f"start 不要带 `$`（脚本会自己加）：{start[:30]!r}",
            {"start": start})
    tagline = str(payload.get("tagline") or "")
    if tagline and not any("\u4e00" <= ch <= "\u9fff" for ch in tagline):
        add("error", "E_TAGLINE_NO_CJK",
            f"tagline 里没有中文，等于没翻译：{tagline[:30]!r}", {"tagline": tagline})

    pains = [p for p in (payload.get("pain") or []) if p]
    if len(pains) < 3:
        add("warn", "W_PAIN_COUNT",
            f"痛点只有 {len(pains)} 条（目标是 3 条；少一条就少一个具体的场景）",
            {"count": len(pains)})
    for p in pains:
        if p.startswith(FEATURE_VERBS):
            add("warn", "W_PAIN_FEATURE",
                f"这条读起来像功能清单，不像痛：{p[:30]!r}", {"pain": p})
    for i in range(len(pains)):
        for j in range(i + 1, len(pains)):
            a, b = set(pains[i]), set(pains[j])
            if a and b and len(a & b) / min(len(a), len(b)) > 0.7:
                add("warn", "W_PAIN_DUP",
                    f"第 {i + 1} 条和第 {j + 1} 条痛点几乎一样", {"i": i + 1, "j": j + 1})

    verdict = str(payload.get("verdict") or "")
    if not verdict:
        add("warn", "W_VERDICT_EMPTY", "没有 verdict —— 卡片最后没有回答「该不该用」")
    elif len(verdict) < 12:
        add("warn", "W_VERDICT_SHORT", f"verdict 只有 {len(verdict)} 字，等于没说", {"len": len(verdict)})

    # 4) 长度预算
    for field, limit in (("tagline", BUDGET["tagline"]), ("for_who", BUDGET["for_who"]),
                         ("verdict", BUDGET["verdict"])):
        n = len(str(payload.get(field) or ""))
        if n > limit:
            add("warn", "W_TOO_LONG",
                f"{field} {n} 字，超过 {limit} 字的预算（多 {n - limit} 字）",
                {"field": field, "len": n, "budget": limit})
    for idx, p in enumerate(pains, 1):
        if len(p) > BUDGET["pain"]:
            add("warn", "W_TOO_LONG",
                f"第 {idx} 条痛点 {len(p)} 字，超过 {BUDGET['pain']} 字的预算",
                {"field": f"pain[{idx}]", "len": len(p), "budget": BUDGET["pain"]})

    # 5) 排版后果：长度超预算到底会怎样，直接算出来
    scale, skipped, truncated = predict_layout(payload)
    if skipped or truncated:
        add("error", "E_COPY_OVERFLOW",
            f"按当前长度排版（按 obsidian 估）会被截断/省略：{'、'.join(skipped) or '结论'} —— 必须改文案",
            {"skipped": skipped, "truncated": truncated})
    elif scale < 1.0:
        add("warn", "W_LAYOUT_SHRINK",
            f"按当前长度排版（按 obsidian 估），字号会被缩到 {scale:.0%}（仍能发，但正文变小）",
            {"scale": scale})

    # 6) 数字：来源、时效、与文案的对齐
    data = dict(payload)
    if live:  # --online：实时数据优先，payload 的旧数字只用来算漂移
        for k in ("stars", "language", "license", "updated", "created_at"):
            if live.get(k) is not None:
                data[k] = live[k]

    stats_stamp = payload.get("stats_fetched_at")
    age = _days_since(stats_stamp, now) if stats_stamp else None
    if payload.get("stars") is None and not live:
        add("info", "I_NO_STATS",
            "这张卡没有记录 stars —— 没法核对文案里的数字，也没法离线复核", {})
    elif stats_stamp is None:
        add("info", "I_NO_STAMP",
            "数据没有记录抓取时间（可用 --fetch 重新抓，或加 stats_fetched_at）", {})
    elif age is not None and max_age is not None and age > max_age:
        add("warn", "W_STATS_STALE",
            f"数据是 {age} 天前抓的，超过 {max_age} 天；星数可能已经变了",
            {"age_days": age})

    stars = payload.get("stars")
    if live and stars is not None and live.get("stars") is not None:
        drift = (live["stars"] - int(stars)) / max(1, int(stars))
        if abs(drift) > 0.05:
            add("warn", "W_STARS_DRIFT",
                f"payload 星数 {stars} 与实时 {live['stars']} 差 {drift:+.0%}，建议重新抓",
                {"payload_stars": stars, "live_stars": live["stars"]})

    if data.get("stars") is not None:
        base_stars = int(data["stars"])
        for m in STAR_RE.finditer(text):
            claim = parse_number(m.group(1))
            if claim is None:
                continue
            if abs(claim - base_stars) / max(1, base_stars) > 0.05:
                where = "实时数据" if live else "payload"
                add("warn", "W_STAR_CLAIM",
                    f"文案写「{m.group(0)}」（≈{fmt_num(claim)}），{where}是 {fmt_num(base_stars)}"
                    f"（差 {abs(claim - base_stars) / max(1, base_stars):.0%}）。"
                    f"如果写的是「现在有」请改；如果是「涨到过」这种历史值，确认一下再保留",
                    {"claim": m.group(0), "claim_value": claim, "actual": base_stars})

    days = _days_since(data.get("updated"), now)
    for m in PUSH_RE.finditer(text):
        claim = parse_number(m.group(1))
        if claim is None or days is None:
            continue
        if abs(claim - days) > 3:
            add("warn", "W_PUSH_CLAIM",
                f"文案写「{m.group(0)}」，但 updated 是 {days} 天前（差 {abs(claim - days)} 天）",
                {"claim": m.group(0), "claim_days": claim, "actual_days": days})

    lic = str(data.get("license") or "")
    for m in LICENSE_RE.finditer(text):
        claimed = m.group(1).lower().replace(" ", "").replace("-", "")
        actual = lic.lower().replace(" ", "").replace("-", "")
        ok = True
        if claimed.startswith("apache"):
            ok = actual.startswith("apache")
        elif claimed in ("mit", "gpl", "agpl", "bsd", "mpl", "unlicense"):
            ok = claimed in actual
        elif claimed in ("未标注", "无许可证", "没有许可证"):
            ok = actual in ("", "未标注", "none", "noassertion")
        elif claimed in ("非开源", "自定义"):
            ok = ("非开源" in actual) or ("自定义" in actual) or actual in ("", "未标注")
        if not ok:
            add("error", "E_LICENSE_CLAIM",
                f"文案说「{m.group(1)}」，但 license 是 {lic or '（空）'!r}",
                {"claim": m.group(1), "license": lic})

    for m in REL_TIME_RE.finditer(text):
        if days is not None and days > 3 and m.group(1) in ("今天", "昨天", "前天"):
            add("warn", "W_REL_TIME",
                f"文案用了「{m.group(1)}」，但这张卡的数据已经是 {days} 天前的了",
                {"phrase": m.group(1), "days": days})

    order = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: (order[f.level], f.code))
    return out


def predict_layout(payload: dict, style_id: str = "obsidian") -> tuple[float, list[str], list[str]]:
    """用真正的排版引擎算：会缩到多少字号、会省掉什么、结论会不会被截断。"""
    try:
        style = gh_card.load_style(style_id)
        d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        p = {"owner": payload.get("owner") or "x", "repo": payload.get("repo") or "y", **payload}
        ops, scale, skip, bottom = gh_card.fit_content(d, p, style)
        _, info = gh_card.plan_verdict(d, p, style, bottom)
        skipped = [gh_card.SKIP_LABELS.get(s, s) for s in skip]
        truncated = [f"{info.get('field')} 少 {len(info.get('dropped') or '')} 字"] if info else []
        return float(scale), skipped, truncated
    except Exception:  # noqa: BLE001 - 体检工具不该因为排版算不出来就崩
        return 1.0, [], []


# ---------------------------------------------------------------- 报告
def report(results: dict[str, list[Finding]], online: bool = False) -> str:
    errors = sum(1 for fs in results.values() for f in fs if f.level == "error")
    warns = sum(1 for fs in results.values() for f in fs if f.level == "warn")
    infos = sum(1 for fs in results.values() for f in fs if f.level == "info")

    lines = []
    for name, findings in results.items():
        noisy = [f for f in findings if f.level in ("error", "warn")]
        if not noisy:
            continue
        lines.append(name)
        icon = {"error": "  [错误]", "warn": "  [提醒]"}
        for f in noisy:
            lines.append(f"{icon[f.level]} {f.message}")
        lines.append("")

    # 信息类按 code 汇总，不然一批卡片会刷出一堆重复行
    by_code: dict[str, list[str]] = {}
    for name, findings in results.items():
        for f in findings:
            if f.level == "info":
                by_code.setdefault(f.code, []).append(name)
    if by_code:
        lines.append("附带信息：")
        for code, names in by_code.items():
            sample = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
            lines.append(f"  [{code}] {len(names)} 份：{sample}")
        lines.append("")

    clean = [n for n, fs in results.items() if not any(f.level != "info" for f in fs)]
    if clean:
        head = "、".join(clean[:5]) + ("…" if len(clean) > 5 else "")
        lines.append(f"没有问题的：{len(clean)} 份（{head}）")
        lines.append("")
    lines.append(f"汇总：{len(results)} 份 payload · {errors} 个错误 / {warns} 个提醒 / {infos} 条信息")
    if online:
        lines.append("（数字已与 GitHub API 实时数据核对过）")
    lines.append("说明：这个工具只判断机械可判的部分；文案有没有说到点上，还得人读 README。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 载入
def load_payloads(args: list[str]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            files = sorted(Path(f) for f in glob.glob(str(p / "**/*.json"), recursive=True))
        else:
            files = [p]
        for f in files:
            if f.name in ("candidates.json", "_featured.json", "stats.json"):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "cards" in data and isinstance(data["cards"], list):
                # 合集：collection.json 里的每张卡
                for c in data["cards"]:
                    if not isinstance(c, dict):
                        continue
                    name = f"issue-{c.get('issue', '?')} {c.get('owner', '?')}/{c.get('repo', '?')}"
                    out.append((name, c))
            elif isinstance(data, dict) and (data.get("repo") or data.get("tagline")):
                out.append((f.stem if f.name != "collection.json" else f.name, data))
    return out


def fetch_live(slug: str, session) -> dict | None:
    if "/" not in slug:
        return None
    try:
        r = session.get(f"https://api.github.com/repos/{slug}", timeout=20)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    d = r.json()
    lic = (d.get("license") or {}).get("spdx_id")
    if not lic or lic == "NOASSERTION":
        lic = "未标注"
    return {"stars": d.get("stargazers_count"), "language": d.get("language") or "—",
            "license": lic, "updated": d.get("pushed_at"), "created_at": d.get("created_at")}


# ---------------------------------------------------------------- 自检
GOOD = {
    "owner": "DIYgod", "repo": "pinky", "url": "https://github.com/DIYgod/pinky",
    "name_zh": "Pinky",
    "tagline": "让任何 AI agent 隔着电脑操作你的 iPhone / Android，而且每一步你都能插手",
    "pain": ["手机上最烦的是重复动作：点确认、翻列表、回消息。脚本能跑，但出事没人兜底",
             "它把「agent 操作手机」和「人随时接管」缝在一起：支付、发消息这类动作默认要你批准",
             "批准逻辑放在网关里强制执行，不是写在提示词里求 agent 自觉"],
    "for_who": "想让 AI 接管手机上的重复劳动，但不敢把手机完全交出去的人",
    "start": "去 Releases 下载安装 → 设置 › Agents 一键接上 Claude Code / Codex / Cursor",
    "verdict": "手机自动化方案里，把「人随时能接管」做得最认真的一个。",
    "stars": 2059, "language": "Go", "license": "Apache-2.0",
    "updated": "2026-11-13T09:00:00Z", "topics": [],
}
BAD = {
    "owner": "demo", "repo": "bad-copy", "url": "https://github.com/demo/bad-copy",
    "name_zh": "bad-copy",
    "tagline": "An MCP-based framework for phone automation",
    "pain": ["支持截图", "支持点击", "支持滚动"],
    "for_who": "开发者",
    "start": "$ pip install bad-copy",
    "verdict": "绝对是你见过最强的工具，史上第一，颠覆行业。",
    "stars": 1000, "language": "Python", "license": "MIT",
    "updated": "2026-01-01T00:00:00Z", "topics": [],
}
WRONG_NUMBER = {**GOOD, "stars": 27600, "updated": "2026-11-18T00:00:00Z",
                "verdict": "22 天涨到两万六千星的工具，58 天没推送还在更新。"}


def selftest() -> int:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    cases = [
        ("好文案", GOOD, None, set()),
        ("反面教材", BAD, None, {"E_BANNED_WORD", "E_DOLLAR_START", "E_TAGLINE_NO_CJK",
                                 "W_PAIN_FEATURE"}),
        ("数字对不上", WRONG_NUMBER, None, {"W_STAR_CLAIM", "W_PUSH_CLAIM"}),
        ("星数漂移（在线）", {**GOOD, "stats_fetched_at": "2026-11-01T00:00:00Z"},
         {"stars": 4000}, {"W_STARS_DRIFT", "W_STATS_STALE"}),
    ]
    ok = True
    print("copy_check 自检")
    for name, payload, live, expect in cases:
        findings = lint(payload, live=live, now=now, max_age=7)
        codes = {f.code for f in findings}
        missing = expect - codes
        unexpected_errors = {f.code for f in findings if f.level == "error"} - expect if name == "好文案" else set()
        good = not missing and not unexpected_errors
        ok = ok and good
        print(f"  {name:12} {'✓' if good else '✗'}  {sorted(codes)}")
        if not good:
            for f in findings:
                print(f"       {f.level} {f.code}: {f.message}")
            if missing:
                print(f"       缺少预期：{sorted(missing)}")

    # 类型写错（agent 写 JSON 时最常见的一种错）：不先修形状，下面所有规则都会拿字符去比
    messy = {**GOOD, "pain": "第一条痛点写成一整段话；第二条也塞在同一行；第三条用分号隔开",
             "verdict": ["结论被写成了列表", "应该用逗号接回一句"],
             "stars": None, "topics": "payload, guard"}
    clean, fixes, blocked = gh_card.normalize_payload(messy)
    shape_checks = [
        ("pain 是字符串 → 拆成 3 条", len(clean["pain"]) == 3),
        ("verdict 是列表 → 接成一句", isinstance(clean["verdict"], str) and "，" in clean["verdict"]),
        ("stars 是 null → 留空（页脚写「—」）", clean["stars"] is None),
        ("topics 是字符串 → 拆成 2 个", clean["topics"] == ["payload", "guard"]),
        ("每一处都记下来了", len(fixes) >= 3),
        ("没有误判成坏 payload", not blocked),
    ]
    bad_shape = [name for name, good in shape_checks if not good]
    ok = ok and not bad_shape
    print(f"  {'类型写错':12} {'✓' if not bad_shape else '✗'}  "
          f"修正 {len(fixes)} 处 / 拦截 {len(blocked)} 处")
    for name in bad_shape:
        print(f"       ✗ {name}")

    # 救不了的形状必须明确拦住，不能渲染出坏卡
    _, _, blocked2 = gh_card.normalize_payload({**GOOD, "pain": {"a": "b"}})
    _, _, blocked3 = gh_card.normalize_payload(["这不是对象"])
    good = bool(blocked2) and bool(blocked3)
    ok = ok and good
    print(f"  {'坏形状拦截':10} {'✓' if good else '✗'}  "
          f"pain 写成对象 / payload 不是对象 都被拦住")

    print("\n结果:", "通过" if ok else "失败")
    return 0 if ok else 1

# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="渲染前体检：文案里的说法 vs payload 里的数据")
    ap.add_argument("targets", nargs="*", help="payload JSON / 目录 / collection.json")
    ap.add_argument("--online", action="store_true", help="打 GitHub API 抓实时数据一起核对")
    ap.add_argument("--max-age", type=int, default=None, help="数据超过 N 天就提醒")
    ap.add_argument("--json", help="把结果写成 JSON")
    ap.add_argument("--selftest", action="store_true", help="跑工具自带的自检用例")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.targets:
        ap.error("需要至少一个 payload / 目录 / collection.json")

    payloads = load_payloads(args.targets)
    if not payloads:
        raise SystemExit("没找到 payload（.json）")

    session = None
    if args.online:
        import requests
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"Accept": "application/vnd.github+json",
                                "User-Agent": "gh-zh-card-copy-check"})

    results: dict[str, list[Finding]] = {}
    for name, payload in payloads:
        live = None
        if session is not None:
            slug = f"{payload.get('owner', '')}/{payload.get('repo', '')}".strip("/")
            live = fetch_live(slug, session)
        try:
            results[name] = lint(payload, live=live, max_age=args.max_age)
        except Exception as e:  # noqa: BLE001 - 一份坏 payload 不该拖垮整批
            results[name] = [Finding("error", "E_CRASH", f"体检时出错：{type(e).__name__}: {e}")]

    print(report(results, online=args.online))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {n: [f.as_dict() for f in fs] for n, fs in results.items()},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n明细: {args.json}")
    return 1 if any(f.level == "error" for fs in results.values() for f in fs) else 0


if __name__ == "__main__":
    sys.exit(main())