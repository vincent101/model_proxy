"""core/session_identity.py 单测：session 注册表只读解析。

覆盖：命中（完整 UUID / uuid8 前缀）、未命中回退、损坏 JSON、空/缺失目录、
字段缺失、同 UUID 多进程取最新 procStart（含 startedAt 回退）。

运行：cd tools/model_proxy && python3 -m unittest tests.test_session_identity -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.session_identity import (  # noqa: E402
    DEFAULT_SESSIONS_DIR,
    format_session_identity,
    load_session_names,
    match_session_name,
    session_display_id,
)

SID = "b6ceb46d-1a05-4604-b6a5-ce6fb1a99e8a"
SID8 = "b6ceb46d"
SID2 = "b6ceb46d-1a05-4604-b6a5-ce6fb1a99e8b"  # 与 SID 共享 uuid8 前缀


def _entry(sid, name, proc_start="Mon Aug 24 03:42:10 2026", started_at=None):
    e = {"pid": 1, "sessionId": sid, "name": name, "procStart": proc_start}
    if started_at is not None:
        e["startedAt"] = started_at
    return e


def _write_registry(d, entries):
    """按序写 <1000+i>.json（文件名序即写入序）。"""
    for i, e in enumerate(entries):
        (Path(d) / f"{1000 + i}.json").write_text(
            json.dumps(e), encoding="utf-8")


class TestLoadSessionNames(unittest.TestCase):

    def test_hit_full_uuid(self):
        with tempfile.TemporaryDirectory() as d:
            _write_registry(d, [_entry(SID, "notevault-44")])
            names = load_session_names(d)
        self.assertEqual(names, {SID: "notevault-44"})

    def test_missing_dir_returns_empty(self):
        self.assertEqual(load_session_names("/nonexistent/sessions-dir"), {})

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_session_names(d), {})

    def test_corrupt_json_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "1.json").write_text("{not json", encoding="utf-8")
            (Path(d) / "2.json").write_text("[]", encoding="utf-8")  # 非 dict
            _write_registry(d, [_entry(SID, "notevault-44")])
            names = load_session_names(d)
        self.assertEqual(names, {SID: "notevault-44"})

    def test_missing_fields_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            _write_registry(d, [
                {"pid": 1, "name": "no-sid"},                       # 缺 sessionId
                {"pid": 1, "sessionId": SID},                       # 缺 name
                {"pid": 1, "sessionId": "", "name": "empty-sid"},   # 空 sessionId
                {"pid": 1, "sessionId": SID, "name": ""},           # 空 name
                _entry(SID, "notevault-44"),                        # 合法
            ])
            names = load_session_names(d)
        self.assertEqual(names, {SID: "notevault-44"})

    def test_same_uuid_multiple_procs_latest_procstart_wins(self):
        """同 UUID 多文件（多进程快照）取 procStart 最新者。"""
        with tempfile.TemporaryDirectory() as d:
            _write_registry(d, [
                _entry(SID, "old-name", proc_start="Mon Aug 24 03:42:10 2026"),
                _entry(SID, "new-name", proc_start="Mon Aug 24 05:00:00 2026"),
            ])
            names = load_session_names(d)
        self.assertEqual(names, {SID: "new-name"})

    def test_same_uuid_latest_wins_regardless_of_filename_order(self):
        """文件名序靠前的文件携带更新的 procStart，同样胜出。"""
        with tempfile.TemporaryDirectory() as d:
            _write_registry(d, [
                _entry(SID, "new-name", proc_start="Mon Aug 24 05:00:00 2026"),
                _entry(SID, "old-name", proc_start="Mon Aug 24 03:42:10 2026"),
            ])
            names = load_session_names(d)
        self.assertEqual(names, {SID: "new-name"})

    def test_same_uuid_procstart_fallback_started_at(self):
        """procStart 缺失/不可解析时回退 startedAt（epoch ms）比较。"""
        with tempfile.TemporaryDirectory() as d:
            _write_registry(d, [
                _entry(SID, "old-name", proc_start="garbage",
                       started_at=1787500000000),
                _entry(SID, "new-name", started_at=1787600000000),
            ])
            names = load_session_names(d)
        self.assertEqual(names, {SID: "new-name"})

    def test_default_dir_points_to_home_sessions(self):
        self.assertEqual(DEFAULT_SESSIONS_DIR, Path.home() / ".claude" / "sessions")


class TestMatchSessionName(unittest.TestCase):

    def test_full_uuid_hit(self):
        names = {SID: "notevault-44"}
        self.assertEqual(match_session_name(names, SID), "notevault-44")

    def test_uuid8_prefix_hit(self):
        names = {SID: "notevault-44"}
        self.assertEqual(match_session_name(names, SID8), "notevault-44")

    def test_prefix_ambiguous_returns_none(self):
        """uuid8 前缀命中多个不同 UUID（歧义）→ None（宁缺勿错）。"""
        names = {SID: "a", SID2: "b"}
        self.assertIsNone(match_session_name(names, SID8))
        self.assertEqual(match_session_name(names, SID), "a")
        self.assertEqual(match_session_name(names, SID2), "b")

    def test_miss_returns_none(self):
        self.assertIsNone(match_session_name({SID: "a"}, "ffffffff-0000-0000-0000-000000000000"))

    def test_bad_inputs_return_none(self):
        names = {SID: "a"}
        self.assertIsNone(match_session_name(names, None))
        self.assertIsNone(match_session_name(names, ""))
        self.assertIsNone(match_session_name(names, 123))
        # 非 8 字符的短前缀不做前缀匹配
        self.assertIsNone(match_session_name(names, "b6ceb4"))


class TestDisplayAndFormat(unittest.TestCase):

    def test_session_display_id_hit(self):
        self.assertEqual(session_display_id({SID: "notevault-44"}, SID),
                         "notevault-44 · b6ceb46d")

    def test_session_display_id_miss(self):
        self.assertEqual(session_display_id({}, SID), SID8)
        self.assertEqual(session_display_id(None, SID), SID8)

    def test_session_display_id_none_bucket_passthrough(self):
        self.assertEqual(session_display_id({SID: "x"}, "(none)"), "(none)")

    def test_format_session_identity_hit(self):
        with tempfile.TemporaryDirectory() as d:
            _write_registry(d, [_entry(SID, "notevault-44")])
            self.assertEqual(format_session_identity(SID, d),
                             "notevault-44 · b6ceb46d")

    def test_format_session_identity_miss_shows_uuid8_only(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(format_session_identity(SID, d), SID8)

    def test_format_session_identity_empty_input_returns_none(self):
        self.assertIsNone(format_session_identity(None))
        self.assertIsNone(format_session_identity(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
