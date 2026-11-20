"""Entry point: python -m monitor.run

Runs every collector, merges new mentions into the archive, and writes the
dashboard data, CSV, RSS feed and README digest.
"""
from __future__ import annotations

import faulthandler
import json
import sys
import time
from datetime import timedelta

from . import sources_news as news
from . import sources_social as social
from .common import CHANNEL_NEWS, DATA, now_utc, iso, normalise_url, title_key
from .context import Context, safe_run, save_state
from .report import write_outputs

COLLECTORS = [
    ("google_news", news.google_news),
    ("gdelt", news.gdelt),
    ("guardian_api", news.guardian_api),
    ("outlets", news.outlets),
    ("bluesky", social.bluesky),
    ("mastodon", social.mastodon),
    ("telegram", social.telegram),
    ("hacker_news", social.hacker_news),
    ("podcasts", social.podcasts),
    ("youtube", social.youtube),
]

PLACEHOLDER_MARK = "appears in the article text"


def load_archive() -> list[dict]:
    path = DATA / "mentions.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return []


def excluded(rec: dict, settings: dict) -> bool:
    ex = settings.get("exclude") or {}
    domains = {d.lower() for d in ex.get("domains") or []}
    authors = {a.lower().lstrip("@") for a in ex.get("authors") or []}
    dom = (rec.get("domain") or "").lower()
    if any(dom == d or dom.endswith("." + d) for d in domains):
        return True
    return (rec.get("author") or "").lower().lstrip("@") in authors


def merge(archive: list[dict], new: list[dict], settings: dict) -> tuple[list[dict], int]:
    by_id = {r["id"]: r for r in archive}
    by_title = {(r.get("domain"), title_key(r["title"])): r
                for r in archive if r.get("channel") == CHANNEL_NEWS}
    added = 0
    for rec in new:
        if not rec.get("url") or excluded(rec, settings):
            continue
        tkey = (rec.get("domain"), title_key(rec["title"]))
        existing = by_id.get(rec["id"])
        if existing is None and rec.get("channel") == CHANNEL_NEWS and tkey[1]:
            existing = by_title.get(tkey)
        if existing is None:
            archive.append(rec)
            by_id[rec["id"]] = rec
            if rec.get("channel") == CHANNEL_NEWS:
                by_title[tkey] = rec
            added += 1
            continue
        # Same story found again: remember every route that found it, and
        # upgrade placeholder snippets / Google redirect links.
        for v in rec["via"]:
            if v not in existing["via"]:
                existing["via"].append(v)
        if PLACEHOLDER_MARK in existing.get("snippet", "") and PLACEHOLDER_MARK not in rec["snippet"]:
            existing["snippet"], existing["term"], existing["category"] = rec["snippet"], rec["term"], rec["category"]
        if "news.google.com" in existing["url"] and "news.google.com" not in rec["url"]:
            existing["url"] = rec["url"]
        for f in ("language", "country", "outlet_type", "published"):
            if not existing.get(f) and rec.get(f):
                existing[f] = rec[f]
    archive.sort(key=lambda r: r.get("published") or r.get("found") or "", reverse=True)
    return archive, added


def prune_state(state: dict, days: int = 30) -> None:
    cutoff = (now_utc() - timedelta(days=days)).strftime("%Y-%m-%d")
    seen = state.get("seen_articles", {})
    state["seen_articles"] = {k: v for k, v in seen.items() if v >= cutoff}
    state.pop("no_feed_found", None)  # replaced by outlet_source_failed


def main() -> None:
    # Show progress in the GitHub log as it happens, and explain any hard crash.
    sys.stdout.reconfigure(line_buffering=True)
    faulthandler.enable()
    start = time.time()
    ctx = Context()
    print(f"First run: {ctx.first_run}")
    found: list[dict] = []
    for name, fn in COLLECTORS:
        t = time.time()
        results = safe_run(ctx, name, fn)
        print(f"[{name}] {len(results)} mentions in {time.time() - t:.0f}s "
              f"({ctx.health.get(name, {}).get('message', '')})")
        found.extend(results)

    news.fill_snippets(ctx, found)
    news.shutdown_pool()
    archive = [r for r in load_archive()
               if not (r.get("via") == ["Google News site search"]
                       and PLACEHOLDER_MARK in r.get("snippet", ""))]
    archive, added = merge(archive, found, ctx.settings)
    ctx.state["last_run"] = iso(now_utc())
    prune_state(ctx.state)
    save_state(ctx.state)
    write_outputs(archive, ctx, added, duration=time.time() - start)
    print(f"Done: {added} new mentions, {len(archive)} in archive, {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
