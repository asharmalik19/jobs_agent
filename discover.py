#!/usr/bin/env python3
"""Turn a wide sweep into a company registry.

    python3 discover.py                 # from cached jobs_all.json
    python3 discover.py --min-roles 2   # stricter: employer must show 2+ takeable roles
    python3 discover.py --dry-run       # report only, write nothing

This is the piece that makes the "curated list vs. wide sweep" question stop
being a choice. The sweep is how you DISCOVER employers who will hire you; the
registry is how you REMEMBER them. Once a company is in the registry we poll its
ATS board directly, which gives full JD text and a real apply URL instead of an
aggregator redirect -- and the aggregator sweep goes back to being a discovery
tool rather than a delivery mechanism.

Why it has to work this way, from measurements on a real 9,138-job sweep:
  - Of 45 companies with a globally-remote role, 32 had exactly ONE. No
    hand-written list reaches that tail.
  - Only 8 companies overlapped between the 1,428 employers the aggregators saw
    and the 31 on the curated list. The two approaches look at different worlds.
  - But aggregator records are lossy: truncated descriptions, redirect apply
    URLs. So discovery via sweep, delivery via ATS.
"""
import argparse
import collections
import json
import sys

import hard_filter
from sources import aggregators


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="jobs_all.json")
    ap.add_argument("--registry", default="companies_remote.json")
    ap.add_argument("--min-roles", type=int, default=1,
                    help="takeable roles an employer needs before it earns an entry")
    ap.add_argument("--buckets", default="remote_global,local",
                    help="which geo buckets count as proof the employer can hire you")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip ATS probing (much faster; entries get ats=null)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    jobs = json.load(open(a.input))
    buckets = tuple(b.strip() for b in a.buckets.split(","))

    # Which employers proved they can hire someone in my situation?
    hits = collections.defaultdict(list)
    for j in jobs:
        if hard_filter.geo_bucket(j) in buckets:
            hits[j["company"].strip()].append(j)
    qualified = {n: r for n, r in hits.items() if len(r) >= a.min_roles and n}

    print(f"{len(jobs)} jobs -> {len(hits)} employers with a takeable role "
          f"-> {len(qualified)} at >={a.min_roles} role(s)", file=sys.stderr)
    print(f"buckets counted: {buckets}\n", file=sys.stderr)

    singletons = sum(1 for r in qualified.values() if len(r) == 1)
    print(f"  {singletons}/{len(qualified)} of these have exactly ONE takeable role "
          f"-- this is the tail curation misses\n", file=sys.stderr)

    if a.dry_run:
        for n, r in sorted(qualified.items(), key=lambda kv: -len(kv[1])):
            print(f"  {len(r):3}  {n[:44]:45} {r[0]['source']}")
        return

    # discover_companies probes each name for an ATS board unless told not to.
    if a.no_probe:
        cands = [{"name": n, "ats": None, "careers_url": "", "tier": 2,
                  "notes": f"discovered via {r[0]['source']}; {len(r)} takeable role(s)"}
                 for n, r in sorted(qualified.items())]
    else:
        print("probing for ATS boards (throttled, this is the slow part)...",
              file=sys.stderr)
        cands = aggregators.discover_companies(
            [j for r in qualified.values() for j in r], buckets=buckets)

    added, total = aggregators.merge_registry(a.registry, cands)
    with_ats = sum(1 for c in cands if c.get("ats"))
    print(f"\n{a.registry}: +{added} new, {total} total", file=sys.stderr)
    print(f"  {with_ats}/{len(cands)} have a pollable ATS board", file=sys.stderr)
    print("  entries are tier 2 with empty careers_url -- edit freely, "
          "merge never clobbers existing rows", file=sys.stderr)


if __name__ == "__main__":
    main()
