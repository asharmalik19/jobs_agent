"""Stage 3: the LLM filter.

This is the only stage that costs money, and the whole pipeline upstream exists
to make it cheap: a 15,660-job sweep becomes ~93 worth asking a model about.

Three design rules, in order of how much they matter:

1. THE MODEL OBSERVES; YOUR CODE DECIDES.
   The model fills in an `Assessment` (rubric.py) -- requirements, quotes,
   0-5 ratings. It never emits a score or a verdict. `rubric.decide()` turns
   those observations into both, using weights you own. This is what lets you
   recalibrate without touching the prompt, and it is what stops a persuasive
   model from talking itself into a bad match.

2. A CLAIM WITHOUT A QUOTE IS NOT A CLAIM.
   `decide()` refuses to credit any requirement marked `met` unless the model
   also produced a verbatim `jd_quote` and named the capability satisfying it.
   Inventing fit therefore lowers the score instead of raising it.

3. NEVER PAY TWICE.
   Every judgement is cached in seen.sqlite keyed on the job's external_id.
   A job is judged once, ever, no matter how many times it resurfaces.

The system prompt is byte-identical across every job in a run, so it is cached
server-side: the first job pays for it and the rest read it at ~10% of the cost.
Keep it that way -- do not interpolate the job, a timestamp, or a counter into
the system prompt, or the cache breaks on every single call.
"""
import argparse
import collections
import json
import sys

import anthropic

import rubric
import state
from rubric import Assessment

# Two tiers. TRIAGE runs over everything that survives the cheap filters; JUDGE
# re-examines only what triage liked, because the marginal jobs are where model
# quality actually changes the answer.
#
# Both are configurable on purpose -- set JUDGE_MODEL = TRIAGE_MODEL to use one
# tier, or point both at claude-opus-5 if you would rather spend more for a
# better answer. At ~90 jobs/day this whole stage is cents either way.
TRIAGE_MODEL = "claude-haiku-4-5"
JUDGE_MODEL = "claude-opus-5"

# $/1M tokens (input, output), for the cost line at the end of a run.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}

INSTRUCTIONS = """\
You are assessing whether a specific job is worth this candidate's time to apply to.

Your ONLY job is to fill in the Assessment schema with observations. You do not
decide anything: you do not output a score, a verdict, a recommendation, or advice.
Code downstream reads your observations and computes the decision.

How to do this well:

- Extract EVERY requirement the job description states, including the ones the
  candidate clearly fails. A partial list produces a wrong score. Requirements the
  candidate misses are the most valuable thing you can report.
- For each requirement, `jd_quote` must be copied VERBATIM from the job description.
  If you cannot point to a line, set met=false and leave the quote empty. A
  requirement marked met without a verbatim quote is discarded and counts against
  the job, so guessing is strictly worse than admitting the gap.
- `my_evidence` must name a specific capability from the candidate profile below.
  "Has relevant experience" is not evidence. "Built a RAG pipeline over Postgres"
  is evidence.
- Judge `interview_odds` as the EMPLOYER would: given this CV against this posting
  and its likely applicant pool, would they actually reply? Not whether the
  candidate could do the job -- whether they would get the call.
- `strongest_objection` is the single best argument against applying. Be blunt.
  A soft objection here is a wasted field; the candidate is relying on it to
  decide where not to spend an application.

Be accurate over generous. The candidate applies to at most 5 jobs a day, so a
false positive costs them one of five slots and a real opportunity elsewhere.
"""


def load_capabilities(path="capabilities.json"):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        sys.exit(f"error: {path} not found.\n"
                 "The rubric needs something to match JD requirements against -- "
                 "profile.json holds only hard constraints (geography, seniority), "
                 "not skills.\n"
                 "Start from the template: cp capabilities.example.json capabilities.json\n"
                 "(capabilities.json is gitignored -- this repo is public and the file "
                 "describes your career and self-assessed gaps.)")


def build_system(caps):
    """Stable across the whole run, so it caches. No per-job content in here."""
    return [{
        "type": "text",
        "text": (INSTRUCTIONS
                 + "\n\n===== CANDIDATE PROFILE =====\n"
                 + json.dumps(caps, indent=2, sort_keys=True)),
        # sort_keys matters: dict ordering changes are byte changes, and a byte
        # change anywhere in the prefix invalidates the cache for every job after.
        "cache_control": {"type": "ephemeral"},
    }]


def job_prompt(job):
    """Everything volatile goes here, after the cached prefix."""
    return (
        f"# Job\n"
        f"Company:  {job.get('company','')}\n"
        f"Title:    {job.get('title','')}\n"
        f"Location: {job.get('location','')}\n"
        f"Geo classification (computed upstream): {job.get('geo_bucket','')}\n"
        f"Posted:   {job.get('posted_at','')}\n\n"
        f"# Job description\n{job.get('description','')[:24000]}"
    )


