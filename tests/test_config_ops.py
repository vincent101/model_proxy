"""_config_ops.py effort 探测 helper 单测（_fix_mojibake / _is_response_complete /
正则抽取），脱网络纯标准库 unittest。

fixture 来源：本次会话中真实拿到的样本（claude类 expected one of 措辞、glm-52
截断类不闭合JSON、haiku/glm-51/deepseek类完整200/400 JSON）。

运行：cd tools/model_proxy && python3 -m unittest tests.test_config_ops -v
"""

import os
import socket
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _config_ops import (
    ReachabilityCategory,
    _extract_enum_candidates,
    _fix_mojibake,
    _is_response_complete,
    _validate_strategy_route_fields,
    classify_supply_reachability,
    connectivity_test_then_probe,
    probe_effort,
    run_connectivity_test,
    run_probe_and_maybe_accept,
    supply_check,
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
        self.assertTrue(_is_response_complete(text))

    def test_truncated_json_incomplete(self):
        # glm-52 截断类：故意不闭合的 JSON 片段
        text = ('{"type":"error","error":{"type":"invalid_request_error",'
                '"message":"[1210][reasoning_effort 参数值非法，可选值为：'
                'none、minimal、low、medium、high')
        self.assertFalse(_is_response_complete(text))

    def test_complete_200_json(self):
        text = '{"id":"msg_1","type":"message","content":[{"type":"text","text":"ok"}]}'
        self.assertTrue(_is_response_complete(text))


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
    """probe_effort 对未知协议直接短路返回，不发网络请求，验证五元组返回结构。"""

    def test_unknown_protocol_returns_five_tuple(self):
        # 非法 protocol 现在由 resolve_protocol 抛 ValueError，probe_effort 按契约
        # 把 exc 原样返回（不再是 None），文案来自 resolve_protocol。
        result = probe_effort({"protocol": "bogus", "url": "http://x", "target_model": "m"})
        self.assertEqual(len(result), 5)
        status, text, cands, is_complete, exc = result
        self.assertIsNone(status)
        self.assertIn("非法 protocol", text)
        self.assertIsNone(cands)
        self.assertFalse(is_complete)
        self.assertIsInstance(exc, ValueError)

    def test_network_exception_returns_exc_object(self):
        # mock urlopen 抛出 DNS 解析失败异常，验证 probe_effort 保留原始异常对象
        # （不仅是 str(e)），供 classify_supply_reachability 按类型判断。
        supply = {"protocol": "anthropic", "url": "http://x", "target_model": "m", "appkey": "k"}
        gaierror = socket.gaierror("Name or service not known")
        with patch("_config_ops.urllib.request.urlopen", side_effect=gaierror):
            status, text, cands, is_complete, exc = probe_effort(supply)
        self.assertIsNone(status)
        self.assertIsNone(cands)
        self.assertFalse(is_complete)
        self.assertIs(exc, gaierror)

    def test_network_exception_real_urlerror_wrapping_gaierror(self):
        # urllib.request.urlopen 真实抛出的网络层异常几乎总是 urllib.error.URLError，
        # 底层原因包在 .reason 里，不是裸的 socket.gaierror 本身。用真实形态验证
        # probe_effort 原样保留这个 URLError（不拆包、不吞掉 .reason）。
        supply = {"protocol": "anthropic", "url": "http://x", "target_model": "m", "appkey": "k"}
        real_exc = urllib.error.URLError(socket.gaierror("nodename nor servname provided, or not known"))
        with patch("_config_ops.urllib.request.urlopen", side_effect=real_exc):
            status, text, cands, is_complete, exc = probe_effort(supply)
        self.assertIsNone(status)
        self.assertIs(exc, real_exc)
        self.assertIsInstance(exc.reason, socket.gaierror)


class TestClassifySupplyReachability(unittest.TestCase):
    """classify_supply_reachability 纯函数单测，不发请求。"""

    def test_dns_error(self):
        # 裸 gaierror 形态（非 urlopen 真实抛出路径，但兼容其他调用方式）
        exc = socket.gaierror("Name or service not known")
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.DNS_ERROR)

    def test_dns_error_real_urlerror_wrapping_gaierror(self):
        # urllib.request.urlopen 真实抛出的形态：URLError.reason 是 gaierror，
        # 不是裸 gaierror 本身。这是 reviewer 发现的分类死代码 bug 的回归测试。
        exc = urllib.error.URLError(socket.gaierror("nodename nor servname provided, or not known"))
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.DNS_ERROR)

    def test_timeout_socket_timeout(self):
        exc = socket.timeout("timed out")
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.TIMEOUT)

    def test_timeout_built_in_timeout_error(self):
        exc = TimeoutError("timed out")
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.TIMEOUT)

    def test_timeout_urlerror_reason_timed_out(self):
        exc = urllib.error.URLError("timed out")
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.TIMEOUT)

    def test_timeout_real_urlerror_wrapping_socket_timeout(self):
        # 真实形态：URLError.reason 是 socket.timeout 实例。
        exc = urllib.error.URLError(socket.timeout("timed out"))
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.TIMEOUT)

    def test_conn_refused_error_type(self):
        exc = ConnectionRefusedError("Connection refused")
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.CONN_REFUSED)

    def test_conn_refused_urlerror_reason(self):
        exc = urllib.error.URLError("Connection refused")
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.CONN_REFUSED)

    def test_conn_refused_real_urlerror_wrapping_connection_refused(self):
        # 真实形态：URLError.reason 是 ConnectionRefusedError 实例。
        exc = urllib.error.URLError(ConnectionRefusedError("Connection refused"))
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.CONN_REFUSED)

    def test_network_other_unrecognized_exception(self):
        exc = OSError("some other network failure")
        category, desc = classify_supply_reachability(None, str(exc), exc)
        self.assertEqual(category, ReachabilityCategory.NETWORK_OTHER)

    def test_auth_error_401(self):
        category, desc = classify_supply_reachability(401, "unauthorized", None)
        self.assertEqual(category, ReachabilityCategory.AUTH_ERROR)

    def test_auth_error_403(self):
        category, desc = classify_supply_reachability(403, "forbidden", None)
        self.assertEqual(category, ReachabilityCategory.AUTH_ERROR)

    def test_model_error_404(self):
        category, desc = classify_supply_reachability(404, "not found", None)
        self.assertEqual(category, ReachabilityCategory.MODEL_ERROR)

    def test_model_error_400_with_model_keyword(self):
        category, desc = classify_supply_reachability(
            400, '{"error":{"message":"model not found: xyz"}}', None)
        self.assertEqual(category, ReachabilityCategory.MODEL_ERROR)

    def test_model_error_400_with_chinese_keyword(self):
        category, desc = classify_supply_reachability(
            400, '{"error":{"message":"该模型不存在"}}', None)
        self.assertEqual(category, ReachabilityCategory.MODEL_ERROR)

    def test_reachable_400_without_model_keyword(self):
        # 典型：effort 参数被拒，说明连通鉴权都OK
        category, desc = classify_supply_reachability(
            400, '{"error":{"message":"expected one of `low`, `high`"}}', None)
        self.assertEqual(category, ReachabilityCategory.REACHABLE)

    def test_reachable_200(self):
        category, desc = classify_supply_reachability(200, '{"ok":true}', None)
        self.assertEqual(category, ReachabilityCategory.REACHABLE)

    def test_unknown_429(self):
        category, desc = classify_supply_reachability(429, "too many requests", None)
        self.assertEqual(category, ReachabilityCategory.UNKNOWN)

    def test_unknown_500(self):
        category, desc = classify_supply_reachability(500, "internal error", None)
        self.assertEqual(category, ReachabilityCategory.UNKNOWN)


