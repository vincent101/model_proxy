"""Phase D：read(amt) → read1(amt) 读取及时性与等价性单测。

背景（2026-11-06 实测）：http.client.HTTPResponse.read(amt) 在 chunked 响应
上跨 chunk 累积直到凑满 amt 或 EOF，转换链路首事件被压到流近结束才交付
（线上 first_event_ms/total p50≈0.86）。read1(amt) 单次读取当前可用数据。

三门槛（对应设计 2026-11-05 §2.5）：
- D1 价值：首事件在第二 chunk 未释放时已交付；
- D2 等价：完整流 byte 不丢/不重/不乱序；
- D3 吞吐：大 payload 读取不退化。

运行：cd tools/model_proxy && python3 -m pytest tests/test_stream_reading.py -q
"""
import http.client
import socket
import threading
import time
import unittest


def _build_chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


class _ChunkedPair:
    """受控 socketpair：服务端按脚本发送 chunked 响应；客户端 HTTPResponse。

    不创建真实网络 socket 对象（socketpair 仅本机字节管道），producer 内容
    全部合成，不触碰任何真实供应商/配置。
    """

    def __init__(self):
        self.a, self.b = socket.socketpair()
        self.gate = threading.Event()
        self.first_chunk_sent = threading.Event()

    def start(self, first_chunk: bytes, second_chunks: bytes):
        head = (b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n")

        def serve():
            self.b.sendall(head + _build_chunk(first_chunk))
            self.first_chunk_sent.set()
            self.gate.wait(timeout=10)
            self.b.sendall(second_chunks + b"0\r\n\r\n")
            self.b.close()

        threading.Thread(target=serve, daemon=True).start()
        resp = http.client.HTTPResponse(self.a, method="POST")
        resp.begin()
        return resp


class TestReadTimeliness(unittest.TestCase):
    """D1：read1 及时性。"""

    def test_read1_returns_first_event_before_second_chunk(self):
        first = b'event: response.created\ndata: {"type":"a"}\n\n'
        second = b'event: response.completed\ndata: {"type":"b"}\n\n'
        pair = _ChunkedPair()
        resp = pair.start(first, _build_chunk(second))
        pair.first_chunk_sent.wait(timeout=2)
        t0 = time.monotonic()
        data = resp.read1(4096)
        elapsed = time.monotonic() - t0
        # gate 未释放时 read1 必须已返回首个事件
        self.assertIn(b"response.created", data)
        self.assertFalse(pair.gate.is_set())
        self.assertLess(elapsed, 1.0)
        pair.gate.set()
        # 剩余事件正常读出
        rest = b""
        while True:
            d = resp.read1(4096)
            if not d:
                break
            rest += d
        self.assertIn(b"response.completed", rest)
        pair.a.close()

    def test_read_amt_blocks_until_accumulation(self):
        """对照：read(amt) 会等待凑满。不是收益断言，是证明问题存在。"""
        first = b'event: response.created\ndata: {"type":"a"}\n\n'
        second = b'event: response.completed\ndata: {"type":"b"}\n\n'
        pair = _ChunkedPair()
        resp = pair.start(first, _build_chunk(second))
        pair.first_chunk_sent.wait(timeout=2)
        # 在 gate 未释放时启动 read；0.3s 后放行，避免用例长时间挂起
        threading.Timer(0.3, pair.gate.set).start()
        data = resp.read(4096)
        # 凑满路径最终也能拿到完整数据（等价性由 D2 保证）
        self.assertIn(b"response.created", data)
        self.assertIn(b"response.completed", data)
        pair.a.close()


class TestStreamEquivalence(unittest.TestCase):
    """D2：完整流 byte 不丢/不重/不乱序。"""

    def _collect(self, reader) -> bytes:
        events = [f'event: e{i}\ndata: {{"n":{i}}}\n\n'.encode() for i in range(200)]
        payload = b"".join(events)
        tail = b'event: done\ndata: {}\n\n'
        pair = _ChunkedPair()
        resp = pair.start(payload, _build_chunk(tail))
        pair.first_chunk_sent.wait(timeout=2)
        pair.gate.set()
        out = b""
        while True:
            d = reader(resp)
            if not d:
                break
            out += d
        pair.a.close()
        return out

    def test_read1_stream_equals_read_stream(self):
        by_read = self._collect(lambda r: r.read(4096))
        by_read1 = self._collect(lambda r: r.read1(4096))
        self.assertEqual(len(by_read), len(by_read1))
        self.assertEqual(by_read, by_read1)
        self.assertIn(b"event: done", by_read1)

    def test_read1_multi_chunk_utf8_equivalence(self):
        text = "中文✓" * 100
        events = f'event: delta\ndata: {{"text":"{text}"}}\n\n'.encode()
        pair = _ChunkedPair()
        resp = pair.start(events, _build_chunk(b'event: done\ndata: {}\n\n'))
        pair.first_chunk_sent.wait(timeout=2)
        pair.gate.set()
        out = b""
        while True:
            d = resp.read1(4096)
            if not d:
                break
            out += d
        pair.a.close()
        self.assertIn("中文✓".encode(), out)
        self.assertIn(b"event: done", out)


class TestThroughput(unittest.TestCase):
    """D3：大 payload 读取不退化成逐字节。"""

    def test_read1_large_payload_not_degenerate(self):
        payload = b"x" * (1024 * 1024)
        a, b = socket.socketpair()

        head = (b"HTTP/1.1 200 OK\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n")

        def serve():
            b.sendall(head)
            for i in range(0, len(payload), 65536):
                b.sendall(_build_chunk(payload[i:i + 65536]))
            b.sendall(b"0\r\n\r\n")
            b.close()

        threading.Thread(target=serve, daemon=True).start()
        resp = http.client.HTTPResponse(a, method="POST")
        resp.begin()
        total = 0
        reads = 0
        t0 = time.perf_counter()
        while True:
            d = resp.read1(8192)
            if not d:
                break
            total += len(d)
            reads += 1
        elapsed_ms = (time.perf_counter() - t0) * 1000
        a.close()
        self.assertEqual(total, len(payload))
        # 1MB / 64KB chunks ≈ 16 次读；逐字节会是 ~100 万次。
        # 宽松上限防 flaky：≤256 次（系统调用 batching 正常远低于此）。
        self.assertLessEqual(reads, 256)
        # 本机管道 1MB 应在 1s 内完成（宽防挂）。
        self.assertLess(elapsed_ms, 1000)


class TestHelperFallback(unittest.TestCase):
    """_read_stream_chunk 的 fake/mock 兼容边界。"""

    def test_no_read1_falls_back_to_read(self):
        from core.server import ModelProxyHandler

        class OnlyRead:
            def __init__(self):
                self.calls = 0

            def read(self, amt):
                self.calls += 1
                return b"data"

        r = OnlyRead()
        self.assertEqual(ModelProxyHandler._read_stream_chunk(r, 4096), b"data")
        self.assertEqual(r.calls, 1)

    def test_noncallable_read1_raises(self):
        from core.server import ModelProxyHandler

        class Bad:
            read1 = "not-callable"

        with self.assertRaises(TypeError):
            ModelProxyHandler._read_stream_chunk(Bad(), 4096)

    def test_nonbytes_read1_falls_back(self):
        """MagicMock 自动生成 read1 属性时回退 read，不造成死循环。"""
        from core.server import ModelProxyHandler

        class Mocky:
            def __init__(self):
                self.read_calls = 0

            read1 = lambda self, amt: object()  # noqa: E731 — 故意返回非 bytes

            def read(self, amt):
                self.read_calls += 1
                return b"ok"

        r = Mocky()
        self.assertEqual(ModelProxyHandler._read_stream_chunk(r, 4096), b"ok")
        self.assertEqual(r.read_calls, 1)


if __name__ == "__main__":
    unittest.main()
