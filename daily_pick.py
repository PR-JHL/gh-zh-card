#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日 GitHub 精选：抓当天值得一看的候选项目，供出卡用。

这一步**只做数据**，不写中文文案——文案必须由读过 README 的 agent 写，
否则卡片就会变成「机器翻译的功能表」，那正是这个项目要避免的东西。

用法：
  python daily_pick.py --count 5                  # 抓候选，写到 daily/<今天>/candidates.json
  python daily_pick.py --count 5 --no-readme      # 跳过 README（更快）
  python daily_pick.py --list                     # 看历史出过的项目

抓完以后，agent 读 candidates.json，为每个项目写 payload.json，再调用：
  python gh_card.py --payload daily/<date>/<slug>.json --offline --out daily/<date>/ --style <id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
STATE = HERE / "daily" / "_featured.json"
PREFS = HERE / "daily" / "_prefs.json"
UA = {"Accept": "application/vnd.github+json", "User-Agent": "gh-zh-card-daily"}

# 默认偏好：只影响「挑哪些候选」，不影响文案。
# 第一次运行会把这份默认写成 daily/_prefs.json，之后改那个文件即可。
DEFAULT_PREFS = {
    "$note": "exclude = 不要的项目（整词命中 topic/描述/仓库名就跳过）；boost = 多找的项目"
             "（同一类里排前面）。自己改这个文件，daily_pick.py 下次运行就按新偏好挑。",
    "exclude": ["video", "videos", "clipping", "clip", "clips", "video editor", "livestream",
                "subtitle", "subtitles", "film", "font", "fonts", "typeface", "typefaces"],
    "boost": ["agent", "agent-skill", "claude-code", "llm", "mcp",
              "prototype", "playground", "interaction", "experiment", "playable",
              "cli", "terminal", "tui", "developer tools", "devtool", "debugger", "compiler",
              "database", "sql", "postgres", "sqlite", "redis", "storage engine",
              "infrastructure", "devops", "kubernetes", "self hosted",
              "frontend", "react", "svelte", "css", "design system", "typescript", "wasm", "webgpu",
              "embedded", "firmware", "arduino", "esp32", "raspberry pi", "hardware", "robotics"],
}

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
S = requests.Session()
S.trust_env = False
if TOKEN:
    S.headers.update({**UA, "Authorization": f"Bearer {TOKEN}"})
else:
    S.headers.update(UA)


