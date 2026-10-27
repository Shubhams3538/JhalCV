"""Rule-based tailoring: score a job description against the master profile and
select what goes on the page.

This module is deliberately deterministic and LLM-free. It exists so v1 can be
judged on RENDER quality alone. The LLM version swaps in behind `tailor()` with
the same signature and the same output contract, so nothing downstream changes.
"""

from __future__ import annotations

import re
from collections import defaultdict

# Angle vocabulary. A JD is scored against each angle; those scores then rank
# bullets and projects. Closed enum by design — see notes in README.
ANGLE_KEYWORDS = {
    "backend": """api apis rest restful backend server-side microservice microservices
        endpoint endpoints node nodejs express spring django fastapi flask grpc graphql
        sql database databases postgres postgresql mysql mongodb redis orm auth
        authentication authorization webhook queue caching""",
    "systems": """performance latency throughput concurrency concurrent thread threading
        threads multithreaded memory low-level systems compiler runtime interpreter
        optimize optimization profiling kernel socket sockets sandbox sandboxing
        parallel lock-free allocator embedded c c++ cpp rust zig go golang""",
    "infra": """docker kubernetes k8s aws gcp azure cloud ci cd ci/cd pipeline deploy
        deployment terraform ansible infrastructure devops sre cdn scaling scalability
        observability monitoring prometheus grafana serverless lambda s3 ec2""",
    "frontend": """react reactjs frontend front-end ui ux javascript typescript css html
        tailwind next nextjs vue angular svelte responsive android ios mobile electron
        chromium browser dom component components""",
    "agentic": """agent agents agentic llm llms rag prompt prompting openai anthropic
        claude gpt langchain langgraph llamaindex embedding embeddings vector
        vectordb pinecone chroma genai generative tool-calling function-calling mcp
        fine-tuning inference chatbot copilot autonomous multi-agent""",
    "data": """data etl elt pipeline pipelines analytics warehouse lakehouse pandas numpy
        spark airflow dbt kafka streaming ml machine-learning model training dataset""",
    "eval": """eval evals evaluation testing test tests qa benchmark benchmarking
        regression pytest unit integration coverage golden""",
    "dsa": """algorithm algorithms data-structures problem-solving competitive
        complexity optimization leetcode codeforces""",
    "teaching": """mentor mentoring mentorship teach teaching training instructor
        onboarding curriculum workshop lead leadership""",
}

# Terms that mean the same thing to a human but not to a substring match.
ALIASES = {
    "k8s": "kubernetes", "kubernetes": "k8s",
    "js": "javascript", "ts": "typescript",
    "postgres": "postgresql", "postgresql": "postgres",
    "golang": "go", "cpp": "c++", "c++": "cpp",
    "nodejs": "node.js", "node": "node.js",
    "ml": "machine learning", "ai": "artificial intelligence",
    "rest": "rest apis", "restful": "rest apis",
}

# Named technologies only. The gap report draws from this set, so it surfaces
# "you're missing Kubernetes" and never "you're missing 'performance'" — concept
# words are things you demonstrate, not things you list.
TECH_TERMS = set("""
    python java javascript typescript go golang rust c c++ cpp ruby php scala kotlin swift
    node nodejs express django flask fastapi spring rails laravel dotnet
    react vue angular svelte next nextjs tailwind redux
    postgres postgresql mysql mongodb redis sqlite cassandra dynamodb elasticsearch neo4j
    docker kubernetes k8s terraform ansible jenkins github gitlab circleci argocd
    aws gcp azure lambda s3 ec2 eks cloudflare vercel
    kafka rabbitmq celery airflow spark hadoop dbt snowflake databricks
    graphql grpc rest websocket protobuf openapi
    prometheus grafana datadog sentry opentelemetry
    openai anthropic claude gpt langchain langgraph llamaindex huggingface
    pinecone weaviate chroma qdrant faiss pgvector
    pytorch tensorflow sklearn pandas numpy
    pytest jest cypress playwright selenium junit
    linux kubernetes nginx envoy istio grpc
    electron chromium android ios flutter firebase supabase
""".split())

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./-]*")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _term_present(term: str, blob: str) -> bool:
    """Word-boundary-ish containment that survives '+', '#', '.' in tech names."""
    t = re.escape(term.lower())
    return re.search(rf"(?<![a-z0-9]){t}(?![a-z0-9])", blob) is not None


