"""三阶段路由匹配单测（脱网络，纯标准库 unittest）。

覆盖新架构 route(家族模板) + strategies(token 绑定) 分层：
resolve_strategy（token→strategy）、extract_route_candidates（strategy→route 列表）、
resolve_tier（model 精确查表）、select_supply_list（按 tier 取 supplies）、
select_supply（列表签名 + cooling/tried 跳过）、tier 内 failover、以及端到端串联。

运行：cd tools/model_proxy && python3 -m unittest tests.test_route
"""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# tests/ 与 core/ 同级，sys.path 指向 tools/model_proxy/ 以便 from core.server import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cli import _config_ops, dispatch  # noqa: E402
from core.cli._config_ops import atomic_write, load_config, WritePreflightFailed  # noqa: E402
from core.mac_config_path import MacConfigError  # noqa: E402
from core import mac_config_writer  # noqa: E402

from core.server import (  # noqa: E402
    resolve_strategy,
    extract_route_candidates,
    resolve_tier,
    select_supply_list,
    select_supply,
    pick_translator,
    PASSTHROUGH,
    ANTHROPIC_TO_CHAT,
    RESPONSES_TO_ANTHROPIC,
    ANTHROPIC_TO_RESPONSES,
    UNSUPPORTED,
    _sanitize_forward_query,
    extract_client_token,
    detect_source,
)


class _FakeCooldown:
    """脱离真实 CooldownStore，只按集合判定是否 cooling。"""

    def __init__(self, cooling=None):
        self._cooling = set(cooling or [])

    def is_cooling(self, sid: str) -> bool:
        return sid in self._cooling


def _routes_map():
    return {
        "claude": {"id": "claude", "tiers": {
            "opus": ["claude-opus-k0"],
            "sonnet": ["claude-sonnet-k0"],
            "haiku": ["claude-haiku-k0"],
        }, "failover": "on"},
        "glm": {"id": "glm", "tiers": {
            "opus": ["glm-opus-k1", "glm-opus-k0"],
        }, "failover": "on"},
    }


class TestResolveTier(unittest.TestCase):

    def test_exact_opus(self):
        self.assertEqual(resolve_tier("claude-opus"), "opus")

    def test_exact_sonnet(self):
        self.assertEqual(resolve_tier("claude-sonnet"), "sonnet")

    def test_exact_haiku(self):
        self.assertEqual(resolve_tier("claude-haiku"), "haiku")

    def test_non_preset_miss(self):
        # 精确查表：含关键字但非精确值必须 miss（不是子串猜测）
        self.assertIsNone(resolve_tier("claude-opus-20240229"))
        self.assertIsNone(resolve_tier("claude-3-opus"))
        self.assertIsNone(resolve_tier("gpt-4"))
        self.assertIsNone(resolve_tier(None))
        self.assertIsNone(resolve_tier(""))


class TestSelectSupplyList(unittest.TestCase):

    def test_hit(self):
        route = _routes_map()["claude"]
        self.assertEqual(select_supply_list(route, "opus"), ["claude-opus-k0"])

    def test_missing_tier(self):
        route = _routes_map()["glm"]  # 只有 opus 档
        self.assertIsNone(select_supply_list(route, "sonnet"))