class TestRunProbeAndMaybeAcceptFlow(unittest.TestCase):
    """模拟 a 成功 / a 失败→人工输入 两分支，验证不崩溃且交互确认后产出正确 dict。"""

    def _supply(self):
        return {"id": "test-supply", "protocol": "anthropic",
                "url": "http://x", "target_model": "m", "appkey": "k"}

    def test_a_branch_success_writes_regex_candidates_on_confirm(self):
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"expected one of `low`, `high`"}}',
                                 ["low", "high"], True, None)), \
             patch("builtins.input", return_value=""), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": ["low", "high"]})

    def test_a_branch_user_declines_returns_none(self):
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"expected one of `low`, `high`"}}',
                                 ["low", "high"], True, None)), \
             patch("builtins.input", return_value=""), \
             patch("_config_ops.confirm", return_value=False):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertIsNone(result)

    def test_truncated_then_doc_none_falls_to_manual_edit_no_crash(self):
        # is_complete=False（截断）→ 截断不采纳→直接人工输入 → candidates=None
        # → 仍进入人工输入环节（不能因为探测不出就直接放弃，人工可能依据外部信息判断）；
        # 本用例模拟用户留空 = 跳过，不写入。
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"可选值为：none、low',
                                 ["none", "low"], False, None)), \
             patch("builtins.input", return_value=""):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertIsNone(result)

    def test_no_regex_hit_falls_to_manual_edit(self):
        # 正则未命中 → candidates=None 仍进入人工输入环节；本用例模拟用户留空 = 跳过，不写入。
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"Extra inputs are not permitted"}}',
                                 None, True, None)), \
             patch("builtins.input", return_value=""):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertIsNone(result)

    def test_no_candidates_user_can_manually_input_empty_list(self):
        # candidates=None（探测无结论），但人工依据外部信息（如已知官方文档结论）
        # 判断该 supply 确认不支持任何档位，输入 "-" 应能写入空列表——这是本次修复的
        # 核心场景：探测拿不出候选时，人工仍必须有输入空集的入口。
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"Extra inputs are not permitted"}}',
                                 None, True, None)), \
             patch("builtins.input", return_value="-"), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": []})

    def test_no_candidates_user_can_manually_input_explicit_list(self):
        # candidates=None 时人工也可以直接敲入一组已知档位（比如凭官方文档结论），
        # 不局限于"-"表示空集这一种输入。
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"Extra inputs are not permitted"}}',
                                 None, True, None)), \
             patch("builtins.input", return_value="high,max"), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": ["high", "max"]})

    def test_user_can_edit_candidates_before_writing(self):
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"expected one of `low`, `Unhandled`"}}',
                                 ["low", "Unhandled"], True, None)), \
             patch("builtins.input", return_value="low,high"), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": ["low", "high"]})

    def test_user_can_edit_to_empty_list_via_dash(self):
        with patch("_config_ops.probe_effort",
                   return_value=(400, '{"error":{"message":"expected one of `low`"}}',
                                 ["low"], True, None)), \
             patch("builtins.input", return_value="-"), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": []})

    def test_prefetched_result_skips_probe_effort_call(self):
        # prefetched 非 None 时跳过 probe_effort 调用——这是 add/edit 流程避免
        # 二次真实请求的核心机制，必须验证 probe_effort 完全不被调用。
        prefetched = (400, '{"error":{"message":"expected one of `low`, `high`"}}',
                      ["low", "high"], True, None)
        with patch("_config_ops.probe_effort") as mock_probe, \
             patch("builtins.input", return_value=""), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply(), prefetched=prefetched)
        mock_probe.assert_not_called()
        self.assertEqual(result, {"effort_enum": ["low", "high"]})

    def test_non_reachable_category_prints_attribution_but_still_allows_manual_input(self):
        # 分类为 AUTH_ERROR（非 REACHABLE）时，仍应打印明确归因，且不因为分类是
        # 错误就直接 return——继续进入人工输入环节（保持现有设计原则不变）。
        with patch("_config_ops.probe_effort",
                   return_value=(401, "unauthorized", None, True, None)), \
             patch("builtins.input", return_value="-"), \
             patch("_config_ops.confirm", return_value=True):
            result = run_probe_and_maybe_accept(self._supply())
        self.assertEqual(result, {"effort_enum": []})


