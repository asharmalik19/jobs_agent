"""ATS board adapters: Greenhouse, Ashby, Lever.

One function per ATS, and each takes a *token* (the company's board slug). Adding
an ATS is a new function; adding a company is a line in a registry JSON file.

Both tracks funnel through here:
  Track A (Pakistan)      companies_pk.json      -- curated by hand
  Track B (remote-global) companies_remote.json  -- DERIVED from the wide sweep
The adapters neither know nor care which. That is the whole point of routing
everything through a company registry instead of a hardcoded list.
"""
import json
import sys
import urllib.error
from datetime import datetime, timezone

from .models import Job, get, strip_html


def from_greenhouse(token, company=None, track=""):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    for j in get(url)["jobs"]:
        meta = {m["name"]: m.get("value") for m in (j.get("metadata") or [])}
        yield Job(
            source="greenhouse", company=company or j.get("company_name") or token,
            title=j["title"],
            location=(j.get("location") or {}).get("name", ""),
            remote="remote" in str(meta.get("Location Type", "")).lower(),
            department=", ".join(d["name"] for d in (j.get("departments") or [])),
            description=strip_html(j.get("content")),
            url=j["absolute_url"], apply_url=j["absolute_url"],
            posted_at=(j.get("first_published") or j.get("updated_at") or "")[:10],
            external_id=str(j["id"]), track=track,
        )


def from_ashby(token, company=None, track=""):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    for j in get(url)["jobs"]:
        if not j.get("isListed", True):
            continue
        extra = [s["location"] for s in (j.get("secondaryLocations") or []) if s.get("location")]
        yield Job(
            source="ashby", company=company or token, title=j["title"],
            location=" / ".join([j.get("location", "")] + extra),
            remote=bool(j.get("isRemote")),
            department=j.get("department") or "",
            description=j.get("descriptionPlain") or "",
            url=j["jobUrl"], apply_url=j.get("applyUrl") or j["jobUrl"],
            posted_at=(j.get("publishedAt") or "")[:10],
            external_id=j["id"], track=track,
        )


def from_lever(token, company=None, track=""):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    for j in get(url):
        cats = j.get("categories") or {}
        # the requirements live in descriptionBodyPlain, NOT descriptionPlain
        body = "\n\n".join(filter(None, [
            j.get("descriptionPlain"), j.get("descriptionBodyPlain"), j.get("additionalPlain")]))
        posted = ""
        if j.get("createdAt"):  # epoch milliseconds
            posted = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).date().isoformat()
        yield Job(
            source="lever", company=company or token, title=j["text"],
            location=cats.get("location") or "",
            remote=(j.get("workplaceType") or "").lower() == "remote",
            department=cats.get("department") or "",
            description=body,
            url=j["hostedUrl"], apply_url=j.get("applyUrl") or j["hostedUrl"],
            posted_at=posted, external_id=j["id"], track=track,
        )


ADAPTERS = {"greenhouse": from_greenhouse, "ashby": from_ashby, "lever": from_lever}


def probe(kind, token):
    """Does this company have a board on this ATS? Used to grow the registries.

    Returns the role count, or None. A 404 is the normal answer -- most companies
    are not on any given ATS -- so it is not an error worth reporting.
    """
    try:
        return len(list(ADAPTERS[kind](token)))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        return None


def fetch_registry(path, track="", verbose=True):
    """Poll every company in a registry file that has a known ATS board.

    Registry entry shape:
        {"name": "Careem", "ats": {"kind": "greenhouse", "token": "careem"},
         "careers_url": "...", "tier": 1, "notes": "..."}
    `ats: null` means we know the company but have no API for it -- skipped here,
    handled by the board/sitemap sources instead.
    """
    companies = json.load(open(path))
    jobs, no_api = [], []
    for c in companies:
        ats = c.get("ats")
        if not ats:
            no_api.append(c["name"])
            continue
        try:
            got = list(ADAPTERS[ats["kind"]](ats["token"], company=c["name"], track=track))
            jobs.extend(got)
            if verbose:
                print(f"  {ats['kind']:11} {c['name'][:20]:21} {len(got):4} roles", file=sys.stderr)
        except Exception as e:
            print(f"  {ats['kind']:11} {c['name'][:20]:21} FAILED: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
    if verbose and no_api:
        print(f"  ({len(no_api)} companies have no ATS API: "
              f"{', '.join(no_api[:6])}{'...' if len(no_api) > 6 else ''})", file=sys.stderr)
    return jobs
