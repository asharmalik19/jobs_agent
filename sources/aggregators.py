"""Company-agnostic job sources: the WIDE SWEEP behind Track B.

These aggregate across thousands of employers, and several expose STRUCTURED
geographic eligibility (locationRestrictions / candidate_required_location),
which is far more reliable for hard filtering than regex over prose.

Their job is DISCOVERY, not delivery. Aggregator records have truncated
descriptions and apply_urls that point at a redirect rather than the real form,
so what we actually want out of them is the *employer name*: proof that some
company hires globally-remote. `discover_companies()` at the bottom turns a
sweep into registry entries, and from then on we poll that company's ATS directly.
"""
import json
import re
import sys
import urllib.parse

from .models import Job, get, strip_html, iso


def from_remoteok():
    for j in get("https://remoteok.com/api"):
        if not j.get("position"):
            continue                       # first element is a legal notice
        yield Job(
            source="remoteok", company=j.get("company", ""), title=j["position"],
            location=j.get("location") or "Remote", remote=True,
            department=", ".join(j.get("tags") or [])[:80],
            description=strip_html(j.get("description")),
            url=j.get("url", ""), apply_url=j.get("apply_url") or j.get("url", ""),
            posted_at=iso(j.get("date")), external_id=f"remoteok-{j.get('id')}",
            salary_min=j.get("salary_min") or None, salary_max=j.get("salary_max") or None,
        )


def from_remotive():
    d = get("https://remotive.com/api/remote-jobs")
    for j in d.get("jobs", []):
        yield Job(
            source="remotive", company=j.get("company_name", ""), title=j.get("title", ""),
            location=j.get("candidate_required_location") or "Remote", remote=True,
            department=j.get("category", ""),
            description=strip_html(j.get("description")),
            url=j.get("url", ""), apply_url=j.get("url", ""),
            posted_at=iso(j.get("publication_date")), external_id=f"remotive-{j.get('id')}",
            # this is the field that matters: who is eligible to be hired
            location_restrictions=[s.strip() for s in
                                   (j.get("candidate_required_location") or "").split(",") if s.strip()],
        )


def from_himalayas(since_days=14, max_pages=400):
    """Biggest source (~102k jobs) and the only one with explicit
    locationRestrictions + timezoneRestrictions.

    Two things about this API that are easy to get wrong:
      1. `limit` is IGNORED. It serves ~20 jobs per page whatever you ask for, so a
         fixed page count is a bad way to reason about coverage: pages=75 yielded
         1,500 of ~102k jobs and looked like it had finished.
      2. The cursor is a timestamp -- base64 of "<iso8601>|<id>" -- and the feed is
         NEWEST FIRST.

    (2) is what makes this tractable. Instead of guessing a page count, walk back
    until we cross `since_days` and then stop. A daily sweep only needs to reach
    back to the previous sweep, so cost scales with elapsed time, not corpus size.
    """
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=since_days)).isoformat()
    cursor, pages = None, 0
    for pages in range(1, max_pages + 1):
        url = "https://himalayas.app/jobs/api?limit=100"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"
        d = get(url)
        jobs = d.get("jobs", [])
        if not jobs:
            break
        # newest-first, so once a whole page predates the cutoff we are done
        if all(iso(j.get("pubDate")) < cutoff for j in jobs):
            break
        for j in jobs:
            yield Job(
                source="himalayas", company=j.get("companyName", ""), title=j.get("title", ""),
                location=", ".join(j.get("locationRestrictions") or []) or "Worldwide",
                remote=True,
                department=", ".join(j.get("categories") or [])[:80],
                description=strip_html(j.get("description") or j.get("excerpt")),
                url=j.get("guid", ""), apply_url=j.get("applicationLink") or j.get("guid", ""),
                posted_at=iso(j.get("pubDate")),
                external_id=f"himalayas-{j.get('guid','')[-40:]}",
                location_restrictions=j.get("locationRestrictions") or [],
                timezone_restrictions=j.get("timezoneRestrictions") or [],
                salary_min=j.get("minSalary"), salary_max=j.get("maxSalary"),
                seniority_hint=", ".join(j.get("seniority") or []),
            )
        cursor = d.get("nextCursor")
        if not cursor:
            break
    else:
        # Hit the page ceiling rather than the date cutoff, so coverage is
        # TRUNCATED -- say so, otherwise a partial sweep looks like a full one.
        # (Observed: pubDate is a refresh timestamp, not the original posting
        #  date, so a 3-day window still spans ~8k jobs.)
        print(f"    himalayas: stopped at max_pages={max_pages}, "
              f"older jobs NOT fetched", file=sys.stderr)


