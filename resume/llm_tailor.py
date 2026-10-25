"""LLM-backed tailoring. Mirrors the contract of the rule-based `tailor` module.

Stage 1 (here): comprehend the job description.
Stage 2 (Step 3): choose projects, bullets and skill ordering.

Every function returns the same shapes the rule-based tailorer returns, so
nothing downstream needs to know which brain produced them.
"""

from __future__ import annotations

import re

from . import llm
from .tailor import (ANGLE_KEYWORDS, parse_jd as parse_jd_rules,
                     tailor as tailor_rules, renderable, project_cap,
                     _score_bullet, _score_project)

# The closed angle enum. Kept in sync with the rule-based vocabulary by deriving
# it from the same dict, so the two tailorers can never disagree about what
# angles exist.
ANGLES = sorted(ANGLE_KEYWORDS.keys())

# Model chosen by measurement, not by price. On the sample JDs, gpt-4o-mini
# scored a role demanding "close to the metal" work, thread pooling and memory
# optimisation as 0% systems / 60% backend, and claimed Docker was required by a
# JD that never mentions it. gpt-4o fixed that; gpt-5.5 does better still and is
# the only one that mapped "networking" onto Computer Networks - the kind of
# semantic catch plain string matching can never make. Cached after first use.
JD_MODEL = "gpt-5.5"

ANGLE_GLOSSARY = {
    "agentic":  "LLMs, agents, RAG, embeddings, vector search, tool calling, prompting, evals of model output",
    "backend":  "APIs, services, databases, auth, caching, business logic",
    "systems":  "performance, concurrency, memory, compilers, runtimes, sockets, low-level work",
    "infra":    "Docker, Kubernetes, cloud, CI/CD, deployment, observability, scaling",
    "frontend": "UI, React/Vue/Next, browser, mobile, desktop clients",
    "data":     "pipelines, ETL, warehouses, analytics, ML training, streaming",
    "eval":     "testing, benchmarking, regression suites, quality harnesses",
    "dsa":      "algorithms, data structures, competitive programming",
    "teaching": "mentoring, instruction, curriculum, onboarding others",
}


# The gap report must name technologies, never qualities. A model asked for
# "missing technologies" will still occasionally return "problem solving" or
# "scalability"; any entry containing one of these tokens is rejected.
CONCEPT_TOKENS = set("""
    problem solving skills skill ability abilities experience knowledge understanding
    scalability resilience reliability availability maintainability performance
    communication teamwork collaboration leadership ownership mentorship
    quality excellence fundamentals principles practices methodologies patterns
    thinking mindset attitude passion motivation degree bachelors masters phd
    strong excellent good solid proven deep broad hands-on
    architecture design development engineering programming coding software
    systems backend frontend fullstack cloud security testing agile scrum
""".split())

SYSTEM = """You read software job descriptions and characterise what the role \
actually wants. You are precise and you do not flatter the reader: if a job is \
mostly CRUD backend work with one mention of AI, say so in the weights.

You judge meaning, not keywords. "We run everything in containers on GCP" is \
infrastructure even though it never says Docker or Kubernetes. "You will own \
the harness that stops our agents regressing" is both agentic and eval."""


def _prompt(jd_text: str, vocab: list[str]) -> str:
    glossary = "\n".join(f"  {a}: {ANGLE_GLOSSARY[a]}" for a in ANGLES)
    return f"""Characterise this job description.

ANGLES — distribute weight across these, summing to roughly 1.0. Use 0.0 for \
angles the role genuinely does not involve; do not spread weight evenly out of \
politeness.
{glossary}

CANDIDATE SKILLS — this is the full list of technologies the applicant can \
claim. Return the subset this job actually calls for, including ones named \
only by implication ("container orchestration" implies Kubernetes).
{", ".join(sorted(vocab))}

MISSING SKILLS — technologies this job requires that are NOT in the candidate \
list above. Name the technology only (e.g. "kubernetes", "redis"), lowercase, \
no concepts like "scalability" or "problem solving".

JOB DESCRIPTION
---
{jd_text.strip()}
---"""


def _schema(vocab: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "One sentence: what this role really is."},
            "seniority": {"type": "string",
                          "enum": ["intern", "junior", "mid", "senior", "staff", "unclear"]},
            "angle_scores": {
                "type": "object",
                # Every angle is required, so the model must state an explicit
                # 0.0 rather than silently omitting one.
                "properties": {a: {"type": "number"} for a in ANGLES},
            },
            # An enum makes inventing a skill structurally impossible: the API
            # rejects any value outside the candidate's own vocabulary.
            "matched_skills": {"type": "array",
                               "items": {"type": "string", "enum": sorted(vocab)}},
            "gap_skills": {"type": "array", "items": {"type": "string"}},
        },
    }


