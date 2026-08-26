"""Command line entry point: python -m resume make --jd jds/backend.txt"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from .render import fit, RenderError
from .tailor import tailor
from .verify import verify

ROOT = Path(__file__).resolve().parent.parent

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def cmd_make(args) -> int:
    profile = json.loads(Path(args.profile).read_text())
    jd_path = Path(args.jd)
    jd_text = jd_path.read_text()

    print(f"{BOLD}▸ tailoring{RESET} {jd_path.name}")
    data, report = tailor(
        profile, jd_text,
        max_projects=args.projects,
        bullets_per_job=args.job_bullets,
        bullets_per_project=args.project_bullets,
    )

    top = list(report["angle_scores"].items())[:4]
    print(f"  {DIM}angles:{RESET} " + ", ".join(f"{a} {s:.0%}" for a, s in top))
    for d in report["decisions"]:
        if d["section"] == "projects" and "name" in d:
            print(f"  {DIM}project:{RESET} {d['name']} "
                  f"{DIM}(score {d['score']:.2f}, angle: {d['angle']}){RESET}")
        elif d["section"] == "projects" and "not_selected" in d:
            for p in d["not_selected"]:
                print(f"  {DIM}  skipped: {p['name']} (score {p['score']:.2f}){RESET}")

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
        "tailorer": "rule-based v1",
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
    lk.set_defaults(func=cmd_lock)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
