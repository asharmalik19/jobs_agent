"""Fetch jobs from multiple ATS boards, normalized into one shape.

Usage:  python3 fetch_jobs.py                 # all companies
        python3 fetch_jobs.py "machine learning"   # filter by title
"""
import html
import json
import re
import ssl
import sys
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone

# company token -> ATS.  Adding a company is one line; adding an ATS is one function.
COMPANIES = {
    # remote-first / global-hiring: most likely to employ from Pakistan
    "greenhouse": ["gitlab", "canonical", "grafanalabs", "remotecom", "turing",
                   "elastic", "mozilla", "cloudflare", "datadog",
                   # US/on-site majors: relocation-track targets
                   "anthropic", "databricks", "scaleai", "figma", "vercel", "faire", "stripe"],
    "ashby":      ["posthog", "supabase", "railway", "replit", "linear", "resend", "neon",
                   "openai", "cohere", "perplexity", "harvey", "sierra", "notion"],
    "lever":      ["toptal", "waabi"],
}


@dataclass
class Job:
    """The only job shape the rest of the pipeline ever sees."""
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


# Some boards (Ashby) 403 the default "Python-urllib/x.y" agent, so send a real one.
# Deliberately contains no identifying information: these requests go to boards of
# companies you may apply to, and their logs are not a place to disclose anything.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _strip_html(s):
    """Greenhouse double-escapes its HTML, so unescape twice, then de-tag."""
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]*\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", s)).strip()


def from_greenhouse(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    for j in _get(url)["jobs"]:
        meta = {m["name"]: m.get("value") for m in (j.get("metadata") or [])}
        yield Job(
            source="greenhouse", company=j.get("company_name") or token,
            title=j["title"],
            location=(j.get("location") or {}).get("name", ""),
            remote="remote" in str(meta.get("Location Type", "")).lower(),
            department=", ".join(d["name"] for d in (j.get("departments") or [])),
            description=_strip_html(j.get("content")),
            url=j["absolute_url"], apply_url=j["absolute_url"],
            posted_at=(j.get("first_published") or j.get("updated_at") or "")[:10],
            external_id=str(j["id"]),
        )


def from_ashby(token):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    for j in _get(url)["jobs"]:
        if not j.get("isListed", True):
            continue
        extra = [s["location"] for s in (j.get("secondaryLocations") or []) if s.get("location")]
        yield Job(
            source="ashby", company=token, title=j["title"],
            location=" / ".join([j.get("location", "")] + extra),
            remote=bool(j.get("isRemote")),
            department=j.get("department") or "",
            description=j.get("descriptionPlain") or "",
            url=j["jobUrl"], apply_url=j.get("applyUrl") or j["jobUrl"],
            posted_at=(j.get("publishedAt") or "")[:10],
            external_id=j["id"],
        )


def from_lever(token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    for j in _get(url):
        cats = j.get("categories") or {}
        # the requirements live in descriptionBodyPlain, NOT descriptionPlain
        body = "\n\n".join(filter(None, [
            j.get("descriptionPlain"), j.get("descriptionBodyPlain"), j.get("additionalPlain")]))
        posted = ""
        if j.get("createdAt"):  # epoch milliseconds
            posted = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).date().isoformat()
        yield Job(
            source="lever", company=token, title=j["text"],
            location=cats.get("location") or "",
            remote=(j.get("workplaceType") or "").lower() == "remote",
            department=cats.get("department") or "",
            description=body,
            url=j["hostedUrl"], apply_url=j.get("applyUrl") or j["hostedUrl"],
            posted_at=posted, external_id=j["id"],
        )


ADAPTERS = {"greenhouse": from_greenhouse, "ashby": from_ashby, "lever": from_lever}


def fetch_all():
    jobs, errors = [], []
    for ats, tokens in COMPANIES.items():
        for token in tokens:
            try:
                got = list(ADAPTERS[ats](token))
                jobs.extend(got)
                print(f"  {ats:11} {token:12} {len(got):4} roles", file=sys.stderr)
            except Exception as e:
                errors.append((ats, token, e))
                print(f"  {ats:11} {token:12} FAILED: {e}", file=sys.stderr)
    return jobs, errors


if __name__ == "__main__":
    term = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    jobs, errors = fetch_all()
    hits = [j for j in jobs if term in j.title.lower()]
    print(f"\n{len(jobs)} roles from {sum(len(v) for v in COMPANIES.values())} companies; "
          f"{len(hits)} match {term!r}\n", file=sys.stderr)
    for j in hits[:20]:
        print(f"{j.company:12} {j.title}")
        print(f"{'':12} {j.location[:50]:52} {j.posted_at}  ({len(j.description)} chars)")
    with open("jobs.json", "w") as f:
        json.dump([asdict(j) for j in jobs], f, indent=2)
    print(f"\nwrote jobs.json ({len(jobs)} roles)", file=sys.stderr)
