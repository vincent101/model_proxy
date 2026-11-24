"""`dispatch.runtime_paths` 的项目根入参契约。

回归（2026-11-24）：Windows 的 install/switch 分支先用 `ntpath` 拼路径、把 root
归一化成 str，再调用本函数；函数内部用 `/` 拼 `<root>/config/runtime_paths.json`，
于是 `str / str` 抛 TypeError。该异常在 install 里被当作「运行目录证据不可验证」
吞掉，表现为安装直接失败且没有任何可诊断信息。虚拟 Windows 用例因为整体 patch
了 `dispatch.runtime_paths`，所以一直没暴露它。

这里只锁入参归一化本身（不碰文件系统），两种形态必须得到同一条子路径。
"""
import os
import pathlib
import unittest
from pathlib import Path
from unittest.mock import patch

from core.cli import dispatch


class RuntimePathsRootInputTests(unittest.TestCase):
    def _capture(self, root):
        seen = {}

        def fake_read(path, **kwargs):
            seen['path'] = path
            seen['project_root'] = kwargs['project_root']
            return {'fake': 'paths'}

        with patch.object(dispatch, 'read_runtime_paths', side_effect=fake_read), \
             patch.object(dispatch, 'inspect_runtime_identities'):
            result = dispatch.runtime_paths(root)
        return seen, result

    def test_accepts_a_str_root(self):
        seen, result = self._capture('/synthetic/project')
        self.assertEqual(seen['path'], Path('/synthetic/project') / 'config' / 'runtime_paths.json')
        self.assertEqual(seen['project_root'], Path('/synthetic/project'))
        self.assertEqual(result, {'fake': 'paths'})

    def test_accepts_a_path_root_unchanged(self):
        root = pathlib.Path('/synthetic/project')
        seen, _ = self._capture(root)
        self.assertEqual(seen['path'], root / 'config' / 'runtime_paths.json')
        self.assertEqual(seen['project_root'], root)

    def test_str_and_path_inputs_produce_the_same_child_path(self):
        from_str, _ = self._capture(os.path.join('/synthetic', 'project'))
        from_path, _ = self._capture(Path('/synthetic/project'))
        self.assertEqual(from_str['path'], from_path['path'])


if __name__ == '__main__':
    unittest.main()
