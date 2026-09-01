#!/usr/bin/env python3
"""THE FILE YOU RUN.

    python3 main.py                     # re-filter the cached sweep, print shortlist
    python3 main.py --fetch             # re-poll everything first (slow: throttled)
    python3 main.py --fetch --track pk  # only the Pakistan track (fast)
    python3 main.py --min-score 3       # loosen/tighten the cheap relevance bar
    python3 judge.py                    # then: LLM-judge the shortlist (costs money)

Two tracks, because the two job pools have opposite shapes:

  TRACK pk      A CURATED company list plus local job boards. Curation is right
                here because no aggregator indexes Pakistan (6 of 9,138 jobs in an
                earlier sweep mentioned it) and only 3 of 40 good local employers
                have an ATS API. The set of good local companies is small and
                changes slowly, so a hand-owned file is both feasible and best.

  TRACK remote  A WIDE SWEEP, then filter. Curation is wrong here because the
                takeable roles are a long tail: of 45 companies with a
                globally-remote role, 32 had exactly one, and only 8 companies
                overlapped between 1,428 aggregator employers and a 31-company
                curated list. You cannot guess your way to that tail.

Pipeline:
    sources/       fetch, both tracks                -> jobs_all.json
    hard_filter    drop the impossible (free)        -> jobs_filtered.json  stage 1
    score_content  cheap recall pre-filter (free)    -> shortlist.json      stage 2
    judge.py       LLM judgement (costs money)       -> seen.sqlite         stage 3

Stage 2's bar is deliberately LOW. It exists to cut volume so stage 3 is
affordable, not to decide anything -- on an earlier sweep it scored only 1 of 210
genuinely remote-global jobs above 15, so treating it as the gate threw away
almost everything good. Precision is stage 3's job.
"""
import argparse
import collections
import json
import sys
from dataclasses import asdict

import hard_filter
import score_content
import state
from sources import aggregators, ats, boards_pk

P = json.load(open("profile.json"))

PK_REGISTRY = "companies_pk.json"
REMOTE_REGISTRY = "companies_remote.json"


