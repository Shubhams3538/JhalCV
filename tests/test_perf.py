"""Poll-load regressions: pruning stays off the hot polling path."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from resume.jobs.service import JobService
from resume.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class PruneThrottleTests(unittest.TestCase):
    def test_repeated_state_reads_prune_at_most_once_per_window(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'profile.example.json').write_bytes((ROOT / 'profile.example.json').read_bytes())
            service = JobService(root)
            profile = Store(root).profile()
            with patch.object(service.store, 'prune_old') as prune, \
                    patch('resume.jobs.service.Agents', return_value=Mock(order=[])):
                service.state(profile)
                service.state(profile)
                self.assertEqual(prune.call_count, 1)
                service._last_prune = None
                service.state(profile)
                self.assertEqual(prune.call_count, 2)


if __name__ == '__main__':
    unittest.main()
