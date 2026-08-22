import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import _trim_log


class TestLogRetention(unittest.TestCase):
    NOW = datetime(2026, 8, 22, 12, 0, 0)

    def _trim(self, content):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "proxy.log"
            path.write_text(content, encoding="utf-8")
            _trim_log(path, now=self.NOW, keep_days=8)
            return path.read_text(encoding="utf-8")

    def test_keeps_inside_window_drops_outside(self):
        old = "2026-08-13 11:59:59,000 INFO req_id=- old\n"
        recent = "2026-08-14 12:00:00,000 INFO req_id=- recent\n"
        self.assertEqual(self._trim(old + recent), recent)

    def test_no_parseable_timestamp_preserves_original(self):
        original = "traceback only\n  File x, line 1\n"
        self.assertEqual(self._trim(original), original)

    def test_continuation_belongs_to_previous_record(self):
        old = "2026-08-13 11:00:00,000 ERROR req_id=x old\nold traceback\n"
        recent = "2026-08-22 11:00:00,000 ERROR req_id=y recent\nrecent traceback\n"
        self.assertEqual(self._trim(old + recent), recent)


if __name__ == "__main__":
    unittest.main()
