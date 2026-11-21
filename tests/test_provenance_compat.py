"""com.apple.provenance 不参与扩展属性相等性比较。

背景（2026-11-22 实测）：账本文件由某个启动上下文（终端 A）创建后，若从另一个
启动上下文（终端 B / launchd）重启服务，新建的 staging 文件会带**不同**的
provenance 值，于是 `_open_private` 的 `expected_attrs` 比较恒失败——所有提交
返回 NOT_APPLIED，日志只有一句 `usage_totals.persistence state=PENDING`，
账本永久停止写入且没有任何可诊断信息。

规则：比较时剔除 provenance；**其他任何属性仍必须完全相等**。
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import secure_files as secure  # noqa: E402

PROV = b'com.apple.provenance'


def attrs(*pairs):
    return tuple(pairs)


class TestProvenanceStable(unittest.TestCase):
    def test_different_provenance_values_compare_equal(self):
        a = attrs((PROV, b'\x01\x02\x00\xbe\x02z\xc4'))
        b = attrs((PROV, b'\x01\x02\x00\xc6u\x009'))
        self.assertNotEqual(a, b)
        self.assertEqual(secure._provenance_stable(a), secure._provenance_stable(b))

    def test_provenance_is_dropped_entirely(self):
        self.assertEqual(
            secure._provenance_stable(attrs((PROV, b'x'))), ())

    def test_presence_mismatch_is_tolerated(self):
        """一方有、一方没有 provenance：不因这个属性判不相等。"""
        self.assertEqual(secure._provenance_stable(attrs((PROV, b'x'))),
                         secure._provenance_stable(()))

    def test_other_attributes_still_must_match(self):
        a = attrs((PROV, b'x'), (b'com.apple.quarantine', b'0081'))
        b = attrs((PROV, b'y'))
        self.assertNotEqual(secure._provenance_stable(a),
                            secure._provenance_stable(b))

    def test_other_attribute_value_differences_still_fail(self):
        a = attrs((b'user.custom', b'v1'))
        b = attrs((b'user.custom', b'v2'))
        self.assertNotEqual(secure._provenance_stable(a),
                            secure._provenance_stable(b))

    def test_none_stays_none(self):
        self.assertIsNone(secure._provenance_stable(None))

    def test_empty_stays_empty(self):
        self.assertEqual(secure._provenance_stable(()), ())


class TestUnexpectedAttributeStillRejected(unittest.TestCase):
    def test_quarantine_is_still_rejected_by_protection_reader(self):
        """`_mac_fd_protection` 仍只放行 provenance；其他属性一律拒绝。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'f')
            with open(path, 'wb') as f:
                f.write(b'{}')
            os.chmod(path, 0o600)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                # 不设置额外属性时正常
                secure._mac_fd_protection(fd, directory=False)
            finally:
                os.close(fd)


if __name__ == '__main__':
    unittest.main()
