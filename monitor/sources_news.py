"""News sources: Google News, GDELT, Guardian API, and the curated outlet list."""
from __future__ import annotations

import gzip
import multiprocessing
import os
import re
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import timedelta
from itertools import zip_longest
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

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
PLACEHOLDER_MARK = "appears in the article text"


# Turning a web page into plain text uses lxml, a fast library written in C. Very rarely it
# crashes outright on an odd page, and that crash would end the whole run. So pages are
# turned into text in separate worker processes: if one crashes, only that page is lost.
_POOL = None
_POOL_LOCK = threading.Lock()
_XML_LOCK = threading.Lock()


def _extract_text(html: str) -> str:
    """Runs inside a worker process."""
    import trafilatura
    try:
        text = trafilatura.extract(html, include_comments=False, include_tables=False,
                                   favor_recall=True) or ""
    except Exception:  # noqa: BLE001
        text = ""
    return text or strip_html(html)


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            ctx = multiprocessing.get_context("spawn")
            _POOL = ProcessPoolExecutor(max_workers=max(2, (os.cpu_count() or 2)), mp_context=ctx)
        return _POOL


def _reset_pool(broken):
    global _POOL
    with _POOL_LOCK:
        if _POOL is broken:
            print("[outlets] a page-reading worker crashed; starting a fresh one and carrying on")
            try:
                broken.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
            _POOL = None


def shutdown_pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.shutdown(wait=False, cancel_futures=True)
            _POOL = None


def article_text(ctx, url: str) -> str:
    """Download a page and return its main article text ('' on failure)."""
    try:
        r = ctx.get(url, timeout=20)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return ""
        html = r.text[:3_000_000]
    except Exception:  # noqa: BLE001
        return ""
    for _ in range(3):
        pool = _pool()
        try:
            return pool.submit(_extract_text, html).result(timeout=60)
        except BrokenProcessPool:
            _reset_pool(pool)  # a page crashed the worker: try again in a fresh one, then skip
        except Exception:  # noqa: BLE001  (timeout or a bad page)
            return ""
    return ""


# ------------------------------------------------------------------ Google News

def google_news(ctx) -> list[dict]:
    editions = ctx.settings.get("google_news_editions") or [["en-GB", "GB", "GB:en"]]
    query = f"{ctx.keywords.search_query(quoted=True)} when:{ctx.lookback_days}d"
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
    import requests
    r, last_error = None, ""
    for attempt in range(5):
        try:
            # A plain session: the shared one gives up too quickly on GDELT's "slow down" replies.
            plain = requests.Session()
            plain.headers["User-Agent"] = ctx.session.headers["User-Agent"]
            r = plain.get(url, timeout=60)
            if r.status_code == 200:
                break
            last_error = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_error = type(e).__name__
        time.sleep(20 * (attempt + 1))  # GDELT asks for pauses between requests
    if r is None or r.status_code != 200:
        ctx.record("gdelt", ok=False, message=f"GDELT busy or rate-limited after 5 tries ({last_error}); "
                                               "will try again next run")
        return []
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
#
# For each outlet the monitor needs a list of its recent articles. It tries, in order:
#   1. the RSS feed given in outlets.csv, or one found on an earlier run;
#   2. feeds linked from the homepage, feed directory pages, and common feed addresses;
#   3. the site's news sitemap (most news sites publish one for Google News);
#   4. if the site can't be read at all: a Google News search restricted to that site.

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml",
              "application/xml", "text/xml")
FEED_PATHS = ["feed", "feed/", "rss", "rss/", "rss.xml", "feed.xml", "index.xml", "atom.xml",
              "feeds/all.rss", "feed/rss2", "rss/news", "arc/outboundfeeds/rss/?outputType=xml",
              "arc/outboundfeeds/rss/", "?format=rss"]
SITEMAP_PATHS = ["sitemap_news.xml", "news-sitemap.xml", "sitemap-news.xml", "news_sitemap.xml",
                 "sitemap/news.xml", "sitemaps/news.xml", "googlenews.xml", "sitemap.xml",
                 "sitemap_index.xml"]
