"""平台范围清单自身的守则测试。

清单是「让 Windows CI 有信号」的唯一开关，它自己腐化（写错文件名、把共享逻辑
悄悄登记成另一平台、或者删了理由）比个别用例失败更危险，所以单独钉住。
"""
import os
import pathlib
import sys
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import platform_scope as scope  # noqa: E402


class ManifestShapeTests(unittest.TestCase):
    def test_every_declared_file_exists(self):
        for name in scope.FILE_SCOPE:
            with self.subTest(name=name):
                self.assertTrue((TESTS_DIR / name).is_file(), f'{name} 不存在')

    def test_every_entry_has_a_reason_and_known_scope(self):
        for name, entry in scope.FILE_SCOPE.items():
            with self.subTest(name=name):
                declared, reason, gap = scope._split(entry)
                self.assertIn(declared, (scope.POSIX, scope.WINDOWS))
                self.assertGreater(len(reason.strip()), 8, '理由不能为空')
                self.assertIsInstance(gap, bool)
        for key, entry in scope.TEST_SCOPE.items():
            with self.subTest(key=str(key)):
                name, pattern = key
                self.assertTrue((TESTS_DIR / name).is_file(), f'{name} 不存在')
                self.assertTrue(pattern, '用例级声明必须给出类名或方法名')
                declared, reason, gap = scope._split(entry)
                self.assertIn(declared, (scope.POSIX, scope.WINDOWS))
                self.assertGreater(len(reason.strip()), 8, '理由不能为空')

    def test_undeclared_file_defaults_to_both(self):
        # fail-closed：没登记的文件必须两边都过，否则新用例可以静默逃出 Windows 验收
        self.assertEqual(scope.file_scope('test_not_listed_anywhere.py'),
                         (scope.BOTH, '', False))
        self.assertFalse(scope.covers('test_not_listed_anywhere.py', 'X.test_y', scope.POSIX))
        self.assertFalse(scope.covers('test_not_listed_anywhere.py', 'X.test_y', scope.WINDOWS))

    def test_file_declaration_only_excludes_the_other_host(self):
        name = 'test_platform_lock.py'
        self.assertTrue(scope.covers(name, 'X.test_y', scope.WINDOWS))
        self.assertFalse(scope.covers(name, 'X.test_y', scope.POSIX))

    def test_test_level_declaration_is_narrower_than_its_file(self):
        name, pattern = next(iter(k for k in scope.TEST_SCOPE if k[1]))
        with self.subTest(name=name, pattern=pattern):
            self.assertFalse(scope.covers(name, pattern + '.test_a', scope.POSIX))
            self.assertTrue(scope.covers(name, pattern + '.test_a', scope.WINDOWS))
            self.assertFalse(scope.covers(name, 'OtherClass.test_a', scope.WINDOWS))

    def test_gaps_are_well_formed(self):
        # 缺口表可以被补完（当前已清空）。这里只约束「留下来的条目必须合规」，
        # 不再要求非空——否则补完缺口反而要让测试失败。
        for name, reason in scope.gaps():
            self.assertIn('::', name, '缺口条目应能定位到文件或用例')
            self.assertGreater(len(reason.strip()), 8, '缺口必须写清待补内容')


if __name__ == '__main__':
    unittest.main()