def api(path: str, params: dict | None = None, tries: int = 3):
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=40)
            if r.status_code == 200:
                return r.json(), r.headers
            if r.status_code in (403, 404):
                return {"__err": r.status_code}, r.headers
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2 * (i + 1))
    return {"__err": "net"}, {}


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"featured": {}, "runs": []}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def load_prefs() -> dict:
    """读 daily/_prefs.json；不存在就按默认偏好建一份，方便直接改。"""
    prefs = dict(DEFAULT_PREFS)
    if PREFS.exists():
        try:
            data = json.loads(PREFS.read_text(encoding="utf-8"))
        except ValueError:
            print("  [警告] daily/_prefs.json 不是合法 JSON，这次按默认偏好挑", file=sys.stderr)
            return prefs
        for key in ("exclude", "boost"):
            if isinstance(data.get(key), list):
                prefs[key] = [str(w) for w in data[key] if str(w).strip()]
    else:
        PREFS.parent.mkdir(parents=True, exist_ok=True)
        PREFS.write_text(json.dumps(prefs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  [偏好] 已生成 {PREFS}（默认排除视频/剪辑类，偏好 agent / LLM / 交互原型 / CLI）")
    return prefs


def _norm(text: str) -> str:
    """小写 + 把 - _ / 等分隔符归一成空格，方便按整词匹配（cli 不该命中 client）。"""
    return " ".join(re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", (text or "").lower()).split())


def _token_re(token: str) -> str:
    t = _norm(token)
    if not t:
        return ""
    left = r"(?<![a-z0-9])" if t[0].isalnum() else ""
    right = r"(?![a-z0-9])" if t[-1].isalnum() else ""
    return left + re.escape(t) + right


def _haystack(p: dict) -> str:
    return _norm(" ".join([p.get("full_name") or "", p.get("description") or "",
                           " ".join(p.get("topics") or [])]))


def _hits(p: dict, words: list) -> list:
    hay = _haystack(p)
    out = []
    for w in words:
        rx = _token_re(w)
        if rx and re.search(rx, hay):
            out.append(w)
    return out


def is_excluded(p: dict, prefs: dict) -> list:
    return _hits(p, prefs.get("exclude") or [])


def is_boosted(p: dict, prefs: dict) -> bool:
    return bool(_hits(p, prefs.get("boost") or []))


def search(q: str, sort: str, per_page: int = 30) -> list:
    data, hdr = api("/search/repositories", {
        "q": q, "sort": sort, "order": "desc", "per_page": per_page})
    if isinstance(data, dict) and "__err" in data:
        print(f"  [警告] 搜索失败 {data}", file=sys.stderr)
        return []
    return data.get("items", [])


def readme_snippet(full: str, chars: int = 1800) -> str:
    r = S.get(f"https://api.github.com/repos/{full}/readme",
              headers={"Accept": "application/vnd.github.raw"}, timeout=40)
    if r.status_code != 200:
        return ""
    try:
        return r.text[:chars]
    except Exception:  # noqa: BLE001
        return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--days", type=int, default=7, help="只看最近 N 天新建/推送的")
    ap.add_argument("--no-readme", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只看挑中什么，不写文件、不记账")
    ap.add_argument("--no-prefs", action="store_true", help="忽略 daily/_prefs.json")
    args = ap.parse_args()

    st = load_state()
    prefs = {} if args.no_prefs else load_prefs()

    if args.list:
        for full, meta in sorted(st["featured"].items(), key=lambda kv: kv[1].get("date", "")):
            print(f"  {meta.get('date', '')}  {full}  (star {meta.get('stars', '?')})")
        print(f"\n共出过 {len(st['featured'])} 个项目")
        return

    since = (date.today() - timedelta(days=args.days)).isoformat()

    # 三个视角混合，避免全是同一类东西
    queries = [
        (f"created:>{since} stars:>30", "stars", "新建即热门"),
        (f"pushed:>{since} stars:>3000", "updated", "老项目新动向"),
        (f"created:>{since} stars:>5 fork:false", "stars", "小体量新项目"),
    ]

    # GitHub 的 owner/repo 不区分大小写，改个大小写就是同一个仓库，
    # 所以查重也统一转小写比较（仓库改名换大小写后会重新出现在搜索里）。
    featured_ci = {k.lower() for k in st["featured"]}

    picked: list[dict] = []
    seen: set[str] = set()
    skipped: list[tuple[str, list]] = []
    seen_keys: set[str] = set()

    def _keys(full: str, desc: str | None) -> list[str]:
        """同名或同描述的克隆仓（常有人换 owner 重发）只留一个。"""
        name = "".join(ch for ch in (full.split("/")[-1] or "").lower() if ch.isalnum())
        text = "".join(ch for ch in (desc or "").lower() if ch.isalnum())[:60]
        return [k for k in (f"n:{name}", f"d:{text}") if k not in ("n:", "d:")]

    for q, sort, label in queries:
        for it in search(q, sort):
            full = it["full_name"]
            if full.lower() in seen or full.lower() in featured_ci:
                continue
            seen.add(full.lower())
            item = {
                "label": label,
                "full_name": full,
                "url": it["html_url"],
                "stars": it["stargazers_count"],
                "forks": it["forks_count"],
                "language": it.get("language"),
                "license": (it.get("license") or {}).get("spdx_id"),
                "topics": (it.get("topics") or [])[:5],
                "created_at": it["created_at"],
                "pushed_at": it["pushed_at"],
                "description": it.get("description"),
                "days_old": (datetime.now(timezone.utc)
                             - datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))).days,
            }
            hit = is_excluded(item, prefs)
            if hit:
                skipped.append((full, hit))
                continue
            dup_keys = _keys(full, item.get("description"))
            if any(k in seen_keys for k in dup_keys):
                skipped.append((full, ["重复项目"]))
                continue
            seen_keys.update(dup_keys)
            picked.append(item)
            if len(picked) >= args.count * 4:
                break
        if len(picked) >= args.count * 4:
            break

    if not picked:
        print("没找到候选（可能限流或条件太严）")
        return

    # 排序：新建即热门优先 → 同类里偏好命中的在前 → 星数
    order = {"新建即热门": 0, "小体量新项目": 1, "老项目新动向": 2}
    picked.sort(key=lambda x: (order.get(x["label"], 9),
                               0 if is_boosted(x, prefs) else 1,
                               -x["stars"]))
    picked = picked[: args.count]

    if not args.no_readme and not args.dry_run:
        for p in picked:
            p["readme"] = readme_snippet(p["full_name"])

    today = date.today().isoformat()
    out = HERE / "daily" / today / "candidates.json"
    if args.dry_run:
        print("[dry-run] 不写 candidates.json、不记 _featured")
    else:
        out_dir = HERE / "daily" / today
        out_dir.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"date": today, "items": picked}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        for p in picked:
            st["featured"][p["full_name"]] = {"date": today, "stars": p["stars"]}
        st["runs"] = (st.get("runs") or [])[-30:] + [{"date": today, "count": len(picked)}]
        save_state(st)

    print(f"抓到 {len(picked)} 个候选 -> {out}")
    if prefs:
        hit_words = sorted({w for _, words in skipped for w in words})
        print(f"偏好：按 _prefs.json 排除 {len(skipped)} 个"
              + (f"（命中 {', '.join(hit_words)}）" if hit_words else "")
              + f"，{sum(1 for p in picked if is_boosted(p, prefs))} 个命中偏好词")
    print()
    for i, p in enumerate(picked, 1):
        flag = " ★偏好" if prefs and is_boosted(p, prefs) else ""
        print(f"{i}. {p['full_name']}  ★{p['stars']}  {p['language']}  "
              f"({p['label']}, {p['days_old']} 天前建){flag}")
        print(f"   {(p.get('description') or '')[:100]}")
    print()
    print("下一步：读 candidates.json 里的 readme，为每个项目写 payload.json，然后：")
    print(f"  python copy_check.py daily/{today}/ --online   # 先体检文案和数字")
    print(f"  python gh_card.py --payload daily/{today}/<slug>.json --offline "
          f"--out daily/{today}/ --style <id> --check")
    print("（--check 有 error 就改文案，别改排版）")


if __name__ == "__main__":
    main()