"""Typst invocation plus the one-page fitting loop."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# Typography presets, loosest first. The fitter walks down this list before it
# will drop any content — losing a line of leading is always better than losing
# an achievement.
PRESETS = [
    # Comfortable first. The fitter only tightens when the page actually
    # overflows, so a short resume never gets needlessly compressed.
    {},
    {"leading": "0.62em", "bullet_gap": "0.72em", "entry_gap": "10pt", "section_gap": "12pt", "rule_gap": "7pt", "head_gap": "5.5pt"},
    {"leading": "0.58em", "bullet_gap": "0.68em", "entry_gap": "9pt", "section_gap": "10pt",
     "body_size": "9.8pt", "margin_y": "0.45in", "head_gap": "5pt"},
    {"leading": "0.55em", "bullet_gap": "0.64em", "entry_gap": "8pt", "section_gap": "9pt",
     "body_size": "9.6pt", "margin_y": "0.42in", "margin_x": "0.5in", "name_size": "18pt",
     "head_gap": "4.5pt"},
    {"leading": "0.52em", "bullet_gap": "0.6em", "entry_gap": "7pt", "section_gap": "8pt",
     "body_size": "9.4pt", "margin_y": "0.38in", "margin_x": "0.45in", "name_size": "17.5pt",
     "section_size": "10pt", "contact_size": "9.2pt", "rule_gap": "5pt", "head_gap": "4pt"},
]


class RenderError(RuntimeError):
    pass


def _typst_length(v):
    """Typst can't read '9.6pt' from JSON as a length, so emit a float+unit pair
    the template turns back into a length."""
    m = re.fullmatch(r"([\d.]+)(pt|in|mm|em)", str(v))
    if not m:
        return v
    return {"__len": float(m.group(1)), "__unit": m.group(2)}


def compile_pdf(data: dict, out_pdf: Path, *, root: Path, template: Path,
                data_path: Path) -> Path:
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    rel = "/" + str(data_path.relative_to(root))
    cmd = [
        "typst", "compile",
        "--root", str(root),
        "--font-path", str(root / "fonts"),
        "--input", f"data={rel}",
        str(template), str(out_pdf),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RenderError(proc.stderr.strip() or proc.stdout.strip())
    return out_pdf


def page_count(pdf: Path) -> int:
    proc = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RenderError(f"pdfinfo failed: {proc.stderr}")
    m = re.search(r"^Pages:\s+(\d+)", proc.stdout, re.M)
    if not m:
        raise RenderError("could not read page count")
    return int(m.group(1))


def _all_bullet_refs(data: dict):
    """Yield (container, index, score) for every droppable bullet, so the fitter
    can find the globally least-relevant line."""
    for section in ("experience", "projects"):
        for entry in data.get(section, []):
            meta = entry.get("_bullet_meta", [])
            # Frozen bullets carry score None and are never candidates for trimming.
            if any(m.get("score") is None for m in meta):
                continue
            # Never strip an entry down to nothing; two bullets is the floor.
            if len(entry["bullets"]) <= 2:
                continue
            for i, m in enumerate(meta):
                yield entry, i, m.get("score", 0.0)


def fit(data: dict, out_pdf: Path, *, root: Path, template: Path, data_path: Path,
        max_pages: int = 1, verbose: bool = False) -> dict:
    """Compile, and if it overflows, tighten then trim until it fits."""
    log = []

    for i, preset in enumerate(PRESETS):
        data["config"] = {k: _typst_length(v) for k, v in preset.items()}
        compile_pdf(data, out_pdf, root=root, template=template, data_path=data_path)
        pages = page_count(out_pdf)
        log.append({"step": f"preset {i}", "pages": pages, "config": preset})
        if verbose:
            print(f"  preset {i}: {pages} page(s)")
        if pages <= max_pages:
            return {"pages": pages, "log": log, "dropped": []}

    # Still overflowing at the tightest typography: start dropping the
    # lowest-scoring bullets, one at a time.
    dropped = []
    for _ in range(20):
        candidates = list(_all_bullet_refs(data))
        if not candidates:
            break
        entry, idx, score = min(candidates, key=lambda t: t[2])
        text = entry["bullets"].pop(idx)
        meta = entry["_bullet_meta"].pop(idx)
        dropped.append({"id": meta.get("id"), "score": score, "text": text})
        if verbose:
            print(f"  dropping {meta.get('id')} (score {score})")
        compile_pdf(data, out_pdf, root=root, template=template, data_path=data_path)
        pages = page_count(out_pdf)
        log.append({"step": f"drop {meta.get('id')}", "pages": pages})
        if pages <= max_pages:
            return {"pages": pages, "log": log, "dropped": dropped}

    return {"pages": page_count(out_pdf), "log": log, "dropped": dropped,
            "warning": f"could not fit into {max_pages} page(s)"}