def _vocabulary(profile: dict) -> list[str]:
    vocab = set()
    for group in profile.get("skills", {}).values():
        if isinstance(group, list):
            vocab.update(group)
    for section in ("experience", "projects"):
        for entry in profile.get(section, []):
            vocab.update(entry.get("stack", []))
    return sorted(vocab)


def normalise(raw: dict, vocab: list[str]) -> dict:
    """Clamp and normalise a raw model response into the rule-based shape.

    The schema already guarantees types and skill membership; this handles the
    things a schema cannot express — negative weights, weights that do not sum
    to 1, junk in the free-text gap list.
    """
    scores = {}
    for angle in ANGLES:
        try:
            v = float(raw.get("angle_scores", {}).get(angle, 0.0))
        except (TypeError, ValueError):
            v = 0.0
        scores[angle] = max(0.0, v)
    total = sum(scores.values())
    if total > 0:
        scores = {a: round(v / total, 4) for a, v in scores.items()}
    scores = dict(sorted(scores.items(), key=lambda kv: -kv[1]))

    allowed = set(vocab)
    matched = sorted({s for s in raw.get("matched_skills", []) if s in allowed})

    known = {v.lower() for v in vocab}
    gaps = set()
    for g in raw.get("gap_skills", []):
        g = str(g).strip().lower()
        # One or two tokens, not already claimed, not a vague concept word.
        if not g or len(g) > 30 or g in known:
            continue
        tokens = g.split()
        if len(tokens) > 2 or any(t in CONCEPT_TOKENS for t in tokens):
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9+#./ -]*", g):
            gaps.add(g)

    return {
        "angle_scores": scores,
        "matched_skills": matched,
        "gap_skills": sorted(gaps),
        "jd_blob": raw.get("_jd_blob", ""),
        "summary": raw.get("summary", ""),
        "seniority": raw.get("seniority", "unclear"),
    }


def parse_jd_llm(jd_text: str, profile: dict, *, model: str = JD_MODEL,
                 cache: bool = True) -> tuple[dict, bool]:
    """LLM version of `tailor.parse_jd`. Returns (result, cache_hit).

    Raises LLMError on failure — callers fall back to the rule-based parser.
    """
    vocab = _vocabulary(profile)
    raw, hit = llm.ask(
        _prompt(jd_text, vocab),
        _schema(vocab),
        model=model,
        system=SYSTEM,
        schema_name="jd_analysis",
        cache=cache,
    )
    raw["_jd_blob"] = " " + jd_text.lower() + " "
    result = normalise(raw, vocab)

    # Hybrid. Measured on real JDs, the model is better at weighting emphasis but
    # WORSE at recall than plain string matching - gpt-4o-mini missed "Kubernetes"
    # and "Node.js" where the JD named them outright, and both models missed
    # "anthropic" and "react" as gaps. Literal matching is already solved, so the
    # rule-based pass stays and the model only adds to it.
    #
    #   angle_scores    <- model alone (the judgment call)
    #   matched_skills  <- union (rules guarantee literals, model adds implied)
    #   gap_skills      <- union (rules guarantee recall, model adds semantic)
    rules = parse_jd_rules(jd_text, profile)
    result["matched_skills"] = sorted(set(result["matched_skills"]) | set(rules["matched_skills"]))
    result["gap_skills"] = sorted(set(result["gap_skills"]) | set(rules["gap_skills"]))
    result["_angles_from"] = "llm"
    result["_skills_from"] = "rules+llm"
    return result, hit


def parse_jd_safe(jd_text: str, profile: dict, **kw) -> tuple[dict, str]:
    """Try the LLM, fall back to rules. Returns (result, which_ran)."""
    if not llm.have_key():
        return parse_jd_rules(jd_text, profile), "rules (no API key)"
    try:
        result, hit = parse_jd_llm(jd_text, profile, **kw)
        return result, f"llm{' (cached)' if hit else ''}"
    except llm.LLMError as e:
        result = parse_jd_rules(jd_text, profile)
        result["_fallback_reason"] = str(e)
        return result, f"rules (llm failed: {e})"


# ── Stage 2: selection ───────────────────────────────────────────────────

SELECT_MODEL = JD_MODEL

SELECT_SYSTEM = """You choose which of a candidate's existing projects and \
bullets belong on their resume for one specific job.

You never write resume text. You return identifiers only; the renderer looks up \
the candidate's own wording. Pick what a hiring manager for THIS role would most \
want to see, and order skills so the ones this job asks for come first."""


