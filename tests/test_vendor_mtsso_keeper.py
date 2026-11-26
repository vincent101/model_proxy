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
    "sso_session.json", "catpaw_ide_session.json", "catpaw_ide_pending.json",
    ".config.yaml", "state.json", "auth.json",
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
            if path.name == "__pycache__":
                # 运行快照脚本会生成字节码目录（Python 副产物，非快照内容）；
                # 想保持目录干净可设 PYTHONDONTWRITEBYTECODE=1。
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
        self.assertIn("catpaw-cli-refresh", declared)
        for component, names in declared.items():
            for name in names:
                with self.subTest(component=component, name=name):
                    self.assertIn(name, self.source["files"], "组件声明了未登记文件")
                    self.assertTrue((VENDOR / name).is_file())

    def test_snapshot_carries_provenance(self):
        self.assertTrue(self.source.get("source_commit"), "缺少来源 commit")
        self.assertIn("source_dirty", self.source, "缺少脏工作区标记")
        self.assertIn("snapshot_at", self.source)


if __name__ == "__main__":
    unittest.main()
