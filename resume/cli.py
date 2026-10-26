"""Command line entry point: python -m resume make --jd jds/backend.txt"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from functools import wraps

from .usage import UsageStore, usage_scope
from pathlib import Path

from .render import fit, RenderError
from .tailor import tailor
from .verify import verify

ROOT = Path(__file__).resolve().parent.parent

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def _accounted(function):
    @wraps(function)
    def run(args):
        with usage_scope(UsageStore(ROOT), 'resume CLI'):
            return function(args)
    return run


@_accounted
def cmd_make(args) -> int:
    profile = json.loads(Path(args.profile).read_text())
    jd_path = Path(args.jd)
    jd_text = jd_path.read_text()

    kw = dict(max_projects=args.projects, bullets_per_job=args.job_bullets,
              bullets_per_project=args.project_bullets)

    print(f"{BOLD}▸ tailoring{RESET} {jd_path.name}")
    if args.tailorer == "rules":
        data, report = tailor(profile, jd_text, **kw)
        report.setdefault("tailorer", "rules")
    else:
        # `auto` and `llm` share a path: tailor_llm always falls back to the
        # rule-based result internally and never raises. The difference is only
        # whether a silent fallback is acceptable.
        from .llm_tailor import tailor_llm
        data, report = tailor_llm(profile, jd_text, model=args.model, **kw)
        if args.tailorer == "llm" and report["tailorer"] != "llm":
            print(f"  {RED}✗ llm unavailable: {report['llm'].get('reason')}{RESET}")
            return 1

    info = report.get("llm", {})
    if report["tailorer"] == "llm":
        cached = info.get("cached", {})
        marks = "".join(("j" if cached.get("jd") else "J", "s" if cached.get("selection") else "S"))
        print(f"  {GREEN}llm{RESET} {DIM}{info.get('model')} "
              f"({'cached' if marks.islower() else 'called'}){RESET}")
        if info.get("summary"):
            print(f"  {DIM}reads as:{RESET} {info['summary']}")
    elif info.get("reason"):
        print(f"  {YELLOW}fell back to rules:{RESET} {DIM}{info['reason']}{RESET}")

    top = list(report["angle_scores"].items())[:4]
    print(f"  {DIM}angles:{RESET} " + ", ".join(f"{a} {s:.0%}" for a, s in top if s))
    for d in report["decisions"]:
        if d.get("section") != "projects":
            continue
        if d.get("by") == "llm":
            print(f"  {DIM}project:{RESET} {d['name']} {DIM}({d['angle']}) — {d.get('why','')}{RESET}")
        elif "name" in d:
            print(f"  {DIM}project:{RESET} {d['name']} "
                  f"{DIM}(score {d['score']:.2f}, angle: {d['angle']}){RESET}")
        elif "not_selected" in d:
            for pr in d["not_selected"]:
                print(f"  {DIM}  skipped: {pr['name']} (score {pr['score']:.2f}){RESET}")
    for fix in info.get("repairs", []):
        print(f"  {YELLOW}repaired:{RESET} {DIM}{fix}{RESET}")

    name = args.out or f"{date.today():%Y-%m-%d}_{_slug(jd_path.stem)}"
    out_pdf = ROOT / "build" / f"{name}.pdf"
    out_pdf.parent.mkdir(exist_ok=True)

    print(f"{BOLD}▸ fitting{RESET} to {args.max_pages} page(s)")
    try:
        result = fit(
            data, out_pdf,
            root=ROOT,
            template=ROOT / "templates" / f"{args.template}.typ",
            data_path=ROOT / "build" / f"{name}.data.json",
            max_pages=args.max_pages,
            verbose=args.verbose,
        )
    except RenderError as e:
        print(f"{RED}✗ typst failed{RESET}\n{e}", file=sys.stderr)
        return 1

    if result.get("warning"):
        print(f"  {YELLOW}⚠ {result['warning']}{RESET}")
    for d in result["dropped"]:
        print(f"  {YELLOW}dropped for space:{RESET} {d['text'][:60]}...")
    print(f"  {DIM}{result['pages']} page(s){RESET}")

    print(f"{BOLD}▸ verifying{RESET} ATS text layer")
    fails = verify(out_pdf, data, max_pages=args.max_pages)
    if fails:
        for f in fails:
            print(f"  {RED}✗ [{f['check']}]{RESET} {f['detail']}")
    else:
        print(f"  {GREEN}✓ all checks passed{RESET}")

    # Gap report: what the JD wants that the profile can't back up.
    if report["gap_skills"]:
        print(f"{BOLD}▸ gaps{RESET} {DIM}(in JD, not in your profile — not on the PDF){RESET}")
        print(f"  {YELLOW}{', '.join(report['gap_skills'])}{RESET}")

    # Sidecar: what was claimed, to whom, from which JD.
    sidecar = ROOT / "build" / f"{name}.report.json"
    sidecar.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "jd_file": str(jd_path),
        "jd_text": jd_text,
        "tailorer": report.get("tailorer", "rules"),
        "report": report,
        "fit": result,
        "verify_failures": fails,
    }, indent=2))

    print(f"\n{GREEN}→{RESET} {out_pdf}")
    print(f"{DIM}  {sidecar.name} · {name}.data.json{RESET}")
    return 1 if fails else 0


def cmd_check(args) -> int:
    """Run the ATS checks against any PDF, including your old one."""
    pdf = Path(args.pdf)
    data_file = Path(args.data) if args.data else None
    if data_file and data_file.exists():
        data = json.loads(data_file.read_text())
        fails = verify(pdf, data, max_pages=args.max_pages)
    else:
        from .verify import structural_checks
        fails = structural_checks(pdf, max_pages=args.max_pages)
    print(f"{BOLD}▸ checking{RESET} {pdf.name}")
    for f in fails:
        print(f"{RED}✗ [{f['check']}]{RESET} {f['detail']}")
    if not fails:
        print(f"{GREEN}✓ all checks passed{RESET}")
    return 1 if fails else 0



@_accounted
def cmd_lock(args) -> int:
    """Prove the frozen sections are identical across every JD in jds/."""
    import hashlib
    profile = json.loads(Path(args.profile).read_text())
    frozen = profile.get("_locked", {}).get("frozen_sections", [])
    jds = sorted(Path(args.jds).glob("*.txt"))
    if len(jds) < 2:
        print(f"{RED}need at least 2 JDs in {args.jds}{RESET}"); return 1

    def digest(d, k):
        return hashlib.sha256(json.dumps(d[k], sort_keys=True).encode()).hexdigest()[:12]

    results = {}
    for jd in jds:
        if args.tailorer == "llm":
            from .llm_tailor import tailor_llm
            data, _ = tailor_llm(profile, jd.read_text())
        else:
            data, _ = tailor(profile, jd.read_text())
        for k in data:
            if k in ("section_order", "config"):
                continue
            results.setdefault(k, {})[jd.stem] = digest(data, k)

    print(f"{BOLD}▸ lock check{RESET} {DIM}across {len(jds)} job descriptions{RESET}")
    failed = False
    for k, per_jd in results.items():
        uniq = set(per_jd.values())
        if k in frozen:
            ok = len(uniq) == 1
            failed |= not ok
            mark = f"{GREEN}✓ locked{RESET}" if ok else f"{RED}✗ LOCK BROKEN — varies by JD{RESET}"
        else:
            mark = f"{DIM}varies ({len(uniq)} variant(s)){RESET}"
        print(f"  {k:<18}{mark}")
    if failed:
        print(f"\n{RED}A frozen section changed with the job description.{RESET}")
    return 1 if failed else 0



@_accounted
def cmd_compare(args) -> int:
    """Run both JD parsers on the same job description, side by side."""
    from .tailor import parse_jd as parse_rules
    from .llm_tailor import parse_jd_llm, ANGLES
    from . import llm as llmmod

    profile = json.loads(Path(args.profile).read_text())
    jd_text = Path(args.jd).read_text()

    rules = parse_rules(jd_text, profile)
    if not llmmod.have_key():
        print(f"{RED}no OPENAI_API_KEY — nothing to compare against{RESET}")
        return 1
    try:
        from .llm_tailor import JD_MODEL
        chosen = args.model or JD_MODEL
        model, hit = parse_jd_llm(jd_text, profile, model=chosen)
    except llmmod.LLMError as e:
        print(f"{RED}✗ llm failed: {e}{RESET}")
        return 1

    print(f"{BOLD}▸ {Path(args.jd).name}{RESET}  {DIM}{chosen}"
          f"{' · cached' if hit else ''}{RESET}")
    if model.get("summary"):
        print(f"  {DIM}llm reads it as:{RESET} {model['summary']}")
        print(f"  {DIM}seniority:{RESET} {model.get('seniority', '?')}")

    print(f"\n  {'angle':<12}{'rules':>8}{'llm':>8}   {DIM}(shown where either is non-zero){RESET}")
    for a in ANGLES:
        r = rules["angle_scores"].get(a, 0.0)
        m = model["angle_scores"].get(a, 0.0)
        if r == 0 and m == 0:
            continue
        delta = abs(r - m)
        mark = f"{YELLOW}  ←{RESET}" if delta >= 0.15 else ""
        print(f"  {a:<12}{r:>7.0%}{m:>8.0%}{mark}")

    for label, key in (("matched skills", "matched_skills"), ("gaps", "gap_skills")):
        r, m = set(rules[key]), set(model[key])
        print(f"\n  {BOLD}{label}{RESET}")
        print(f"    {DIM}both       :{RESET} {', '.join(sorted(r & m)) or '-'}")
        print(f"    {DIM}rules only :{RESET} {', '.join(sorted(r - m)) or '-'}")
        print(f"    {GREEN}llm only   :{RESET} {', '.join(sorted(m - r)) or '-'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="resume", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("make", help="generate a tailored resume from a JD")
    m.add_argument("--jd", required=True, help="path to the job description text file")
    m.add_argument("--profile", default=str(ROOT / "profile.json"))
    m.add_argument("--template", default="classic")
    m.add_argument("--out", help="output basename (default: date_jdname)")
    m.add_argument("--max-pages", type=int, default=1)
    m.add_argument("--projects", type=int, default=2)
    m.add_argument("--job-bullets", type=int, default=5)
    m.add_argument("--project-bullets", type=int, default=3)
    m.add_argument("--tailorer", choices=["auto", "llm", "rules"], default="auto",
                   help="auto: use the LLM when a key is present, else rules")
    m.add_argument("--model", default=None, help="override the LLM model")
    m.add_argument("-v", "--verbose", action="store_true")
    m.set_defaults(func=cmd_make)

    c = sub.add_parser("check", help="run ATS checks on a PDF (or dump its text)")
    c.add_argument("pdf")
    c.add_argument("--data", help="the .data.json that produced it "
                   "(omit to run structural checks only)")
    c.add_argument("--max-pages", type=int, default=1)
    c.set_defaults(func=cmd_check)

    lk = sub.add_parser("lock", help="verify frozen sections never vary by JD")
    lk.add_argument("--profile", default=str(ROOT / "profile.json"))
    lk.add_argument("--jds", default=str(ROOT / "jds"))
    lk.add_argument("--tailorer", choices=["rules", "llm"], default="rules")
    lk.set_defaults(func=cmd_lock)

    cp = sub.add_parser("compare", help="diff the rule-based and LLM JD parsers")
    cp.add_argument("--jd", required=True)
    cp.add_argument("--profile", default=str(ROOT / "profile.json"))
    cp.add_argument("--model", default=None, help="override the default JD model")
    cp.set_defaults(func=cmd_compare)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
