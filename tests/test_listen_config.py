"""listen / allow_hosts 配置解析矩阵单测（core/listen_config.py）。

覆盖回退矩阵全部分支：
- 无 listen 段：host 固定 127.0.0.1，port 取 MODEL_PROXY_PORT（合法时）否则 18889；
- 有 listen 段：host/port 缺一或非法 → 失败（fail-fast，不静默回退）；
- MODEL_PROXY_PORT 本身非法 → 无论哪条路径都失败；
- allow_hosts 非字符串数组 / 含空串 → 失败；
- listen.host 非 loopback 而 allow_hosts 为空 → 失败；绑 loopback 不启用校验；
- 脚本模式（--shell）输出与退出码，供 model_proxy_cli.sh / ensure 复用。

运行：cd tools/model_proxy && python3 -m unittest tests.test_listen_config -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.listen_config import (  # noqa: E402
    ListenConfigError,
    resolve_listen,
)

_PKG_ROOT = Path(__file__).resolve().parent.parent
_MODULE = _PKG_ROOT / "core" / "listen_config.py"


def _write_config(tmpdir, payload) -> str:
    path = Path(tmpdir) / "model_proxy_config.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class TestNoListenSection(unittest.TestCase):
    """无 listen 段：旧逻辑回退（向后兼容现状部署）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_fallback_default(self):
        cfg = _write_config(self._tmp.name, {"admin_token": "x"})
        self.assertEqual(resolve_listen(cfg, None),
                         {"host": "127.0.0.1", "port": 18889,
                          "allow_hosts": [], "host_check_enabled": False})

    def test_fallback_env_port(self):
        cfg = _write_config(self._tmp.name, {"admin_token": "x"})
        self.assertEqual(resolve_listen(cfg, "19000")["port"], 19000)

    def test_fallback_env_port_empty_string(self):
        # 空串视同未设置
        cfg = _write_config(self._tmp.name, {"admin_token": "x"})
        self.assertEqual(resolve_listen(cfg, "")["port"], 18889)

    def test_missing_config_file_falls_back(self):
        # 文件缺失 → 无 listen 段，走回退（与 ConfigStore 之前的行为兼容）
        self.assertEqual(
            resolve_listen(str(Path(self._tmp.name) / "absent.json"), None)["port"],
            18889)

    def test_env_port_invalid_fails_even_without_listen(self):
        # MODEL_PROXY_PORT 非法 → 无论哪条路径都失败
        cfg = _write_config(self._tmp.name, {"admin_token": "x"})
        for bad in ("abc", "-1", "0", "65536", "18 889", "1.5"):
            with self.assertRaises(ListenConfigError, msg=repr(bad)):
                resolve_listen(cfg, bad)

    def test_corrupt_json_fails(self):
        cfg = _write_config(self._tmp.name, "{ not json")
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)

    def test_non_object_root_fails(self):
        cfg = _write_config(self._tmp.name, [1, 2])
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)