def assess(client, job, model, system):
    """One API call -> a validated Assessment. Raises on malformed output."""
    r = client.messages.parse(
        model=model,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": job_prompt(job)}],
        output_format=Assessment,
    )
    return r.parsed_output, r.usage


def cost_of(usage, model):
    inp, out = PRICES.get(model, (0, 0))
    # Cache reads are ~10% of input price; cache writes ~1.25x. Close enough for
    # a running total you use to sanity-check that a daily run costs cents.
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return ((usage.input_tokens * inp
             + cached * inp * 0.1
             + written * inp * 1.25
             + usage.output_tokens * out) / 1_000_000)


def make_client():
    """Build the client, failing fast and legibly if there are no credentials.

    Without this, a missing key surfaces as one auth error per job -- 93 identical
    tracebacks that bury the actual problem.
    """
    try:
        client = anthropic.Anthropic()
        client.api_key or client.auth_token       # touch it to force resolution
    except Exception as e:
        sys.exit(f"no Anthropic credentials found ({type(e).__name__}).\n"
                 "Set one of:\n"
                 "  export ANTHROPIC_API_KEY=sk-ant-...\n"
                 "  ant auth login            (stores a profile the SDK reads)")
    if not (client.api_key or client.auth_token):
        sys.exit("no Anthropic credentials found.\n"
                 "Set one of:\n"
                 "  export ANTHROPIC_API_KEY=sk-ant-...\n"
                 "  ant auth login            (stores a profile the SDK reads)")
    return client


def judge_jobs(jobs, model, conn, caps, verbose=True):
    """Assess each job, persist the verdict, return (job, decision) pairs."""
    client = make_client()
    system = build_system(caps)
    results, spend, cache_hits = [], 0.0, 0

    for i, j in enumerate(jobs, 1):
        try:
            a, usage = assess(client, j, model, system)
        except anthropic.APIStatusError as e:
            print(f"  [{i}/{len(jobs)}] {j['company'][:18]:19} API error "
                  f"{e.status_code}: {e.message}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  [{i}/{len(jobs)}] {j['company'][:18]:19} "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            continue

        d = rubric.decide(a)
        state.save_judgement(conn, j["external_id"], model, d, a)
        # keep the objection on the decision so the report can show WHY, not just
        # the number -- it is the field you will actually read when reviewing
        d["objection"] = a.strongest_objection
        j["llm"] = d
        results.append((j, d))

        spend += cost_of(usage, model)
        if (getattr(usage, "cache_read_input_tokens", 0) or 0) > 0:
            cache_hits += 1
        if verbose:
            print(f"  [{i}/{len(jobs)}] {d['verdict']:7} {d['score']:3} "
                  f"{d['coverage']:>6}  {j['company'][:16]:17} {j['title'][:40]}",
                  file=sys.stderr)

    if verbose:
        print(f"\n  judged {len(results)} with {model}", file=sys.stderr)
        print(f"  cache hits: {cache_hits}/{len(results)}"
              f"  (0 means the system prompt is being invalidated)", file=sys.stderr)
        print(f"  spend: ${spend:.4f}", file=sys.stderr)
    return results, spend


def main():
    ap = argparse.ArgumentParser(description="LLM-judge the shortlist.")
    ap.add_argument("--input", default="shortlist.json")
    ap.add_argument("--model", default=TRIAGE_MODEL,
                    help=f"default {TRIAGE_MODEL}; use {JUDGE_MODEL} for the final pass")
    ap.add_argument("--limit", type=int, default=0, help="judge at most N jobs")
    ap.add_argument("--force", action="store_true",
                    help="re-judge jobs already in the cache (costs money again)")
    a = ap.parse_args()

    jobs = json.load(open(a.input))
    conn = state.connect()
    state.record_seen(conn, jobs)

    todo = jobs if a.force else state.unjudged(conn, jobs)
    print(f"{len(jobs)} in {a.input}; {len(todo)} not yet judged", file=sys.stderr)
    if a.limit:
        todo = todo[:a.limit]
    if not todo:
        print("nothing to judge (all cached). --force to re-judge.", file=sys.stderr)
        return

    caps = load_capabilities()
    results, _ = judge_jobs(todo, a.model, conn, caps)

    results.sort(key=lambda t: -t[1]["score"])
    print(f"\n{'='*88}\n  {len(results)} judged, best first\n{'='*88}")
    for j, d in results:
        print(f"[{d['score']:3}] {d['verdict']:7} {d['readiness']:10} "
              f"cov {d['coverage']:>6}  {j['company'][:18]:19} {j['title'][:40]}")
        if d.get("missing"):
            print(f"       missing: {', '.join(d['missing'][:3])}")
        if d.get("objection"):
            print(f"       objection: {d['objection']}")
    print("\nverdicts:", dict(collections.Counter(d["verdict"] for _, d in results)))


if __name__ == "__main__":
    main()