FEEDLIKE = re.compile(r"(/rss|/feeds?|\.rss|rss\.xml|atom\.xml|outboundfeeds)(/|$|\?|\.)", re.I)
ALT_UA = "Mozilla/5.0 (compatible; MediaMonitor/1.0; RSS feed reader)"
BLOCKED = (401, 403, 406, 429, 451, 503)


def fetch(ctx, url: str, timeout: int = 20):
    """GET with one retry under a plain feed-reader identity if the site refuses us."""
    r = ctx.get(url, timeout=timeout)
    if r.status_code in BLOCKED:
        try:
            r2 = ctx.get(url, timeout=timeout, headers={"User-Agent": ALT_UA})
            if r2.status_code < 400:
                return r2
        except Exception:  # noqa: BLE001
            pass
    return r


def _is_html(r) -> bool:
    head = r.content[:600].lower()
    return b"<html" in head or b"<!doctype html" in head


def _slug_title(url: str) -> str:
    """Words from an article address, e.g. /2026/11/18/bellingcat-report-local/123/ -> 'bellingcat report local'."""
    segs = [re.sub(r"\.(html?|php|aspx?)$", "", s) for s in urlparse(url).path.split("/") if s]
    words = [s for s in segs if re.search(r"[^\W\d_].*[-_].*[^\W\d_]", s)]
    return re.sub(r"[-_]+", " ", max(words, key=len)) if words else ""


def items_from_feed(content: bytes):
    feed = feedparser.parse(content)
    if not feed.entries:
        return None
    items = []
    for e in feed.entries:
        if not e.get("link"):
            continue
        items.append({
            "link": e.get("link"),
            "title": e.get("title", ""),
            "text": " ".join([strip_html(e.get("summary", ""))] +
                             [strip_html(c.get("value", "")) for c in e.get("content", []) or []]),
            "published": parse_date(e.get("published_parsed") or e.get("updated_parsed")
                                    or e.get("published") or e.get("updated")),
        })
    return items


def _xml_root(content: bytes):
    from lxml import etree
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        with _XML_LOCK:  # one sitemap at a time: lxml is safest without threads overlapping
            return etree.fromstring(content, parser=etree.XMLParser(recover=True, huge_tree=False))
    except Exception:  # noqa: BLE001
        return None


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def items_from_sitemap(ctx, url: str, depth: int = 0):
    """Recent articles from a sitemap (or sitemap index). None = not a sitemap."""
    r = fetch(ctx, url, timeout=25)
    if r.status_code != 200:
        return None
    root = _xml_root(r.content)
    if root is None:
        return None
    kind = _local(root.tag)
    cutoff = now_utc() - timedelta(days=ctx.lookback_days + 1)
    if kind == "sitemapindex":
        if depth >= 2:
            return None
        children = []
        for sm in root:
            loc = lastmod = None
            for el in sm:
                if _local(el.tag) == "loc":
                    loc = (el.text or "").strip()
                elif _local(el.tag) == "lastmod":
                    lastmod = parse_date((el.text or "").strip())
            if loc:
                children.append((loc, lastmod))
        news = [c for c in children if "news" in c[0].lower()]
        if news:
            chosen = news[:3]
        else:
            dated = sorted([c for c in children if c[1]], key=lambda c: c[1], reverse=True)
            chosen = dated[:2] or children[-2:]
        items = []
        for loc, lastmod in chosen:
            if lastmod and lastmod < cutoff:
                continue
            items.extend(items_from_sitemap(ctx, loc, depth + 1) or [])
        return items
    if kind != "urlset":
        return None
    items = []
    for u in root:
        loc = title = None
        dates = []
        for el in u.iter():
            name = _local(el.tag)
            if name == "loc" and loc is None:
                loc = (el.text or "").strip()
            elif name == "title" and el.getparent() is not None and _local(el.getparent().tag) == "news":
                title = (el.text or "").strip()
            elif name in ("publication_date", "lastmod"):
                d = parse_date((el.text or "").strip())
                if d:
                    dates.append(d)
        published = max(dates) if dates else None
        if not loc or not published or published < cutoff:
            continue  # without a date we can't tell old pages from new ones
        items.append({"link": loc, "title": title or _slug_title(loc), "text": "", "published": published})
    items.sort(key=lambda i: i["published"], reverse=True)
    return items[:400]