class TestListenSection(unittest.TestCase):
    """有 listen 段：host/port 必须齐全且合法，配置为唯一权威。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_complete_section(self):
        cfg = _write_config(self._tmp.name, {
            "listen": {"host": "0.0.0.0", "port": 18889},
            "allow_hosts": ["mba.local:18889"],
        })
        out = resolve_listen(cfg, "29000")  # env port 被忽略
        self.assertEqual(out["host"], "0.0.0.0")
        self.assertEqual(out["port"], 18889)
        self.assertTrue(out["host_check_enabled"])
        self.assertEqual(out["allow_hosts"], ["mba.local:18889"])

    def test_port_as_digit_string_accepted(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "0.0.0.0", "port": "18889"},
                             "allow_hosts": ["a:18889"]})
        self.assertEqual(resolve_listen(cfg, None)["port"], 18889)

    def test_missing_host_fails(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"port": 18889},
                             "allow_hosts": ["a:18889"]})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)

    def test_missing_port_fails(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "0.0.0.0"},
                             "allow_hosts": ["a:18889"]})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)

    def test_empty_host_fails(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "", "port": 18889},
                             "allow_hosts": ["a:18889"]})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)

    def test_invalid_port_fails(self):
        for bad in (0, -1, 65536, "abc", 1.5, True, None):
            cfg = _write_config(self._tmp.name, {
                "listen": {"host": "0.0.0.0", "port": bad},
                "allow_hosts": ["a:18889"]})
            with self.assertRaises(ListenConfigError, msg=repr(bad)):
                resolve_listen(cfg, None)

    def test_non_dict_listen_fails(self):
        cfg = _write_config(self._tmp.name, {
            "listen": ["0.0.0.0", 18889], "allow_hosts": ["a:18889"]})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)

    def test_loopback_listen_ignores_env_port(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "127.0.0.1", "port": 18889}})
        out = resolve_listen(cfg, "29000")
        self.assertEqual((out["host"], out["port"]), ("127.0.0.1", 18889))
        self.assertFalse(out["host_check_enabled"])

    def test_env_port_invalid_fails_with_listen_section(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "127.0.0.1", "port": 18889}})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, "abc")

    def test_ipv6_loopback_listen_no_check(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "::1", "port": 18889}})
        self.assertFalse(resolve_listen(cfg, None)["host_check_enabled"])

    def test_localhost_listen_no_check(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "localhost", "port": 18889}})
        self.assertFalse(resolve_listen(cfg, None)["host_check_enabled"])


class TestAllowHosts(unittest.TestCase):
    """allow_hosts 类型校验与非 loopback 强制非空。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_non_list_fails(self):
        for bad in ("mba.local:18889", 42, {"a": 1}):
            cfg = _write_config(self._tmp.name,
                                {"listen": {"host": "0.0.0.0", "port": 18889},
                                 "allow_hosts": bad})
            with self.assertRaises(ListenConfigError, msg=repr(bad)):
                resolve_listen(cfg, None)

    def test_non_string_entry_fails(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "0.0.0.0", "port": 18889},
                             "allow_hosts": ["a:18889", 5]})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)

    def test_empty_string_entry_fails(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "0.0.0.0", "port": 18889},
                             "allow_hosts": ["a:18889", ""]})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)

    def test_non_loopback_with_empty_allow_hosts_fails(self):
        # 未配置 allow_hosts（缺 key）
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "0.0.0.0", "port": 18889}})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)
        # 显式空数组
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "0.0.0.0", "port": 18889},
                             "allow_hosts": []})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)
        # 绑具体 LAN IP 同样要求非空
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "192.168.1.5", "port": 18889},
                             "allow_hosts": []})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)

    def test_loopback_with_empty_allow_hosts_ok(self):
        # 绑 loopback 时不校验白名单（无暴露面），空列表合法
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "127.0.0.1", "port": 18889},
                             "allow_hosts": []})
        out = resolve_listen(cfg, None)
        self.assertFalse(out["host_check_enabled"])
        self.assertEqual(out["allow_hosts"], [])

    def test_loopback_without_allow_hosts_key_ok(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "127.0.0.1", "port": 18889}})
        out = resolve_listen(cfg, None)
        self.assertFalse(out["host_check_enabled"])
        self.assertEqual(out["allow_hosts"], [])

    def test_loopback_with_allow_hosts_type_error_still_fails(self):
        # 类型校验不因绑 loopback 而豁免（fail-fast，不静默忽略）
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "127.0.0.1", "port": 18889},
                             "allow_hosts": "mba.local"})
        with self.assertRaises(ListenConfigError):
            resolve_listen(cfg, None)

    def test_entries_stripped(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "0.0.0.0", "port": 18889},
                             "allow_hosts": ["  mba.local:18889  "]})
        self.assertEqual(resolve_listen(cfg, None)["allow_hosts"],
                         ["mba.local:18889"])


class TestShellMode(unittest.TestCase):
    """--shell 脚本模式：cli.sh / ensure 经此与 server 共用同一实现。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _run(self, config_path, env_port=""):
        return subprocess.run(
            [sys.executable, str(_MODULE), "--shell", str(config_path), env_port],
            capture_output=True, text=True)

    def test_shell_output_ok(self):
        cfg = _write_config(self._tmp.name, {
            "listen": {"host": "0.0.0.0", "port": 29999},
            "allow_hosts": ["mba.local:29999"]})
        r = self._run(cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout,
                         "LISTEN_HOST='0.0.0.0'\nLISTEN_PORT='29999'\n")

    def test_shell_invalid_config_exits_nonzero(self):
        cfg = _write_config(self._tmp.name,
                            {"listen": {"host": "0.0.0.0"}})
        r = self._run(cfg)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("listen.port", r.stderr)

    def test_shell_invalid_env_port_exits_nonzero(self):
        cfg = _write_config(self._tmp.name, {"admin_token": "x"})
        r = self._run(cfg, env_port="abc")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MODEL_PROXY_PORT", r.stderr)

    def test_shell_fallback_without_config(self):
        r = self._run(Path(self._tmp.name) / "absent.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LISTEN_PORT='18889'", r.stdout)


if __name__ == "__main__":
    unittest.main()