class TestSelectSupply(unittest.TestCase):

    @staticmethod
    def _isolated_config_fixture(directory):
        base = Path(directory).resolve()
        root, home = base / 'project', base / 'home'
        (root / 'config').mkdir(parents=True, mode=0o700)
        (root / 'runtime').mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        path = root / 'config' / 'synthetic.json'
        path.write_text('{}\n', encoding='utf-8')
        path.chmod(0o600)
        keys = ('log', 'totals', 'lock', 'pid', 'ensure_log', 'start_lock')
        runtime_file = root / 'config' / 'runtime_paths.json'
        runtime_file.write_text(
            json.dumps({key: str(root / 'runtime' / key) for key in keys}),
            encoding='utf-8')
        runtime_file.chmod(0o600)
        env = {'HOME': str(home), 'USERPROFILE': str(home),
               'LOCALAPPDATA': str(home / 'Local'), 'MODEL_PROXY_CONFIG': str(path),
               'TMPDIR': str(base), 'TMP': str(base), 'TEMP': str(base)}
        return root, path, env

    @staticmethod
    @contextlib.contextmanager
    def _isolated_write_state(root, env):
        with patch.object(dispatch, 'ROOT', root), patch.dict(os.environ, env, clear=True), \
             patch.multiple(_config_ops, _WRITE_BASELINE=None, _WRITE_STARTED=False,
                            _WRITE_OPERATION='route-edit', _WRITE_AUTHORIZATION=None,
                            _WRITE_PREFLIGHT=None, _WRITE_INPUT_PATH=None, _WRITE_COMMITTED=None,
                            _WRITE_LOCATIONS=None, _WRITE_BACKUP_BASELINE=None,
                            _MARKER_BASELINE=None, _MARKER_CONTENT=None, _MARKER_REQUIRED=False):
            yield

    def test_first_available(self):
        supply_map = {"k0": {"id": "k0"}, "k1": {"id": "k1"}}
        cd = _FakeCooldown()
        supply = select_supply(["k0", "k1"], supply_map, cd, set())
        self.assertEqual(supply["id"], "k0")

    @unittest.skipUnless(sys.platform == 'darwin', 'requires native macOS config writer')
    def test_roundtrip_comma_supply_id_matches_exactly(self):
        cfg = {"supplies": [{"id": "s,1"}, {"id": "s"}, {"id": "1"}],
               "routes": [{"id": "r,1", "tiers": {"opus": ["s,1", "s"]},
                           "failover": "on"}]}
        with tempfile.TemporaryDirectory() as directory:
            root, path, env = self._isolated_config_fixture(directory)
            with self._isolated_write_state(root, env):
                _config_ops._prepare_write(str(path), 'route-edit')
                self.assertEqual(_config_ops._WRITE_INPUT_PATH, str(path))
                self.assertEqual(_config_ops._WRITE_AUTHORIZATION.target_path, str(path))
                self.assertIs(_config_ops._WRITE_AUTHORIZATION.allow_backup_and_staging, True)
                self.assertTrue(_config_ops._WRITE_PREFLIGHT(_config_ops._WRITE_BASELINE))
                atomic_write(str(path), cfg)
                loaded = load_config(str(path))
        self.assertEqual(loaded, cfg)
        ids = select_supply_list(loaded['routes'][0], 'opus')
        self.assertEqual(ids, ['s,1', 's'])
        supply_map = {s['id']: s for s in loaded['supplies']}
        self.assertEqual(select_supply(ids, supply_map, _FakeCooldown(), set())['id'], 's,1')
        self.assertEqual(select_supply(ids, supply_map, _FakeCooldown(), {'s,1'})['id'], 's')

    @unittest.skipUnless(sys.platform == 'darwin', 'requires native macOS config capture')
    def test_prepare_write_missing_config_fails_without_creating(self):
        with tempfile.TemporaryDirectory() as directory:
            root, path, env = self._isolated_config_fixture(directory)
            missing = root / 'config' / 'absent.json'
            before = sorted(p.name for p in path.parent.iterdir())
            with self._isolated_write_state(root, env):
                with self.assertRaises(MacConfigError) as caught:
                    _config_ops._prepare_write(str(missing), 'route-edit')
                self.assertEqual(caught.exception.code, 'file_not_found')
                with self.assertRaisesRegex(WritePreflightFailed, '缺少编辑前快照'):
                    atomic_write(str(missing), {'supplies': []})
            self.assertFalse(missing.exists())
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), before)

    def test_windows_config_write_is_rejected_before_staging(self):
        # Runs on native Windows and simulates the platform guard elsewhere.
        with tempfile.TemporaryDirectory() as directory:
            root, path, env = self._isolated_config_fixture(directory)
            path_arg = str(path)
            before = path.read_bytes()
            base = root.parent
            entries = sorted(str(p.relative_to(base)) for p in base.rglob('*'))
            with self._isolated_write_state(root, env), \
                 patch.object(mac_config_writer, 'replace_config') as write, \
                 patch.object(_config_ops.os, 'name', 'nt'):
                # Windows writes are now implemented; without a captured
                # pre-write authorization they must still fail closed before
                # any staging or replacement happens.
                with self.assertRaises(WritePreflightFailed):
                    atomic_write(path_arg, {'supplies': [{'id': 's,1'}]})
                write.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(str(p.relative_to(base)) for p in base.rglob('*')), entries)

    def test_simulated_windows_helper_rejects_marker_before_writer(self):
        for transport in ('untrusted_path', 'pipe'):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory, \
                 contextlib.ExitStack() as stack:
                root, path, env = self._isolated_config_fixture(directory)
                if transport == 'untrusted_path':
                    marker = root / 'config' / 'untrusted-marker'
                    marker.write_bytes(b'fake-marker')
                    env['CONFIG_OPS_RELOAD_MARKER'] = str(marker)
                else:
                    read_fd, write_fd = os.pipe()
                    stack.callback(os.close, read_fd)
                    stack.callback(os.close, write_fd)
                    os.set_inheritable(write_fd, True)
                    env['CONFIG_OPS_RELOAD_MARKER_FD'] = str(write_fd)
                before = path.read_bytes()
                base = root.parent
                entries = sorted(str(p.relative_to(base)) for p in base.rglob('*'))
                error = io.StringIO()
                argv = ['helper', 'switch', str(path), 'fake-token', 'fake-route']
                with self._isolated_write_state(root, env), \
                     patch.object(_config_ops, '_precheck_inherited_marker',
                                  wraps=_config_ops._precheck_inherited_marker) as precheck, \
                     patch.object(_config_ops, 'atomic_write',
                                  wraps=_config_ops.atomic_write) as atomic, \
                     patch.object(mac_config_writer, '_open_private') as stage, \
                     patch.object(mac_config_writer, 'replace_config') as writer, \
                     patch('core.mac_config_path.capture_config') as capture, \
                     patch.object(_config_ops.os, 'name', 'nt'), \
                     patch.object(_config_ops.sys, 'argv', argv), \
                     contextlib.redirect_stderr(error):
                    with self.assertRaises(SystemExit) as caught:
                        _config_ops.main()
                    self.assertNotEqual(caught.exception.code, 0)
                    precheck.assert_called_once()
                    # Refusal happens before any config work. The untrusted
                    # path is rejected by the path check, so only require that
                    # generic fail-closed message for that transport.
                    message = error.getvalue()
                    self.assertTrue('marker' in message or '路径校验失败' in message, message)
                    self.assertIn('未触发 reload', message)
                    capture.assert_not_called()
                    atomic.assert_not_called()
                    stage.assert_not_called()
                    writer.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(sorted(str(p.relative_to(base)) for p in base.rglob('*')), entries)
                if transport == 'untrusted_path':
                    self.assertEqual(marker.read_bytes(), b'fake-marker')

    @unittest.skipUnless(sys.platform == 'darwin', 'requires native macOS fake config writer')
    def test_helper_and_roundtrip_restore_state_in_both_orders(self):
        from tests.test_cli_write_preflight import WritePreflightTests

        fields = ('_WRITE_BASELINE', '_WRITE_STARTED', '_WRITE_OPERATION',
                  '_WRITE_AUTHORIZATION', '_WRITE_PREFLIGHT', '_WRITE_INPUT_PATH',
                  '_WRITE_COMMITTED', '_WRITE_LOCATIONS', '_WRITE_BACKUP_BASELINE',
                  '_MARKER_BASELINE', '_MARKER_CONTENT', '_MARKER_REQUIRED')
        self.assertEqual({name for name in vars(_config_ops)
                          if name.startswith(('_WRITE_', '_MARKER_'))}, set(fields))
        original = {name: getattr(_config_ops, name) for name in fields}
        sentinels = {name: object() for name in fields}
        sentinels['_WRITE_STARTED'] = sentinels['_MARKER_REQUIRED'] = True
        factories = {
            'route': lambda: TestSelectSupply('test_roundtrip_comma_supply_id_matches_exactly'),
            'helper': lambda: WritePreflightTests('test_windows_direct_helper_fails_before_config_staging'),
        }
        with patch.multiple(_config_ops, **sentinels):
            for order in (('route', 'helper'), ('helper', 'route')):
                for name in order:
                    with self.subTest(order=order, test=name):
                        result = unittest.TestResult()
                        factories[name]().run(result)
                        self.assertEqual(result.testsRun, 1)
                        self.assertFalse(result.skipped)
                        self.assertTrue(result.wasSuccessful(), (result.failures, result.errors))
                        for field in fields:
                            self.assertIs(getattr(_config_ops, field), sentinels[field], field)
        for field in fields:
            self.assertIs(getattr(_config_ops, field), original[field], field)

    def test_skip_tried(self):
        supply_map = {"k0": {"id": "k0"}, "k1": {"id": "k1"}}
        cd = _FakeCooldown()
        supply = select_supply(["k0", "k1"], supply_map, cd, {"k0"})
        self.assertEqual(supply["id"], "k1")

    def test_skip_missing_in_map(self):
        supply_map = {"k1": {"id": "k1"}}  # k0 缺失
        cd = _FakeCooldown()
        supply = select_supply(["k0", "k1"], supply_map, cd, set())
        self.assertEqual(supply["id"], "k1")

    def test_all_unavailable(self):
        supply_map = {"k0": {"id": "k0"}}
        cd = _FakeCooldown(cooling={"k0"})
        self.assertIsNone(select_supply(["k0"], supply_map, cd, set()))

    def test_select_supply_honors_request_wide_stream_exclusion(self):
        supply_map = {"k0": {"id": "k0"}, "k1": {"id": "k1"}}
        cd = _FakeCooldown()
        supply = select_supply(["k0", "k1"], supply_map, cd, {"k0"})
        self.assertEqual(supply["id"], "k1")

    def test_failover_skip_cooling(self):
        # tier 内 failover：k1 在 cooling → 返回 k0
        supply_map = {"k0": {"id": "k0"}, "k1": {"id": "k1"}}
        cd = _FakeCooldown(cooling={"k1"})
        supply = select_supply(["k1", "k0"], supply_map, cd, set())
        self.assertEqual(supply["id"], "k0")


