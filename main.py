#!/usr/bin/env python3
"""THE FILE YOU RUN.

    python3 main.py                  # use cached jobs.json, print the shortlist
    python3 main.py --fetch          # re-poll all job boards first (~3 min)
    python3 main.py --top 30         # show more
    python3 main.py --min-score 8    # loosen/tighten the relevance bar

Pipeline:
    fetch_jobs.py    poll ATS boards            -> jobs.json
    hard_filter.py   drop the impossible        -> jobs_filtered.json     (stage 1)
    score_content.py rank by actual JD content  -> shortlist.json         (stage 2a)
    rubric.py        LLM judgement              (stage 2b - needs API key, not wired yet)
"""
import argparse
import collections
import json
import sys

import fetch_jobs
import fetch_aggregators
import hard_filter
import score_content

P = json.load(open("profile.json"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="re-poll everything")
    ap.add_argument("--source", default="all", choices=["all", "aggregators", "ats"],
                    help="aggregators = company-agnostic (663+ companies); ats = curated list")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-score", type=int, default=10, help="content relevance threshold")
    ap.add_argument("--bucket", help="only this geo bucket")
    ap.add_argument("--per-company", type=int, default=3,
                    help="max roles per company, so one employer can't own the queue")
    a = ap.parse_args()

    # ---- fetch ----
    from dataclasses import asdict
    if a.fetch:
        print("[1/3] fetching...", file=sys.stderr)
        got = []
        if a.source in ("all", "aggregators"):
            got += fetch_aggregators.fetch_all()
        if a.source in ("all", "ats"):
            ats, _ = fetch_jobs.fetch_all()
            got += ats
        jobs = [asdict(j) for j in got]
        json.dump(jobs, open("jobs_all.json", "w"), indent=2)
    else:
        jobs = []
        for f in (["jobs_agg.json"] if a.source in ("all", "aggregators") else []) + \
                 (["jobs.json"] if a.source in ("all", "ats") else []):
            try:
                jobs += json.load(open(f))
            except FileNotFoundError:
                print(f"      (no {f} yet — run with --fetch)", file=sys.stderr)
        print(f"[1/3] cached: {len(jobs)} roles from "
              f"{len({j['company'] for j in jobs})} companies", file=sys.stderr)

    # ---- stage 1: hard filters ----
    kept, dropped = hard_filter.hard_filter(jobs)
    print(f"[2/3] hard filters: {len(jobs)} -> {len(kept)}", file=sys.stderr)
    for reason, n in dropped.most_common(6):
        print(f"        -{n:5}  {reason}", file=sys.stderr)
    json.dump(kept, open("jobs_filtered.json", "w"), indent=2)

    # ---- stage 2a: content relevance ----
    for j in kept:
        pts, hits, flags = score_content.score(j)
        j.update(content_score=pts, hits=hits, flags=flags)
    short = [j for j in kept if j["content_score"] >= a.min_score]
    if a.bucket:
        short = [j for j in short if j["geo_bucket"] == a.bucket]
    # rank: best geography, then relevance, then most recently posted
    short.sort(key=lambda j: (P["bucket_priority"].get(j["geo_bucket"], 9),
                              -j["content_score"], j["posted_at"] or ""), reverse=False)
    short.sort(key=lambda j: (P["bucket_priority"].get(j["geo_bucket"], 9),
                              -j["content_score"], -(int((j["posted_at"] or "0-0-0")
                              .replace("-", "") or 0))))
    # cap per company: a verbose employer shouldn't dominate the review queue
    seen, capped = collections.Counter(), []
    for j in short:
        if seen[j["company"]] < a.per_company:
            seen[j["company"]] += 1
            capped.append(j)
    dropped_cap = len(short) - len(capped)
    short = capped
    print(f"[3/3] content relevance >= {a.min_score}: {len(kept)} -> {len(short)}"
          f" (capped {dropped_cap} over {a.per_company}/company)\n", file=sys.stderr)
    json.dump(short, open("shortlist.json", "w"), indent=2)

    # ---- report ----
    print(f"{'='*94}\n  SHORTLIST — {len(short)} roles, best geography first\n{'='*94}")
    print("  buckets:", dict(collections.Counter(j["geo_bucket"] for j in short)), "\n")
    for i, j in enumerate(short[:a.top], 1):
        print(f"{i:3}. [{j['content_score']:3}] {j['company']:11} {j['title'][:52]}")
        print(f"      {j['geo_bucket']:20} {j['location'][:40]:42} posted {j['posted_at']}")
        print(f"      matched: {', '.join(j['hits'][:8])}")
        if j["flags"]:
            print(f"      flags:   {', '.join(j['flags'])}")
        print(f"      {j['url']}\n")
    print(f"full list in shortlist.json   |   browse: python3 show.py <term>")


if __name__ == "__main__":
    main()