class TestConnectivityTestRequestReuse(unittest.TestCase):
    """验证 run_connectivity_test 与 run_probe_and_maybe_accept(prefetched=...) 串联时，
    probe_effort（也即 urllib.request.urlopen）只被调用一次，不会对同一 supply 发第二次
    真实上游请求——这是本次改动的核心正确性要求。
    """

    def _supply(self):
        return {"id": "test-supply", "protocol": "anthropic",
                "url": "http://x", "target_model": "m", "appkey": "k"}

    def test_reachable_flow_calls_probe_effort_exactly_once(self):
        reachable_result = (400, '{"error":{"message":"expected one of `low`, `high`"}}',
                             ["low", "high"], True, None)
        with patch("_config_ops.probe_effort", return_value=reachable_result) as mock_probe, \
             patch("builtins.input", return_value=""), \
             patch("_config_ops.confirm", return_value=True):
            result = run_connectivity_test(self._supply())
            status, text_fixed, _, _, exc = result
            category, _ = classify_supply_reachability(status, text_fixed, exc)
            self.assertEqual(category, ReachabilityCategory.REACHABLE)
            rcap = run_probe_and_maybe_accept(self._supply(), prefetched=result)
        mock_probe.assert_called_once()
        self.assertEqual(rcap, {"effort_enum": ["low", "high"]})

    def test_non_reachable_flow_does_not_trigger_second_probe_call(self):
        # 非 REACHABLE 分类时调用方（supply_add/edit）设计上不再调用
        # run_probe_and_maybe_accept，这里验证 run_connectivity_test 本身
        # 也只发一次请求。
        auth_error_result = (401, "unauthorized", None, True, None)
        with patch("_config_ops.probe_effort", return_value=auth_error_result) as mock_probe:
            result = run_connectivity_test(self._supply())
        mock_probe.assert_called_once()
        status, text_fixed, _, _, exc = result
        category, _ = classify_supply_reachability(status, text_fixed, exc)
        self.assertEqual(category, ReachabilityCategory.AUTH_ERROR)


