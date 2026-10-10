"""新闻源健康检查：逐个探测 RSS / 网页是否可用，报告条目数与最新时间。

用法：
    python probe_sources.py                     # 检查 sources.json 里全部源
    python probe_sources.py --file other.json   # 检查另一份配置（同结构）
    python probe_sources.py --workers 8 --timeout 8

输出的 OK / STALE / EMPTY / FAIL 直接告诉你哪条源该换。
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import sys

import fetcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load(path: str) -> tuple:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, {}
    return data.get("sources", []), data.get("settings", {})


def probe(source: dict, settings: dict) -> dict:
    session = fetcher._session()
    items, error = fetcher.fetch_source(source, session, settings)
    dates = sorted([i["published"] for i in items if i.get("published")])
    return {
        "name": source.get("name", "?"),
        "domain": source.get("domain", ""),
        "rss": source.get("rss", ""),
        "count": len(items),
        "newest": dates[-1] if dates else "",
        "error": error or "",
        "sample": items[0]["title"] if items else "",
    }


def verdict(row: dict) -> str:
    if row["count"] == 0:
        return "FAIL"
    if not row["newest"]:
        return "OK?"      # 有条目但无时间（网页抓取常见）
    return "OK"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.join(BASE_DIR, "sources.json"))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    sources, settings = load(args.file)
    settings = dict(settings)
    settings["timeout"] = args.timeout
    settings.setdefault("max_per_source", 12)
    settings.setdefault("lookback_hours", 0)  # 探测时不按时间过滤，才能看出源是否还在更新

    print(f"探测 {len(sources)} 个源（timeout={args.timeout}s，并发={args.workers}）…\n")
    results = []
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(lambda s: probe(s, settings), sources):
            results.append(row)

    order = {"FAIL": 0, "OK?": 1, "OK": 2}
    results.sort(key=lambda r: (order[verdict(r)], r["domain"], r["name"]))
    ok = 0
    for r in results:
        v = verdict(r)
        ok += 1 if v != "FAIL" else 0
        head = f"[{v:4}] {r['name']} ({r['domain']}) 条目 {r['count']:>2}"
        tail = f" 最新 {r['newest']}" if r["newest"] else ""
        print(head + tail)
        if r["sample"]:
            print("        · " + r["sample"][:80])
        if v == "FAIL":
            print("        ! " + (r["error"] or "无条目")[:160])
        else:
            print("        rss: " + (r["rss"] or "(无，走网页)"))

    print(f"\n可用 {ok} / {len(results)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
