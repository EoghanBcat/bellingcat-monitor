"""News sources: Google News, GDELT, Guardian API, and the curated outlet list."""
from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import quote_plus, urljoin

import feedparser
from bs4 import BeautifulSoup

from .common import (CHANNEL_NEWS, domain_of, env, load_csv, mention, normalise_url,
                     now_utc, parse_date, strip_html, too_old)

GDELT_LANGS = {
    "english": "en", "russian": "ru", "ukrainian": "uk", "dutch": "nl", "german": "de",
    "french": "fr", "spanish": "es", "portuguese": "pt", "italian": "it", "polish": "pl",
    "turkish": "tr", "arabic": "ar", "persian": "fa", "hebrew": "he", "japanese": "ja",
    "chinese": "zh", "korean": "ko", "georgian": "ka", "armenian": "hy", "serbian": "sr",
    "croatian": "hr", "czech": "cs", "romanian": "ro", "hungarian": "hu", "greek": "el",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi", "bulgarian": "bg",
    "estonian": "et", "latvian": "lv", "lithuanian": "lt", "slovak": "sk", "slovenian": "sl",
    "indonesian": "id", "vietnamese": "vi", "thai": "th", "hindi": "hi", "urdu": "ur",
}

SEARCH_NOTE = "The search term appears in the article text (found by {name})."


def article_text(ctx, url: str) -> str:
    """Download a page and return its main article text ('' on failure)."""
    import trafilatura
    try:
        r = ctx.get(url, timeout=20)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return ""
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False,
                                   favor_recall=True) or ""
        if not text:
            text = strip_html(r.text)
        return text
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ Google News

def google_news(ctx) -> list[dict]:
    editions = ctx.settings.get("google_news_editions") or [["en-GB", "GB", "GB:en"]]
    query = f"{ctx.keywords.search_query()} when:{ctx.lookback_days}d"
    results, checked, errors = [], 0, []

    def fetch(ed):
        hl, gl, ceid = ed
        url = (f"https://news.google.com/rss/search?q={quote_plus(query)}"
               f"&hl={hl}&gl={gl}&ceid={quote_plus(ceid)}")
        try:
            r = ctx.get(url)
            r.raise_for_status()
            return hl, feedparser.parse(r.content), None
        except Exception as e:  # noqa: BLE001
            return hl, None, f"{hl}: {e}"

    seen_links = set()
    for hl, feed, err in ctx.parallel(fetch, editions, workers=4):
        if err:
            errors.append(err)
            continue
        for e in feed.entries:
            checked += 1
            if e.get("link") in seen_links:
                continue
            seen_links.add(e.get("link"))
            published = parse_date(e.get("published"))
            if too_old(published, ctx.max_age_days):
                continue
            src = e.get("source") or {}
            outlet = src.get("title") or ""
            outlet_url = src.get("href") or ""
            title = e.get("title", "")
            if outlet and title.endswith(" - " + outlet):
                title = title[: -len(outlet) - 3]
            summary = strip_html(e.get("summary", ""))
            m = ctx.keywords.match(title, summary) or {
                "term": "Bellingcat", "category": "main",
                "snippet": SEARCH_NOTE.format(name="Google News"),
            }
            rec = mention(url=e.get("link", ""), title=title, snippet=m["snippet"],
                          source=outlet or "Google News", channel=CHANNEL_NEWS,
                          platform="Web", language=hl.split("-")[0], published=published,
                          match=m, via="Google News")
            rec["domain"] = domain_of(outlet_url) or rec["domain"]
            results.append(rec)
    ctx.record("google_news", ok=len(errors) < len(editions), checked=checked,
               found=len(results), message="; ".join(errors))
    return results


# ------------------------------------------------------------------ GDELT

