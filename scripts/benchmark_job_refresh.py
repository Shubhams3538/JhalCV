"""Offline fetch-concurrency benchmark: no live sites, keys or user database.

Run: python3 scripts/benchmark_job_refresh.py
This measures simulated network waits plus actual SQLite/progress processing;
it does not predict live career-site or subscription-model response times.
"""
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resume.jobs.service import JobService
from resume.jobs import sources


def measure(workers, companies=147, delay=.02):
    with tempfile.TemporaryDirectory() as root:
        service = JobService(root)
        service.import_sources([{'company': f'Company {i}', 'url': f'https://job-boards.greenhouse.io/company{i}'}
                                for i in range(companies)])
        def reader(url):
            time.sleep(delay)
            return []
        with patch('resume.jobs.service.SOURCE_WORKERS', workers), \
                patch.dict(sources.READERS, {'greenhouse': reader}), \
                patch('resume.jobs.agents.shutil.which', return_value=None):
            start = time.monotonic()
            service.refresh({'experience': []}, lambda _: None)
            elapsed = time.monotonic() - start
        assert service.progress['completed'] == companies
        return elapsed


if __name__ == '__main__':
    serial, parallel = measure(1), measure(8)
    print(f'147 simulated company feeds: serial {serial:.3f}s; 8 workers {parallel:.3f}s; {serial / parallel:.2f}x faster.')
    print('Offline fetch benchmark only; live-site and AI latency are not included.')
