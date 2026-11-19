"""Run context shared by all collectors: settings, HTTP session, state and health log."""
from __future__ import annotations

import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import ROOT, Keywords, load_settings, make_session, now_utc, iso

STATE_FILE = ROOT / "state" / "state.json"


class Context:
    def __init__(self, session=None, settings=None, keywords=None, state=None):
        self.settings = settings if settings is not None else load_settings()
        self.session = session or make_session()
        self.keywords = keywords or Keywords()
        self.state = state if state is not None else load_state()
        self.max_age_days = int(self.settings.get("max_age_days", 14))
        self.first_run = not self.state.get("last_run")
        self.lookback_days = 7 if self.first_run else int(self.settings.get("lookback_days", 3))
        self.health: dict[str, dict] = {}
        self.outlet_health: list[dict] = []

    def enabled(self, name: str) -> bool:
        return bool(self.settings.get("sources", {}).get(name, True))

    def record(self, source: str, *, ok: bool, checked: int = 0, found: int = 0, message: str = ""):
        self.health[source] = {
            "status": "ok" if ok else "error",
            "checked": checked,
            "found": found,
            "message": message[:300],
            "time": iso(now_utc()),
        }

    def get(self, url: str, **kw):
        kw.setdefault("timeout", 25)
        return self.session.get(url, **kw)

    def parallel(self, fn, items, workers: int = 12):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(fn, items))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def safe_run(ctx: Context, name: str, fn) -> list[dict]:
    """Run one collector; never let one failing source stop the others."""
    if not ctx.enabled(name):
        ctx.health[name] = {"status": "off", "checked": 0, "found": 0,
                            "message": "Switched off in settings.yml", "time": iso(now_utc())}
        return []
    try:
        results = fn(ctx) or []
        if name not in ctx.health:
            ctx.record(name, ok=True, found=len(results))
        return results
    except Exception as e:  # noqa: BLE001
        print(f"[{name}] FAILED: {e}")
        traceback.print_exc()
        ctx.record(name, ok=False, message=f"{type(e).__name__}: {e}")
        return []