def gdelt(ctx) -> list[dict]:
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?query=bellingcat"
           f"&mode=artlist&format=json&maxrecords=250&sort=datedesc&timespan={ctx.lookback_days}d")
    r = ctx.get(url, timeout=40)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        # GDELT returns plain-text errors (e.g. rate limit) instead of JSON.
        ctx.record("gdelt", ok=False, message=r.text[:200])
        return []
    articles = data.get("articles") or []
    results = []
    for a in articles:
        published = parse_date(a.get("seendate"))
        if too_old(published, ctx.max_age_days):
            continue
        title = a.get("title", "")
        m = ctx.keywords.match(title) or {
            "term": "Bellingcat", "category": "main",
            "snippet": SEARCH_NOTE.format(name="GDELT"),
        }
        rec = mention(url=a.get("url", ""), title=title, snippet=m["snippet"],
                      source=a.get("domain") or domain_of(a.get("url", "")),
                      channel=CHANNEL_NEWS, platform="Web",
                      language=GDELT_LANGS.get((a.get("language") or "").lower(), a.get("language", "")),
                      country=a.get("sourcecountry", ""), published=published, match=m, via="GDELT")
        rec["_needs_snippet"] = True
        results.append(rec)
    ctx.record("gdelt", ok=True, checked=len(articles), found=len(results))
    return results


# ------------------------------------------------------------------ Guardian API

def guardian_api(ctx) -> list[dict]:
    key = env("GUARDIAN_API_KEY")
    if not key:
        ctx.health["guardian_api"] = {"status": "off", "checked": 0, "found": 0,
                                      "message": "No GUARDIAN_API_KEY secret added (optional)",
                                      "time": None}
        return []
    since = (now_utc() - timedelta(days=ctx.lookback_days)).strftime("%Y-%m-%d")
    r = ctx.get("https://content.guardianapis.com/search", params={
        "q": "bellingcat", "from-date": since, "order-by": "newest", "page-size": 50,
        "show-fields": "bodyText,trailText", "api-key": key})
    r.raise_for_status()
    items = r.json().get("response", {}).get("results", [])
    results = []
    for it in items:
        fields = it.get("fields") or {}
        m = ctx.keywords.match(it.get("webTitle"), fields.get("trailText"), fields.get("bodyText"))
        if not m:
            continue
        results.append(mention(url=it["webUrl"], title=it.get("webTitle", ""), snippet=m["snippet"],
                               source="The Guardian", channel=CHANNEL_NEWS, platform="Web",
                               language="en", country="UK", outlet_type="newspaper",
                               published=parse_date(it.get("webPublicationDate")),
                               match=m, via="Guardian API"))
    ctx.record("guardian_api", ok=True, checked=len(items), found=len(results))
    return results


# ------------------------------------------------------------------ Curated outlets

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml",
              "application/xml", "text/xml")


def discover_feed(ctx, website: str) -> str | None:
    try:
        r = ctx.get(website, timeout=20)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            if (link.get("type") or "").lower() in FEED_TYPES and link.get("href"):
                return urljoin(r.url, link["href"])
        for a in soup.find_all("a", href=True):
            if re.search(r"(/rss|/feed|\.rss|\.xml)(/|$|\?)", a["href"], re.I):
                return urljoin(r.url, a["href"])
    except Exception:  # noqa: BLE001
        return None
    return None


def fetch_feed(ctx, url: str):
    r = ctx.get(url, timeout=25)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if not feed.entries:
        raise ValueError("feed has no items")
    return feed


