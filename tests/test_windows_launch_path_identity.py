"""无参直启的入口路径同一性判定（跨平台可测）。

背景（2026-11-22 实测 GitHub Windows runner）：原实现比较
``normcase(abspath(original[-1]))`` 与 ``normcase(resolve(entry))``——
前者是纯文本运算、保留 8.3 短名，后者走文件系统、返回长名。路径里只要
出现短名（``C:/Users/RUNNER~1/...``，以及任何过长的 Windows 用户名），
两者必然不等，于是无参直启被误判成「不可安全归一化」而退出 1。

这里用「同一文件的不同拼写」（符号链接）与「不同文件」两个方向锁住新语义：
判定必须在解析后比较，且不能因此放行真正不同的脚本。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.cli.model_proxy import _windows_same_path  # noqa: E402


class SamePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()

    def test_identical_path_is_same(self):
        target = self.dir / "entry.py"
        target.write_text("", encoding="utf-8")
        self.assertFalse(_windows_same_path(str(target), str(target)))

    def test_relative_and_absolute_spelling_is_same(self):
        """相对路径按当前工作目录解析后仍是同一文件。"""
        target = self.dir / "entry.py"
        target.write_text("", encoding="utf-8")
        previous = os.getcwd()
        os.chdir(self.dir)
        try:
            self.assertFalse(_windows_same_path("entry.py", str(target)))
        finally:
            os.chdir(previous)

    def test_symlink_spelling_is_same(self):
        """不同拼写、同一文件（符号链接）必须判为同一——这正是短名展开的等价场景。"""
        target = self.dir / "entry.py"
        target.write_text("", encoding="utf-8")
        link = self.dir / "alias.py"
        link.symlink_to(target)
        self.assertFalse(_windows_same_path(str(link), str(target)))

    def test_different_files_are_not_same(self):
        first = self.dir / "first.py"
        second = self.dir / "second.py"
        first.write_text("", encoding="utf-8")
        second.write_text("", encoding="utf-8")
        self.assertTrue(_windows_same_path(str(first), str(second)))

    def test_missing_path_falls_back_without_raising(self):
        missing = self.dir / "absent.py"
        self.assertTrue(_windows_same_path(str(missing), str(self.dir / "other.py")))


if __name__ == "__main__":
    unittest.main()
