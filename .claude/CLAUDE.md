The goal of this project is to build a solution to a real-world problem but i also want to learn how to build AI-powered systems and get better at it. So don't rush to completion and explain things properly.

---

# jobs_agent

Finds jobs worth applying to, filters them down, and (eventually) tailors a CV per
job. I review and submit manually — **max 5 applications/day**. That cap is the
number the whole design falls out of: the goal is ~5 excellent matches surfaced,
not maximum coverage. See `spec.md` for the original intent.

## The core architecture decision

**Two tracks with opposite sourcing strategies, because the two job pools have
opposite shapes.** This was measured on a real 9,138-job sweep, not assumed:

| | strategy | why |
|---|---|---|
| **`pk`** (Pakistan-local) | curated company registry | No aggregator indexes Pakistan — 6 jobs of 9,138 mentioned it. Only **3 of 40** good local employers have an ATS API (Careem/greenhouse, Educative/lever, Tajir/ashby). The set of good local employers is small and changes slowly, so hand-curation is both feasible and best. |
| **`remote`** (remote-global) | wide sweep, then filter | The takeable roles are a long tail: of 45 companies with a globally-remote role, **32 had exactly one**. Only **8 companies overlapped** between the 1,428 employers aggregators saw and a 31-company curated list. Curation structurally cannot reach that tail. |

The consequence worth internalising: **the company list is an OUTPUT too, not only
an input.** The sweep *discovers* employers who will hire from Pakistan;
`companies_remote.json` *remembers* them; from then on we poll their ATS directly,
which gives full JD text and real apply URLs instead of aggregator redirects
(those redirects would break the form-filling stage later).

## Pipeline

```
sources/            fetch, both tracks
      ↓
jobs_all.json       ~15,700  raw, unfiltered (100 MB, gitignored)
      ↓             hard_filter.hard_filter()          free
jobs_filtered.json      713  geo + title + seniority filters applied
      ↓             score_content.score() >= 3         free
shortlist.json           93  LLM stage input
      ↓             judge.py                           costs money
seen.sqlite             ---  verdicts, cached forever
```

Run it: `uv run python main.py [--fetch] [--track pk|remote] [--top N]`
Browse stage 1: `uv run python show.py <term> [--bucket NAME]`
Grow the remote registry: `uv run python discover.py [--dry-run]`

**Stage 2's bar is deliberately LOW (`--min-score 3`).** It exists only to cut
volume so stage 3 is affordable. On an earlier sweep it scored just 1 of 210
genuinely remote-global jobs above 15 — treating it as the gate threw away almost
everything good. Precision is stage 3's job. Do not raise this to "improve
quality"; it will silently destroy recall.

## Current status

Working and verified against live data: fetch (both tracks), hard filters, cheap
pre-filter. 15,660 → 713 → 93, of which 23 `remote_global` and 6 `local` (both
were 0 before this work).

**`judge.py` has never made an API call.** Verified offline only: pydantic schema
validates, the quote-audit correctly refuses unquoted claims, the system prompt is
byte-stable with `cache_control` set, no job content leaks into the cached prefix,
and it fails fast without credentials. Untested: the real API round-trip, whether
caching actually engages, actual cost, and whether the verdicts are any good.

Blocked on two things only I can provide:
1. `ANTHROPIC_API_KEY` (unset)
2. `capabilities.json` — still the template. Needs filling from my LaTeX CV, whose
   path I have not yet given. `profile.json` holds only hard constraints
   (geography, seniority), no skills, so the rubric has nothing to match against.

**Calibration has not happened and matters more than anything else.** Before
trusting the LLM to gate the 5/day: hand-label ~20 jobs apply/reject, run the
judge, compare. `llm_raw` stores the full assessment, so weights in `rubric.py`
can be re-tuned over judgements already paid for — no new API calls.

## Design rules — do not undo these