def fetch(track, since_days, skip):
    """Poll the sources for the requested track(s). Returns a list of Job dicts."""
    got = []
    if track in ("all", "pk"):
        print("[1/3] fetching PK track...", file=sys.stderr)
        got += ats.fetch_registry(PK_REGISTRY, track="pk")
        got += boards_pk.fetch_all(since_days=since_days, skip=skip)
    if track in ("all", "remote"):
        print("[1/3] fetching remote track...", file=sys.stderr)
        got += aggregators.fetch_all(since_days=since_days)
        try:
            got += ats.fetch_registry(REMOTE_REGISTRY, track="remote")
        except FileNotFoundError:
            print(f"      (no {REMOTE_REGISTRY} yet -- run discover.py to build it)",
                  file=sys.stderr)
    return [j if isinstance(j, dict) else asdict(j) for j in got]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="re-poll the sources")
    ap.add_argument("--track", default="all", choices=["all", "pk", "remote"])
    ap.add_argument("--since-days", type=int, default=14,
                    help="how far back time-windowed sources should walk")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-score", type=int, default=3,
                    help="cheap recall pre-filter; keep this LOW, the LLM decides")
    ap.add_argument("--bucket", help="only this geo bucket")
    ap.add_argument("--per-company", type=int, default=3,
                    help="max roles per company, so one employer can't own the queue")
    ap.add_argument("--new-only", action="store_true",
                    help="show only jobs never seen before (needs seen.sqlite)")
    a = ap.parse_args()

    conn = state.connect()

    # ---- fetch ----
    if a.fetch:
        # Board sources cost one throttled HTTP request per job, so we tell them
        # what we already have and they skip refetching those detail pages.
        #
        # But skipping means those jobs are ABSENT from this fetch's results, so
        # we must MERGE with the previous jobs_all.json rather than overwrite it.
        # Overwriting silently shrinks the corpus every run: a job fetched
        # yesterday is skipped today, vanishes from the file, and drops out of the
        # shortlist even though it is still open.
        try:
            previous = {j["external_id"]: j for j in json.load(open("jobs_all.json"))}
        except FileNotFoundError:
            previous = {}
        skip = set(previous)
        fresh = fetch(a.track, a.since_days, skip)

        merged = dict(previous)
        merged.update({j["external_id"]: j for j in fresh})
        jobs = list(merged.values())
        json.dump(jobs, open("jobs_all.json", "w"), indent=2)
        print(f"      wrote jobs_all.json ({len(fresh)} fetched, "
              f"{len(jobs)} total after merge)", file=sys.stderr)

        # Record EVERY job we have fetched, not just the survivors. seen.sqlite is
        # the record of what we have looked at; if only post-filter survivors land
        # in it, the skip set above stays tiny and we refetch the same detail
        # pages forever.
        new_fetched, _ = state.record_seen(conn, jobs)
        print(f"      state: {new_fetched} newly seen at fetch level", file=sys.stderr)
    else:
        try:
            jobs = json.load(open("jobs_all.json"))
        except FileNotFoundError:
            sys.exit("no jobs_all.json yet -- run with --fetch")
        if a.track != "all":
            jobs = [j for j in jobs if j.get("track", "") == a.track]
        print(f"[1/3] cached: {len(jobs)} roles from "
              f"{len({j['company'] for j in jobs})} companies", file=sys.stderr)

    # ---- stage 1: hard filters (free) ----
    kept, dropped = hard_filter.hard_filter(jobs)
    print(f"[2/3] hard filters: {len(jobs)} -> {len(kept)}", file=sys.stderr)
    for reason, n in dropped.most_common(6):
        print(f"        -{n:5}  {reason}", file=sys.stderr)
    json.dump(kept, open("jobs_filtered.json", "w"), indent=2)

    # ---- stage 2: cheap relevance, high recall (free) ----
    for j in kept:
        pts, hits, flags = score_content.score(j)
        j.update(content_score=pts, hits=hits, flags=flags)

    # Re-record the survivors, now that they carry geo_bucket and content_score.
    # ON CONFLICT updates those columns in place; first_seen and every llm_* column
    # are left alone, so this never disturbs a judgement we already paid for.
    state.record_seen(conn, kept)

    short = [j for j in kept if j["content_score"] >= a.min_score]
    if a.bucket:
        short = [j for j in short if j["geo_bucket"] == a.bucket]
    if a.new_only:
        judged = {r[0] for r in conn.execute(
            "SELECT external_id FROM jobs WHERE llm_judged_at IS NOT NULL")}
        short = [j for j in short if j["external_id"] not in judged]

    # rank: best geography, then relevance, then most recently posted
    short.sort(key=lambda j: (P["bucket_priority"].get(j["geo_bucket"], 9),
                              -j["content_score"],
                              -(int((j["posted_at"] or "0").replace("-", "") or 0))))

    # cap per company: a verbose employer shouldn't dominate the review queue
    seen, capped = collections.Counter(), []
    for j in short:
        if seen[j["company"]] < a.per_company:
            seen[j["company"]] += 1
            capped.append(j)
    dropped_cap = len(short) - len(capped)
    short = capped
    print(f"[3/3] relevance >= {a.min_score}: {len(kept)} -> {len(short)}"
          f" (capped {dropped_cap} over {a.per_company}/company)\n", file=sys.stderr)
    json.dump(short, open("shortlist.json", "w"), indent=2)

    # ---- report ----
    print(f"{'='*94}\n  SHORTLIST — {len(short)} roles, best geography first\n{'='*94}")
    print("  buckets:", dict(collections.Counter(j["geo_bucket"] for j in short)))
    print("  tracks: ", dict(collections.Counter(j.get("track") or "?" for j in short)), "\n")
    for i, j in enumerate(short[:a.top], 1):
        print(f"{i:3}. [{j['content_score']:3}] {j['company'][:18]:19} {j['title'][:50]}")
        print(f"      {j['geo_bucket']:20} {j['location'][:40]:42} posted {j['posted_at']}")
        print(f"      matched: {', '.join(j['hits'][:8])}")
        if j["flags"]:
            print(f"      flags:   {', '.join(j['flags'])}")
        print(f"      {j['url']}\n")
    print("full list in shortlist.json   |   next: python3 judge.py")


if __name__ == "__main__":
    main()
