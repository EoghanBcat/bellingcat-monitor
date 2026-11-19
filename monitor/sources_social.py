"""Social, podcast and video sources: Bluesky, Mastodon, Telegram, Hacker News,
Apple Podcasts and YouTube."""
from __future__ import annotations

from datetime import timedelta

from bs4 import BeautifulSoup

from .common import (CHANNEL_PODCAST, CHANNEL_SOCIAL, CHANNEL_VIDEO, env, load_csv, mention,
                     now_utc, parse_date, strip_html, too_old)


def _search_terms(ctx, n=3):
    terms = [t for t, c in ctx.keywords.terms if c == "main" and "." not in t]
    return terms[:n] or ["Bellingcat"]


# ------------------------------------------------------------------ Bluesky

def bluesky(ctx) -> list[dict]:
    handle, password = env("BSKY_HANDLE"), env("BSKY_APP_PASSWORD")
    base, headers, note = "https://public.api.bsky.app", {}, ""
    results, checked, seen = [], 0, set()

    def login():
        r = ctx.session.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                             json={"identifier": handle, "password": password}, timeout=20)
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['accessJwt']}"}

    cutoff = now_utc() - timedelta(days=ctx.lookback_days)
    pages = [(t, None) for t in _search_terms(ctx)]
    page_count = {}
    while pages:
        term, cursor = pages.pop(0)
        page_count[term] = page_count.get(term, 0) + 1
        url = f"{base}/xrpc/app.bsky.feed.searchPosts"
        params = {"q": term, "sort": "latest", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = ctx.get(url, params=params, headers=headers)
        if r.status_code in (401, 403) and not headers:
            if not (handle and password):
                ctx.record("bluesky", ok=False, message=(
                    "Bluesky now requires a login for search. Add the BSKY_HANDLE and "
                    "BSKY_APP_PASSWORD secrets (see setup guide)."))
                return results
            headers = login()
            base = "https://bsky.social"
            note = "Using Bluesky login"
            r = ctx.get(f"{base}/xrpc/app.bsky.feed.searchPosts", params=params, headers=headers)
        if cursor and r.status_code >= 400:
            continue  # later pages not available; keep what we have
        r.raise_for_status()
        data = r.json()
        oldest = None
        for p in data.get("posts", []):
            checked += 1
            if p.get("uri") in seen:
                continue
            seen.add(p.get("uri"))
            rec = p.get("record") or {}
            text = rec.get("text", "")
            published = parse_date(rec.get("createdAt") or p.get("indexedAt"))
            if published and (oldest is None or published < oldest):
                oldest = published
            if too_old(published, ctx.max_age_days):
                continue
            m = ctx.keywords.match(text)
            if not m:
                continue
            author = (p.get("author") or {})
            h = author.get("handle", "")
            rkey = p.get("uri", "").rsplit("/", 1)[-1]
            langs = rec.get("langs") or [""]
            results.append(mention(
                url=f"https://bsky.app/profile/{h}/post/{rkey}",
                title=f"{author.get('displayName') or h} on Bluesky", snippet=m["snippet"],
                source="Bluesky", channel=CHANNEL_SOCIAL, platform="Bluesky",
                language=langs[0], published=published, match=m, via="Bluesky search",
                author=h))
        if data.get("cursor") and oldest and oldest > cutoff and page_count[term] < 10:
            pages.append((term, data["cursor"]))
    ctx.record("bluesky", ok=True, checked=checked, found=len(results), message=note)
    return results


# ------------------------------------------------------------------ Mastodon

def mastodon(ctx) -> list[dict]:
    servers = ctx.settings.get("mastodon_servers") or ["mastodon.social"]
    results, checked, errors = [], 0, []
    for server in servers:
        posts, max_id = [], None
        try:
            for _ in range(5):
                params = {"limit": 40}
                if max_id:
                    params["max_id"] = max_id
                r = ctx.get(f"https://{server}/api/v1/timelines/tag/bellingcat", params=params)
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                posts.extend(batch)
                max_id = batch[-1].get("id")
                if not max_id:
                    break
                last = parse_date(batch[-1].get("created_at"))
                if last and last < now_utc() - timedelta(days=ctx.lookback_days):
                    break
        except Exception as e:  # noqa: BLE001
            if not posts:
                errors.append(f"{server}: {e}")
                continue
        for p in posts:
            checked += 1
            text = strip_html(p.get("content", ""))
            published = parse_date(p.get("created_at"))
            if too_old(published, ctx.max_age_days):
                continue
            m = ctx.keywords.match(text)
            if not m:
                continue
            acct = (p.get("account") or {})
            results.append(mention(
                url=p.get("url") or p.get("uri", ""),
                title=f"{acct.get('display_name') or acct.get('acct')} on Mastodon",
                snippet=m["snippet"], source="Mastodon", channel=CHANNEL_SOCIAL,
                platform="Mastodon", language=p.get("language"), published=published,
                match=m, via=f"Mastodon ({server})", author=acct.get("acct")))
    ctx.record("mastodon", ok=len(errors) < len(servers), checked=checked,
               found=len(results), message="; ".join(errors))
    return results


# ------------------------------------------------------------------ Telegram

def telegram(ctx) -> list[dict]:
    rows = load_csv("telegram_channels.csv")
    results, checked, errors = [], 0, []

    cutoff = now_utc() - timedelta(days=ctx.lookback_days)

    def fetch(row):
        """Read the channel's public web page, going back page by page to the cutoff."""
        ch = row.get("channel", "").lstrip("@").strip()
        pages, before = [], None
        try:
            for _ in range(40):
                url = f"https://t.me/s/{ch}" + (f"?before={before}" if before else "")
                r = ctx.get(url)
                r.raise_for_status()
                pages.append(r.text)
                soup = BeautifulSoup(r.text, "html.parser")
                msgs = soup.select("div.tgme_widget_message[data-post]")
                if not msgs:
                    break
                ids = [int(m["data-post"].rsplit("/", 1)[-1]) for m in msgs
                       if m["data-post"].rsplit("/", 1)[-1].isdigit()]
                times = [parse_date(t["datetime"]) for t in soup.select("time[datetime]")]
                times = [t for t in times if t]
                if not ids or not times or min(times) < cutoff or min(ids) <= 1:
                    break
                before = min(ids)
            return row, ch, "\n".join(pages), None
        except Exception as e:  # noqa: BLE001
            if pages:
                return row, ch, "\n".join(pages), None
            return row, ch, None, f"{ch}: {e}"

    for row, ch, page, err in ctx.parallel(fetch, rows, workers=6):
        if err:
            errors.append(err)
            continue
        soup = BeautifulSoup(page, "html.parser")
        msgs = soup.select("div.tgme_widget_message[data-post]")
        if not msgs:
            errors.append(f"{ch}: no public posts found (channel name wrong or private?)")
        for msg in msgs:
            checked += 1
            text_el = msg.select_one(".tgme_widget_message_text")
            text = text_el.get_text(" ", strip=True) if text_el else ""
            time_el = msg.select_one("time[datetime]")
            published = parse_date(time_el["datetime"]) if time_el else None
            if too_old(published, ctx.max_age_days):
                continue
            m = ctx.keywords.match(text)
            if not m:
                continue
            results.append(mention(
                url=f"https://t.me/{msg['data-post']}",
                title=f"{row.get('name') or ch} on Telegram", snippet=m["snippet"],
                source=row.get("name") or ch, channel=CHANNEL_SOCIAL, platform="Telegram",
                language=row.get("language"), published=published, match=m,
                via="Telegram channel", author=ch))
    ctx.record("telegram", ok=len(errors) < max(1, len(rows)), checked=checked,
               found=len(results), message="; ".join(errors))
    return results


# ------------------------------------------------------------------ Hacker News

def hacker_news(ctx) -> list[dict]:
    since = int((now_utc() - timedelta(days=ctx.lookback_days)).timestamp())
    r = ctx.get("https://hn.algolia.com/api/v1/search_by_date", params={
        "query": "bellingcat", "numericFilters": f"created_at_i>{since}", "hitsPerPage": 100})
    r.raise_for_status()
    hits = r.json().get("hits", [])
    results = []
    for h in hits:
        title = h.get("title") or h.get("story_title") or ""
        text = strip_html(h.get("comment_text") or h.get("story_text") or "")
        m = ctx.keywords.match(title, text, h.get("url"))
        if not m:
            continue
        kind = "comment" if h.get("comment_text") else "story"
        results.append(mention(
            url=f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            title=f"Hacker News {kind}: {title}", snippet=m["snippet"], source="Hacker News",
            channel=CHANNEL_SOCIAL, platform="Hacker News", language="en",
            published=parse_date(h.get("created_at")), match=m, via="Hacker News search",
            author=h.get("author")))
    ctx.record("hacker_news", ok=True, checked=len(hits), found=len(results))
    return results


# ------------------------------------------------------------------ Podcasts

def podcasts(ctx) -> list[dict]:
    results, checked, seen = [], 0, set()
    for term in _search_terms(ctx, 2):
        r = ctx.get("https://itunes.apple.com/search", params={
            "term": term, "media": "podcast", "entity": "podcastEpisode", "limit": 200})
        r.raise_for_status()
        for ep in r.json().get("results", []):
            checked += 1
            url = ep.get("trackViewUrl") or ep.get("episodeUrl")
            if not url or url in seen:
                continue
            seen.add(url)
            published = parse_date(ep.get("releaseDate"))
            if too_old(published, max(ctx.max_age_days, 30 if ctx.first_run else 0)):
                continue
            m = ctx.keywords.match(ep.get("trackName"), ep.get("description"),
                                   ep.get("shortDescription"))
            if not m:
                continue
            results.append(mention(
                url=url, title=ep.get("trackName", ""), snippet=m["snippet"],
                source=ep.get("collectionName") or "Podcast", channel=CHANNEL_PODCAST,
                platform="Apple Podcasts", language="", published=published, match=m,
                via="Apple Podcasts search"))
    ctx.record("podcasts", ok=True, checked=checked, found=len(results),
               message="Searches episode titles and descriptions (not the audio itself)")
    return results


# ------------------------------------------------------------------ YouTube

def youtube(ctx) -> list[dict]:
    key = env("YOUTUBE_API_KEY")
    if not key:
        ctx.health["youtube"] = {"status": "off", "checked": 0, "found": 0,
                                 "message": "No YOUTUBE_API_KEY secret added (optional)", "time": None}
        return []
    after = (now_utc() - timedelta(days=ctx.lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    results, checked = [], 0
    for term in _search_terms(ctx, 2):
        r = ctx.get("https://www.googleapis.com/youtube/v3/search", params={
            "part": "snippet", "q": term, "type": "video", "order": "date",
            "publishedAfter": after, "maxResults": 50, "key": key})
        r.raise_for_status()
        for it in r.json().get("items", []):
            checked += 1
            sn = it.get("snippet") or {}
            m = ctx.keywords.match(sn.get("title"), sn.get("description"))
            if not m:
                continue
            vid = (it.get("id") or {}).get("videoId")
            results.append(mention(
                url=f"https://www.youtube.com/watch?v={vid}", title=strip_html(sn.get("title", "")),
                snippet=m["snippet"], source=sn.get("channelTitle") or "YouTube",
                channel=CHANNEL_VIDEO, platform="YouTube", language=sn.get("defaultAudioLanguage", ""),
                published=parse_date(sn.get("publishedAt")), match=m, via="YouTube search"))
    ctx.record("youtube", ok=True, checked=checked, found=len(results),
               message="Searches video titles and descriptions")
    return results
