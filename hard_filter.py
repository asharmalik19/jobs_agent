"""Step 1: hard filters. Pure code, zero cost, runs in under a second.

Kills jobs that are impossible regardless of how well the skills match, so the
LLM only ever sees jobs you could actually take.
"""
import json
import re
import sys
import collections

P = json.load(open("profile.json"))

# Location strings use ANY of | ; • / , and "and" as separators. Split on all of them.
SPLIT = re.compile(r"\s*[|;•·/]\s*|\s+or\s+|\s+and\s+")

COUNTRY_HINTS = {
    "us": r"\b(USA?|United States|U\.S\.|San Francisco|New York|NYC|Seattle|Austin|Boston|"
          r"Mountain View|Palo Alto|Washington,? ?DC|Chicago|Denver|Atlanta|Los Angeles|"
          r"Bellevue|Redmond|San Jose|Sunnyvale|Menlo Park|,\s*(CA|NY|WA|TX|MA|IL|VA|MD|CO|GA|OR|NC|FL|PA|NJ|AZ|UT|MN|MI|OH|DC)\b)",
    "uk": r"\b(UK|United Kingdom|London|Manchester|Edinburgh|Cambridge, England)\b",
    "ca": r"\b(Canada|Toronto|Vancouver|Montreal|Ottawa|Waterloo|Ontario|CAN)\b",
    "eu": r"\b(Ireland|Dublin|Germany|Berlin|Munich|France|Paris|Netherlands|Amsterdam|"
          r"Spain|Madrid|Barcelona|Sweden|Stockholm|Poland|Warsaw|Serbia|Belgrade|Zurich|Switzerland)\b",
    "apac": r"\b(Japan|Tokyo|Singapore|Korea|Seoul|Australia|Sydney|Melbourne|Hong Kong|China|Taiwan)\b",
    "pk": r"\b(Pakistan|Karachi|Lahore|Islamabad|Rawalpindi)\b",
    "in": r"\b(India|Bengaluru|Bangalore|Mumbai|Hyderabad|Delhi|Pune|Chennai|Gurgaon|Noida)\b",
    "me": r"\b(Dubai|UAE|Abu Dhabi|Saudi|Riyadh|Qatar|Doha|Israel|Tel Aviv)\b",
}

# What the DESCRIPTION says about who can be hired.
# Must be about HIRING, not about the product. "work from anywhere in the world"
# appears in Figma's product blurb on 163 jobs; it says nothing about who they employ.
GLOBAL_HIRING = re.compile(
    r"employer of record|\bEOR\b|"
    r"(hire|employ|work)\w*\s+(from\s+)?anywhere\s+(in the world\s+)?"
    r"(you|they)?\s*(are|is|live|reside|based)|"
    r"open to candidates (in|from) any|no location requirement|"
    r"we (hire|employ)\w*\s+(globally|internationally|in \d+\+? countries)", re.I)
GEO_LOCKED = re.compile(
    r"must (reside|be located|live|be based) in|residing in|"
    r"authoriz\w+ to work in the (US|United States|UK)|legally authorized to work|"
    r"eligib\w+ to work in", re.I)
RELOCATION = re.compile(r"relocation (assistance|support|package|benefits)|"
                        r"visa (sponsorship|support)|immigration support|will sponsor", re.I)
CLEARANCE = re.compile(r"security clearance|TS/SCI|\bpolygraph\b|US ?Citizen(ship)? (is )?required", re.I)


def countries(location):
    """Which countries/regions does this location string touch?"""
    parts = [p.strip() for p in SPLIT.split(location) if p.strip()]
    found = set()
    for part in parts:
        for code, pat in COUNTRY_HINTS.items():
            if re.search(pat, part, re.I):
                found.add(code)
    return found


def _age_days(iso):
    from datetime import date
    try:
        y, m, d = (int(x) for x in iso[:10].split("-"))
        return (date.today() - date(y, m, d)).days
    except Exception:
        return None


