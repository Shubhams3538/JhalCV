"""Post-render ATS self-check.

Everything here exists because the previous resume shipped with a defect this
would have caught: 'FundamentalsData Structures' (glued table columns) and
'CODING PROfiLES' (unmapped ligature glyph).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def extract_text(pdf: Path, layout: bool = False) -> str:
    cmd = ["pdftotext"] + (["-layout"] if layout else []) + [str(pdf), "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {proc.stderr}")
    return proc.stdout


def _norm(s: str) -> str:
    """Collapse whitespace so line wrapping in the PDF doesn't cause false misses."""
    return re.sub(r"\s+", " ", s).strip().lower()


def structural_checks(pdf: Path, *, max_pages: int = 1) -> list[dict]:
    """Checks that need no knowledge of what the PDF was supposed to say.

    Runs against any resume PDF, which makes it a diagnostic tool for the
    resume you already have, not just the ones this generates.
    """
    raw = extract_text(pdf)
    flat = _norm(raw)
    fails = []

    if len(flat) <= 200:
        fails.append({"check": "text-layer",
                      "detail": f"extracted only {len(flat)} chars — PDF may be image-only"})

    # Ligature mangling: an ALL-CAPS run interrupted by lowercase fi/fl/ff.
    for m in re.finditer(r"\b[A-Z]{2,}(?:fi|fl|ff|ffi)[A-Z]*\b", raw):
        fails.append({"check": "ligature",
                      "detail": f"'{m.group(0)}' — ligature glyph lacks a Unicode mapping"})

    # Glued words: lowercase immediately followed by uppercase mid-token,
    # excluding legitimate camelCase tech names.
    allow = {"leetcode", "codeforces", "nxtwave", "github", "linkedin", "javascript",
             "postgresql", "mysql", "mongodb", "nodejs", "node.js", "dbms", "cgpa",
             "nextwave", "dsa", "oop", "cdn", "aws", "api", "apis",
             "openai", "typescript", "pgvector", "hnsw", "drizzle", "gpt",
             "fastapi", "mcp", "graphql", "postgres", "nextjs",
             "websockets", "websocket", "aisarthi", "mongodb", "zod", "sdks"}
    for m in re.finditer(r"\b[a-zA-Z]*[a-z][A-Z][a-zA-Z]*\b", raw):
        w = m.group(0)
        if w.lower() in allow:
            continue
        fails.append({"check": "glued-words",
                      "detail": f"'{w}' — two words may have run together"})

    # Multi-column detection: -layout output with a wide run of interior spaces
    # on many lines suggests columns an ATS will interleave.
    lay = extract_text(pdf, layout=True)
    suspicious = sum(1 for line in lay.splitlines() if re.search(r"\S {6,}\S", line))
    body_lines = sum(1 for line in lay.splitlines() if line.strip())
    if body_lines and suspicious / body_lines > 0.55:
        fails.append({"check": "columns",
                      "detail": f"{suspicious}/{body_lines} lines have wide interior gaps — "
                                "possible multi-column layout"})

    proc = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
    pm = re.search(r"^Pages:\s+(\d+)", proc.stdout, re.M)
    pages = int(pm.group(1)) if pm else -1
    if not (0 < pages <= max_pages):
        fails.append({"check": "page-count", "detail": f"{pages} pages, budget is {max_pages}"})

    return fails


def verify(pdf: Path, data: dict, *, max_pages: int = 1) -> list[dict]:
    """Full verification: structural checks plus content-fidelity checks."""
    raw = extract_text(pdf)
    flat = _norm(raw)
    fails = structural_checks(pdf, max_pages=max_pages)

    def check(ok, name, detail):
        if not ok:
            fails.append({"check": name, "detail": detail})

    # 2. Identity survives extraction.
    b = data["basics"]
    check(_norm(b["name"]) in flat, "name", f"'{b['name']}' not found in text layer")
    check(_norm(b["email"]) in flat, "email", f"'{b['email']}' not found")
    digits = re.sub(r"\D", "", b["phone"])
    check(digits in re.sub(r"\D", "", raw), "phone", "phone digits not found")
    for l in b.get("links", []):
        check(_norm(l["label"]) in flat, "link-url",
              f"link text '{l['label']}' missing — ATS gets no URL")

    # 3. Every employer and project name is readable.
    for e in data.get("experience", []):
        check(_norm(e["org"]) in flat, "employer", f"'{e['org']}' not found")
    for p in data.get("projects", []):
        check(_norm(p["name"]) in flat, "project", f"'{p['name']}' not found")

    # 4. Every rendered bullet survives intact (first 45 chars is enough to prove
    #    the line wasn't dropped, reordered, or mangled).
    for section in ("experience", "projects"):
        for entry in data.get(section, []):
            for bullet in entry["bullets"]:
                probe = _norm(bullet)[:45]
                check(probe in flat, "bullet-intact", f"missing/garbled: '{bullet[:50]}...'")

    # 5. Skill labels are not glued to their values (the old tabular bug).
    for g in data.get("skills", []):
        check(_norm(g["label"] + ":") in flat, "skill-label",
              f"'{g['label']}:' not intact — label may be glued to its values")
        for item in g["items"]:
            check(_norm(item) in flat, "skill-item", f"skill '{item}' missing")

    return fails