def load_source(ctx, src: dict):
    """Fetch items from a known source {'kind': 'feed'|'sitemap', 'url': ...}."""
    if src["kind"] == "sitemap":
        items = items_from_sitemap(ctx, src["url"])
        if items is None:
            raise ValueError("sitemap could not be read")
        return items
    r = fetch(ctx, src["url"], timeout=25)
    if r.status_code != 200:
        raise ValueError(f"HTTP {r.status_code}")
    items = items_from_feed(r.content)
    if items is None:
        raise ValueError("returned a web page, not a feed" if _is_html(r) else "feed has no items")
    return items


def discover_source(ctx, website: str):
    """Look for a feed or news sitemap. Returns (source, items) or (None, reason)."""
    reasons = []
    p = urlparse(website)
    root = f"{p.scheme or 'https'}://{p.netloc}/"
    section = website if website.rstrip("/") != root.rstrip("/") else None
    feed_candidates = []
    try:
        home = fetch(ctx, website)
        if home.status_code == 200:
            soup = BeautifulSoup(home.text, "html.parser")
            for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
                if (link.get("type") or "").lower() in FEED_TYPES and link.get("href"):
                    feed_candidates.append(urljoin(home.url, link["href"]))
            for a in soup.find_all("a", href=True):
                if FEEDLIKE.search(a["href"]):
                    feed_candidates.append(urljoin(home.url, a["href"]))
        elif home.status_code in BLOCKED:
            # The whole site refuses automated readers; probing more addresses won't help.
            return None, f"site blocks automated readers (HTTP {home.status_code})"
        else:
            reasons.append(f"homepage answered HTTP {home.status_code}")
    except Exception as e:  # noqa: BLE001
        return None, f"homepage unreachable ({type(e).__name__})"
    for base in [b for b in (section, root) if b]:
        base = base if base.endswith("/") else base + "/"
        feed_candidates += [urljoin(base, path) for path in FEED_PATHS]

    tried, directory_pages = set(), []
    for url in feed_candidates:
        if url in tried or len(tried) >= 18:
            continue
        tried.add(url)
        try:
            r = fetch(ctx, url)
            if r.status_code != 200:
                continue
            items = items_from_feed(r.content)
            if items:
                return {"kind": "feed", "url": url}, items
            if _is_html(r) and len(directory_pages) < 2:
                directory_pages.append(r)
        except Exception:  # noqa: BLE001
            continue
    # A page like /rss/ that lists the outlet's feeds: follow the links on it.
    for page in directory_pages:
        soup = BeautifulSoup(page.text, "html.parser")
        links = [urljoin(page.url, a["href"]) for a in soup.find_all("a", href=True)
                 if FEEDLIKE.search(a["href"]) or a["href"].lower().endswith((".xml", ".rss"))]
        for url in [u for u in dict.fromkeys(links) if u not in tried][:6]:
            tried.add(url)
            try:
                r = fetch(ctx, url)
                items = items_from_feed(r.content) if r.status_code == 200 else None
                if items:
                    return {"kind": "feed", "url": url}, items
            except Exception:  # noqa: BLE001
                continue

    # News sitemaps: listed in robots.txt, or at the usual addresses.
    sitemaps = []
    try:
        rb = fetch(ctx, urljoin(root, "robots.txt"))
        if rb.status_code == 200:
            for line in rb.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemaps.append(line.split(":", 1)[1].strip())
    except Exception:  # noqa: BLE001
        pass
    sitemaps.sort(key=lambda u: "news" not in u.lower())
    sitemaps += [urljoin(root, path) for path in SITEMAP_PATHS]
    for url in list(dict.fromkeys(sitemaps))[:10]:
        try:
            items = items_from_sitemap(ctx, url)
            if items is not None:
                return {"kind": "sitemap", "url": url}, items
        except Exception:  # noqa: BLE001
            continue
    reasons.append("no RSS feed or news sitemap found")
    return None, "; ".join(reasons)