def from_jobicy(count=100):
    d = get(f"https://jobicy.com/api/v2/remote-jobs?count={count}")
    for j in d.get("jobs", []):
        geo = j.get("jobGeo") or ""
        yield Job(
            source="jobicy", company=j.get("companyName", ""), title=j.get("jobTitle", ""),
            location=geo or "Remote", remote=True,
            department=", ".join(j.get("jobIndustry") or []),
            description=strip_html(j.get("jobDescription") or j.get("jobExcerpt")),
            url=j.get("url", ""), apply_url=j.get("url", ""),
            posted_at=iso(j.get("pubDate")), external_id=f"jobicy-{j.get('id')}",
            location_restrictions=[s.strip() for s in geo.split(",") if s.strip()],
            salary_min=j.get("salaryMin"), salary_max=j.get("salaryMax"),
            seniority_hint=j.get("jobLevel") or "",
        )


def from_workingnomads():
    for j in get("https://www.workingnomads.com/api/exposed_jobs/"):
        yield Job(
            source="workingnomads", company=j.get("company_name", ""), title=j.get("title", ""),
            location=j.get("location") or "Remote", remote=True,
            department=j.get("category_name", ""),
            description=strip_html(j.get("description")),
            url=j.get("url", ""), apply_url=j.get("url", ""),
            posted_at=iso(j.get("pub_date")),
            external_id=f"wn-{(j.get('url') or '')[-20:]}",
            location_restrictions=[j["location"]] if j.get("location") else [],
        )


def from_arbeitnow(pages=6):
    url = "https://www.arbeitnow.com/api/job-board-api"
    for _ in range(pages):
        d = get(url)
        for j in d.get("data", []):
            yield Job(
                source="arbeitnow", company=j.get("company_name", ""), title=j.get("title", ""),
                location=j.get("location") or "", remote=bool(j.get("remote")),
                department=", ".join(j.get("tags") or [])[:80],
                description=strip_html(j.get("description")),
                url=j.get("url", ""), apply_url=j.get("url", ""),
                posted_at=iso(j.get("created_at")), external_id=f"arbeitnow-{j.get('slug','')[:40]}",
            )
        url = (d.get("links") or {}).get("next")
        if not url:
            break


SOURCES = {
    "himalayas": from_himalayas, "remotive": from_remotive, "remoteok": from_remoteok,
    "jobicy": from_jobicy, "workingnomads": from_workingnomads, "arbeitnow": from_arbeitnow,
}


def fetch_all(only=None, since_days=None):
    """One source failing must not abort the sweep -- these are third-party APIs
    and any of them can be down or rate-limiting on a given day."""
    jobs = []
    for name, fn in SOURCES.items():
        if only and name not in only:
            continue
        try:
            # only himalayas supports a time window; the rest return a fixed feed
            kw = {"since_days": since_days} if (since_days and name == "himalayas") else {}
            got = list(fn(**kw))
            for j in got:
                j.track = "remote"
            jobs.extend(got)
            print(f"  {name:15} {len(got):5} roles", file=sys.stderr)
        except Exception as e:
            print(f"  {name:15} FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    return jobs


# ---------------------------------------------------------------- discovery ----

def discover_companies(jobs, buckets=("remote_global", "local")):
    """Turn a sweep into registry candidates.

    An employer earns a registry entry by having posted at least one role in a
    bucket we can actually take. That is the signal curation cannot reproduce: in
    a 9,138-job sweep, 32 of the 45 globally-remote employers had exactly ONE such
    role, so you would never have guessed them.

    Also probes each candidate for an ATS board, because once we know a company is
    worth watching we would rather poll its board than re-find it through a lossy
    aggregator.
    """
    from . import ats
    import hard_filter

    names = {}
    for j in jobs:
        d = j if isinstance(j, dict) else j.__dict__
        if hard_filter.geo_bucket(d) in buckets:
            names.setdefault(d["company"].strip(), []).append(d)
    out = []
    for name, roles in sorted(names.items()):
        token = re.sub(r"[^a-z0-9]", "", name.lower())
        found = None
        for kind in ("greenhouse", "lever", "ashby"):
            if (n := ats.probe(kind, token)) is not None:
                found = {"kind": kind, "token": token, "roles_seen": n}
                break
        out.append({
            "name": name,
            "ats": {"kind": found["kind"], "token": found["token"]} if found else None,
            "careers_url": "",
            "tier": 2,
            "notes": f"discovered via {roles[0]['source']}; "
                     f"{len(roles)} takeable role(s) in sweep",
        })
        print(f"  {name[:28]:29} {'ATS: ' + found['kind'] if found else 'no ATS API':16}"
              f" {len(roles)} role(s)", file=sys.stderr)
    return out


def merge_registry(path, candidates):
    """Append only genuinely new companies; never clobber hand-edited entries."""
    try:
        existing = json.load(open(path))
    except FileNotFoundError:
        existing = []
    have = {c["name"].lower() for c in existing}
    added = [c for c in candidates if c["name"].lower() not in have]
    existing.extend(added)
    json.dump(existing, open(path, "w"), indent=2)
    return len(added), len(existing)
