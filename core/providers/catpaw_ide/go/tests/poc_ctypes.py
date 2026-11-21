"""离线 PoC：Python ctypes 调用真实 CatPaw IDE 薄桥（假上游，不出网）。

用法：POC_FAKE=1 python3 tests/poc_ctypes.py /tmp/libcatpaw_poc_fake.dylib
"""
import ctypes
import json
import os
import sys

AUTH = {
    "key": "",
    "metadata": {
        "oidc": {
            "id": "u1",
            "accessToken": "at",
            "refreshToken": "rt",
            "attestation": "a",
            "mis": "mis1",
        }
    },
}
BODY = {
    "model": "gpt-6.2-sol",
    "max_tokens": 64,
    "stream": True,
    "messages": [{"role": "user", "content": "hi"}],
}


def main(path):
    lib = ctypes.CDLL(path)
    lib.CatpawBridgeVersion.restype = ctypes.c_int32

    lib.CatpawStart.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_char_p, ctypes.c_int32,
    ]
    lib.CatpawStart.restype = ctypes.c_int64
    lib.CatpawNext.argtypes = [
        ctypes.c_int64, ctypes.c_char_p, ctypes.c_int32,
        ctypes.c_char_p, ctypes.c_int32,
    ]
    lib.CatpawNext.restype = ctypes.c_int32
    lib.CatpawCancel.argtypes = [ctypes.c_int64]
    lib.CatpawClose.argtypes = [ctypes.c_int64]

    version = lib.CatpawBridgeVersion()
    assert version == 1, f"ABI 版本不匹配: {version}"

    err = ctypes.create_string_buffer(1024)
    handle = lib.CatpawStart(
        json.dumps(AUTH).encode(), json.dumps(BODY).encode(), b"conv-1", err, 1024
    )
    if handle < 0:
        raise SystemExit(f"Start 失败: {err.value.decode()}")

    out = []
    buf = ctypes.create_string_buffer(65536)
    try:
        while True:
            n = lib.CatpawNext(handle, buf, 65536, err, 1024)
            if n == 0:
                break
            if n < 0:
                raise SystemExit(f"Next 失败: {err.value.decode()}")
            out.append(buf.raw[:n])
        text = b"".join(out).decode()
    finally:
        lib.CatpawClose(handle)

    for want in (
        "event: message_start",
        "event: content_block_delta",
        "event: message_delta",
        "event: message_stop",
        '"stop_reason":"end_turn"',
        '"input_tokens":11',
        '"output_tokens":3',
    ):
        assert want in text, f"缺少 {want}\n---\n{text}"

    # 正文按增量下发，累加后必须与上游累积快照一致。
    chunks = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            chunks.append(delta.get("text", ""))
    assert "".join(chunks) == "Hello CatPaw", f"正文被改写: {chunks}"
    print("OK: ctypes → Go 薄桥 → Anthropic SSE 全链路通过")
    print(f"事件数: {text.count('event: ')}  字节: {len(text)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/libcatpaw_poc_fake.dylib")
