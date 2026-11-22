"""Whisky direct-launch smoke; creates only owned fake projects and children."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "requires Windows Python")
class WindowsExecSmoke(unittest.TestCase):
    def test_no_argument_service_reexecs_with_observable_explicit_arguments(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="mp-exec-test-") as temp:
            root = Path(temp) / "项目 space"
            root.mkdir()
            shutil.copytree(source / "core", root / "core",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (root / "config").mkdir()
            entry = root / "core" / "cli" / "model_proxy.py"
            config = root / "config" / "配置 fake.json"
            config.write_text(json.dumps({"listen": {"host": "127.0.0.1", "port": 12345}}),
                              encoding="utf-8")
            receipt = root / "receipt.json"
            (root / "core" / "server.py").write_text(
                "import json,os,sys\n"
                "from pathlib import Path\n"
                "from core.cli.windows_command_line import verify_shared_launch_argv\n"
                "def main():\n"
                " e=str(Path(__file__).resolve().parent/'cli'/'model_proxy.py')\n"
                " verify_shared_launch_argv(sys.orig_argv,entry=e,config=os.environ['MODEL_PROXY_CONFIG'],"
                "port=int(os.environ['MODEL_PROXY_PORT']))\n"
                " p=os.environ['MP_FAKE_RECEIPT']\n"
                " with open(p,'w',encoding='utf-8') as f:\n"
                "  json.dump({'argv':sys.orig_argv,'config':os.environ.get('MODEL_PROXY_CONFIG'),"
                "'port':os.environ.get('MODEL_PROXY_PORT')},f,ensure_ascii=False)\n",
                encoding="utf-8")
            env = dict(os.environ, MODEL_PROXY_CONFIG=str(config), MODEL_PROXY_PORT="60000",
                       MP_FAKE_RECEIPT=str(receipt))
            child = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(entry)],
                                     cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                stdout, stderr = child.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()  # only our directly created Popen child
                child.communicate(timeout=10)
                self.fail("owned direct-launch child timed out")
            self.assertEqual(child.returncode, 0, stderr.decode("utf-8", "replace"))
            self.assertTrue(receipt.exists(), stderr.decode("utf-8", "replace"))
            data = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(data["config"], str(config))
            self.assertEqual(data["port"], "12345")
            # 产品在 re-exec 前把入口/配置规范化成长路径（8.3 短名会被展开）。
            # tempfile 在 runner 上给的是 RUNNER~1 短名，因此期望值同样取规范化结果。
            self.assertEqual(data["argv"][-5:], [str(entry.resolve()), "--config",
                                                 str(config.resolve()), "--port", "12345"])

    def test_direct_parent_returns_the_owned_service_child_failure(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="mp-direct-status-") as temp:
            root = Path(temp) / "project with space"
            (root / "core" / "cli").mkdir(parents=True)
            (root / "config").mkdir()
            (root / "core" / "__init__.py").write_text("", encoding="utf-8")
            (root / "core" / "cli" / "__init__.py").write_text("", encoding="utf-8")
            entry = root / "core" / "cli" / "model_proxy.py"
            shutil.copyfile(source / "core" / "cli" / "model_proxy.py", entry)
            shutil.copyfile(source / "core" / "listen_config.py", root / "core" / "listen_config.py")
            (root / "config" / "model_proxy_config.json").write_text("{}", encoding="utf-8")
            (root / "core" / "server.py").write_text(
                "def main():\n raise SystemExit(17)\n", encoding="utf-8")
            env = os.environ.copy()
            env.pop("MODEL_PROXY_CONFIG", None)
            env.pop("MODEL_PROXY_PORT", None)
            child = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(entry)],
                                     cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                _, stderr = child.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=10)
                self.fail("owned direct-launch child timed out")
            self.assertEqual(child.returncode, 17, stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    if len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("requires -I -S -B and a fresh result path")
    result_path = Path(sys.argv[1])
    if result_path.exists():
        raise SystemExit("result exists")
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(WindowsExecSmoke))
    result_path.write_text(json.dumps({"tests": result.testsRun, "errors": len(result.errors),
                                      "failures": len(result.failures), "output": output.getvalue()},
                                     ensure_ascii=False), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