class TestPickTranslator(unittest.TestCase):

    def test_combinations(self):
        self.assertEqual(pick_translator("anthropic", "anthropic"), PASSTHROUGH)
        self.assertEqual(pick_translator("responses", "responses"), PASSTHROUGH)
        self.assertEqual(pick_translator("anthropic", "chat"), ANTHROPIC_TO_CHAT)
        self.assertEqual(pick_translator("responses", "anthropic"), RESPONSES_TO_ANTHROPIC)
        self.assertEqual(pick_translator("anthropic", "responses"), ANTHROPIC_TO_RESPONSES)

    def test_anthropic_to_responses(self):
        self.assertEqual(pick_translator("anthropic", "responses"), ANTHROPIC_TO_RESPONSES)
        self.assertEqual(ANTHROPIC_TO_RESPONSES, "anthropic_to_responses")

    def test_unsupported(self):
        self.assertEqual(pick_translator("chat", "anthropic"), UNSUPPORTED)
        self.assertEqual(pick_translator("responses", "chat"), UNSUPPORTED)


class TestSanitizeForwardQuery(unittest.TestCase):
    """出站 URL 的 query 净化：统一丢弃客户端 path，只保留剔除 beta 后的 query。

    supply["url"] 现在语义是完整终态端点，四个转发分支的 target_url 均由
    base_url + _sanitize_forward_query(path) 统一计算（原
    _build_passthrough_target_url，见 core/server.py 改名说明）。
    """

    def test_anthropic_client_path_dropped(self):
        base_url = "https://xxx/v1/anthropic/v1/messages"
        result = base_url + _sanitize_forward_query("/v1/messages")
        self.assertEqual(result, base_url)
        self.assertNotIn("/v1/messages/v1/messages", result)

    def test_responses_client_path_dropped_regression(self):
        base_url = "https://xxx/v1/responses"
        result = base_url + _sanitize_forward_query("/v1/responses")
        self.assertEqual(result, base_url)

    def test_query_kept_beta_stripped(self):
        base_url = "https://xxx/v1/anthropic/v1/messages"
        result = base_url + _sanitize_forward_query("/v1/messages?beta=xxx&foo=1")
        self.assertEqual(result, base_url + "?foo=1")

    def test_root_path_no_error(self):
        base_url = "https://xxx/v1/anthropic/v1/messages"
        result = base_url + _sanitize_forward_query("/")
        self.assertEqual(result, base_url)


