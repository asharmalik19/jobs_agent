"""Pakistani job boards -- the part of Track A that no ATS API covers.

Why this file has to exist: of 40 good Pakistani employers probed, exactly THREE
have a Greenhouse/Ashby/Lever board (Careem, Educative, Tajir). Local hiring does
not run on the Western ATS rails, so without a board source Track A sees almost
nothing.

Both boards were checked against their robots.txt first; both permit this, and
both publish a sitemap, which is the polite way in -- one cheap request tells us
what exists and when it changed, so we only fetch detail pages we actually want.

    mustakbil   IMPLEMENTED. Sitemap is live (lastmod within days) and every job
                page carries a schema.org JobPosting blob: title, description,
                company, city, salary, monthsOfExperience. Structured data beats
                scraping, so we parse that.

    rozee.pk    DEFERRED, deliberately. It is the bigger board but currently the
                worse target: its sitemap's lastmod was ~3 months stale, detail
                pages carry no JSON-LD, and search results are rendered
                client-side so there are no job links in the server HTML. Getting
                fresh Rozee data needs a real browser -- which spec.md already
                plans for (Playwright), so this belongs in that stage, not here.
"""
import json
import re
import sys
from datetime import date, datetime, timedelta

from .models import Job, get_text, strip_html

MUSTAKBIL_INDEX = "https://sitemaps.mustakbil.com/sitemaps"
LD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
URL_LASTMOD_RE = re.compile(r"<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>")


def _sitemap_pages(index_url, kind):
    """The index lists per-type sitemaps; return the ones matching `kind`."""
    xml = get_text(index_url)
    return [u for u in re.findall(r"<loc>(.*?)</loc>", xml) if f"/{kind}/" in u]


def _job_urls(since_days):
    """(url, lastmod) for jobs touched inside the window, newest first."""
    cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    out = []
    for page in _sitemap_pages(MUSTAKBIL_INDEX, "jobs"):
        for url, lastmod in URL_LASTMOD_RE.findall(get_text(page)):
            if lastmod[:10] >= cutoff:
                out.append((url, lastmod))
    out.sort(key=lambda p: p[1], reverse=True)
    return out


def _parse_jobposting(html_text):
    for m in LD_RE.finditer(html_text):
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for node in (d if isinstance(d, list) else [d]):
            if isinstance(node, dict) and node.get("@type") == "JobPosting":
                return node
    return None


def _dig(d, *path, default=""):
    for k in path:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return d if d not in (None, "") else default


def from_mustakbil(since_days=21, skip=(), max_jobs=400):
    """Yield Jobs for postings updated in the last `since_days`.

    `skip` is a set of external_ids already in seen.sqlite. Passing it matters:
    each job costs one HTTP request at a 1s throttle, so not refetching what we
    already have is the difference between a 30-second run and a 7-minute one.
    """
    urls = _job_urls(since_days)
    print(f"  mustakbil       {len(urls)} jobs in last {since_days}d", file=sys.stderr)
    fetched = skipped = failed = 0
    for url, lastmod in urls:
        jid = url.rstrip("/").rsplit("/", 1)[-1]
        ext = f"mustakbil-{jid}"
        if ext in skip:
            skipped += 1
            continue
        if fetched >= max_jobs:
            break
        try:
            post = _parse_jobposting(get_text(url))
        except Exception as e:
            failed += 1
            continue
        if not post:
            failed += 1
            continue
        fetched += 1

        city = _dig(post, "jobLocation", "address", "addressLocality")
        country = _dig(post, "jobLocation", "address", "addressCountry")
        # Normalise to a string the existing geo classifier already understands:
        # hard_filter.COUNTRY_HINTS matches "Pakistan", not the ISO code "PK".
        loc = ", ".join(p for p in [city, "Pakistan" if country == "PK" else country] if p)

        desc = strip_html(post.get("description") or "")
        months = _dig(post, "experienceRequirements", "monthsOfExperience", default=None)
        sal = _dig(post, "baseSalary", "value", default={})

        yield Job(
            source="mustakbil",
            company=_dig(post, "hiringOrganization", "name") or "",
            title=post.get("title") or "",
            location=loc or "Pakistan",
            remote=bool(re.search(r"\bremote\b|work from home", desc, re.I)),
            department="",
            description=desc,
            url=url, apply_url=post.get("url") or url,
            posted_at=(post.get("datePosted") or lastmod)[:10],
            external_id=ext,
            # A PK-board posting is by construction a Pakistan-eligible job. Stating
            # it structurally lets bucket_from_restrictions classify it as "local"
            # instead of leaving it to regex over prose.
            location_restrictions=["Pakistan"],
            salary_min=sal.get("minValue") if isinstance(sal, dict) else None,
            salary_max=sal.get("maxValue") if isinstance(sal, dict) else None,
            seniority_hint=f"{months // 12}y+" if isinstance(months, int) and months else "",
            track="pk",
        )
    print(f"  mustakbil       fetched {fetched}, skipped {skipped} (already seen), "
          f"{failed} unparseable", file=sys.stderr)


SOURCES = {"mustakbil": from_mustakbil}


def fetch_all(since_days=21, skip=(), only=None):
    jobs = []
    for name, fn in SOURCES.items():
        if only and name not in only:
            continue
        try:
            jobs.extend(fn(since_days=since_days, skip=skip))
        except Exception as e:
            print(f"  {name:15} FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    return jobs
