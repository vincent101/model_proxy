"""Codex rollout 会话名派生（core/session_identity.py 的 codex 侧）。

Codex 不写 CC 的 pid 注册表，会话元数据只在
`<root>/<Y>/<M>/<D>/rollout-*.jsonl` 首行（session_meta.payload 的
session_id / cwd）。status 与 $route 回执按它派生 `<目录名>-<uuid8 后两位>`。

运行：cd tools/model_proxy && python3 -m unittest tests.test_session_identity_codex -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.session_identity import (  # noqa: E402
    format_session_identity,
    load_codex_session_names,
    load_merged_session_names,
)

SID = "01a1a4ab-6ed5-7a42-9512-3d90353b9b4a"
SID_OTHER = "01a2171f-12ba-7582-bd98-007e0d7e148e"
SID_PREFIX_TWIN = "01a1a4ab-0000-0000-0000-000000000001"  # 与 SID 共享 uuid8


def _write_rollout(root: Path, sid: str, cwd: str, *, day="26", mtime=None,
                   first_line=None) -> Path:
    d = root / "2026" / "11" / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-11-{day}T17-00-00-{sid}.jsonl"
    line = first_line if first_line is not None else json.dumps(
        {"timestamp": "2026-11-26T09:00:00.000Z", "ordinal": 0, "type": "session_meta",
         "payload": {"session_id": sid, "cwd": cwd}})
    p.write_text(line + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class CodexSessionNameTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def test_derives_name_from_cwd_and_uuid_tail(self):
        _write_rollout(self.root, SID, "/Users/x/Documents/NoteVault")
        names = load_codex_session_names(self.root)
        # UUIDv7 前两位恒为 01，故后缀取 uuid8 的后两位
        self.assertEqual(names, {SID: "notevault-ab"})

    def test_distinct_sessions_get_distinct_names(self):
        _write_rollout(self.root, SID, "/Users/x/NoteVault")
        _write_rollout(self.root, SID_OTHER, "/Users/x/NoteVault")
        names = load_codex_session_names(self.root)
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names.values())), 2)

    def test_windows_cwd_uses_last_segment(self):
        _write_rollout(self.root, SID, "C:\\Users\\x\\Documents\\NoteVault")
        self.assertEqual(load_codex_session_names(self.root)[SID], "notevault-ab")

    def test_missing_directory_returns_empty(self):
        self.assertEqual(load_codex_session_names(self.root / "nope"), {})

    def test_broken_first_line_is_skipped(self):
        _write_rollout(self.root, SID, "/x/NoteVault", first_line="{not json")
        self.assertEqual(load_codex_session_names(self.root), {})

    def test_missing_fields_are_skipped(self):
        _write_rollout(self.root, SID, "/x/NoteVault", first_line=json.dumps(
            {"type": "session_meta", "payload": {"cwd": "/x/NoteVault"}}))
        _write_rollout(self.root, SID_OTHER, "", first_line=json.dumps(
            {"type": "session_meta", "payload": {"session_id": SID_OTHER, "cwd": ""}}))
        self.assertEqual(load_codex_session_names(self.root), {})

    def test_root_cwd_without_segment_is_skipped(self):
        _write_rollout(self.root, SID, "/")
        self.assertEqual(load_codex_session_names(self.root), {})

    def test_newest_rollout_wins_for_same_session(self):
        now = time.time()
        _write_rollout(self.root, SID, "/x/Old", day="25", mtime=now - 100)
        _write_rollout(self.root, SID, "/x/New", day="26", mtime=now)
        self.assertEqual(load_codex_session_names(self.root)[SID], "new-ab")

    def test_max_scan_limits_to_newest_files(self):
        now = time.time()
        _write_rollout(self.root, SID, "/x/Newest", day="26", mtime=now)
        _write_rollout(self.root, SID_OTHER, "/x/Older", day="25", mtime=now - 100)
        names = load_codex_session_names(self.root, max_scan=1)
        self.assertEqual(list(names), [SID])

    def test_oversized_first_line_is_skipped(self):
        big = json.dumps({"payload": {"session_id": SID, "cwd": "/x/NoteVault",
                                      "pad": "x" * (300 * 1024)}})
        _write_rollout(self.root, SID, "/x/NoteVault", first_line=big)
        self.assertEqual(load_codex_session_names(self.root), {})


class MergedSessionNameTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.cc = self.root / "cc"
        self.cc.mkdir()
        self.codex = self.root / "codex"

    def _write_cc(self, sid: str, name: str):
        (self.cc / f"{abs(hash(name)) % 999}.json").write_text(json.dumps(
            {"pid": 1, "sessionId": sid, "name": name}), encoding="utf-8")

    def test_cc_registry_wins_over_rollout(self):
        self._write_cc(SID, "notevault-authoritative")
        _write_rollout(self.codex, SID, "/x/NoteVault")
        names = load_merged_session_names(self.cc, self.codex)
        self.assertEqual(names[SID], "notevault-authoritative")

    def test_codex_rollout_fills_missing_uuid(self):
        self._write_cc(SID, "notevault-e2")
        _write_rollout(self.codex, SID_OTHER, "/x/NoteVault")
        names = load_merged_session_names(self.cc, self.codex)
        self.assertEqual(names[SID], "notevault-e2")
        self.assertEqual(names[SID_OTHER], "notevault-1f")

    def test_format_identity_uses_codex_name(self):
        _write_rollout(self.codex, SID, "/x/NoteVault")
        self.assertEqual(format_session_identity(SID, self.cc, self.codex),
                         "notevault-ab · 01a1a4ab")

    def test_ambiguous_uuid8_prefix_falls_back_to_uuid8_only(self):
        _write_rollout(self.codex, SID, "/x/NoteVault")
        _write_rollout(self.codex, SID_PREFIX_TWIN, "/x/NoteVault")
        self.assertEqual(format_session_identity(SID[:8], self.cc, self.codex),
                         SID[:8])


if __name__ == "__main__":
    unittest.main()