class TestExtractClientToken(unittest.TestCase):
    """入站 client_token 提取：Authorization: Bearer 优先，回退 x-api-key。"""

    def test_only_authorization_bearer(self):
        headers = {"Authorization": "Bearer xxx"}
        self.assertEqual(extract_client_token(headers), "xxx")

    def test_only_x_api_key(self):
        # 本次修复的核心复现场景：客户端只发 x-api-key，无 Authorization
        headers = {"x-api-key": "xxx"}
        self.assertEqual(extract_client_token(headers), "xxx")

    def test_both_same_value(self):
        headers = {"Authorization": "Bearer xxx", "x-api-key": "xxx"}
        self.assertEqual(extract_client_token(headers), "xxx")

    def test_both_different_value_authorization_wins(self):
        headers = {"Authorization": "Bearer a", "x-api-key": "b"}
        self.assertEqual(extract_client_token(headers), "a")

    def test_neither_present(self):
        headers = {}
        self.assertEqual(extract_client_token(headers), "")

    def test_authorization_not_bearer_falls_back_to_x_api_key(self):
        headers = {"Authorization": "Basic xxx", "x-api-key": "cc"}
        self.assertEqual(extract_client_token(headers), "cc")

    def test_bearer_scheme_case_insensitive(self):
        # RFC 6750：Bearer scheme 大小写不敏感
        headers = {"Authorization": "bearer cc"}
        self.assertEqual(extract_client_token(headers), "cc")
        headers = {"Authorization": "BEARER cc"}
        self.assertEqual(extract_client_token(headers), "cc")

    def test_x_api_key_value_stripped(self):
        # 带首尾空白的 x-api-key 取值要 strip，否则查表会找不到 strategy
        headers = {"x-api-key": " cc "}
        self.assertEqual(extract_client_token(headers), "cc")

    def test_bearer_value_stripped(self):
        headers = {"Authorization": "Bearer  cc  "}
        self.assertEqual(extract_client_token(headers), "cc")