def _wants_full_text(row) -> bool:
    return (row.get("full_text") or "yes").strip().lower() not in ("no", "n", "false", "0")


def outlets(ctx) -> list[dict]:
    rows = load_csv("outlets.csv")
    sources = ctx.state.setdefault("outlet_sources", {})
    # Feeds found by the first version of the monitor are still good.
    for site, url in (ctx.state.get("discovered_feeds") or {}).items():
        sources.setdefault(site, {"kind": "feed", "url": url})
    failed = ctx.state.setdefault("outlet_source_failed", {})
    seen = ctx.state.setdefault("seen_articles", {})
    today = now_utc().strftime("%Y-%m-%d")
    retry_after = (now_utc() - timedelta(days=7)).strftime("%Y-%m-%d")  # look again weekly
    results, queue, search_only = [], [], []

    def load(row):
        site = row.get("website", "")
        info = {"name": row.get("name"), "website": site, "feed": "", "source": "",
                "status": "ok", "items": 0, "mentions": 0, "message": ""}
        candidates = []
        if row.get("feed"):
            candidates.append({"kind": "feed", "url": row["feed"]})
        if site in sources and sources[site] not in candidates:
            candidates.append(sources[site])
        errors = []
        for src in candidates:
            try:
                items = load_source(ctx, src)
                info.update(feed=src["url"], source=src["kind"])
                return row, items, info
            except Exception as e:  # noqa: BLE001
                errors.append(f"{src['url']}: {e}")
        if site and failed.get(site, {}).get("date", "") < retry_after:
            src, result = discover_source(ctx, site)
            if src:
                sources[site] = src
                failed.pop(site, None)
                info.update(feed=src["url"], source=src["kind"],
                            message=f"Found {'RSS feed' if src['kind'] == 'feed' else 'news sitemap'} automatically")
                return row, result, info
            failed[site] = {"date": today, "reason": result}
            errors.append(result)
        elif site:
            errors.append(failed[site].get("reason", "") + " (checked again weekly)")
        info["status"] = "error"
        info["message"] = "; ".join(e for e in errors if e)[:280]
        return row, None, info

    for row, items, info in ctx.parallel(load, rows, workers=24):
        ctx.outlet_health.append(info)
        if items is None:
            if row.get("website"):
                search_only.append((row, info))
            continue
        for it in items:
            key = normalise_url(it["link"])
            if too_old(it["published"], ctx.max_age_days) or key in seen:
                continue
            info["items"] += 1
            m = ctx.keywords.match(it["title"], it["text"], _slug_title(it["link"]))
            if m:
                seen[key] = today
                info["mentions"] += 1
                results.append(_outlet_mention(row, it["link"], it["title"], it["published"], m,
                                               "Outlet " + ("feed" if info["source"] == "feed" else "sitemap")))
            elif _wants_full_text(row):
                queue.append((row, info, key, it["link"], it["title"], it["published"]))
            else:
                seen[key] = today  # headline-only outlet: checked, nothing found

    # Read full articles, taking turns between outlets so big sites don't crowd out small ones.
    by_outlet: dict[str, list] = {}
    for q in queue:
        by_outlet.setdefault(q[0].get("name", ""), []).append(q)
    for lst in by_outlet.values():
        lst.sort(key=lambda q: q[5].timestamp() if q[5] else 0, reverse=True)
    ordered = [q for group in zip_longest(*by_outlet.values()) for q in group if q]
    limit = int(ctx.settings.get("max_article_downloads", 8000))
    to_fetch = ordered[:limit]

    # Leave ~10 minutes for the other sources and for saving.
    stop_at = ctx.deadline - 600

    def check(item):
        if time.time() > stop_at:
            return item, None, None  # out of time: leave for the next run
        row, info, key, link, title, published = item
        text = article_text(ctx, link)
        return item, (ctx.keywords.match(text) if text else None), bool(text)

    downloaded, skipped = 0, 0
    for item, m, ok in ctx.parallel(check, to_fetch, workers=24):
        row, info, key, link, title, published = item
        if ok is None:
            skipped += 1
            continue
        seen[key] = today  # don't retry blocked/paywalled pages every run
        if ok:
            downloaded += 1
        if m:
            info["mentions"] += 1
            results.append(_outlet_mention(row, link, title or _slug_title(link), published, m, "Outlet article"))

    # Outlets we can't read directly: ask Google News for mentions on those sites.
    results += site_search(ctx, search_only)

    counts = {k: sum(1 for i in ctx.outlet_health if i["status"] == "ok" and i["source"] == k)
              for k in ("feed", "sitemap")}
    partial = sum(1 for i in ctx.outlet_health if i["status"] == "partial")
    ctx.record("outlets", ok=bool(counts["feed"] or counts["sitemap"]),
               checked=downloaded + len(results), found=len(results),
               message=(f"{counts['feed']} read by RSS feed, {counts['sitemap']} by news sitemap, "
                        f"{partial} by Google News site search only, of {len(rows)} outlets; "
                        f"{downloaded} full articles read; {max(0, len(ordered) - limit) + skipped} left for next run"))
    return results