def parse_jd(jd_text: str, profile: dict) -> dict:
    """Extract angle weights and matched/missing technologies from a JD.

    The LLM version of this returns the same dict shape with better recall.
    """
    blob = " " + jd_text.lower() + " "
    counts = defaultdict(int)

    for angle, words in ANGLE_KEYWORDS.items():
        for w in words.split():
            if _term_present(w, blob):
                counts[angle] += 1

    total = sum(counts.values()) or 1
    angle_scores = {a: round(c / total, 4) for a, c in counts.items()}

    # Technology matching against everything the profile actually claims.
    vocab = set()
    for group in profile.get("skills", {}).values():
        if isinstance(group, list):
            vocab.update(group)
    for e in profile.get("experience", []):
        vocab.update(e.get("stack", []))
    for p in profile.get("projects", []):
        vocab.update(p.get("stack", []))

    matched, missing_from_jd = set(), set()
    for term in vocab:
        if _term_present(term, blob) or _term_present(ALIASES.get(term.lower(), "\0"), blob):
            matched.add(term)

    # Tech the JD asks for that we can't find anywhere in the profile: the gap report.
    jd_tokens = set(_tokens(jd_text))
    known = {v.lower() for v in vocab}
    # A profile skill's alias also counts as covering the JD's spelling.
    for v in list(known):
        if v in ALIASES:
            known.add(ALIASES[v].lower())
    for tok in jd_tokens & TECH_TERMS:
        if tok in known or ALIASES.get(tok, "\0").lower() in known:
            continue
        missing_from_jd.add(tok)

    return {
        "angle_scores": dict(sorted(angle_scores.items(), key=lambda kv: -kv[1])),
        "matched_skills": sorted(matched),
        "gap_skills": sorted(missing_from_jd),
        "jd_blob": blob,
    }



MIN_SHIPPED_BULLETS = 2

# Skills printed per group. Groups may hold more; the most relevant ones for the
# job come first and the rest wait for a job that asks for them.
MAX_SKILLS_PER_GROUP = 9


def renderable(profile: dict) -> dict:
    """Strip anything not yet true from the profile before any tailorer sees it.

    Bullets carry status "shipped" (default) or "planned". Planned bullets are
    written in advance - they are the spec for what you are building - but they
    describe work that does not exist yet, so no tailorer is ever shown them. A
    project with fewer than MIN_SHIPPED_BULLETS shipped bullets is not resume-ready
    and is dropped entirely rather than rendered thin.
    """
    out = dict(profile)
    projects = []
    for p in profile.get("projects", []):
        shipped = [b for b in p["bullets"] if b.get("status", "shipped") == "shipped"]
        if len(shipped) >= MIN_SHIPPED_BULLETS:
            projects.append({**p, "bullets": shipped})
    out["projects"] = projects
    return out


def project_cap(profile: dict, requested: int) -> int:
    """The page cap is a property of the profile, not a command-line flag."""
    hard = profile.get("_locked", {}).get("max_projects_on_page")
    return min(requested, hard) if hard else requested


def _score_bullet(bullet: dict, jd: dict) -> float:
    angles = bullet.get("angles", [])
    score = sum(jd["angle_scores"].get(a, 0.0) for a in angles)
    # Small bonus when the bullet text itself names something the JD names.
    for term in jd["matched_skills"]:
        if _term_present(term, bullet["text"].lower()):
            score += 0.05
    # Slight preference for bullets carrying a number.
    if re.search(r"\d", bullet["text"]):
        score += 0.02
    return round(score, 5)


def _score_project(project: dict, jd: dict) -> float:
    score = sum(jd["angle_scores"].get(a, 0.0) for a in project.get("angles", []))
    for tech in project.get("stack", []):
        if tech in jd["matched_skills"]:
            score += 0.15
    best_bullets = sorted((_score_bullet(b, jd) for b in project["bullets"]), reverse=True)
    score += sum(best_bullets[:3]) * 0.5
    return round(score, 5)


def _pick_angle(project: dict, jd: dict) -> str | None:
    angles = project.get("angles", [])
    if not angles:
        return None
    return max(angles, key=lambda a: jd["angle_scores"].get(a, 0.0))