class TestConnectivityTestThenProbe(unittest.TestCase):
    """connectivity_test_then_probe：整合 supply-test/supply-probe 的共用辅助函数单测。"""

    def _supply(self):
        return {"id": "test-supply", "protocol": "anthropic",
                "url": "http://x", "target_model": "m", "appkey": "k"}

    def test_connectivity_test_then_probe_reachable_reuses_response(self):
        reachable_result = (400, '{"error":{"message":"expected one of `low`, `high`"}}',
                             ["low", "high"], True, None)
        with patch("_config_ops.probe_effort", return_value=reachable_result) as mock_probe, \
             patch("builtins.input", return_value=""), \
             patch("_config_ops.confirm", return_value=True):
            category, desc, rcap = connectivity_test_then_probe(self._supply())
        mock_probe.assert_called_once()
        self.assertEqual(category, ReachabilityCategory.REACHABLE)
        self.assertEqual(rcap, {"effort_enum": ["low", "high"]})

    def test_connectivity_test_then_probe_non_reachable_returns_none_rcap(self):
        auth_error_result = (401, "unauthorized", None, True, None)
        with patch("_config_ops.probe_effort", return_value=auth_error_result) as mock_probe:
            category, desc, rcap = connectivity_test_then_probe(self._supply())
        mock_probe.assert_called_once()
        self.assertEqual(category, ReachabilityCategory.AUTH_ERROR)
        self.assertIsNone(rcap)


