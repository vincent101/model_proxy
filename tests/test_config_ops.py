"""_config_ops.py effort 探测 helper 单测（_fix_mojibake / _is_response_complete /
正则抽取），脱网络纯标准库 unittest。

fixture 来源：本次会话中真实拿到的样本（claude类 expected one of 措辞、glm-52
截断类不闭合JSON、haiku/glm-51/deepseek类完整200/400 JSON）。

运行：cd tools/model_proxy && python3 -m unittest tests.test_config_ops -v
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _config_ops import (
    _extract_enum_candidates,
    _fix_mojibake,
    _is_response_complete,
    _llm_probe_official_doc,
    probe_effort,
    run_probe_and_maybe_accept,
)


class TestFixMojibake(unittest.TestCase):

    def test_plain_ascii_unaffected(self):
        raw = b'{"error":{"message":"reasoning_effort must be one of low, medium, high"}}'
        self.assertEqual(_fix_mojibake(raw), raw.decode("utf-8"))

    def test_plain_utf8_chinese_unaffected(self):
        raw = "参数有误，请检查请求体".encode("utf-8")
        self.assertEqual(_fix_mojibake(raw), "参数有误，请检查请求体")

    def test_double_encoded_mojibake_repaired(self):
        # 模拟：正确的 UTF-8 中文字节被网关误当 latin-1 重新编码成 UTF-8
        # （即先把 utf-8 bytes 解成 latin-1 字符串，再以 utf-8 编回 bytes，模拟双重编码现场）。
        original = "可选值为：无、低、中、高"
        mojibake_bytes = original.encode("utf-8").decode("latin-1").encode("utf-8")
        fixed = _fix_mojibake(mojibake_bytes)
        self.assertEqual(fixed, original)


class TestIsResponseComplete(unittest.TestCase):

    def test_complete_json_object(self):
        text = '{"error":{"message":"output_config.effort: Extra inputs are not permitted"}}'
        self.assertTrue(_is_response_complete(text.encode("utf-8"), text))

    def test_truncated_json_incomplete(self):
        # glm-52 截断类：故意不闭合的 JSON 片段
        text = ('{"type":"error","error":{"type":"invalid_request_error",'
                '"message":"[1210][reasoning_effort 参数值非法，可选值为：'
                'none、minimal、low、medium、high')
        self.assertFalse(_is_response_complete(text.encode("utf-8"), text))

    def test_complete_200_json(self):
        text = '{"id":"msg_1","type":"message","content":[{"type":"text","text":"ok"}]}'
        self.assertTrue(_is_response_complete(text.encode("utf-8"), text))


class TestExtractEnumCandidates(unittest.TestCase):

    def test_claude_expected_one_of_pattern_with_backticks(self):
        text = ('{"error":{"message":"expected one of `low`, `medium`, `high`, `xhigh`, '
                '`max`, `Unhandled` at line 1 column 165"}}')
        cands = _extract_enum_candidates(text)
        self.assertIsNotNone(cands)
        self.assertIn("Unhandled", cands)  # 不清洗白名单，噪音原样保留
        self.assertEqual(cands, ["low", "medium", "high", "xhigh", "max", "Unhandled"])

    def test_supported_values_pattern_with_quotes(self):
        text = 'Supported values are: "none", "low", "medium", "high".'
        cands = _extract_enum_candidates(text)
        self.assertEqual(cands, ["none", "low", "medium", "high"])

    def test_chinese_keyword_pattern_bare_words(self):
        text = "参数错误，可选值为：none、minimal、low、medium、high"
        cands = _extract_enum_candidates(text)
        self.assertEqual(cands, ["none", "minimal", "low", "medium", "high"])

    def test_no_match_returns_none(self):
        text = '{"error":{"message":"output_config.effort: Extra inputs are not permitted"}}'
        self.assertIsNone(_extract_enum_candidates(text))

    def test_glm52_truncated_text_still_extracts_bare_words(self):
        # 截断样本：正则依然能命中"可选值为"措辞，抽出裸词（is_complete 单独判定为 False）
        text = ('[1210][reasoning_effort 参数值非法，可选值为：'
                'none、minimal、low、medium、high')
        cands = _extract_enum_candidates(text)
        self.assertEqual(cands, ["none", "minimal", "low", "medium", "high"])


class TestProbeEffortReturnShape(unittest.TestCase):
    """probe_effort 对未知协议直接短路返回，不发网络请求，验证四元组返回结构。"""

    def test_unknown_protocol_returns_four_tuple(self):
        result = probe_effort({"protocol": "bogus", "url": "http://x", "target_model": "m"})
        self.assertEqual(len(result), 4)
        status, text, cands, is_complete = result
        self.assertIsNone(status)
        self.assertIn("未知 protocol", text)
        self.assertIsNone(cands)
        self.assertFalse(is_complete)


class TestLlmProbeOfficialDocDegradesGracefully(unittest.TestCase):

    def test_returns_none_no_crash(self):
        # 当前代码库无可复用联网上游调用机制，方案要求合理降级返回 None，不崩溃。
        result = _llm_probe_official_doc("claude-sonnet-sankuai-0956", "claude-sonnet-5", "anthropic")
        self.assertIsNone(result)


class TestRunProbeAndMaybeAcceptFlow(unittest.TestCase):
    """模拟 a/b/c 三步线性逻辑，验证不崩溃且交互确认后产出正确 dict。"""

    def _supply(self):
        return {"id": "test-supply", "protocol": "anthropic",
                "url": "http://x", "target_model": "m", "appkey": "k"}

    def test_a_branch_success_writes_regex_candidates_on_confirm(self):
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"expected one of `low`, `high`"}}',
                                 ["low", "high"], True)), \
             patch("builtins.input", return_value=""), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": ["low", "high"]})

    def test_a_branch_user_declines_returns_none(self):
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"expected one of `low`, `high`"}}',
                                 ["low", "high"], True)), \
             patch("builtins.input", return_value=""), \
             patch("_config_ops.confirm", return_value=False):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertIsNone(result)

    def test_truncated_then_doc_none_falls_to_manual_edit_no_crash(self):
        # is_complete=False（截断）→ 转查文档 → doc_result=None（当前必然降级）→ candidates=None
        # → 仍进入人工输入环节（不能因为探测不出就直接放弃，人工可能依据外部信息判断）；
        # 本用例模拟用户留空 = 跳过，不写入。
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"可选值为：none、low', ["none", "low"], False)), \
             patch("builtins.input", return_value=""):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertIsNone(result)

    def test_b_branch_complete_json_no_regex_hit_falls_to_doc_none(self):
        # candidates=None 仍进入人工输入环节；本用例模拟用户留空 = 跳过，不写入。
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"Extra inputs are not permitted"}}',
                                 None, True)), \
             patch("builtins.input", return_value=""):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertIsNone(result)

    def test_no_candidates_user_can_manually_input_empty_list(self):
        # candidates=None（a/b 均无结论），但人工依据外部信息（如已知官方文档结论）
        # 判断该 supply 确认不支持任何档位，输入 "-" 应能写入空列表——这是本次修复的
        # 核心场景：探测/文档查询都拿不出候选时，人工仍必须有输入空集的入口。
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"Extra inputs are not permitted"}}',
                                 None, True)), \
             patch("builtins.input", return_value="-"), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": []})

    def test_no_candidates_user_can_manually_input_explicit_list(self):
        # candidates=None 时人工也可以直接敲入一组已知档位（比如凭官方文档结论），
        # 不局限于"-"表示空集这一种输入。
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"Extra inputs are not permitted"}}',
                                 None, True)), \
             patch("builtins.input", return_value="high,max"), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": ["high", "max"]})

    def test_user_can_edit_candidates_before_writing(self):
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"expected one of `low`, `Unhandled`"}}',
                                 ["low", "Unhandled"], True)), \
             patch("builtins.input", return_value="low,high"), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": ["low", "high"]})

    def test_user_can_edit_to_empty_list_via_dash(self):
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"expected one of `low`"}}',
                                 ["low"], True)), \
             patch("builtins.input", return_value="-"), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": []})


if __name__ == "__main__":
    unittest.main()
