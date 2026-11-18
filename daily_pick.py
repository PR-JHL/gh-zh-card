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
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
STATE = HERE / "daily" / "_featured.json"
UA = {"Accept": "application/vnd.github+json", "User-Agent": "gh-zh-card-daily"}

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
    args = ap.parse_args()

    st = load_state()

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

    picked: list[dict] = []
    seen: set[str] = set()
    for q, sort, label in queries:
        for it in search(q, sort):
            full = it["full_name"]
            if full in seen or full in st["featured"]:
                continue
            seen.add(full)
            picked.append({
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
            })
            if len(picked) >= args.count * 4:
                break
        if len(picked) >= args.count * 4:
            break

    if not picked:
        print("没找到候选（可能限流或条件太严）")
        return

    # 排序：新建即热门优先，其次按星数
    order = {"新建即热门": 0, "小体量新项目": 1, "老项目新动向": 2}
    picked.sort(key=lambda x: (order.get(x["label"], 9), -x["stars"]))
    picked = picked[: args.count]

    if not args.no_readme:
        for p in picked:
            p["readme"] = readme_snippet(p["full_name"])

    today = date.today().isoformat()
    out_dir = HERE / "daily" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "candidates.json"
    out.write_text(json.dumps({"date": today, "items": picked}, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    for p in picked:
        st["featured"][p["full_name"]] = {"date": today, "stars": p["stars"]}
    st["runs"] = (st.get("runs") or [])[-30:] + [{"date": today, "count": len(picked)}]
    save_state(st)

    print(f"抓到 {len(picked)} 个候选 -> {out}")
    print()
    for i, p in enumerate(picked, 1):
        print(f"{i}. {p['full_name']}  ★{p['stars']}  {p['language']}  "
              f"({p['label']}, {p['days_old']} 天前建)")
        print(f"   {(p.get('description') or '')[:100]}")
    print()
    print("下一步：读 candidates.json 里的 readme，为每个项目写 payload.json，然后：")
    print(f"  python gh_card.py --payload daily/{today}/<slug>.json --offline "
          f"--out daily/{today}/ --style <id>")


if __name__ == "__main__":
    main()