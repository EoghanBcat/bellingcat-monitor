"""Writes the dashboard data, CSV export, RSS feed and README digest."""
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import timedelta
from xml.sax.saxutils import escape

from .common import DATA, ROOT, now_utc, iso, parse_date

CSV_FIELDS = ["published", "found", "channel", "platform", "source", "domain", "language",
              "country", "outlet_type", "author", "title", "url", "term", "category", "snippet", "via"]

SOURCE_LABELS = {
    "google_news": "Google News", "gdelt": "GDELT", "guardian_api": "Guardian API",
    "outlets": "Outlet list", "bluesky": "Bluesky", "mastodon": "Mastodon",
    "telegram": "Telegram", "hacker_news": "Hacker News", "podcasts": "Podcasts (Apple)",
    "youtube": "YouTube",
}


def pages_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


def write_outputs(archive: list[dict], ctx, added: int, duration: float) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "mentions.json").write_text(json.dumps(archive, ensure_ascii=False, indent=0), encoding="utf-8")

    health = {
        "last_run": iso(now_utc()),
        "duration_seconds": round(duration),
        "added_last_run": added,
        "total": len(archive),
        "sources": {SOURCE_LABELS.get(k, k): v for k, v in ctx.health.items()},
        "outlets": sorted(ctx.outlet_health, key=lambda o: ({"error": 0, "partial": 1}.get(o["status"], 2), o["name"] or "")),
    }
    (DATA / "health.json").write_text(json.dumps(health, ensure_ascii=False, indent=1), encoding="utf-8")

    with open(ROOT / "docs" / "mentions.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in archive:
            w.writerow({**r, "via": "; ".join(r.get("via", []))})

    write_rss(archive[:100])
    write_readme(archive, health)


def write_rss(items: list[dict]) -> None:
    base = pages_url()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
             "<title>Bellingcat media mentions</title>",
             f"<link>{escape(base or 'https://github.com')}</link>",
             "<description>Latest media mentions of Bellingcat</description>"]
    for r in items:
        dt = parse_date(r.get("published") or r.get("found"))
        pub = dt.strftime("%a, %d %b %Y %H:%M:%S +0000") if dt else ""
        desc = f"[{r['channel']} · {r['source']}] {r.get('snippet', '')}"
        parts.append(
            f"<item><title>{escape(r['source'] + ': ' + r['title'])}</title>"
            f"<link>{escape(r['url'])}</link><guid isPermaLink=\"false\">{r['id']}</guid>"
            f"<pubDate>{pub}</pubDate><description>{escape(desc)}</description></item>")
    parts.append("</channel></rss>")
    (ROOT / "docs" / "feed.xml").write_text("\n".join(parts), encoding="utf-8")


def _md(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").replace("[", "(").replace("]", ")")


def write_readme(archive: list[dict], health: dict) -> None:
    now = now_utc()
    def recent(days):
        cutoff = iso(now - timedelta(days=days))
        return [r for r in archive if (r.get("published") or r.get("found") or "") >= cutoff]

    day, week = recent(2), recent(7)
    by_channel = Counter(r["channel"] for r in week)
    by_lang = Counter((r.get("language") or "?") for r in week)
    dash = pages_url()

    lines = ["# Bellingcat media monitor", "",
             f"_Last updated {health['last_run'].replace('T', ' ').replace('Z', ' UTC')}. "
             f"Runs automatically every two days._", ""]
    if dash:
        lines += [f"**[Open the dashboard]({dash})** · [Download all mentions (CSV)](docs/mentions.csv) · "
                  f"[RSS feed]({dash}feed.xml) · [How to change what is monitored](HOW-TO-EDIT.md)", ""]
    else:
        lines += ["[Download all mentions (CSV)](docs/mentions.csv) · "
                  "[How to change what is monitored](HOW-TO-EDIT.md)", ""]
    lines += [f"**Last 2 days:** {len(day)} mentions · **Last 7 days:** {len(week)} · "
              f"**All time:** {len(archive)} · **New this run:** {health['added_last_run']}", ""]
    if week:
        lines.append("**Last 7 days by type:** " + ", ".join(f"{k} {v}" for k, v in by_channel.most_common()))
        lines.append("")
        lines.append("**Last 7 days by language:** " + ", ".join(f"{k} {v}" for k, v in by_lang.most_common(12)))
        lines.append("")

    lines += ["## Latest mentions", "", "| Date | Type | Source | Mention | Lang |", "|---|---|---|---|---|"]
    for r in archive[:60]:
        date = (r.get("published") or r.get("found") or "")[:10]
        lines.append(f"| {date} | {_md(r['channel'])} | {_md(r['source'])} | "
                     f"[{_md(r['title'])[:110]}]({r['url']}) | {r.get('language') or ''} |")
    if not archive:
        lines.append("| | | | No mentions found yet | |")

    lines += ["", "## Source health", "", "| Source | Status | Checked | Found | Notes |", "|---|---|---|---|---|"]
    icons = {"ok": "✅ working", "error": "⚠️ problem", "off": "⏸ off"}
    for name, h in health["sources"].items():
        lines.append(f"| {name} | {icons.get(h['status'], h['status'])} | {h.get('checked', 0)} | "
                     f"{h.get('found', 0)} | {_md(h.get('message', ''))} |")
    broken = [o for o in health["outlets"] if o["status"] == "error"]
    if broken:
        lines += ["", f"<details><summary>{len(broken)} outlets that couldn't be read "
                      f"(still covered by Google News and GDELT)</summary>", "",
                  "| Outlet | Problem |", "|---|---|"]
        for o in broken[:300]:
            lines.append(f"| {_md(o['name'])} | {_md(o['message'])[:160]} |")
        lines += ["", "</details>"]
    lines.append("")
    (ROOT / "README.md").write_text("\n".join(lines), encoding="utf-8")