class TestSupplyCheck(unittest.TestCase):
    """supply_check：整合原 supply_test/supply_probe 为单一入口后的顶层子命令单测。
    覆盖核心正确性要求：全流程 probe_effort 只被调用一次。
    """

    def _cfg_with_supply(self):
        return {
            "supplies": [
                {"id": "s1", "protocol": "anthropic", "url": "http://x",
                 "target_model": "m", "appkey": "k"}
            ]
        }

    def test_supply_check_reachable_probes_and_writes_once(self):
        cfg = self._cfg_with_supply()
        reachable_result = (400, '{"error":{"message":"expected one of `low`, `high`"}}',
                             ["low", "high"], True, None)
        with patch("_config_ops.load_config", return_value=cfg), \
             patch("_config_ops.atomic_write") as mock_write, \
             patch("_config_ops.probe_effort", return_value=reachable_result) as mock_probe, \
             patch("builtins.input", return_value=""), \
             patch("_config_ops.confirm", return_value=True):
            supply_check("dummy_path", "s1")
        mock_probe.assert_called_once()
        mock_write.assert_called_once()
        self.assertEqual(cfg["supplies"][0]["reasoning_capability"],
                          {"effort_enum": ["low", "high"]})

    def test_supply_check_non_reachable_ends_without_probe_or_write(self):
        cfg = self._cfg_with_supply()
        auth_error_result = (401, "unauthorized", None, True, None)
        with patch("_config_ops.load_config", return_value=cfg), \
             patch("_config_ops.atomic_write") as mock_write, \
             patch("_config_ops.probe_effort", return_value=auth_error_result) as mock_probe, \
             patch("_config_ops.run_probe_and_maybe_accept") as mock_run_probe:
            supply_check("dummy_path", "s1")
        mock_probe.assert_called_once()
        mock_run_probe.assert_not_called()
        mock_write.assert_not_called()
        self.assertNotIn("reasoning_capability", cfg["supplies"][0])


class TestValidateStrategyRouteFields(unittest.TestCase):
    """_validate_strategy_route_fields：strategy 写盘前 route_id 与 route_pool 互斥校验。"""

    def test_both_fields_present_raises_and_reports_error(self):
        entry = {"client_token": "tok1", "route_id": "claude",
                  "route_pool": ["claude", "nation"]}
        with patch("_config_ops.err") as mock_err, \
             patch("_config_ops.done") as mock_done:
            with self.assertRaises(SystemExit) as ctx:
                _validate_strategy_route_fields(entry)
        self.assertEqual(ctx.exception.code, 1)
        mock_err.assert_called_once()
        self.assertIn("route_id", mock_err.call_args[0][0])
        self.assertIn("route_pool", mock_err.call_args[0][0])
        mock_done.assert_called_once_with(False)

    def test_only_route_id_does_not_raise(self):
        entry = {"client_token": "tok2", "route_id": "claude"}
        with patch("_config_ops.err") as mock_err:
            result = _validate_strategy_route_fields(entry)
        self.assertIsNone(result)
        mock_err.assert_not_called()

    def test_only_route_pool_does_not_raise(self):
        entry = {"client_token": "tok3", "route_pool": ["claude", "nation"]}
        with patch("_config_ops.err") as mock_err:
            result = _validate_strategy_route_fields(entry)
        self.assertIsNone(result)
        mock_err.assert_not_called()

    def test_neither_field_present_does_not_raise(self):
        # 脏配置（既无 route_id 也无 route_pool）不属于本函数职责，交由其他校验处理。
        entry = {"client_token": "tok4"}
        with patch("_config_ops.err") as mock_err:
            result = _validate_strategy_route_fields(entry)
        self.assertIsNone(result)
        mock_err.assert_not_called()

    def test_route_id_with_empty_route_pool_list_does_not_raise(self):
        # 当前实现：route_pool 为空列表 [] 时视为 falsy，等同于"未配置 route_pool"，
        # 不会与 route_id 冲突判定为互斥违规。此为当前实现的既有行为，用测试明确固化，
        # 而非遗漏；若未来需要区分"字段存在但为空"与"字段不存在"，需先修改函数逻辑。
        entry = {"client_token": "tok5", "route_id": "claude", "route_pool": []}
        with patch("_config_ops.err") as mock_err:
            result = _validate_strategy_route_fields(entry)
        self.assertIsNone(result)
        mock_err.assert_not_called()


if __name__ == "__main__":
    unittest.main()