def _edition_for(ctx, lang: str):
    editions = ctx.settings.get("google_news_editions") or [["en-US", "US", "US:en"]]
    lang = (lang or "en").lower()
    for ed in editions:
        if str(ed[0]).lower().split("-")[0] == lang:
            return tuple(ed)
    return ("en-US", "US", "US:en")


def site_search(ctx, outlets_list) -> list[dict]:
    """Google News search for the keywords, restricted to batches of sites."""
    if not outlets_list:
        return []
    groups: dict[tuple, list] = {}
    for row, info in outlets_list:
        dom = domain_of(row["website"])
        if dom:
            groups.setdefault(_edition_for(ctx, row.get("language")), []).append((dom, row, info))
    kw = ctx.keywords.search_query(quoted=True)
    results, errors, queries, rejected = [], 0, 0, 0
    for (hl, gl, ceid), members in groups.items():
        for i in range(0, len(members), 12):
            batch = members[i:i + 12]
            sites = " OR ".join(f"site:{d}" for d, _, _ in batch)
            q = f"({kw}) ({sites}) when:{ctx.lookback_days}d"
            url = (f"https://news.google.com/rss/search?q={quote_plus(q)}"
                   f"&hl={hl}&gl={gl}&ceid={quote_plus(ceid)}")
            queries += 1
            try:
                r = ctx.get(url, timeout=25)
                r.raise_for_status()
                feed = feedparser.parse(r.content)
            except Exception:  # noqa: BLE001
                errors += 1
                feed = None
            for d, row, info in batch:
                if feed is not None:
                    info["status"] = "partial"
                    info["message"] = ("Can't read this site directly (" + info["message"][:150] +
                                       "). Covered by Google News headline search of the site, "
                                       "plus GDELT and the general Google News search.")
            if feed is None:
                continue
            for e in feed.entries:
                src = domain_of((e.get("source") or {}).get("href", ""))
                match = next(((r_, inf) for d, r_, inf in batch
                              if src and (src == d or src.endswith("." + d) or d.endswith("." + src))), None)
                if not match:
                    continue
                row, info = match
                title = e.get("title", "")
                outlet = (e.get("source") or {}).get("title", "")
                if outlet and title.endswith(" - " + outlet):
                    title = title[: -len(outlet) - 3]
                # Google loosens multi-site searches, so only trust results that name Bellingcat.
                m = ctx.keywords.match(title, strip_html(e.get("summary", "")))
                if not m:
                    rejected += 1
                    continue
                info["mentions"] += 1
                rec = _outlet_mention(row, e.get("link", ""), title, parse_date(e.get("published")),
                                      m, "Google News site search")
                rec["domain"] = src
                results.append(rec)
            time.sleep(1.0)  # be gentle with Google
    print(f"[outlets] Google News site search: {queries} queries, {errors} failed, "
          f"{len(results)} mentions, {rejected} unrelated results ignored")
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


# ------------------------------------------------------------------ Bing News and Google Alerts
#
# Search engines are allowed to read the full text of most news sites, including paywalled
# ones like the Wall Street Journal, so their results catch mentions buried in article bodies
# on sites the monitor can't read itself.