def _catalog(profile: dict, max_projects: int, bullets_per_project: int) -> str:
    required = min(max_projects, len(profile["projects"]))
    lines = [f"PROJECTS — choose exactly {required}, "
             f"at most {bullets_per_project} bullets each."]
    for p in profile["projects"]:
        lines.append(f"\n  id: {p['id']}   name: {p['name']}")
        lines.append(f"    stack: {', '.join(p.get('stack', []))}")
        lines.append(f"    angles available: {', '.join(p.get('angles', []))}")
        for b in p["bullets"]:
            lines.append(f"    [{b['id']}] ({'/'.join(b.get('angles', []))}) {b['text']}")
    lines.append("\nSKILL GROUPS — reorder within each group. "
                 "Return every item, none added, none dropped.")
    for label, items in profile["skills"].items():
        if not label.startswith("_") and isinstance(items, list):
            lines.append(f"  {label}: {', '.join(items)}")
    return "\n".join(lines)


def _select_prompt(profile, analysis, max_projects, bullets_per_project) -> str:
    weights = ", ".join(f"{a} {v:.0%}" for a, v in list(analysis["angle_scores"].items())[:5] if v)
    return f"""This job weights: {weights}
It asks for: {', '.join(analysis['matched_skills']) or '-'}
Summary: {analysis.get('summary', '')}

{_catalog(profile, max_projects, bullets_per_project)}

Choose the projects, the angle to present each one under, the bullets to show, \
and the ordering of every skill group."""


def _select_schema(profile: dict) -> dict:
    project_ids = [p["id"] for p in profile["projects"]]
    bullet_ids = [b["id"] for p in profile["projects"] for b in p["bullets"]]
    angles = sorted({a for p in profile["projects"] for a in p.get("angles", [])})
    groups = [k for k, v in profile["skills"].items()
              if not k.startswith("_") and isinstance(v, list)]
    items = sorted({i for k, v in profile["skills"].items()
                    if not k.startswith("_") and isinstance(v, list) for i in v})
    return {
        "type": "object",
        "properties": {
            "projects": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": project_ids},
                    "angle": {"type": "string", "enum": angles},
                    "bullet_ids": {"type": "array",
                                   "items": {"type": "string", "enum": bullet_ids}},
                    "why": {"type": "string",
                            "description": "One short clause: why this project for this job."},
                }}},
            "skills": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "enum": groups},
                    "ordered_items": {"type": "array",
                                      "items": {"type": "string", "enum": items}},
                }}},
        },
    }


def validate_selection(raw: dict, profile: dict, analysis: dict, *,
                       max_projects: int, bullets_per_project: int) -> tuple[dict, list[str]]:
    """Repair a raw selection into something safe to render.

    The schema already guarantees that every identifier exists somewhere. What it
    cannot express is ownership (does this bullet belong to this project?) or
    completeness (did every skill come back?). Both are enforced here, and every
    repair is recorded so nothing is silently corrected.
    """
    problems: list[str] = []
    by_id = {p["id"]: p for p in profile["projects"]}

    chosen, seen = [], set()
    for entry in raw.get("projects", [])[:max_projects]:
        pid = entry.get("id")
        proj = by_id.get(pid)
        if proj is None or pid in seen:
            problems.append(f"dropped unknown or duplicate project {pid!r}")
            continue
        seen.add(pid)

        angle = entry.get("angle")
        if angle not in proj.get("angles", []):
            angle = max(proj.get("angles", ["backend"]),
                        key=lambda a: analysis["angle_scores"].get(a, 0.0))
            problems.append(f"{pid}: angle not offered by the project, using {angle!r}")

        owned = {b["id"]: b for b in proj["bullets"]}
        ids, dupes = [], set()
        for bid in entry.get("bullet_ids", []):
            if bid not in owned:
                problems.append(f"{pid}: bullet {bid!r} belongs to another project")
            elif bid in dupes:
                problems.append(f"{pid}: bullet {bid!r} listed twice")
            else:
                dupes.add(bid)
                ids.append(bid)
        ids = ids[:bullets_per_project]
        if not ids:
            ids = [b["id"] for b in proj["bullets"][:bullets_per_project]]
            problems.append(f"{pid}: no usable bullets returned, using profile order")

        # Render in profile order, not relevance order: chronology reads better
        # and the reader skims the left edge either way.
        order = {b["id"]: i for i, b in enumerate(proj["bullets"])}
        ids.sort(key=lambda b: order[b])

        entry_out = {"name": proj["name"], "stack": proj.get("stack", []),
                     "bullets": [owned[b]["text"] for b in ids],
                     "_bullet_meta": [{"id": b, "score": 0.0} for b in ids]}
        if "link" in proj:
            entry_out["link"] = proj["link"]
        chosen.append(entry_out)

    # The user already narrowed the library to the projects they want. When that
    # fits the page, every one of them is shown, whatever the model returned.
    required = min(max_projects, len(profile["projects"]))
    if chosen and len(chosen) < required:
        missing = sorted((p for p in profile["projects"] if p["id"] not in seen),
                         key=lambda p: -_score_project(p, analysis))
        for proj in missing[:required - len(chosen)]:
            scored = sorted(proj["bullets"], key=lambda b: -_score_bullet(b, analysis))
            ids = {b["id"] for b in scored[:bullets_per_project]}
            kept = [b for b in proj["bullets"] if b["id"] in ids]
            entry_out = {"name": proj["name"], "stack": proj.get("stack", []),
                         "bullets": [b["text"] for b in kept],
                         "_bullet_meta": [{"id": b["id"], "score": 0.0} for b in kept]}
            if "link" in proj:
                entry_out["link"] = proj["link"]
            chosen.append(entry_out)
            problems.append(f"{proj['id']}: selected by you but omitted by the model, added")

    if not chosen:
        raise llm.LLMError("selection returned no usable projects")

    # Skills: the output must be a permutation of the input. Anything missing is
    # appended, anything foreign is dropped. A skill can never be invented, and a
    # skill you actually have can never be silently lost.
    returned = {g.get("label"): g.get("ordered_items", []) for g in raw.get("skills", [])}
    skills = []
    for label, items in profile["skills"].items():
        if label.startswith("_") or not isinstance(items, list):
            continue
        allowed = set(items)
        ordered, used = [], set()
        for item in returned.get(label, []):
            if item in allowed and item not in used:
                used.add(item)
                ordered.append(item)
            elif item not in allowed:
                problems.append(f"{label}: dropped {item!r}, not in this group")
        missing = [i for i in items if i not in used]
        if missing:
            problems.append(f"{label}: appended {len(missing)} item(s) the model omitted")
        skills.append({"label": label, "items": ordered + missing})

    return {"projects": chosen, "skills": skills}, problems


