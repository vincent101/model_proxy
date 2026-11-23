"""provider 本机设置层：默认值、文件覆盖、supply 覆盖的优先级。

设计边界：credential_file / bridge_lib 不再是 supply 必填项——模块内置默认值，
换机器改 config/provider_settings.json。本文件锁定三层优先级与 fail-open 行为。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.providers import catpaw_cli, catpaw_ide, settings  # noqa: E402


class _SettingsFile:
    """临时 provider_settings.json，用环境变量指过去。"""

    def __init__(self, payload):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "provider_settings.json")
        Path(self.path).write_text(json.dumps(payload), encoding="utf-8")
        self._old = os.environ.get("MODEL_PROXY_PROVIDER_SETTINGS")
        os.environ["MODEL_PROXY_PROVIDER_SETTINGS"] = self.path
        settings.reset_cache()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("MODEL_PROXY_PROVIDER_SETTINGS", None)
        else:
            os.environ["MODEL_PROXY_PROVIDER_SETTINGS"] = self._old
        settings.reset_cache()
        Path(self.path).unlink(missing_ok=True)
        os.rmdir(self.dir)
        return False


class TestDefaults(unittest.TestCase):
    def test_missing_file_falls_back_to_defaults(self):
        with _SettingsFile({}):
            cred, lib = catpaw_ide.resolve_paths({})
            self.assertEqual(cred, catpaw_ide.DEFAULT_CREDENTIAL_FILE)
            self.assertEqual(lib, settings.default_bridge_lib())
            self.assertEqual(catpaw_cli.credential_file(), catpaw_cli.CREDENTIAL_FILE)

    def test_bridge_lib_suffix_follows_platform(self):
        lib = settings.default_bridge_lib()
        expect = {"darwin": ".dylib", "win32": ".dll"}.get(sys.platform, ".so")
        self.assertTrue(lib.endswith(expect), lib)

    def test_supply_alone_is_enough(self):
        """仿真生产：supply 只写 4 个字段，不打任何路径。"""
        with _SettingsFile({}):
            supply = {"id": "openai-sol-mcli-3339", "protocol": "catpaw-ide",
                      "target_model": "gpt-6.1-sol"}
            cred, lib = catpaw_ide.resolve_paths(supply)
            self.assertTrue(cred and lib)


class TestOverrideLayers(unittest.TestCase):
    def test_settings_file_beats_default(self):
        with _SettingsFile({"catpaw-ide": {
                "credential_file": "/tmp/a.json", "bridge_lib": "/tmp/a.dll"},
                "catpaw-cli": {"credential_file": "/tmp/m.yaml"}}):
            self.assertEqual(catpaw_ide.resolve_paths({}), ("/tmp/a.json", "/tmp/a.dll"))
            self.assertEqual(catpaw_cli.credential_file(), "/tmp/m.yaml")
            self.assertEqual(
                catpaw_cli.materialize({"id": "x", "protocol": "catpaw-cli",
                                        "target_model": "m"})["appkey_file"],
                "/tmp/m.yaml")

    def test_supply_beats_settings_file(self):
        with _SettingsFile({"catpaw-ide": {
                "credential_file": "/tmp/a.json", "bridge_lib": "/tmp/a.dll"}}):
            cred, lib = catpaw_ide.resolve_paths(
                {"credential_file": "/s/b.json", "bridge_lib": "/s/b.so"})
            self.assertEqual((cred, lib), ("/s/b.json", "/s/b.so"))

    def test_partial_section_keeps_other_defaults(self):
        with _SettingsFile({"catpaw-ide": {"bridge_lib": "/tmp/only.dll"}}):
            cred, lib = catpaw_ide.resolve_paths({})
            self.assertEqual(cred, catpaw_ide.DEFAULT_CREDENTIAL_FILE)
            self.assertEqual(lib, "/tmp/only.dll")

    def test_corrupt_file_is_fail_open(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "provider_settings.json")
        Path(p).write_text("{not json", encoding="utf-8")
        old = os.environ.get("MODEL_PROXY_PROVIDER_SETTINGS")
        os.environ["MODEL_PROXY_PROVIDER_SETTINGS"] = p
        settings.reset_cache()
        try:
            self.assertEqual(catpaw_ide.resolve_paths({})[0],
                             catpaw_ide.DEFAULT_CREDENTIAL_FILE)
        finally:
            settings.reset_cache()
            if old is None:
                os.environ.pop("MODEL_PROXY_PROVIDER_SETTINGS", None)
            else:
                os.environ["MODEL_PROXY_PROVIDER_SETTINGS"] = old
            Path(p).unlink()
            os.rmdir(d)

    def test_cache_invalidated_when_file_changes(self):
        # 缓存键是 (mtime_ns, size)。Windows 上同一时钟刻度内的两次写入可能得到
        # 相同的 mtime_ns，因此这里故意让内容长度不同，只测「文件变了要重新读」
        # 这个契约；同长度改写的边界见注释说明，不在这里断言。
        with _SettingsFile({"catpaw-ide": {"bridge_lib": "/tmp/v1.dll"}}):
            self.assertEqual(catpaw_ide.resolve_paths({})[1], "/tmp/v1.dll")
            Path(os.environ["MODEL_PROXY_PROVIDER_SETTINGS"]).write_text(
                json.dumps({"catpaw-ide": {"bridge_lib": "/tmp/v2-rebuilt.dll"}}),
                encoding="utf-8")
            self.assertEqual(catpaw_ide.resolve_paths({})[1], "/tmp/v2-rebuilt.dll")


class TestSupplyValidation(unittest.TestCase):
    def test_ide_supply_without_paths_is_valid(self):
        from core.server import validate_supply_ext
        validate_supply_ext({"id": "ide", "protocol": "catpaw-ide",
                             "target_model": "gpt-6.2-sol"})

    def test_ide_supply_non_string_path_rejected(self):
        from core.server import validate_supply_ext
        with self.assertRaises(ValueError):
            validate_supply_ext({"id": "ide", "protocol": "catpaw-ide",
                                 "target_model": "gpt-6.2-sol",
                                 "bridge_lib": 123})

    def test_ide_supply_still_rejects_appkey(self):
        from core.server import validate_supply_ext
        with self.assertRaises(ValueError):
            validate_supply_ext({"id": "ide", "protocol": "catpaw-ide",
                                 "target_model": "gpt-6.2-sol", "appkey": "k"})


if __name__ == "__main__":
    unittest.main()
