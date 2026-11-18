"""Compare the migrated CLI help against the legacy golden in a temporary copy."""

import os
import subprocess
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
HELP_GOLDEN = PROJECT / "tests" / "golden" / "legacy-help.txt"
ISOLATION = Path(os.environ.get("MODEL_PROXY_TEST_ISOLATION", "/invalid")).resolve()
if (PROJECT != ISOLATION / "project"
        or not (ISOLATION / ".model-proxy-isolation").is_file()
        or Path(os.environ.get("HOME", "/invalid")).resolve() != ISOLATION / "home"):
    raise RuntimeError("an isolated source snapshot and fake HOME are required")


class LegacyCliBaselineTests(unittest.TestCase):
    def _run(self, *args, cwd=PROJECT):
        return subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", str(PROJECT / "model_proxy_cli.sh"), *args],
            cwd=cwd, input="", text=True, capture_output=True, check=False,
        )

    def test_help_and_no_arguments_match_golden(self):
        expected = HELP_GOLDEN.read_text(encoding="utf-8")
        for args in ((), ("--help",), ("-h",)):
            with self.subTest(args=args):
                result = self._run(*args)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(result.stderr, "")

    def test_help_is_independent_of_caller_directory(self):
        result = self._run("--help", cwd=PROJECT.parent)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, HELP_GOLDEN.read_text(encoding="utf-8"))

    def test_config_is_unknown_and_nonzero(self):
        result = self._run("config")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "Unknown command: config. Use --help.\n")


if __name__ == "__main__":
    unittest.main()
