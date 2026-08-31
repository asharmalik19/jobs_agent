"""What you ask the LLM for, and what you compute yourself.

The LLM fills in observations. Your code turns observations into a decision.
(Dataclasses so this runs with no deps; swap to pydantic.BaseModel when you
add them, so you can use client.messages.parse() for schema-validated output.)
"""
from dataclasses import dataclass, field


@dataclass
class Requirement:
    requirement: str        # what the JD asks for, in the JD's own words
    met: bool               # does the candidate meet it
    jd_quote: str           # VERBATIM line from the JD stating this requirement
    my_evidence: str        # which capability atom satisfies it ("" if unmet)


@dataclass
class Assessment:
    """Every field is an observation, not a decision."""
    requirements: list[Requirement] = field(default_factory=list)
    seniority: str = "at"          # "under" | "at" | "over"
    years_required: int | None = None
    domain_fit: int = 0            # 0-5: how close to the target domain
    trajectory_fit: int = 0        # 0-5: does this move me toward the 2-year goal
    interview_odds: int = 0        # 0-5: would they realistically interview me
    anti_signals: list[str] = field(default_factory=list)
    strongest_objection: str = ""


# ---- everything below is YOUR code. No model involved. Tune freely. ----

WEIGHTS = {"coverage": 45, "domain": 20, "trajectory": 20, "odds": 15}


def decide(a: Assessment) -> dict:
    # 1. AUDIT: a requirement claimed as met with no verbatim JD quote is not
    #    credited. This is what stops the model inventing fit.
    credited, uncredited = [], []
    for r in a.requirements:
        if r.met and r.jd_quote.strip() and r.my_evidence.strip():
            credited.append(r)
        elif r.met:
            uncredited.append(r.requirement)

    total = len(a.requirements) or 1
    coverage = len(credited) / total

    # 2. SCORE: computed from the observations, with weights you own.
    score = (WEIGHTS["coverage"] * coverage
             + WEIGHTS["domain"] * a.domain_fit / 5
             + WEIGHTS["trajectory"] * a.trajectory_fit / 5
             + WEIGHTS["odds"] * a.interview_odds / 5)
    score -= 15 * len(a.anti_signals)
    if a.seniority == "over":
        score -= 25                      # they want someone more senior
    score = max(0, min(100, round(score)))

    # 3. READINESS: separate axis from score. A reach role is not a bad role.
    if coverage >= 0.7 and a.seniority != "over":
        readiness = "ready_now"
    elif coverage >= 0.4:
        readiness = "reach"
    else:
        readiness = "not_yet"

    # 4. VERDICT: thresholds live here, so recalibrating never touches the prompt.
    if a.anti_signals and score < 55:
        verdict = "reject"
    elif score >= 70 and readiness == "ready_now":
        verdict = "apply"
    elif score >= 55:
        verdict = "review"              # goes in the queue, flagged
    else:
        verdict = "reject"

    return dict(score=score, verdict=verdict, readiness=readiness,
                coverage=f"{len(credited)}/{total}",
                missing=[r.requirement for r in a.requirements if not r.met],
                uncredited_claims=uncredited)
