"""compact_config_json 紧凑格式单测。

验证 effort_enum 数组和 tiers_source_capability 下的 tier 对象被压成单行，
其余结构保持 indent=2 多行，且数据无损。

运行：cd tools/model_proxy && python3 -m unittest tests.test_config_compact_format -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _config_ops import compact_config_json


def _make_full_cfg():
    """构造一个含 effort_enum / tiers_source_capability / supplies / routes 的完整 config 样本。"""
    return {
        "supplies": [
            {"id": "s1", "url": "http://x", "appkey": "k1", "target_model": "m1"},
            {"id": "s2", "url": "http://y", "appkey": "k2", "target_model": "m2"},
        ],
        "routes": [
            {"id": "r1", "tiers": {"opus": ["s1"], "sonnet": ["s2"], "haiku": []},
             "failover": "on"},
        ],
        "strategies": [
            {
                "client_token": "tok1",
                "route_id": "r1",
                "tiers_source_capability": {
                    "opus": {"effort_enum": ["low", "medium", "high", "xhigh", "max"]},
                    "sonnet": {"effort_enum": ["low", "medium", "high", "xhigh", "max"]},
                    "haiku": {"effort_enum": ["low", "medium", "high", "max"]},
                },
                "note": "test strategy",
            },
        ],
    }


class TestCompactFormat(unittest.TestCase):

    # 1. 格式断言
    def test_effort_enum_array_single_line(self):
        """effort_enum 数组应被压成单行。"""
        text = compact_config_json(_make_full_cfg())
        # 期望 "effort_enum": ["low","medium",...] 单行出现
        self.assertIn('"effort_enum": ["low","medium","high","xhigh","max"]', text)
        # 不应出现跨行的 effort_enum 数组
        self.assertNotRegex(text, r'"effort_enum":\s*\[\s*\n')

    def test_tier_object_single_line(self):
        """tiers_source_capability 下的 tier 对象应被压成单行。"""
        text = compact_config_json(_make_full_cfg())
        self.assertIn('"opus": {"effort_enum": ["low","medium","high","xhigh","max"]}', text)
        self.assertIn('"sonnet": {"effort_enum": ["low","medium","high","xhigh","max"]}', text)
        self.assertIn('"haiku": {"effort_enum": ["low","medium","high","max"]}', text)

    def test_other_arrays_remain_multiline(self):
        """supplies / routes 等多元素数组仍应多行展开。"""
        text = compact_config_json(_make_full_cfg())
        # supplies 数组应跨行（含换行后的 "id"）
        self.assertRegex(text, r'"supplies":\s*\[\s*\n\s*\{')
        # routes 数组应跨行
        self.assertRegex(text, r'"routes":\s*\[\s*\n\s*\{')

    # 2. 数据无损
    def test_data_roundtrip(self):
        """json.loads(compact_config_json(cfg)) == cfg（深相等）。"""
        cfg = _make_full_cfg()
        text = compact_config_json(cfg)
        self.assertEqual(json.loads(text), cfg)

    # 3. 无 effort_enum 时正常多行
    def test_no_effort_enum_fallback(self):
        """不含 effort_enum 的 config，输出应与 json.dumps(indent=2) 完全一致。"""
        cfg = {"a": 1, "b": [1, 2, 3], "c": {"x": "y"}}
        expected = json.dumps(cfg, indent=2, ensure_ascii=False)
        self.assertEqual(compact_config_json(cfg), expected)

    # 4. note 字段含 "effort_enum" 文字不误伤
    def test_note_containing_effort_enum_text(self):
        """note 值里包含 "effort_enum" 文字不应被正则误伤。"""
        cfg = {
            "strategies": [
                {
                    "client_token": "tok1",
                    "route_id": "r1",
                    "tiers_source_capability": {
                        "opus": {"effort_enum": ["low", "high"]},
                    },
                    "note": "see effort_enum field above",
                },
            ],
        }
        text = compact_config_json(cfg)
        # note 值应原样保留
        self.assertIn('"see effort_enum field above"', text)
        # effort_enum 数组仍被压成单行
        self.assertIn('"effort_enum": ["low","high"]', text)
        # 数据无损
        self.assertEqual(json.loads(text), cfg)

    # 5. 多 tier 名覆盖
    def test_all_tier_names_compacted(self):
        """opus/sonnet/haiku 三个 tier 名都应压成单行。"""
        cfg = {
            "tiers_source_capability": {
                "opus": {"effort_enum": ["low", "high"]},
                "sonnet": {"effort_enum": ["low", "medium", "high"]},
                "haiku": {"effort_enum": ["low"]},
            },
        }
        text = compact_config_json(cfg)
        self.assertIn('"opus": {"effort_enum": ["low","high"]}', text)
        self.assertIn('"sonnet": {"effort_enum": ["low","medium","high"]}', text)
        self.assertIn('"haiku": {"effort_enum": ["low"]}', text)

    # 6. 空数组边界
    def test_empty_effort_enum(self):
        """effort_enum: [] 不崩（json.dumps 对空数组默认就是单行 []）。"""
        cfg = {
            "tiers_source_capability": {
                "opus": {"effort_enum": []},
            },
        }
        text = compact_config_json(cfg)
        # 空数组应保持 []
        self.assertIn('"effort_enum": []', text)
        # 数据无损
        self.assertEqual(json.loads(text), cfg)


if __name__ == "__main__":
    unittest.main()