class TestDetectSourceCaseInsensitive(unittest.TestCase):
    """detect_source 路径尾缀大小写归一。"""

    def test_v1_messages_uppercase(self):
        self.assertEqual(detect_source("/V1/MESSAGES", None), "anthropic")

    def test_v1_messages_mixed_case(self):
        self.assertEqual(detect_source("/V1/Messages", None), "anthropic")

    def test_v1_responses_mixed_case(self):
        self.assertEqual(detect_source("/V1/Responses", None), "responses")

    def test_chat_completions_mixed_case(self):
        self.assertEqual(detect_source("/Chat/Completions", None), "chat")

    def test_lowercase_regression(self):
        # 回归：现有全小写用例仍通过
        self.assertEqual(detect_source("/v1/messages", None), "anthropic")
        self.assertEqual(detect_source("/v1/responses", None), "responses")
        self.assertEqual(detect_source("/chat/completions", None), "chat")


class TestEndToEnd(unittest.TestCase):
    """端到端：token → strategy → route 列表 → tier → supply 列表 → supply。

    用 resolve_strategy + extract_route_candidates 替代已删的 resolve_route，
    保持从 client_token 出发到最终选到 supply 的完整链路语义。
    """

    def test_full_chain(self):
        strategies = [{"client_token": "cc", "route_id": "claude"}]
        rm = _routes_map()
        supply_map = {
            "claude-opus-k0": {"id": "claude-opus-k0", "target_model": "aws.claude-opus"},
        }
        cd = _FakeCooldown()

        strategy = resolve_strategy(strategies, "cc")
        self.assertIsNotNone(strategy)
        routes = extract_route_candidates(strategy, None, rm)
        self.assertEqual(len(routes), 1)
        route = routes[0]
        tier = resolve_tier("claude-opus")
        self.assertEqual(tier, "opus")
        supplies_list = select_supply_list(route, tier)
        self.assertEqual(supplies_list, ["claude-opus-k0"])
        supply = select_supply(supplies_list, supply_map, cd, set())
        self.assertEqual(supply["id"], "claude-opus-k0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