def select_llm(profile: dict, analysis: dict, *, max_projects: int = 2,
               bullets_per_project: int = 3, model: str = SELECT_MODEL,
               cache: bool = True) -> tuple[dict, list[str], bool, dict]:
    """Returns (selection, problems, cache_hit, raw)."""
    raw, hit = llm.ask(
        _select_prompt(profile, analysis, max_projects, bullets_per_project),
        _select_schema(profile),
        model=model, system=SELECT_SYSTEM, schema_name="resume_selection", cache=cache,
    )
    selection, problems = validate_selection(
        raw, profile, analysis,
        max_projects=max_projects, bullets_per_project=bullets_per_project)
    return selection, problems, hit, raw


def tailor_llm(profile: dict, jd_text: str, *, max_projects: int = 2,
               bullets_per_job: int = 5, bullets_per_project: int = 3,
               model: str | None = None, cache: bool = True) -> tuple[dict, dict]:
    """LLM tailorer. Same signature and return contract as `tailor.tailor`.

    The rule-based tailorer runs first and produces the complete resume; the LLM
    then overwrites only `projects` and `skills`. Frozen sections are therefore
    generated by exactly the same code path in both modes, which makes it
    impossible for the LLM to disturb them — `resume lock` cannot break because
    the LLM never touches that data.

    Any failure leaves the rule-based result in place. This function does not
    raise; it records what happened in the report and always returns a resume.
    """
    profile = renderable(profile)
    max_projects = project_cap(profile, max_projects)
    data, report = tailor_rules(
        profile, jd_text, max_projects=max_projects,
        bullets_per_job=bullets_per_job, bullets_per_project=bullets_per_project)
    report["tailorer"] = "rules"

    if not llm.have_key():
        report["llm"] = {"used": False, "reason": "no available API key"}
        return data, report

    try:
        analysis, jd_hit = parse_jd_llm(jd_text, profile, model=model or JD_MODEL, cache=cache)
        selection, problems, sel_hit, raw = select_llm(
            profile, analysis, max_projects=max_projects,
            bullets_per_project=bullets_per_project, model=model or SELECT_MODEL, cache=cache)
    except llm.LLMError as e:
        report["llm"] = {"used": False, "reason": str(e)}
        return data, report

    data["projects"] = selection["projects"]
    data["skills"] = selection["skills"]
    report["tailorer"] = "llm"
    report["angle_scores"] = analysis["angle_scores"]
    report["gap_skills"] = analysis["gap_skills"]
    report["llm"] = {
        "used": True,
        "model": model or JD_MODEL,
        "cached": {"jd": jd_hit, "selection": sel_hit},
        "summary": analysis.get("summary", ""),
        "seniority": analysis.get("seniority", ""),
        "repairs": problems,
        "choices": [{"id": c.get("id"), "angle": c.get("angle"), "why": c.get("why", "")}
                    for c in raw.get("projects", [])],
    }
    report["decisions"] = [d for d in report["decisions"] if d.get("section") != "projects"]
    for c in raw.get("projects", []):
        report["decisions"].append({"section": "projects", "name": c.get("id"),
                                    "angle": c.get("angle"), "why": c.get("why", ""),
                                    "by": "llm"})
    return data, report
