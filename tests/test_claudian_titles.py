"""core/claudian_titles.py 单测：claudian 会话元数据头部只读解析。

覆盖：完整 UUID 命中、sessionId null 回退 providerSessionId、无 UUID/title null
跳过、二进制垃圾跳过、目录缺失、同 UUID 双 conv 取 updatedAt 最新、标题含
引号/中文反序列化、大文件头部读取、空输入、真实目录冒烟（格式漂移检出闭环）。

合成 fixture 与 claudian 真实序列化输出无同步机制：⑧ 只验证头读逻辑本身，
不承担格式漂移检出——那是 ⑩ 真实目录冒烟的职责（命中率跌破 95% 阈值先红）。

运行：cd tools/model_proxy && python3 -m unittest tests.test_claudian_titles -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.claudian_titles import (  # noqa: E402
    DEFAULT_CLAUDIAN_SESSIONS_DIR,
    _iter_title_entries,
    load_session_titles,
    lookup_session_title,
)

SID = "b6ceb46d-1a05-4604-b6a5-ce6fb1a99e8a"
PID = "807390ed-af49-4506-8275-50ce3812d071"


def _meta(
    title="quant策略架构Agent",
    session_id=SID,
    provider_session_id=None,
    updated=1776649791897,
    extra_provider_state=None,
):
    """按 toSessionMetadata 键序构造会话元数据 dict。"""
    provider_state = None
    if provider_session_id is not None or extra_provider_state is not None:
        provider_state = {}
        if provider_session_id is not None:
            provider_state["providerSessionId"] = provider_session_id
        if extra_provider_state is not None:
            provider_state.update(extra_provider_state)
    meta = {
        "id": "conv-1776259022755-oiaafac3i",
        "providerId": "claude",
        "title": title,
        "titleGenerationStatus": "success",
        "createdAt": 1776259022756,
        "updatedAt": updated,
        "lastResponseAt": 1776418472712,
        "sessionId": session_id,
        "providerState": provider_state,
        "hasHistory": True,
        "messageCount": 12,
    }
    return meta


def _write_meta(d, name, meta):
    (Path(d) / name).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


class TestLoadSessionTitles(unittest.TestCase):

    def test_hit_full_uuid(self):
        """① 完整 UUID 命中。"""
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-1.meta.json", _meta())
            titles = load_session_titles(d)
        self.assertEqual(titles, {SID: "quant策略架构Agent"})

    def test_session_id_null_falls_back_to_provider_session_id(self):
        """② 顶层 sessionId 为 null → 回退 providerState.providerSessionId。"""
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-1.meta.json",
                        _meta(session_id=None, provider_session_id=PID))
            titles = load_session_titles(d)
        self.assertEqual(titles, {PID: "quant策略架构Agent"})

    def test_no_uuid_at_all_skipped(self):
        """③ 顶层 sessionId null 且无 providerSessionId → 整文件跳过。"""
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-1.meta.json", _meta(session_id=None))
            titles = load_session_titles(d)
        self.assertEqual(titles, {})

    def test_null_title_skipped(self):
        """③ title 为 null → 跳过。"""
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-1.meta.json", _meta(title=None))
            titles = load_session_titles(d)
        self.assertEqual(titles, {})

    def test_binary_garbage_skipped(self):
        """④ 损坏头部（二进制垃圾，utf-8 解码失败）→ 跳过，不影响其余。"""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "conv-bad.meta.json").write_bytes(
                b"\x00\xff\xfe garbage \x80\x81")
            _write_meta(d, "conv-good.meta.json", _meta())
            titles = load_session_titles(d)
        self.assertEqual(titles, {SID: "quant策略架构Agent"})

    def test_missing_dir_returns_empty(self):
        """⑤ 目录缺失 → {}。"""
        self.assertEqual(load_session_titles("/nonexistent/claudian-sessions"), {})

    def test_same_uuid_latest_updated_at_wins(self):
        """⑥ 同 UUID 双 conv（fork/演进）→ updatedAt 新者胜。"""
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-old.meta.json",
                        _meta(title="旧标题", updated=1000))
            _write_meta(d, "conv-new.meta.json",
                        _meta(title="新标题", updated=2000))
            titles = load_session_titles(d)
        self.assertEqual(titles, {SID: "新标题"})

    def test_same_uuid_latest_wins_regardless_of_filename_order(self):
        """⑥ 文件名序靠前者携带更新的 updatedAt，同样胜出。"""
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-a.meta.json",
                        _meta(title="新标题", updated=2000))
            _write_meta(d, "conv-b.meta.json",
                        _meta(title="旧标题", updated=1000))
            titles = load_session_titles(d)
        self.assertEqual(titles, {SID: "新标题"})

    def test_title_with_quotes_and_chinese_unescaped(self):
        """⑦ 标题含引号/反斜杠/中文 → json.loads 反序列化正确。"""
        raw = '他说："\\路径/标题"'
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-1.meta.json", _meta(title=raw))
            titles = load_session_titles(d)
        self.assertEqual(titles[SID], raw)

    def test_large_file_head_read_still_hits(self):
        """⑧ providerState 大块数据后接尾字段，文件远超 8 KiB，头部读取仍命中。

        只验证头读逻辑本身（顶层 sessionId/title 均在头部）；不承担格式漂移检出。
        """
        blob = "x" * 100_000
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-big.meta.json", _meta(
                session_id=SID, provider_session_id=PID,
                extra_provider_state={"subagentData": blob}))
            p = Path(d) / "conv-big.meta.json"
            self.assertGreater(p.stat().st_size, 8192)
            titles = load_session_titles(d)
        self.assertEqual(titles, {SID: "quant策略架构Agent"})


class TestLookupSessionTitle(unittest.TestCase):

    def test_lookup_full_uuid_hit(self):
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-1.meta.json", _meta())
            self.assertEqual(lookup_session_title(SID, d), "quant策略架构Agent")

    def test_lookup_miss_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            _write_meta(d, "conv-1.meta.json", _meta())
            self.assertIsNone(
                lookup_session_title("ffffffff-0000-0000-0000-000000000000", d))

    def test_lookup_empty_input_returns_none(self):
        """⑨ 空/非法输入 → None。"""
        self.assertIsNone(lookup_session_title(None))
        self.assertIsNone(lookup_session_title(""))
        self.assertIsNone(lookup_session_title(123))


class TestRealDirSmoke(unittest.TestCase):

    def test_real_dir_hit_rate(self):
        """⑩ 真实目录冒烟：title 命中率 ≥ 95%（键序契约漂移的唯一检出手段）。

        目录缺失（脱离 vault 部署）→ skipTest。当前实测 174/177 ≈ 98.3%，
        3 个 miss 均为无 UUID 的旧非活跃 conv。
        """
        d = DEFAULT_CLAUDIAN_SESSIONS_DIR
        if not d.is_dir():
            self.skipTest(f"claudian sessions dir missing: {d}")
        files = list(d.glob("*.meta.json"))
        self.assertGreaterEqual(len(files), 1, "real sessions dir has no meta files")
        entries = list(_iter_title_entries(d))
        hit_rate = len(entries) / len(files)
        self.assertGreaterEqual(
            hit_rate, 0.95,
            f"title head-read hit rate {hit_rate:.3f} ({len(entries)}/{len(files)}) "
            "below 95% — claudian meta serialization key order likely drifted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
