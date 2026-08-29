#!/usr/bin/env python3
"""Interactive resume generator.

    python3 main.py                 prompt for a job description
    python3 main.py path/to/jd.txt  skip the prompts, use a file

Everything lands in build/. For flags and the other subcommands, use the module
interface directly: python3 -m resume --help
"""

from __future__ import annotations

import argparse
import os
import re
import select
import subprocess
import sys
from datetime import date
from pathlib import Path

from resume import cli
from resume.llm import have_key

ROOT = Path(__file__).resolve().parent
RULE = "─" * 52
BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "resume"


def clipboard() -> str:
    """Whatever is on the system clipboard, or empty string."""
    for cmd in (["pbpaste"], ["xclip", "-selection", "clipboard", "-o"], ["wl-paste"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return ""


def read_block(idle: float = 0.6) -> str:
    """Read pasted text, ending on Enter rather than requiring Ctrl-D.

    A pasted job description arrives as one burst, so its internal blank lines
    always have more data immediately behind them. The final Enter does not.
    So: read raw from the terminal, and stop once the buffer ends in a newline
    and nothing further arrives within `idle` seconds. Ctrl-D still works.
    """
    buf = ""
    while True:
        # Block indefinitely until the first byte, then only wait `idle`.
        ready, _, _ = select.select([sys.stdin], [], [], None if not buf else idle)
        if not ready:
            break
        try:
            chunk = os.read(sys.stdin.fileno(), 65536).decode("utf-8", "replace")
        except OSError:
            break
        if not chunk:          # EOF (Ctrl-D)
            break
        buf += chunk
    return buf


def read_jd() -> tuple[str, str]:
    """Return (label, jd_text).

    The label is asked for first on purpose: reading the job description can end
    at EOF, which closes stdin, so nothing can be prompted afterwards.
    """
    print(f"{BOLD}Job description -> resume{RESET}")
    print(f"{DIM}{RULE}{RESET}")

    clip = clipboard()
    use_clip = False
    if len(clip.strip()) >= 200:
        preview = " ".join(clip.split())[:64]
        print(f"{DIM}clipboard:{RESET} {preview}...")
        try:
            ans = input(f"Use the clipboard? {DIM}[Y/n]{RESET} ").strip().lower()
            use_clip = ans in ("", "y", "yes")
        except EOFError:
            use_clip = True

    try:
        label = input(f"{BOLD}Company or role{RESET} {DIM}(for the filename){RESET}: ").strip()
    except EOFError:
        label = ""

    if use_clip:
        print(f"{DIM}using {len(clip.split())} words from the clipboard{RESET}")
        return label, clip

    print(f"\n{BOLD}Paste the job description{RESET}, then press {BOLD}Enter{RESET}:")
    print(f"{DIM}{RULE}{RESET}")
    try:
        if sys.stdin.isatty():
            return label, read_block()
        return label, sys.stdin.read()   # piped input
    except KeyboardInterrupt:
        print(f"\n{DIM}cancelled{RESET}")
        raise SystemExit(130)


def generate(jd_text: str, label: str, *, tailorer: str = "auto") -> int:
    if len(jd_text.strip()) < 40:
        print(f"\n{RED}✗ that job description is too short to tailor against.{RESET}")
        return 1

    # Persist the JD so the run is reproducible and the sidecar can reference it.
    # jds/*.txt is gitignored, so real postings never reach the repo.
    name = f"{date.today():%Y-%m-%d}_{_slug(label)}"
    jd_path = ROOT / "jds" / f"{_slug(label)}.txt"
    jd_path.parent.mkdir(exist_ok=True)
    jd_path.write_text(jd_text)

    print(f"{DIM}{RULE}{RESET}")
    if not have_key():
        print(f"{YELLOW}no OPENAI_API_KEY — using the rule-based tailorer{RESET}")

    args = argparse.Namespace(
        jd=str(jd_path), profile=str(ROOT / "profile.json"), template="classic",
        out=name, max_pages=1, projects=2, job_bullets=5, project_bullets=3,
        tailorer=tailorer, model=None, verbose=False,
    )
    code = cli.cmd_make(args)

    pdf = ROOT / "build" / f"{name}.pdf"
    if pdf.exists() and sys.platform == "darwin":
        subprocess.run(["open", str(pdf)], check=False)
        print(f"{DIM}opened in your PDF viewer{RESET}")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jd_file", nargs="?", help="read the JD from a file instead of prompting")
    ap.add_argument("--label", help="name for the output file")
    ap.add_argument("--tailorer", choices=["auto", "llm", "rules"], default="auto")
    a = ap.parse_args()

    if a.jd_file:
        path = Path(a.jd_file)
        if not path.exists():
            print(f"{RED}✗ no such file: {path}{RESET}")
            return 1
        return generate(path.read_text(), a.label or path.stem, tailorer=a.tailorer)

    label, jd_text = read_jd()
    return generate(jd_text, a.label or label, tailorer=a.tailorer)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{DIM}cancelled{RESET}")
        sys.exit(130)
