"""compact_config_json 紧凑格式单测。

验证根字段和数组元素的稳定格式，以及任意 JSON 配置值的保真。

运行：cd tools/model_proxy && python3 -m unittest tests.test_config_compact_format -v
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _config_ops import compact_config_json


def _make_full_cfg():
    """构造一个含 effort_enum / tiers_source_capability / supplies / routes 的完整 config 样本。

    supplies 里的对象含完整字段（id/url/protocol/appkey/target_model/reasoning_capability），
    routes.tiers 含多元素和单元素数组，也含 cooldown_rules 与 budget_retry。
    """
    return {
        "cooldown_rules": [
            {"errorcode": [429, 500, 502, 503, 504], "cooldown_seconds": 60},
            {"errorcode": [401, 402, 403], "cooldown_seconds": 21600},
        ],
        "budget_retry": {"enabled": True, "max_retries": 5},
        "supplies": [
            {"id": "s1", "url": "http://x", "protocol": "anthropic", "appkey": "k1",
             "target_model": "m1",
             "reasoning_capability": {"effort_enum": ["low", "medium", "high"]}},
            {"id": "s2", "url": "http://y", "protocol": "chat", "appkey": "k2",
             "target_model": "m2",
             "reasoning_capability": {"effort_enum": ["low", "high"]}},
        ],
        "routes": [
            {"id": "r1", "tiers": {"opus": ["s1", "s2"], "sonnet": ["s2"], "haiku": []},
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


def _make_full_cfg_with_route_pool():
    """构造一个含 route_pool（多 route）的完整 config 样本，用于 route_pool 紧凑格式测试。

    在 _make_full_cfg 基础上加一个 route r2，strategy tok1 改用 route_pool（含 r1 + r2 两条）。
    strategy 包含 client_token、route_pool、tiers_source_capability 和 note。
    """
    cfg = _make_full_cfg()
    cfg["routes"].append(
        {"id": "r2", "tiers": {"opus": ["s2"], "sonnet": ["s1"], "haiku": ["s1"]},
         "failover": "off"}
    )
    old = cfg["strategies"][0]
    cfg["strategies"][0] = {
        "client_token": old["client_token"],
        "route_pool": [
            {"route_id": "r1", "weight": 1},
            {"route_id": "r2", "weight": 2},
        ],
        "tiers_source_capability": old["tiers_source_capability"],
        "note": old["note"],
    }
    return cfg


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

    def test_supply_object_single_line(self):
        """supplies 数组里每条 supply 对象应被压成单行（含嵌套 reasoning_capability）。"""
        text = compact_config_json(_make_full_cfg())
        # supply 对象应单行出现（含完整字段 + 嵌套 reasoning_capability）
        self.assertIn(
            '{"id": "s1","url": "http://x","protocol": "anthropic","appkey": "k1",'
            '"target_model": "m1","reasoning_capability": {"effort_enum": ["low","medium","high"]}}',
            text)
        self.assertIn(
            '{"id": "s2","url": "http://y","protocol": "chat","appkey": "k2",'
            '"target_model": "m2","reasoning_capability": {"effort_enum": ["low","high"]}}',
            text)

    def test_routes_tiers_multi_element_single_line(self):
        """routes.tiers.<tier> 多元素数组应压成单行。"""
        text = compact_config_json(_make_full_cfg())
        # opus 有 2 个元素，应单行
        self.assertIn('"opus": ["s1","s2"]', text)

    def test_routes_tiers_single_element_single_line(self):
        """routes.tiers.<tier> 单元素数组也应是单行。"""
        text = compact_config_json(_make_full_cfg())
        self.assertIn('"sonnet": ["s2"]', text)

    def test_routes_tiers_empty_array(self):
        """routes.tiers 空数组不崩（json.dumps 默认单行 []）。"""
        text = compact_config_json(_make_full_cfg())
        self.assertIn('"haiku": []', text)

    def test_routes_array_remains_multiline(self):
        """routes 数组本身仍应多行展开（数组框架多行，每条 route 对象单行）。"""
        text = compact_config_json(_make_full_cfg())
        # routes 数组应跨行（含换行后的 "id"）
        self.assertRegex(text, r'"routes":\s*\[\s*\n\s*\{')

    def test_route_object_single_line(self):
        """routes 数组里每条 route 对象应被压成单行（含嵌套 tiers）。"""
        text = compact_config_json(_make_full_cfg_with_route_pool())
        self.assertIn(
            '{"id": "r1","tiers": {"opus": ["s1","s2"],"sonnet": ["s2"],"haiku": []},'
            '"failover": "on"}',
            text)
        self.assertIn(
            '{"id": "r2","tiers": {"opus": ["s2"],"sonnet": ["s1"],"haiku": ["s1"]},'
            '"failover": "off"}',
            text)

    def test_strategy_object_single_line_route_pool_form(self):
        """route_pool 形式的 strategy 对象应被压成单行。"""
        text = compact_config_json(_make_full_cfg_with_route_pool())
        self.assertIn(
            '{"client_token": "tok1","route_pool": [{"route_id": "r1","weight": 1},'
            '{"route_id": "r2","weight": 2}],"tiers_source_capability": '
            '{"opus": {"effort_enum": ["low","medium","high","xhigh","max"]},'
            '"sonnet": {"effort_enum": ["low","medium","high","xhigh","max"]},',
            text)

    def test_strategy_object_single_line_route_id_form(self):
        """route_id 单值形式（旧写法）的 strategy 对象也应被压成单行。"""
        text = compact_config_json(_make_full_cfg())
        self.assertIn(
            '{"client_token": "tok1","route_id": "r1","tiers_source_capability": '
            '{"opus": {"effort_enum": ["low","medium","high","xhigh","max"]},'
            '"sonnet": {"effort_enum": ["low","medium","high","xhigh","max"]},'
            '"haiku": {"effort_enum": ["low","medium","high","max"]}},"note": "test strategy"}',
            text)

    def test_cooldown_rule_object_single_line(self):
        """cooldown_rules 数组里每条规则对象应被压成单行（含嵌套 errorcode 数字数组）。"""
        text = compact_config_json(_make_full_cfg())
        self.assertIn(
            '{"errorcode": [429,500,502,503,504],"cooldown_seconds": 60}',
            text)
        self.assertIn(
            '{"errorcode": [401,402,403],"cooldown_seconds": 21600}',
            text)

    def test_budget_retry_object_single_line(self):
        """budget_retry 顶层对象应被压成单行（保留键前缀）。"""
        text = compact_config_json(_make_full_cfg())
        self.assertIn(
            '"budget_retry": {"enabled": true,"max_retries": 5}',
            text)

    # 2. 数据无损
    def test_data_roundtrip(self):
        """json.loads(compact_config_json(cfg)) == cfg（深相等，含 supplies + routes + strategies 全结构）。"""
        cfg = _make_full_cfg()
        text = compact_config_json(cfg)
        self.assertEqual(json.loads(text), cfg)

    # 3. 不依赖 effort_enum 的稳定格式
    def test_no_effort_enum_stable_format(self):
        cfg = {"a": 1, "b": [1, 2, 3], "c": {"x": "y"}}
        expected = '{\n  "a": 1,\n  "b": [\n    1,\n    2,\n    3\n  ],\n  "c": {"x": "y"}\n}'
        self.assertEqual(compact_config_json(cfg), expected)
        self.assertEqual(json.loads(expected), cfg)

    # 4. note 字段含 "effort_enum" 文字不误伤
    def test_note_containing_effort_enum_text(self):
        """note 值里包含 "effort_enum" 文字应原样保留。"""
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

    # 7. 混合场景：supplies、routes.tiers 与 effort_enum 均保持紧凑
    def test_mixed_scenario(self):
        """一个 config 同时含 supplies、routes.tiers（多元素/单元素）、
        effort_enum，全部正确压行。
        """
        cfg = {
            "supplies": [
                {"id": "a", "url": "http://a", "protocol": "anthropic", "appkey": "ka",
                 "target_model": "ma",
                 "reasoning_capability": {"effort_enum": ["low", "high", "max"]}},
                {"id": "b", "url": "http://b", "protocol": "chat", "appkey": "kb",
                 "target_model": "mb",
                 "reasoning_capability": {"effort_enum": ["low"]}},
            ],
            "routes": [
                {"id": "multi", "tiers": {"opus": ["a", "b"], "sonnet": ["b"], "haiku": []},
                 "failover": "on"},
                {"id": "single", "tiers": {"opus": ["a"], "sonnet": ["b"], "haiku": ["a"]},
                 "failover": "off"},
            ],
            "strategies": [
                {
                    "client_token": "tok",
                    "route_id": "multi",
                    "tiers_source_capability": {
                        "opus": {"effort_enum": ["low", "medium", "high"]},
                        "haiku": {"effort_enum": ["low"]},
                    },
                    "note": "mixed test",
                },
            ],
        }
        text = compact_config_json(cfg)
        # supply 对象单行
        self.assertIn(
            '{"id": "a","url": "http://a","protocol": "anthropic","appkey": "ka",'
            '"target_model": "ma","reasoning_capability": {"effort_enum": ["low","high","max"]}}',
            text)
        # routes.tiers 多元素单行
        self.assertIn('"opus": ["a","b"]', text)
        # routes.tiers 单元素单行
        self.assertIn('"opus": ["a"]', text)
        # tier 对象单行
        self.assertIn('"opus": {"effort_enum": ["low","medium","high"]}', text)
        # 数据无损
        self.assertEqual(json.loads(text), cfg)

    # 8. supply 结构扩展后仍保真、单行
    def test_supply_with_extra_field_stays_single_line(self):
        """未知字段不能导致扩展对象展开或丢失。"""
        cfg = {
            "supplies": [
                {"id": "s1", "url": "http://x", "protocol": "anthropic", "appkey": "k1",
                 "target_model": "m1", "priority": 1,
                 "reasoning_capability": {"effort_enum": ["low", "high"]}},
            ],
        }
        text = compact_config_json(cfg)
        self.assertEqual(json.loads(text), cfg)
        self.assertIn(
            '    {"id": "s1","url": "http://x","protocol": "anthropic",'
            '"appkey": "k1","target_model": "m1","priority": 1,'
            '"reasoning_capability": {"effort_enum": ["low","high"]}}', text)

    # 9. route_pool 多行对象压单行（2 个 route 的场景，如 cc 的 [nation1, nation2]）
    def test_route_pool_multi_element_single_line(self):
        """strategies.route_pool 下含多个 {route_id, weight} 对象时，
        每个对象都应被压成单行（不是只压第一个）。
        """
        cfg = {
            "strategies": [
                {
                    "client_token": "cc",
                    "route_pool": [
                        {"route_id": "nation1", "weight": 1},
                        {"route_id": "nation2", "weight": 1},
                    ],
                },
            ],
        }
        text = compact_config_json(cfg)
        # 两个对象都应单行出现
        self.assertIn('{"route_id": "nation1","weight": 1}', text)
        self.assertIn('{"route_id": "nation2","weight": 1}', text)
        # 不应出现跨行的 route_id/weight 对象（多行对象里 route_id 后会换行接 weight）
        self.assertNotRegex(text, r'"route_id":\s*"[^"]+",\s*\n\s*"weight"')
        # 数据无损
        self.assertEqual(json.loads(text), cfg)

    # 10. route_pool 单元素也压单行（不崩）
    def test_route_pool_single_element_single_line(self):
        """route_pool 只有 1 个 {route_id, weight} 对象时，也应压成单行（不崩）。"""
        cfg = {
            "strategies": [
                {
                    "client_token": "codex",
                    "route_pool": [
                        {"route_id": "openai", "weight": 1},
                    ],
                },
            ],
        }
        text = compact_config_json(cfg)
        self.assertIn('{"route_id": "openai","weight": 1}', text)
        # 数据无损
        self.assertEqual(json.loads(text), cfg)

    # 11. 含 route_pool 的完整 config roundtrip：json.loads(compact_config_json(cfg)) == cfg
    def test_full_config_with_route_pool_roundtrip(self):
        """含 route_pool 的完整 config（supplies + routes + strategies 全结构）
        经 compact_config_json 后 json.loads 必须深等于原 config。
        """
        cfg = _make_full_cfg_with_route_pool()
        text = compact_config_json(cfg)
        self.assertEqual(json.loads(text), cfg)

    # 12. route_pool 扩展字段仍保真、紧凑
    def test_route_pool_with_extra_field_stays_single_line(self):
        """route_pool 扩展字段不导致展开或丢失。"""
        cfg = {
            "strategies": [
                {
                    "client_token": "x",
                    "route_pool": [
                        {"route_id": "r1", "weight": 1, "fallback": "r2"},
                    ],
                },
            ],
        }
        text = compact_config_json(cfg)
        self.assertEqual(json.loads(text), cfg)
        self.assertIn('{"route_id": "r1","weight": 1,"fallback": "r2"}', text)
        self.assertIn('    {"client_token": "x","route_pool": [', text)


class TestConfigSerializationRegression(unittest.TestCase):
    def test_double_spaces_in_supply_value_roundtrip(self):
        cfg = {"supplies": [{
            "id": "s1", "url": "http://example.invalid", "protocol": "chat",
            "appkey": "synthetic", "target_model": "model  variant",
            "reasoning_capability": {"effort_enum": ["low", "high"]},
        }]}
        self.assertEqual(json.loads(compact_config_json(cfg)), cfg)

    def test_comma_in_tier_supply_id_roundtrip(self):
        cfg = {"routes": [{
            "id": "r1", "tiers": {"opus": ["s,1", "s2"], "sonnet": [], "haiku": []},
            "failover": "on",
        }]}
        self.assertEqual(json.loads(compact_config_json(cfg)), cfg)


class TestGeneralJsonPreservation(unittest.TestCase):
    def assert_same_tree(self, actual, expected):
        self.assertIs(type(actual), type(expected))
        if isinstance(expected, dict):
            self.assertEqual(list(actual), list(expected))
            for key in expected:
                self.assert_same_tree(actual[key], expected[key])
        elif isinstance(expected, list):
            self.assertEqual(len(actual), len(expected))
            for a, b in zip(actual, expected):
                self.assert_same_tree(a, b)
        else:
            self.assertEqual(actual, expected)

    def test_escaping_unicode_unknown_fields_order_and_stability(self):
        special_key = '键"\\\n\t,  '
        special_value = '模型"\\\n\t,  保留'
        cfg = {
            special_key: special_value,
            "strategies": [{"extra": {special_key: [[], {}, special_value]},
                            "route_id": "r,1", "client_token": "synthetic"}],
            "routes": [{"unknown": [[], {}], "failover": "on", "id": "r,1",
                        "tiers": {"sonnet": "s,1", "opus": ["s,1", "s 2"]}}],
            "supplies": [{"extension": {"nested": [[], {}, [special_value]]},
                          "target_model": "model  variant", "id": "s,1",
                          "reasoning_capability": {"effort_enum": ["low,high", "  x  "]}}],
            "unknown": {"array": [[], {}, ["a,b  中文"]]},
        }
        before = copy.deepcopy(cfg)
        text = compact_config_json(cfg)
        self.assert_same_tree(cfg, before)
        loaded = json.loads(text)
        self.assert_same_tree(loaded, before)
        self.assertEqual(compact_config_json(cfg), text)
        self.assertEqual(compact_config_json(loaded), text)
        self.assertIn(json.dumps(special_key, ensure_ascii=False), text)
        self.assertIn(json.dumps(special_value, ensure_ascii=False), text)
        self.assertNotIn('\\u6a21', text)
        self.assertNotIn('\n\n', text)
        self.assertFalse(text.endswith('\n'))
        self.assertEqual(loaded['routes'][0]['tiers']['sonnet'], 's,1')

    def test_root_arrays_mixed_values_and_nested_compact_format(self):
        cfg = {"empty": [], "mixed": [None, True, 3, 1.5, "汉,  字", [], {},
                                     {"z": [[], {}], "a": {}}],
               "nested": {"items": [[], {}], "empty": {}}, "last": False}
        expected = (
            '{\n  "empty": [],\n  "mixed": [\n'
            '    null,\n    true,\n    3,\n    1.5,\n    "汉,  字",\n'
            '    [],\n    {},\n    {"z": [[],{}],"a": {}}\n  ],\n'
            '  "nested": {"items": [[],{}],"empty": {}},\n  "last": false\n}'
        )
        text = compact_config_json(cfg)
        self.assertEqual(text, expected)
        self.assert_same_tree(json.loads(text), cfg)

    def test_empty_dict_and_non_dict_roots_keep_json_dumps_behavior(self):
        self.assertEqual(compact_config_json({}), '{}')
        for value in ([], [1, {"中": [1, 2]}], "文本  ,", 0, False, None):
            with self.subTest(value=value):
                text = compact_config_json(value)
                self.assertEqual(text, json.dumps(value, indent=2, ensure_ascii=False))
                self.assert_same_tree(json.loads(text), value)
                self.assertFalse(text.endswith('\n'))


if __name__ == "__main__":
    unittest.main()