def tailor(profile: dict, jd_text: str, *, max_projects: int = 2,
           bullets_per_job: int = 4, bullets_per_project: int = 3) -> tuple[dict, dict]:
    """Return (resume_data, report).

    resume_data is exactly what templates/classic.typ consumes.
    report is the human-readable explanation of every decision made.
    """
    profile = renderable(profile)
    max_projects = project_cap(profile, max_projects)
    jd = parse_jd(jd_text, profile)
    report = {"angle_scores": jd["angle_scores"], "gap_skills": jd["gap_skills"],
              "matched_skills": jd["matched_skills"], "decisions": []}

    frozen = set(profile.get("_locked", {}).get("frozen_sections", []))

    # ── experience ───────────────────────────────────────────────────────────
    # When frozen, every bullet renders verbatim in profile order. The tailorer
    # is not allowed to drop or reorder anything here, whatever the JD says.
    experience = []
    if "experience" in frozen:
        for e in profile["experience"]:
            experience.append({
                "org": e["org"], "role": e["role"], "location": e["location"],
                "start": e["start"], "end": e["end"],
                "bullets": [b["text"] for b in e["bullets"]],
                "_bullet_meta": [{"id": b["id"], "score": None} for b in e["bullets"]],
            })
            report["decisions"].append({"section": "experience", "org": e["org"],
                                        "frozen": True,
                                        "kept": [b["id"] for b in e["bullets"]]})
    for e in ([] if "experience" in frozen else profile["experience"]):
        scored = sorted(
            ({**b, "_score": _score_bullet(b, jd)} for b in e["bullets"]),
            key=lambda b: -b["_score"],
        )
        quota = e.get("max_bullets", bullets_per_job)
        chosen = scored[:quota]
        # Restore original order — chronology within a job reads better than
        # relevance order, and the reader skims the left edge either way.
        order = {b["id"]: i for i, b in enumerate(e["bullets"])}
        chosen.sort(key=lambda b: order[b["id"]])
        experience.append({
            "org": e["org"], "role": e["role"], "location": e["location"],
            "start": e["start"], "end": e["end"],
            "bullets": [b["text"] for b in chosen],
            "_bullet_meta": [{"id": b["id"], "score": b["_score"]} for b in chosen],
        })
        report["decisions"].append({
            "section": "experience", "org": e["org"],
            "kept": [b["id"] for b in chosen],
            "dropped": [b["id"] for b in scored[quota:]],
        })

    # ── projects: rank, take top N, angle each one ──────────────────────────
    ranked = sorted(
        ({**p, "_score": _score_project(p, jd)} for p in profile["projects"]),
        key=lambda p: -p["_score"],
    )
    projects = []
    for p in ranked[:max_projects]:
        angle = _pick_angle(p, jd)
        pb = sorted(
            ({**b, "_score": _score_bullet(b, jd)} for b in p["bullets"]),
            key=lambda b: -b["_score"],
        )
        chosen = pb[:bullets_per_project]
        order = {b["id"]: i for i, b in enumerate(p["bullets"])}
        chosen.sort(key=lambda b: order[b["id"]])
        entry = {
            "name": p["name"], "stack": p.get("stack", []),
            "bullets": [b["text"] for b in chosen],
            "_bullet_meta": [{"id": b["id"], "score": b["_score"]} for b in chosen],
        }
        if "link" in p:
            entry["link"] = p["link"]
        projects.append(entry)
        report["decisions"].append({
            "section": "projects", "name": p["name"], "score": p["_score"],
            "angle": angle, "kept": [b["id"] for b in chosen],
        })
    report["decisions"].append({
        "section": "projects", "not_selected":
            [{"name": p["name"], "score": p["_score"]} for p in ranked[max_projects:]],
    })

    # ── skills: reorder within groups so JD-matched terms come first ─────────
    skills = []
    for label, items in profile["skills"].items():
        if label.startswith("_") or not isinstance(items, list):
            continue
        hits = [s for s in items if s in jd["matched_skills"]]
        rest = [s for s in items if s not in jd["matched_skills"]]
        skills.append({"label": label, "items": (hits + rest)[:MAX_SKILLS_PER_GROUP], "_matched": hits})

    resume_data = {
        "basics": profile["basics"],
        "experience": experience,
        "projects": projects,
        "coding_profiles": [
            {k: v for k, v in c.items() if not k.startswith("needs_")}
            for c in profile.get("coding_profiles", [])
        ],
        "skills": skills,
        "education": profile["education"],
        "section_order": ["experience", "coding_profiles", "projects", "skills", "education"],
        "config": {},
    }
    return resume_data, report
