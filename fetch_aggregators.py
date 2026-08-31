"""Company-agnostic job sources. No API keys, no curated company list.

These aggregate across thousands of employers, and several expose STRUCTURED
geographic eligibility (locationRestrictions / candidate_required_location),
which is far more reliable for hard filtering than regex over prose.
"""
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fetch_jobs import Job, UA, _strip_html


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def _iso(v):
    """Sources use epoch seconds, epoch ms, or ISO strings. Normalize all three."""
    if not v:
        return ""
    if isinstance(v, (int, float)):
        if v > 1e11:
            v /= 1000
        return datetime.fromtimestamp(v, timezone.utc).date().isoformat()
    return str(v)[:10]


def from_remoteok():
    for j in _get("https://remoteok.com/api"):
        if not j.get("position"):
            continue                       # first element is a legal notice
        yield Job(
            source="remoteok", company=j.get("company", ""), title=j["position"],
            location=j.get("location") or "Remote", remote=True,
            department=", ".join(j.get("tags") or [])[:80],
            description=_strip_html(j.get("description")),
            url=j.get("url", ""), apply_url=j.get("apply_url") or j.get("url", ""),
            posted_at=_iso(j.get("date")), external_id=f"remoteok-{j.get('id')}",
            salary_min=j.get("salary_min") or None, salary_max=j.get("salary_max") or None,
        )


def from_remotive():
    d = _get("https://remotive.com/api/remote-jobs")
    for j in d.get("jobs", []):
        yield Job(
            source="remotive", company=j.get("company_name", ""), title=j.get("title", ""),
            location=j.get("candidate_required_location") or "Remote", remote=True,
            department=j.get("category", ""),
            description=_strip_html(j.get("description")),
            url=j.get("url", ""), apply_url=j.get("url", ""),
            posted_at=_iso(j.get("publication_date")), external_id=f"remotive-{j.get('id')}",
            # this is the field that matters: who is eligible to be hired
            location_restrictions=[s.strip() for s in
                                   (j.get("candidate_required_location") or "").split(",") if s.strip()],
        )


def from_himalayas(pages=75):
    """Biggest source: ~102k jobs, cursor-paginated 20 at a time, and the only one
    with explicit locationRestrictions + timezoneRestrictions."""
    cursor = None
    for _ in range(pages):
        url = "https://himalayas.app/jobs/api?limit=100"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"
        d = _get(url)
        jobs = d.get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            yield Job(
                source="himalayas", company=j.get("companyName", ""), title=j.get("title", ""),
                location=", ".join(j.get("locationRestrictions") or []) or "Worldwide",
                remote=True,
                department=", ".join(j.get("categories") or [])[:80],
                description=_strip_html(j.get("description") or j.get("excerpt")),
                url=j.get("guid", ""), apply_url=j.get("applicationLink") or j.get("guid", ""),
                posted_at=_iso(j.get("pubDate")),
                external_id=f"himalayas-{j.get('guid','')[-40:]}",
                location_restrictions=j.get("locationRestrictions") or [],
                timezone_restrictions=j.get("timezoneRestrictions") or [],
                salary_min=j.get("minSalary"), salary_max=j.get("maxSalary"),
                seniority_hint=", ".join(j.get("seniority") or []),
            )
        cursor = d.get("nextCursor")
        if not cursor:
            break


def from_jobicy(count=100):
    d = _get(f"https://jobicy.com/api/v2/remote-jobs?count={count}")
    for j in d.get("jobs", []):
        geo = j.get("jobGeo") or ""
        yield Job(
            source="jobicy", company=j.get("companyName", ""), title=j.get("jobTitle", ""),
            location=geo or "Remote", remote=True,
            department=", ".join(j.get("jobIndustry") or []),
            description=_strip_html(j.get("jobDescription") or j.get("jobExcerpt")),
            url=j.get("url", ""), apply_url=j.get("url", ""),
            posted_at=_iso(j.get("pubDate")), external_id=f"jobicy-{j.get('id')}",
            location_restrictions=[s.strip() for s in geo.split(",") if s.strip()],
            salary_min=j.get("salaryMin"), salary_max=j.get("salaryMax"),
            seniority_hint=j.get("jobLevel") or "",
        )


def from_workingnomads():
    for j in _get("https://www.workingnomads.com/api/exposed_jobs/"):
        yield Job(
            source="workingnomads", company=j.get("company_name", ""), title=j.get("title", ""),
            location=j.get("location") or "Remote", remote=True,
            department=j.get("category_name", ""),
            description=_strip_html(j.get("description")),
            url=j.get("url", ""), apply_url=j.get("url", ""),
            posted_at=_iso(j.get("pub_date")),
            external_id=f"wn-{(j.get('url') or '')[-20:]}",
            location_restrictions=[j["location"]] if j.get("location") else [],
        )


def from_arbeitnow(pages=6):
    url = "https://www.arbeitnow.com/api/job-board-api"
    for _ in range(pages):
        d = _get(url)
        for j in d.get("data", []):
            yield Job(
                source="arbeitnow", company=j.get("company_name", ""), title=j.get("title", ""),
                location=j.get("location") or "", remote=bool(j.get("remote")),
                department=", ".join(j.get("tags") or [])[:80],
                description=_strip_html(j.get("description")),
                url=j.get("url", ""), apply_url=j.get("url", ""),
                posted_at=_iso(j.get("created_at")), external_id=f"arbeitnow-{j.get('slug','')[:40]}",
            )
        url = (d.get("links") or {}).get("next")
        if not url:
            break


SOURCES = {
    "himalayas": from_himalayas, "remotive": from_remotive, "remoteok": from_remoteok,
    "jobicy": from_jobicy, "workingnomads": from_workingnomads, "arbeitnow": from_arbeitnow,
}


def fetch_all(only=None):
    import sys
    jobs = []
    for name, fn in SOURCES.items():
        if only and name not in only:
            continue
        try:
            got = list(fn())
            jobs.extend(got)
            print(f"  {name:15} {len(got):5} roles", file=sys.stderr)
        except Exception as e:
            print(f"  {name:15} FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    return jobs


if __name__ == "__main__":
    import sys
    from dataclasses import asdict
    jobs = fetch_all()
    print(f"\ntotal {len(jobs)} from {len(SOURCES)} aggregators, "
          f"{len({j.company for j in jobs})} distinct companies", file=sys.stderr)
    json.dump([asdict(j) for j in jobs], open("jobs_agg.json", "w"), indent=2)
