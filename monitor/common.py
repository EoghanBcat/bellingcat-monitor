"""Shared helpers: configuration, HTTP, keyword matching, normalisation."""
from __future__ import annotations

import csv
import hashlib
import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "docs" / "data"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 MediaMonitor/1.0"
)

CHANNEL_NEWS = "News & periodicals"
CHANNEL_SOCIAL = "Social media"
CHANNEL_PODCAST = "Podcasts"
CHANNEL_VIDEO = "Video & broadcast"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value) -> datetime | None:
    """Parse the many date formats sources use. Returns aware UTC datetime or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, time.struct_time):
        return datetime(*value[:6], tzinfo=timezone.utc)
    s = str(value).strip()
    # GDELT style: 20261119T101500Z
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", s)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- config

def load_settings() -> dict:
    with open(CONFIG / "settings.yml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_csv(name: str) -> list[dict]:
    path = CONFIG / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = []
        for row in csv.DictReader(f):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if any(row.values()):
                rows.append(row)
        return rows


class Keywords:
    """Loads config/keywords.txt and finds matches in text."""

    def __init__(self, path: Path | None = None):
        path = path or CONFIG / "keywords.txt"
        self.terms: list[tuple[str, str]] = []  # (term, category)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            category = "main"
            if line.lower().startswith("related:"):
                category = "related"
                line = line.split(":", 1)[1].strip()
            if line:
                self.terms.append((line, category))
        # Main terms first so they win over related ones.
        self.terms.sort(key=lambda t: t[1] != "main")

    def search_query(self, max_terms: int = 6) -> str:
        """An OR query of main terms, for search engines."""
        main = [t for t, c in self.terms if c == "main" and "." not in t]
        return " OR ".join(main[:max_terms])

    def match(self, *texts: str | None):
        """Return dict(term, category, snippet) for the first hit, or None."""
        for term, category in self.terms:
            needle = term.casefold()
            for text in texts:
                if not text:
                    continue
                hay = text.casefold()
                idx = hay.find(needle)
                if idx >= 0:
                    return {
                        "term": term,
                        "category": category,
                        "snippet": make_snippet(text, idx, len(term)),
                    }
        return None


def make_snippet(text: str, idx: int, length: int, radius: int = 200) -> str:
    text = re.sub(r"\s+", " ", text)
    # Index may shift slightly after whitespace collapse; re-find nearby.
    start = max(0, idx - radius)
    end = min(len(text), idx + length + radius)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet.split(" ", 1)[-1]
    if end < len(text):
        snippet = snippet.rsplit(" ", 1)[0] + "…"
    return snippet


# ---------------------------------------------------------------- text / urls

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = TAG_RE.sub(" ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ocid|cmpid|at_|ref$|src$|xtor)")


def normalise_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") or host.startswith("amp."):
        host = host.split(".", 1)[1]
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAMS.match(k.lower())])
    path = re.sub(r"/amp/?$", "", p.path).rstrip("/")
    return urlunparse(("https", host, path, "", query, ""))


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def title_key(title: str) -> str:
    t = re.sub(r"[^\w]+", " ", (title or "").casefold()).strip()
    return t[:90]


def make_id(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- http

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-GB,en;q=0.9,*;q=0.5",
    })
    return s


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def mention(*, url: str, title: str, snippet: str, source: str, channel: str,
            platform: str, language: str | None, published: datetime | None,
            match: dict, via: str, author: str | None = None,
            outlet_type: str | None = None, country: str | None = None) -> dict:
    """Build one standard mention record."""
    return {
        "id": make_id(normalise_url(url)),
        "url": url,
        "title": (title or "").strip()[:400] or "(no title)",
        "snippet": (snippet or "").strip()[:700],
        "source": source,
        "domain": domain_of(url),
        "channel": channel,
        "platform": platform,
        "language": language or "",
        "country": country or "",
        "outlet_type": outlet_type or "",
        "author": author or "",
        "published": iso(published),
        "found": iso(now_utc()),
        "term": match["term"],
        "category": match["category"],
        "via": [via],
    }


def too_old(published: datetime | None, max_age_days: int) -> bool:
    if published is None:
        return False
    return published < now_utc() - timedelta(days=max_age_days)
