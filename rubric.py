"""What you ask the LLM for, and what you compute yourself.

The LLM fills in observations. Your code turns observations into a decision.

Pydantic models rather than dataclasses, so `client.messages.parse()` can
validate the model's output against this schema before your code ever sees it --
a malformed response becomes an exception here instead of a wrong verdict later.

The docstrings on each field are NOT decoration: pydantic puts them in the JSON
schema, the schema goes to the model, and they are the most reliable place to say
what you want. Anything you would have written into the prompt about a field
belongs here instead, next to the field.
"""
from pydantic import BaseModel, Field


class Requirement(BaseModel):
    requirement: str = Field(description="What the JD asks for, in the JD's own words.")
    met: bool = Field(description="Whether the candidate's capabilities satisfy this.")
    jd_quote: str = Field(description=(
        "The VERBATIM line from the job description that states this requirement. "
        "Copy it exactly; do not paraphrase. If you cannot find such a line, "
        "leave this empty -- an empty quote means the requirement is not credited."))
    my_evidence: str = Field(default="", description=(
        "Which specific capability from the candidate profile satisfies this. "
        "Empty string if unmet. Name the capability; do not describe it generically."))


class Assessment(BaseModel):
    """Every field is an OBSERVATION, not a decision.

    Do not output a verdict, a score, or a recommendation anywhere. Scoring is
    done by code that reads these observations.
    """
    requirements: list[Requirement] = Field(default_factory=list, description=(
        "Every requirement the JD states, met or unmet. Cover the whole JD -- "
        "omitting the requirements the candidate fails would corrupt the score."))
    seniority: str = Field(default="at", description=(
        "'under' if the candidate is below the level sought, 'at' if matched, "
        "'over' if the role wants someone materially more senior."))
    years_required: int | None = Field(default=None, description=(
        "Minimum years of experience the JD demands, or null if unstated."))
    domain_fit: int = Field(default=0, ge=0, le=5, description=(
        "0-5: how close this work is to the candidate's target domain."))
    trajectory_fit: int = Field(default=0, ge=0, le=5, description=(
        "0-5: how much taking this role would move the candidate toward their "
        "stated two-year goal."))
    interview_odds: int = Field(default=0, ge=0, le=5, description=(
        "0-5: realistically, would this employer interview this candidate? Judge "
        "the employer's likely behaviour, not the candidate's potential."))
    anti_signals: list[str] = Field(default_factory=list, description=(
        "Concrete disqualifiers present in the JD: clearance, PhD requirement, "
        "onsite-only in an unreachable location, wrong discipline entirely. "
        "Only include things the JD actually states."))
    strongest_objection: str = Field(default="", description=(
        "The single best argument AGAINST the candidate applying. One sentence. "
        "This is the most useful field in the whole assessment -- do not soften it."))


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
