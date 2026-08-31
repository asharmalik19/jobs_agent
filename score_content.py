"""Cheap, transparent content pre-filter. No LLM, no API, runs in <1s over 4k jobs.

Goal is HIGH RECALL, not precision: cut volume enough that the LLM stage is
affordable, without discarding good matches.
"""
import json
import re
import sys

# --- what the work actually involves, weighted by how diagnostic the term is ---
SKILLS = {
    # strong signal: you only write these words if the job is really this job
    r"\bLLMs?\b|large language model": 3, r"\bRAG\b|retrieval.augmented": 3,
    r"agentic|\bagents?\b.{0,20}(framework|system|workflow)": 3,
    r"\bevals?\b|eval.driven|offline evaluation": 3,
    r"fine.tun|\bSFT\b|\bRLHF\b|\bDPO\b": 3, r"vector (db|database|store|search)|embeddings?": 3,
    r"prompt (engineering|design|template)": 2, r"\bMCP\b|model context protocol": 3,
    r"inference (server|optimi|latency|endpoint)|\bvLLM\b|token throughput": 2,
    r"LangChain|LlamaIndex|LangGraph|Haystack|DSPy": 2,
    r"PyTorch|TensorFlow|Hugging ?Face|transformers": 2,
    r"OpenAI|Anthropic|Claude|GPT-|Gemini|Bedrock": 2,
    # backend / integration signal (your partner-developer angle)
    r"\bAPIs?\b.{0,30}(design|build|integrat)|REST|GraphQL": 1,
    r"\bPython\b": 1, r"Postgres|SQL|BigQuery|Snowflake": 1,
    r"Docker|Kubernetes|Terraform|CI/CD": 1,
    r"customer.facing|forward.deployed|solutions? (engineer|architect)": 2,
    r"partner (integration|engineering|developer)|third.party integration": 2,
    r"prototyp|proof.of.concept|\bPoC\b": 1,
}

# --- disqualifiers found in prose, not in the title ---
ANTI = {
    r"\bPhD\b.{0,40}(required|must)": -6, r"\b(1[0-9]|[8-9])\+? years": -5,
    r"publication|first.author|NeurIPS|ICML|ICLR": -3,
    r"security clearance|TS/SCI|\bpolygraph\b": -8,
    r"quota|pipeline generation|book of business|close deals|prospecting": -6,
    r"Salesforce admin|Workday admin|SAP\b|NetSuite": -4,
    r"on.call rotation.{0,40}(24|primary)|tier.[12] support": -3,
}

SECTION_RE = re.compile(
    r"(responsibilit|qualificat|requirement|you (will|may|should)|what you.ll do|"
    r"about you|who you are|skills|experience|we.re looking for|you have|minimum)", re.I)


def requirements_section(desc):
    """JDs are ~60% boilerplate. Signal concentrates after the first requirements heading."""
    m = SECTION_RE.search(desc)
    return desc[m.start():m.start() + 6000] if m else desc[:6000]


def min_years(desc):
    """Seniority is a hard filter hiding in prose."""
    yrs = [int(m) for m in re.findall(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?", desc)]
    yrs = [y for y in yrs if 0 < y < 25]
    return min(yrs) if yrs else None


def score(job):
    text = requirements_section(job["description"])
    hits, pts = [], 0
    for pattern, w in SKILLS.items():
        if re.search(pattern, text, re.I):
            pts += w
            hits.append(pattern.split("|")[0].replace("\\b", "").replace(".", " ")[:18])
    flags = []
    for pattern, w in ANTI.items():
        if re.search(pattern, text, re.I):
            pts += w
            flags.append(pattern.split("|")[0].replace("\\b", "")[:22])
    return pts, hits, flags


if __name__ == "__main__":
    jobs = json.load(open("jobs.json"))
    scored = []
    for j in jobs:
        pts, hits, flags = score(j)
        j.update(content_score=pts, hits=hits, flags=flags, min_years=min_years(j["description"]))
        scored.append(j)

    title_re = re.compile(r"\bai\b|machine learning|\bml\b|\bllm\b", re.I)
    scored.sort(key=lambda j: -j["content_score"])

    mode = sys.argv[1] if len(sys.argv) > 1 else "missed"
    if mode == "missed":
        print("=== HIGH CONTENT SCORE, but NO AI/ML IN TITLE (what you're missing) ===\n")
        pool = [j for j in scored if not title_re.search(j["title"])]
    else:
        print("=== TOP BY CONTENT SCORE ===\n")
        pool = scored
    for j in pool[:14]:
        yrs = f"{j['min_years']}y" if j["min_years"] else "  -"
        print(f"{j['content_score']:3}  {yrs:4} {j['company'][:11]:12} {j['title'][:52]}")
        print(f"          {', '.join(j['hits'][:7])}")
        if j["flags"]:
            print(f"          ANTI: {', '.join(j['flags'])}")
        print()

    band = lambda lo, hi: len([j for j in scored if lo <= j["content_score"] < hi])
    print(f"distribution:  >=15: {band(15,999)}   10-14: {band(10,15)}   "
          f"5-9: {band(5,10)}   <5: {band(-999,5)}   (total {len(scored)})")
