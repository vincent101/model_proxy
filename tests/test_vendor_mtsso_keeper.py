"""vendor/mtsso_keeper 快照必须自洽、可分发、且不含禁止项。

这里是纯文件校验：不解包、不联网、不接触 `~/.mtsso_keeper`。
"""
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "mtsso_keeper"

FORBIDDEN_NAMES = {
    "keeper.py", "install.sh", "uninstall.sh",
    "mcli_token.py", "keeper_common.py",   # 凭据管理/保鲜，不在最小运行集内
    "ciba_chain.py", "mcli_chain.py", "event_scheduler.py", "deploy_txn.py",
    "sso_session.json", "catpaw_ide_session.json", "catpaw_ide_pending.json",
    ".config.yaml", "state.json", "auth.json",
}

# 快照只解决「把 model_proxy 跑起来」，不承担凭据管理/保鲜。
CREDENTIAL_MANAGEMENT_MODULES = {
    "mcli_token.py", "keeper_common.py", "ciba_chain.py", "mcli_chain.py",
    "event_scheduler.py", "deploy_txn.py", "keeper.py",
}
FORBIDDEN_SUFFIXES = (".plist", ".tmpl", ".lock", ".log", ".bak", ".pyc")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VendorSnapshotTests(unittest.TestCase):
    def setUp(self):
        if not VENDOR.is_dir():
            self.skipTest("vendor/mtsso_keeper 不存在")
        self.source = json.loads((VENDOR / "SOURCE.json").read_text(encoding="utf-8"))

    def test_every_recorded_file_matches_hash(self):
        files = self.source["files"]
        self.assertTrue(files, "SOURCE.json 未登记任何文件")
        for name, meta in files.items():
            path = VENDOR / name
            with self.subTest(name=name):
                self.assertTrue(path.is_file(), f"缺少 {name}")
                self.assertEqual(_sha256(path), meta["sha256"], f"{name} 内容与登记不符")

    def test_sha256sums_matches_source_json(self):
        listed = {}
        for line in (VENDOR / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, _, name = line.partition("  ")
            listed[name] = digest
        self.assertEqual(listed, {n: m["sha256"] for n, m in self.source["files"].items()})

    def test_no_unregistered_or_forbidden_files(self):
        allowed = set(self.source["files"]) | {"SOURCE.json", "SHA256SUMS", "README.md", "NOTICE.md"}
        for path in VENDOR.iterdir():
            if path.name == "__pycache__" and path.is_dir():
                # 运行快照脚本会生成字节码目录（Python 副产物，非快照内容）；
                # 想保持目录干净可设 PYTHONDONTWRITEBYTECODE=1。
                # 仅在确为目录时豁免：同名普通文件仍须接受校验。
                continue
            if not path.is_file():
                self.fail(f"快照目录不应有子目录: {path.name}")
            with self.subTest(name=path.name):
                self.assertIn(path.name, allowed, "存在未登记文件")
                self.assertNotIn(path.name, FORBIDDEN_NAMES, "存在禁止分发的文件")
                self.assertFalse(path.name.endswith(FORBIDDEN_SUFFIXES),
                                 "存在禁止分发的后缀")

    def test_components_declare_existing_files(self):
        declared = self.source.get("components", {})
        self.assertIn("catpaw-ide", declared)
        for component, names in declared.items():
            for name in names:
                with self.subTest(component=component, name=name):
                    self.assertIn(name, self.source["files"], "组件声明了未登记文件")
                    self.assertTrue((VENDOR / name).is_file())

    def test_each_component_runs_from_its_own_closure(self):
        """每个组件声明的文件集合必须自洽：只拷这些文件就能跑起来。

        只拿到 model_proxy 的人不会有 keeper 源码，也不会知道还差哪个模块。
        组件清单漏一个文件（例如 mcli_token 依赖 keeper_common）在这里直接暴露，
        而不是等到用户机器上 ModuleNotFoundError。
        """
        import os
        import shutil
        import subprocess
        import sys
        import tempfile

        entry = {"catpaw-ide": "catpaw_ide_session.py"}
        for component, names in self.source["components"].items():
            with self.subTest(component=component):
                self.assertIn(component, entry, "新增组件需在此登记入口脚本")
                with tempfile.TemporaryDirectory() as td:
                    for name in names:
                        shutil.copy2(VENDOR / name, Path(td) / name)
                    env = dict(os.environ, HOME=td, PYTHONDONTWRITEBYTECODE="1")
                    proc = subprocess.run(
                        [sys.executable, str(Path(td) / entry[component]), "--check"],
                        capture_output=True, text=True, env=env, timeout=120)
                    # 隔离 HOME 下无凭据 → 1；纯 import 失败是 1 + traceback，
                    # 用 stderr 有无 ModuleNotFoundError 区分两种情况
                    self.assertNotIn("ModuleNotFoundError", proc.stderr,
                                     f"{component} 闭包缺文件: {proc.stderr[-300:]}")
                    self.assertNotIn("ImportError", proc.stderr,
                                     f"{component} 闭包缺文件: {proc.stderr[-300:]}")
                    self.assertEqual(proc.returncode, 1,
                                     f"{component} 期望无凭据退出 1，"
                                     f"实际 {proc.returncode}: {proc.stderr[-300:]}")

    def test_snapshot_excludes_credential_management(self):
        """快照只解决「model_proxy 能跑」，凭据管理/保鲜一律不收。

        catpaw-cli 的凭据由 mc CLI 自己写，零脚本即可；mcli_token.py 这类
        探测/铸票/回写属于 keeper 的职责（与 6h tick、state 记账耦合），
        混进快照会让「没有 keeper」的使用者以为要自己跑刷新，反而制造
        两处交替写配置的风险。
        """
        present = {p.name for p in VENDOR.iterdir() if p.is_file()}
        leaked = present & CREDENTIAL_MANAGEMENT_MODULES
        self.assertEqual(leaked, set(), f"快照混入了凭据管理脚本: {sorted(leaked)}")
        for component, names in self.source["components"].items():
            with self.subTest(component=component):
                self.assertEqual(set(names) & CREDENTIAL_MANAGEMENT_MODULES, set())

    def test_catpaw_cli_needs_no_script(self):
        """catpaw-cli 通道不得出现在组件清单里：它零脚本可用。"""
        self.assertNotIn("catpaw-cli-refresh", self.source["components"],
                         "catpaw-cli 只需 mc CLI 的登录态，不应有凭证管理组件")

    def test_snapshot_carries_provenance(self):
        self.assertTrue(self.source.get("source_commit"), "缺少来源 commit")
        self.assertIn("source_dirty", self.source, "缺少脏工作区标记")
        self.assertIn("snapshot_at", self.source)


if __name__ == "__main__":
    unittest.main()