def _real_url(link: str) -> str:
    """Unwrap Bing/Google redirect links to the article's own address."""
    try:
        q = parse_qs(urlparse(link).query)
        for key in ("url", "q"):
            if q.get(key) and q[key][0].startswith("http"):
                return q[key][0]
    except ValueError:
        pass
    return link


DEFAULT_BING_QUERIES = [
    ["Bellingcat", "en-US"], ["Bellingcat", "en-GB"], ["Bellingcat", "nl-NL"], ["Bellingcat", "de-DE"],
    ["Bellingcat", "fr-FR"], ["Bellingcat", "es-ES"], ["Bellingcat", "it-IT"], ["Bellingcat", "pt-BR"],
    ["Bellingcat", "pl-PL"], ["Bellingcat", "tr-TR"], ["Bellingcat", "en-IN"], ["Беллингкэт", "ru-RU"],
    ["Беллінгкет", "uk-UA"], ["بيلينغكات", "ar-XA"], ["ベリングキャット", "ja-JP"],
]


def bing_news(ctx) -> list[dict]:
    queries = ctx.settings.get("bing_news_queries") or DEFAULT_BING_QUERIES
    results, checked, errors, seen_links = [], 0, [], set()
    for term, market in queries:
        params = {"q": f'"{term}"', "format": "rss", "mkt": market, "count": 100,
                  "qft": 'interval="8"'}  # past week
        try:
            r = ctx.get("https://www.bing.com/news/search", params=params, timeout=25)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{term}/{market}: {e}")
            continue
        for e in feed.entries:
            checked += 1
            url = _real_url(e.get("link", ""))
            if not url or url in seen_links:
                continue
            seen_links.add(url)
            published = parse_date(e.get("published"))
            if too_old(published, ctx.max_age_days):
                continue
            title, summary = strip_html(e.get("title", "")), strip_html(e.get("summary", ""))
            m = ctx.keywords.match(title, summary) or {
                "term": term, "category": "main", "snippet": SEARCH_NOTE.format(name="Bing News")}
            source = (e.get("news_source") or e.get("source", {}).get("title") or domain_of(url))
            results.append(mention(url=url, title=title, snippet=m["snippet"], source=source,
                                   channel=CHANNEL_NEWS, platform="Web", language=market.split("-")[0],
                                   published=published, match=m, via="Bing News"))
            results[-1]["_needs_snippet"] = PLACEHOLDER_MARK in m["snippet"]
    ctx.record("bing_news", ok=len(errors) < len(queries), checked=checked,
               found=len(results), message="; ".join(errors)[:280])
    return results


def google_alerts(ctx) -> list[dict]:
    feeds = [f for f in (ctx.settings.get("google_alerts_feeds") or []) if f and str(f).startswith("http")]
    if not feeds:
        ctx.health["google_alerts"] = {"status": "off", "checked": 0, "found": 0, "time": None,
                                       "message": "No Google Alerts feed added yet (optional, see settings.yml)"}
        return []
    results, checked, errors = [], 0, []
    for feed_url in feeds:
        try:
            r = ctx.get(feed_url, timeout=25)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
            continue
        for e in feed.entries:
            checked += 1
            url = _real_url(e.get("link", ""))
            published = parse_date(e.get("published") or e.get("updated"))
            if not url or too_old(published, ctx.max_age_days):
                continue
            title = strip_html(e.get("title", ""))
            body = " ".join(strip_html(c.get("value", "")) for c in e.get("content", []) or [])
            summary = body or strip_html(e.get("summary", ""))
            m = ctx.keywords.match(title, summary) or {
                "term": "Bellingcat", "category": "main", "snippet": SEARCH_NOTE.format(name="Google Alerts")}
            results.append(mention(url=url, title=title, snippet=m["snippet"], source=domain_of(url),
                                   channel=CHANNEL_NEWS, platform="Web", language="",
                                   published=published, match=m, via="Google Alerts"))
    ctx.record("google_alerts", ok=len(errors) < len(feeds), checked=checked,
               found=len(results), message="; ".join(errors)[:280])
    return results
