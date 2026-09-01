"""The one job shape the rest of the pipeline sees, plus shared HTTP helpers.

This module exists so `ats.py` and `aggregators.py` are siblings rather than one
importing from the other. Every source's job is to produce a `Job`; nothing
downstream should ever need to know which board it came from.
"""
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Job:
    source: str
    company: str
    title: str
    location: str
    remote: bool
    department: str
    description: str
    url: str
    apply_url: str
    posted_at: str
    external_id: str
    # aggregators supply these as STRUCTURED fields; ATS boards do not
    location_restrictions: list = field(default_factory=list)
    timezone_restrictions: list = field(default_factory=list)
    salary_min: int | None = None
    salary_max: int | None = None
    seniority_hint: str = ""
    # which track surfaced this: "pk" (curated local) or "remote" (wide sweep)
    track: str = ""


# Some boards (Ashby) 403 the default "Python-urllib/x.y" agent, so send a real one.
# Deliberately contains no identifying information: these requests go to boards of
# companies you may apply to, and their logs are not a place to disclose anything.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Deep pagination WILL get you rate-limited (Himalayas answers 429 after a few
# dozen rapid pages). These are other people's servers and we are an unattended
# job that runs daily, so throttle by default and back off when asked to.
MIN_INTERVAL = 1.0          # seconds between requests
_last_call = [0.0]


def get(url, timeout=45, tries=4, accept="application/json"):
    """Throttled GET with retry on 429/503. Returns parsed JSON."""
    for attempt in range(tries):
        wait = MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 429/503 are "slow down", not "give up". Honour Retry-After if sent.
            if e.code not in (429, 503) or attempt == tries - 1:
                raise
            backoff = float(e.headers.get("Retry-After") or 0) or 2 ** (attempt + 2)
            print(f"    rate-limited ({e.code}), sleeping {backoff:.0f}s", file=sys.stderr)
            time.sleep(backoff)


def get_text(url, timeout=45, tries=4, accept="text/html,application/xml"):
    """Same throttle/backoff, but returns raw text -- for sitemaps and careers pages."""
    for attempt in range(tries):
        wait = MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = r.headers.get_content_charset() or "utf-8"
                return raw.decode(enc, "replace")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == tries - 1:
                raise
            backoff = float(e.headers.get("Retry-After") or 0) or 2 ** (attempt + 2)
            print(f"    rate-limited ({e.code}), sleeping {backoff:.0f}s", file=sys.stderr)
            time.sleep(backoff)


def strip_html(s):
    """Greenhouse double-escapes its HTML, so unescape twice, then de-tag."""
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]*\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", s)).strip()


def iso(v):
    """Sources use epoch seconds, epoch ms, or ISO strings. Normalize all three."""
    if not v:
        return ""
    if isinstance(v, (int, float)):
        if v > 1e11:
            v /= 1000
        return datetime.fromtimestamp(v, timezone.utc).date().isoformat()
    return str(v)[:10]
