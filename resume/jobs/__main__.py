"""Refresh jobs from the terminal or a scheduler: python3 -m resume.jobs refresh"""
import argparse
import sys
from pathlib import Path

from ..storage import Store
from .service import JobService

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(prog='python3 -m resume.jobs')
    parser.add_argument('command', choices=['refresh', 'recheck'])
    args = parser.parse_args()
    try:
        summary = JobService(ROOT).refresh(Store(ROOT).profile(), recheck_only=args.command == 'recheck')
    except Exception as e:
        sys.exit(f'Job refresh failed: {e}')
    print(summary)


if __name__ == '__main__':
    main()
