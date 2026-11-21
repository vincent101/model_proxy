"""catpaw-ide provider 离线测试：凭据只读 + Go 桥传输。

桥用 pocfake 构建（假上游，不出网）。未构建时相关用例跳过。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.providers import resolve_provider, LEGACY_HTTP, CATPAW_IDE
from core.providers import catpaw_ide as ide

GO_DIR = ROOT / "core" / "providers" / "catpaw_ide" / "go"


def _build_fake_lib(tmpdir: Path) -> Path:
    out = tmpdir / "libcatpaw_test.dylib"
    env = dict(os.environ, CGO_ENABLED="1")
    subprocess.run(
        ["go", "build", "-tags", "pocfake", "-buildmode", "c-shared",
         "-o", str(out), "./cmd/native"],
        cwd=str(GO_DIR), check=True, capture_output=True, env=env)
    return out


def _cred(expire_delta=3600 * 24):
    return {
        "accessToken": "at", "refreshToken": "rt", "attestation": "a",
        "mis": "wangfan24", "expire": int(time.time()) + expire_delta,
        "issuedAt": int(time.time()),
    }


class TestResolveProvider(unittest.TestCase):
    def test_legacy_when_absent(self):
        self.assertEqual(resolve_provider({}), LEGACY_HTTP)
        self.assertEqual(resolve_provider({"provider": ""}), LEGACY_HTTP)

    def test_ide_variants(self):
        for v in ("catpaw-ide", "catpaw_ide", " CatPaw-IDE "):
            self.assertEqual(resolve_provider({"provider": v}), CATPAW_IDE, v)

    def test_unknown_rejected(self):
        for v in ("catpaw-cli", "CatPaw IDE", "catpawid"):
            with self.assertRaises(ValueError, msg=v):
                resolve_provider({"provider": v})


class TestCredential(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        p = self.tmp / "cred.json"
        p.write_text(json.dumps(obj))
        return str(p)

    def test_valid(self):
        got = ide.read_credential(self._write(_cred()))
        self.assertEqual(got["mis"], "wangfan24")

    def test_missing_file(self):
        with self.assertRaises(ide.CredentialError) as cm:
            ide.read_credential(str(self.tmp / "nope.json"))
        self.assertIn("不存在", str(cm.exception))

    def test_bad_json(self):
        p = self.tmp / "bad.json"
        p.write_text("{not json")
        with self.assertRaises(ide.CredentialError):
            ide.read_credential(str(p))

    def test_missing_field(self):
        c = _cred()
        del c["attestation"]
        with self.assertRaises(ide.CredentialError) as cm:
            ide.read_credential(self._write(c))
        self.assertIn("attestation", str(cm.exception))

    def test_expired(self):
        with self.assertRaises(ide.CredentialError) as cm:
            ide.read_credential(self._write(_cred(expire_delta=-10)))
        self.assertIn("过期", str(cm.exception))

    def test_expiring_soon(self):
        # 剩余不足 5 分钟即拒绝，避免边界抖动
        with self.assertRaises(ide.CredentialError):
            ide.read_credential(self._write(_cred(expire_delta=60)))

    def test_build_auth_shape(self):
        a = ide.build_auth(_cred())
        o = a["metadata"]["oidc"]
        for k in ("id", "accessToken", "refreshToken", "attestation", "mis"):
            self.assertTrue(o[k], k)
        self.assertEqual(a["metadata"]["cookieSource"], "oidc")
        # id 稳定：同一 mis 每次相同，保证会话缓存命中
        self.assertEqual(ide.build_auth(_cred())["metadata"]["oidc"]["id"], o["id"])


class TestBridgeEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("go") is None:
            raise unittest.SkipTest("go toolchain 不可用")
        cls.tmp = Path(tempfile.mkdtemp())
        try:
            cls.lib = _build_fake_lib(cls.tmp)
        except subprocess.CalledProcessError as e:
            raise unittest.SkipTest(f"桥构建失败: {e.stderr[-300:] if e.stderr else ''}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _start(self, stream=True, cred=None):
        body = json.dumps({
            "model": "gpt-6.2-sol", "max_tokens": 64, "stream": stream,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        return ide.start_request(str(self.lib), cred or _cred(), body)

    def test_stream_yields_anthropic_sse(self):
        resp = self._start(stream=True)
        try:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
            chunks, text = [], b""
            while True:
                c = resp.read1(65536)
                if not c:
                    break
                chunks.append(c)
                text += c
            text = text.decode()
            for want in ("event: message_start", "event: content_block_delta",
                         "event: message_delta", "event: message_stop",
                         '"stop_reason":"end_turn"', '"input_tokens":11',
                         '"output_tokens":3'):
                self.assertIn(want, text)
            # 正文按增量下发，累加后应等于上游累积快照
            body = "".join(
                json.loads(l[6:]).get("delta", {}).get("text", "")
                for l in text.splitlines()
                if l.startswith("data: ") and '"text_delta"' in l)
            self.assertEqual(body, "Hello CatPaw")
        finally:
            resp.close()

    def test_non_stream_returns_json(self):
        resp = self._start(stream=False)
        try:
            raw = resp.read()
            msg = json.loads(raw)
            self.assertEqual(msg["content"][0]["text"], "Hello CatPaw")
            self.assertEqual(msg["stop_reason"], "end_turn")
        finally:
            resp.close()

    def test_close_is_idempotent(self):
        resp = self._start()
        resp.close()
        resp.close()
        self.assertEqual(resp.read1(), b"")
        self.assertEqual(resp.read(), b"")

    def test_library_version_checked(self):
        lib = ide._Library.get(str(self.lib))
        self.assertEqual(lib.CatpawBridgeVersion(), 1)

    def test_missing_library_fails_closed(self):
        with self.assertRaises(ide.BridgeError) as cm:
            ide._Library.get(str(self.tmp / "absent.dylib"))
        self.assertIn("不存在", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