- **The model observes; our code decides.** `rubric.py` defines an `Assessment` of
  pure observations. The model never emits a score or verdict; `rubric.decide()`
  computes both. This lets us recalibrate without touching the prompt and stops a
  persuasive model talking itself into a bad match.
- **A claim without a verbatim JD quote is not credited.** `decide()` discards any
  requirement marked `met` without a `jd_quote` and named evidence, so inventing
  fit *lowers* the score.
- **Never pay twice.** `seen.sqlite` caches every judgement by `external_id`.
- **Send the full job description.** I explicitly decided against trimming to
  `requirements_section()` — it saves only 20% and risks dropping geo/visa
  language that sits outside the requirements block.
- **Cost is not a constraint.** Measured: ~$0.58 first run (93 jobs, Haiku),
  ~$0.12/day steady state; Opus 5 ~$0.62/day steady state. Choose on judgement
  quality, not price. Don't spend effort optimising this.

## Gotchas that will bite you

- **Throttle everything.** `sources/models.py` has a 1s min interval + 429/503
  backoff. Himalayas *will* 429 you on deep pagination. These are other people's
  servers and this runs unattended.
- **Himalayas ignores `limit`** and serves ~20/page, so a fixed page count looks
  complete at 1,500 of ~102k jobs. Its cursor is a base64 `"<iso8601>|<id>"` and
  the feed is newest-first, so it walks back by date. It still hits
  `max_pages=400` and says so — coverage is truncated, not complete.
- **`--fetch` MERGES with the previous `jobs_all.json`.** Board sources skip
  already-seen jobs to avoid refetching detail pages, which means those jobs are
  absent from the fetch result. Overwriting would shrink the corpus every run.
- **`seen.sqlite` records every FETCHED job, not just survivors** — otherwise the
  skip set stays empty and detail pages get refetched forever. So a row with an
  empty `geo_bucket` means "raw sighting", and one with a bucket means it passed
  stage 1. Known weakness: drop *reasons* are not persisted, only printed, so you
  cannot ask the DB why a job was rejected.
- **`exclude_departments` is inert on board sources** — they carry no department
  field. That filter is the biggest lever on ATS data (~4,986 drops) and does
  nothing for mustakbil. Hence
  `require_title_patterns_when_no_department`: a title *allowlist* applied only
  where department is absent (120 → 10 on mustakbil). Do not try to win this with
  an ever-longer denylist; a general local board is mostly non-technical work.
- **`unknown_location` is the largest accepted bucket (~209).** It means the
  classifier could not tell, not that the job is good. Weakest part of filtering.
- **`max_age_days` is null on purpose** (see the note in `profile.json`): ATS APIs
  list only open roles, so an old `posted_at` is an evergreen req, not a stale
  posting. Side effect: a 2023 posting can reach the queue.

## Repo hygiene — the repo is PUBLIC

- `capabilities.json` is **gitignored**; `capabilities.example.json` is the tracked
  template. The real file describes my career and self-assessed gaps — not
  something to index under my name while job-hunting.
- Keep `companies_pk.json` notes **factual** (location, domain, stack). No quality
  judgements about named employers; Pakistan's tech market is small.
- Never commit `jobs_all.json` (100 MB), `jobs_filtered.json`, `shortlist.json`,
  or `seen.sqlite`. All regenerable, all gitignored.

## Deferred, deliberately

- **CV tailoring (the 5/day output stage).** Out of scope until the filter is
  trustworthy. Will need the LaTeX project (including its `.cls`/`.sty`, to
  compile) and the tailoring prompt currently in my Google Drive.
- **rozee.pk.** Bigger board than mustakbil but a worse target right now: sitemap
  `lastmod` was ~3 months stale, no JSON-LD on detail pages, and search results
  render client-side. Needs a real browser, so it belongs with the Playwright work
  in `spec.md`. mustakbil is implemented instead — live sitemap plus a full
  schema.org `JobPosting` blob per job.
- **~21 PK employers with no ATS API.** Do not write 21 scrapers. 5/day does not
  require exhaustive coverage.