def min_years(desc):
    yrs = [int(m) for m in re.findall(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?", desc)]
    yrs = [y for y in yrs if 0 < y < 25]
    return min(yrs) if yrs else None


def canonical_id(job):
    """Collapse the same role posted across N locations into one entry."""
    title = re.sub(r"\s*[-–—(].*$", "", job["title"]).strip().lower()
    title = re.sub(r"\b(sr\.?|senior|staff|lead|principal)\b", "", title).strip()
    return f"{job['company'].lower()}::{re.sub(r'[^a-z0-9]+', '-', title)}"


# Remote scope is encoded in the location string, and every company phrases it
# differently. These patterns were derived from real strings in jobs.json:
#   Canonical:   "Home based - Worldwide" / "Home Based - APAC" / "Office Based - Taipei"
#   GitLab:      "Remote, United States" / "Remote, Turkey"
#   Grafana:     "United States (Remote)"
#   Cloudflare:  "Hybrid" / "Distributed" / "In-Office"
WORLDWIDE = re.compile(r"(home.based|remote|distributed)\W*[-,:]?\W*(worldwide|global|anywhere)"
                       r"|\bworldwide\b", re.I)
REGION_REMOTE = re.compile(r"(home.based|remote)\W*[-,:]?\W*(EMEA|APAC|Americas|AMER|LATAM|"
                           r"Asia.Pacific|Europe|Middle East)", re.I)
BARE_REMOTE = re.compile(r"^\s*(remote|distributed|home.based|work from home|wfh)\s*$", re.I)
ONSITE_WORD = re.compile(r"^\s*(hybrid|in.office|on.?site|office.based)", re.I)
PINNED_REMOTE = re.compile(r"remote\W*[-,:]\s*[A-Za-z .]+$|[A-Za-z .]+\s*\(\s*remote\s*\)", re.I)

# Regions Pakistan is *sometimes* inside, depending on the company's entity setup.
AMBIGUOUS_REGIONS = re.compile(r"EMEA|APAC|Asia.Pacific|Middle East", re.I)


# ---- structured geo eligibility (aggregators give this; ATS boards do not) ----
MY_TZ = 5                       # Pakistan is UTC+5
WORLDWIDE_R = re.compile(r"worldwide|anywhere|global|remote$", re.I)
MY_REGIONS = re.compile(r"pakistan|south asia|asia|apac|asia.pacific|middle east|emea", re.I)


def bucket_from_restrictions(job):
    """Aggregators state who is eligible. Trust that over regex on prose."""
    restr = [str(r) for r in (job.get("location_restrictions") or [])]
    tz = job.get("timezone_restrictions") or []

    if tz and MY_TZ not in tz:
        return "remote_geo_locked"          # timezone list excludes UTC+5 outright
    if not restr:
        return None                          # nothing stated -> fall through to prose
    if any(WORLDWIDE_R.search(r) for r in restr):
        return "remote_global"
    if any(re.search(r"\bpakistan\b", r, re.I) for r in restr):
        return "remote_global"
    if any(MY_REGIONS.search(r) for r in restr):
        return "remote_region_maybe"         # "Asia"/"EMEA" — may or may not include PK
    return "remote_geo_locked"               # explicitly restricted elsewhere


def geo_bucket(job):
    """What would have to be true for me to take this job?"""
    desc = job["description"]
    tail = desc[len(desc) // 3:]          # hiring-policy language lives past the intro blurb
    loc = job["location"].strip()
    mine = set(P["authorized_region_codes"])

    if CLEARANCE.search(desc):
        return "clearance_required"

    # --- 0. structured eligibility wins when a source provides it ---
    if (b := bucket_from_restrictions(job)):
        return b

    # --- 1. the location string itself usually answers the question ---
    parts = [p.strip() for p in SPLIT.split(loc) if p.strip()]
    if WORLDWIDE.search(loc):
        return "remote_global"
    if REGION_REMOTE.search(loc):
        # "Home based - EMEA" may or may not include Pakistan -> human/LLM call
        return "remote_region_maybe" if AMBIGUOUS_REGIONS.search(loc) else "remote_geo_locked"
    if any(BARE_REMOTE.match(p) for p in parts):
        return "remote_unclear"
    if parts and all(ONSITE_WORD.match(p) for p in parts):
        return "onsite_no_support"

    locs = countries(loc)
    if locs & mine:
        return "local"
    if PINNED_REMOTE.search(loc) and locs:
        return "remote_geo_locked"        # "Remote, Turkey" = remote, but be in Turkey

    # --- 2. fall back to what the description says about hiring ---
    if GLOBAL_HIRING.search(tail):
        return "remote_global"
    if not locs:
        return "remote_unclear" if job["remote"] else "unknown_location"
    if job["remote"]:
        return "remote_geo_locked"        # THE TRAP: "remote" == remote within that country
    if RELOCATION.search(tail):
        return "relocation_possible"
    return "onsite_no_support"


def hard_filter(jobs):
    kept, dropped, seen = [], collections.Counter(), {}
    for j in jobs:
        why = None
        # --- dedup: same role, many cities ---
        cid = canonical_id(j)
        if cid in seen:
            seen[cid]["also_in"].append(j["location"])
            why = "duplicate_of_earlier_posting"
        # --- department ---
        elif any(d in j["department"].lower() for d in P["exclude_departments"]):
            why = "excluded_department"
        # --- title patterns ---
        elif any(re.search(p, j["title"], re.I) for p in
                 P["exclude_title_patterns"] + P.get("exclude_title_roles", [])):
            why = "excluded_title"
        # --- stale posting (Canonical leaves roles up for years) ---
        elif P.get("max_age_days") and j["posted_at"] and (
                _age_days(j["posted_at"]) or 0) > P["max_age_days"]:
            why = f"older_than_{P['max_age_days']}d"
        # --- seniority hiding in prose ---
        elif (y := min_years(j["description"])) and y > P["max_years_required"]:
            why = f"requires_{y}y_experience"
        if why:
            dropped[why] += 1
            continue

        j["geo_bucket"] = geo_bucket(j)
        j["also_in"] = []
        if j["geo_bucket"] not in P["acceptable_buckets"]:
            dropped[f"geo:{j['geo_bucket']}"] += 1
            continue
        seen[cid] = j
        kept.append(j)
    return kept, dropped


if __name__ == "__main__":
    jobs = json.load(open("jobs.json"))
    kept, dropped = hard_filter(jobs)

    print(f"in:  {len(jobs)}")
    print("dropped:")
    for reason, n in dropped.most_common():
        print(f"  {n:5}  {reason}")
    print(f"out: {len(kept)}\n")

    # what the geo buckets look like across everything, before acceptance is applied
    print("=== geo bucket census (all jobs, ignoring acceptable_buckets) ===")
    census = collections.Counter(geo_bucket(j) for j in jobs)
    for b, n in census.most_common():
        mark = "  <- accepted" if b in P["acceptable_buckets"] else ""
        print(f"  {n:5}  {b}{mark}")

    json.dump(kept, open("jobs_filtered.json", "w"), indent=2)
    print(f"\nwrote jobs_filtered.json ({len(kept)})")
    for j in kept[:15]:
        print(f"  [{j['geo_bucket']:19}] {j['company']:11} {j['title'][:46]:48} {j['location'][:26]}")
