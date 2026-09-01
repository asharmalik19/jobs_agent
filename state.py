"""Persistent memory for the pipeline.

Everything else in this project is stateless: it re-reads JSON, recomputes, and
overwrites. That is fine for filters (they are cheap and deterministic) but wrong
for two things:

  1. LLM judgements cost money. A job should be judged ONCE, ever.
  2. You review 5 jobs/day. Without memory, every run re-surfaces the same jobs
     and you cannot tell what is new.

So: one SQLite file, one row per job, keyed on the source's own id. SQLite because
it is in the standard library, the file is greppable with the sqlite3 CLI, and
concurrent reads are free.

The columns split cleanly into three groups:
  identity//facts   what the board told us          (refreshed each run)
  our judgements    what the filters and LLM said   (written once, cached)
  your decisions    what YOU did about it           (the feedback signal)
"""
import json
import sqlite3
from datetime import date, datetime, timezone

DB = "seen.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    external_id   TEXT PRIMARY KEY,
    source        TEXT,
    track         TEXT,
    company       TEXT,
    title         TEXT,
    url           TEXT,
    posted_at     TEXT,
    first_seen    TEXT,      -- when WE first saw it (posted_at is unreliable)
    last_seen     TEXT,      -- so we can tell when a role is taken down

    geo_bucket    TEXT,
    content_score INTEGER,

    -- LLM stage. NULL llm_judged_at == not yet judged == safe to spend money on.
    llm_judged_at TEXT,
    llm_model     TEXT,
    llm_score     INTEGER,
    llm_verdict   TEXT,       -- apply | review | reject
    llm_readiness TEXT,       -- ready_now | reach | not_yet
    llm_objection TEXT,
    llm_raw       TEXT,       -- full Assessment JSON, for recalibration later

    -- your decisions
    queued_on     TEXT,       -- the day it entered your 5/day review queue
    my_feedback   TEXT        -- your verdict; the training signal for the rubric
);
CREATE INDEX IF NOT EXISTS idx_verdict ON jobs(llm_verdict, llm_score);
CREATE INDEX IF NOT EXISTS idx_unjudged ON jobs(llm_judged_at);
CREATE INDEX IF NOT EXISTS idx_queued ON jobs(queued_on);
"""


def connect(path=DB):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_seen(conn, jobs):
    """Upsert the board facts. Returns (new, refreshed).

    ON CONFLICT deliberately does NOT touch first_seen or any llm_* column: a job
    reappearing in today's sweep is the same job, and re-judging it would be
    paying twice for an answer we already have.
    """
    now, today = _now(), date.today().isoformat()
    new = 0
    for j in jobs:
        cur = conn.execute(
            """INSERT INTO jobs (external_id, source, track, company, title, url,
                                 posted_at, first_seen, last_seen, geo_bucket, content_score)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(external_id) DO UPDATE SET
                   last_seen     = excluded.last_seen,
                   geo_bucket    = excluded.geo_bucket,
                   content_score = excluded.content_score""",
            (j["external_id"], j.get("source", ""), j.get("track", ""), j.get("company", ""),
             j.get("title", ""), j.get("url", ""), j.get("posted_at", ""), now, now,
             j.get("geo_bucket", ""), j.get("content_score")))
        # rowcount is 1 for a fresh insert, and for an update that changed something.
        # changes() alone can't distinguish them, so check first_seen instead.
        new += 1 if conn.execute(
            "SELECT first_seen = ? FROM jobs WHERE external_id = ?",
            (now, j["external_id"])).fetchone()[0] else 0
    conn.commit()
    return new, len(jobs) - new


def unjudged(conn, jobs):
    """Filter a job list down to those we have never sent to the LLM.

    This is the money-saver. Call it right before the LLM stage.
    """
    judged = {r[0] for r in conn.execute(
        "SELECT external_id FROM jobs WHERE llm_judged_at IS NOT NULL")}
    return [j for j in jobs if j["external_id"] not in judged]


def save_judgement(conn, external_id, model, decision, assessment):
    """Persist one LLM verdict. `decision` is rubric.decide() output.

    llm_raw keeps the whole Assessment, not just the score. That is what makes
    recalibration possible later: you can re-run rubric.decide() with different
    weights over judgements you have already paid for, and compare the verdicts
    against your own accept/reject feedback -- no new API calls.
    """
    raw = (assessment.model_dump_json() if hasattr(assessment, "model_dump_json")
           else json.dumps(assessment, default=lambda o: o.__dict__))
    conn.execute(
        """UPDATE jobs SET llm_judged_at=?, llm_model=?, llm_score=?, llm_verdict=?,
                           llm_readiness=?, llm_objection=?, llm_raw=?
           WHERE external_id=?""",
        (_now(), model, decision["score"], decision["verdict"], decision["readiness"],
         getattr(assessment, "strongest_objection", ""), raw, external_id))
    conn.commit()


def load_judgement(conn, external_id):
    r = conn.execute(
        """SELECT llm_score, llm_verdict, llm_readiness, llm_objection, llm_model
           FROM jobs WHERE external_id=? AND llm_judged_at IS NOT NULL""",
        (external_id,)).fetchone()
    return dict(r) if r else None


def queue_today(conn, external_ids):
    today = date.today().isoformat()
    conn.executemany("UPDATE jobs SET queued_on=? WHERE external_id=?",
                     [(today, e) for e in external_ids])
    conn.commit()


def already_queued(conn):
    return {r[0] for r in conn.execute(
        "SELECT external_id FROM jobs WHERE queued_on IS NOT NULL")}


def queued_on(conn, day=None):
    day = day or date.today().isoformat()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE queued_on=? ORDER BY llm_score DESC", (day,))]


def stats(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "total_seen": q("SELECT COUNT(*) FROM jobs"),
        "judged": q("SELECT COUNT(*) FROM jobs WHERE llm_judged_at IS NOT NULL"),
        "verdict_apply": q("SELECT COUNT(*) FROM jobs WHERE llm_verdict='apply'"),
        "verdict_review": q("SELECT COUNT(*) FROM jobs WHERE llm_verdict='review'"),
        "queued": q("SELECT COUNT(*) FROM jobs WHERE queued_on IS NOT NULL"),
        "with_feedback": q("SELECT COUNT(*) FROM jobs WHERE my_feedback IS NOT NULL"),
    }


if __name__ == "__main__":
    import sys
    conn = connect()
    if len(sys.argv) > 1 and sys.argv[1] == "queue":
        for j in queued_on(conn, sys.argv[2] if len(sys.argv) > 2 else None):
            print(f"[{j['llm_score'] or '-':>3}] {j['company'][:18]:19} {j['title'][:46]}")
            print(f"      {j['llm_verdict'] or '-':8} {j['url']}")
    else:
        for k, v in stats(conn).items():
            print(f"  {k:16} {v}")
