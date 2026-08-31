"""Browse what stage 1 kept.  python3 show.py [search term] [--bucket NAME]"""
import json, re, sys, collections

P = json.load(open("profile.json"))
jobs = json.load(open("jobs_filtered.json"))
args = [a for a in sys.argv[1:] if not a.startswith("--")]
bucket = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--bucket"), None)

term = args[0].lower() if args else ""
hits = [j for j in jobs
        if term in j["title"].lower() or term in j["description"].lower()]
if bucket:
    hits = [j for j in hits if j["geo_bucket"] == bucket]
hits.sort(key=lambda j: (P["bucket_priority"].get(j["geo_bucket"], 9), j["company"]))

print(f"{len(jobs)} kept by stage 1; {len(hits)} match {term!r}"
      + (f" in bucket {bucket}" if bucket else "") + "\n")
print("buckets present:", dict(collections.Counter(j["geo_bucket"] for j in hits)), "\n")
for j in hits[:30]:
    print(f"[{j['geo_bucket'][:18]:18}] {j['company']:11} {j['title'][:50]}")
    print(f"{'':21}{j['location'][:44]:46} {j['posted_at']}")
    print(f"{'':21}{j['url']}\n")