def outlets(ctx) -> list[dict]:
    rows = load_csv("outlets.csv")
    discovered = ctx.state.setdefault("discovered_feeds", {})
    seen = ctx.state.setdefault("seen_articles", {})
    no_feed = ctx.state.setdefault("no_feed_found", {})
    today = now_utc().strftime("%Y-%m-%d")
    retry_after = (now_utc() - timedelta(days=7)).strftime("%Y-%m-%d")  # look again weekly
    results, queue = [], []

    def load(row):
        info = {"name": row.get("name"), "website": row.get("website"), "feed": "",
                "status": "ok", "items": 0, "mentions": 0, "message": ""}
        candidates = [row.get("feed"), discovered.get(row.get("website", ""))]
        feed, last_err = None, ""
        for url in [c for c in candidates if c]:
            try:
                feed = fetch_feed(ctx, url)
                info["feed"] = url
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"{url}: {e}"
        site = row.get("website", "")
        recently_failed = no_feed.get(site, "") >= retry_after
        if feed is None and site and not recently_failed:
            found = discover_feed(ctx, site)
            if found:
                try:
                    feed = fetch_feed(ctx, found)
                    info["feed"] = found
                    discovered[site] = found
                    info["message"] = "Feed found automatically"
                except Exception as e:  # noqa: BLE001
                    last_err = f"{found}: {e}"
        if feed is None and site and not recently_failed:
            no_feed[site] = today
        if feed is None:
            info["status"] = "error"
            info["message"] = ("No working feed found. Mentions from this outlet can still "
                               "appear via Google News / GDELT. " + last_err)[:300]
        return row, feed, info

    loaded = ctx.parallel(load, rows, workers=24)
    for row, feed, info in loaded:
        ctx.outlet_health.append(info)
        if feed is None:
            continue
        for e in feed.entries:
            link = e.get("link")
            if not link:
                continue
            key = normalise_url(link)
            published = parse_date(e.get("published_parsed") or e.get("updated_parsed")
                                   or e.get("published") or e.get("updated"))
            if too_old(published, ctx.max_age_days) or key in seen:
                continue
            info["items"] += 1
            body = " ".join(strip_html(c.get("value", "")) for c in e.get("content", []) or [])
            summary = strip_html(e.get("summary", ""))
            m = ctx.keywords.match(e.get("title"), summary, body)
            if m:
                seen[key] = today
                info["mentions"] += 1
                results.append(_outlet_mention(row, link, e.get("title", ""), published, m, "Outlet feed"))
            elif (row.get("full_text") or "yes").strip().lower() not in ("no", "n", "false", "0"):
                queue.append((row, info, key, link, e.get("title", ""), published))
            else:
                seen[key] = today  # headline-only outlet: checked, nothing found

    # Download full articles for items whose headline/summary didn't mention the keywords.
    queue.sort(key=lambda q: q[5].timestamp() if q[5] else 0, reverse=True)
    limit = int(ctx.settings.get("max_article_downloads", 8000))
    to_fetch = queue[:limit]

    def check(item):
        row, info, key, link, title, published = item
        text = article_text(ctx, link)
        return item, (ctx.keywords.match(text) if text else None), bool(text)

    downloaded = 0
    for item, m, ok in ctx.parallel(check, to_fetch, workers=24):
        row, info, key, link, title, published = item
        seen[key] = today  # don't retry blocked/paywalled pages every run
        if ok:
            downloaded += 1
        if m:
            info["mentions"] += 1
            results.append(_outlet_mention(row, link, title, published, m, "Outlet article"))

    working = sum(1 for i in ctx.outlet_health if i["status"] == "ok")
    ctx.record("outlets", ok=working > 0, checked=downloaded + len(results), found=len(results),
               message=f"{working} of {len(rows)} outlet feeds working; "
                       f"{downloaded} full articles read; {max(0, len(queue) - limit)} left for next run")
    return results


def _outlet_mention(row, link, title, published, m, via):
    return mention(url=link, title=title, snippet=m["snippet"], source=row.get("name") or domain_of(link),
                   channel=CHANNEL_NEWS, platform="Web", language=row.get("language"),
                   country=row.get("country"), outlet_type=row.get("type"),
                   published=published, match=m, via=via)


def fill_snippets(ctx, records: list[dict], limit: int = 80) -> None:
    """For search-engine hits with no context, read the article to show the sentence."""
    todo = [r for r in records if r.pop("_needs_snippet", False)][:limit]

    def work(rec):
        text = article_text(ctx, rec["url"])
        m = ctx.keywords.match(text) if text else None
        if m:
            rec["snippet"] = m["snippet"]
            rec["term"], rec["category"] = m["term"], m["category"]

    if todo:
        ctx.parallel(work, todo, workers=10)
    for r in records:
        r.pop("_needs_snippet", None)
