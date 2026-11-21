"""CatPaw IDE provider：凭据只读 + Go 桥传输。

职责边界（设计记录 §三）：
- 本模块**不刷新、不登录**。凭据由 mtsso_keeper 独占管理，这里只读一个文件。
- 桥在传输层工作：Anthropic 请求进、Anthropic 响应出。因此返回的
  IdeResponse 可直接替换 urllib 响应对象，复用既有全部写回路径。

凭据文件格式（keeper 产出）：
    {"accessToken","refreshToken","attestation","mis","expire","issuedAt"}
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time

# 剩余寿命低于该阈值即拒绝出站：给 keeper 一次 tick 的窗口，避免边界抖动。
_EXPIRE_MARGIN_SECONDS = 300

# 桥输出缓冲：单次 Next 的上限，也是流式读取的粒度。
_READ_SIZE = 65536
_ERR_SIZE = 1024


class CredentialError(ValueError):
    """凭据缺失/过期/残缺。调用方应 500，且**不进 cooldown**。"""


def read_credential(path: str) -> dict:
    """读取并校验凭据文件；任何问题抛 CredentialError（消息可直接给用户）。"""
    if not path:
        raise CredentialError("未配置 catpaw-ide credential_file")
    real = os.path.expanduser(path)
    try:
        with open(real, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        raise CredentialError(
            f"凭据文件不存在: {real}（请运行 mtsso_keeper 完成一次登录）") from None
    except OSError as e:
        raise CredentialError(f"凭据文件读取失败: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CredentialError(f"凭据文件不是有效 JSON: {e}") from e
    if not isinstance(data, dict):
        raise CredentialError("凭据文件顶层必须是对象")

    for key in ("accessToken", "refreshToken", "attestation", "mis"):
        val = data.get(key)
        if not isinstance(val, str) or not val:
            raise CredentialError(f"凭据文件缺少字段 {key}")

    expire = data.get("expire")
    if not isinstance(expire, (int, float)):
        raise CredentialError("凭据文件缺少数值字段 expire")
    remaining = int(expire) - int(time.time())
    if remaining <= _EXPIRE_MARGIN_SECONDS:
        raise CredentialError(
            f"CatPaw IDE 凭据已过期或即将过期（剩余 {remaining}s）；"
            "请运行 mtsso_keeper 刷新后重试")
    return data


def build_auth(cred: dict) -> dict:
    """映射成 Go 桥的 Auth 结构。id 由 mis 派生，保证同一身份稳定命中会话缓存。"""
    return {
        "key": "catpaw-oidc",
        "metadata": {
            "oidc": {
                "id": f"modelproxy-{cred['mis']}",
                "sharedId": f"modelproxy-{cred['mis']}",
                "accessToken": cred["accessToken"],
                "refreshToken": cred["refreshToken"],
                "attestation": cred["attestation"],
                "mis": cred["mis"],
            },
            "cookieSource": "oidc",
        },
    }


class BridgeError(RuntimeError):
    """桥调用失败（库缺失、启动失败、读取错误）。"""


class UpstreamError(BridgeError):
    """上游返回非 2xx。

    与 BridgeError 区分开：这是**上游的**响应，应当走与 legacy 相同的
    冷却/failover 路径；而凭据/库问题才该快速失败。
    """

    def __init__(self, status: int, body: str):
        super().__init__(f"上游 HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


# Go 桥把上游非 2xx 编码成 `上游 HTTP <code>: <body>`
_UPSTREAM_PREFIX = "上游 HTTP "


def _as_upstream_error(exc: "BridgeError") -> "BridgeError":
    """把桥的 `上游 HTTP N: body` 错误转成 UpstreamError，其余原样返回。"""
    msg = str(exc)
    if not msg.startswith(_UPSTREAM_PREFIX):
        return exc
    head, _, body = msg[len(_UPSTREAM_PREFIX):].partition(": ")
    try:
        status = int(head)
    except ValueError:
        return exc
    return UpstreamError(status, body)


class _Library:
    """惰性加载 c-shared 库；未使用 IDE provider 时不会触发任何加载。"""

    _lock = threading.Lock()
    _lib = None
    _loaded_path = ""

    @classmethod
    def get(cls, path: str):
        with cls._lock:
            if cls._lib is not None and cls._loaded_path == path:
                return cls._lib
            if not path:
                raise BridgeError("未配置 catpaw-ide 桥库路径")
            real = os.path.expanduser(path)
            if not os.path.exists(real):
                raise BridgeError(f"桥库不存在: {real}")
            try:
                lib = ctypes.CDLL(real)
            except OSError as e:
                raise BridgeError(f"桥库加载失败: {e}") from e
            cls._decl(lib)
            version = lib.CatpawBridgeVersion()
            if version != 1:
                raise BridgeError(f"桥 ABI 版本不匹配: 期望 1，实际 {version}")
            cls._lib, cls._loaded_path = lib, real
            return lib

    @staticmethod
    def _decl(lib) -> None:
        lib.CatpawBridgeVersion.restype = ctypes.c_int32
        lib.CatpawStart.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.c_int32]
        lib.CatpawStart.restype = ctypes.c_int64
        lib.CatpawNext.argtypes = [
            ctypes.c_int64, ctypes.c_char_p, ctypes.c_int32,
            ctypes.c_char_p, ctypes.c_int32]
        lib.CatpawNext.restype = ctypes.c_int32
        lib.CatpawCancel.argtypes = [ctypes.c_int64]
        lib.CatpawCancel.restype = None
        lib.CatpawClose.argtypes = [ctypes.c_int64]
        lib.CatpawClose.restype = None


class IdeResponse:
    """伪装 urllib 响应：既有写回路径（透传/转换适配器）无需感知桥的存在。

    read1(n) 每次触发一次 CatpawNext；ctypes 调用会释放 GIL，因此阻塞读取
    不阻塞其他请求线程。
    """

    def __init__(self, lib, handle: int, streaming: bool, model: str = ""):
        self._lib = lib
        self._handle = handle
        self._eof = False
        self._lock = threading.Lock()
        self._closed = False
        self.status = 200
        self._buf = ctypes.create_string_buffer(_READ_SIZE)
        self._err = ctypes.create_string_buffer(_ERR_SIZE)
        # IDE 流式与非流式都返回 Anthropic 形态；非流式给 JSON，流式给 SSE。
        ctype = "text/event-stream" if streaming else "application/json"
        self._headers = [("Content-Type", ctype), ("Cache-Control", "no-cache")]

    # ---- urllib 兼容面 ----
    def getheaders(self):
        return list(self._headers)

    def getheader(self, name, default=None):
        for k, v in self._headers:
            if k.lower() == name.lower():
                return v
        return default

    def read(self, amt=None):
        if self._eof:
            return b""
        chunks = []
        while True:
            piece = self.read1(_READ_SIZE if amt is None else amt)
            if not piece:
                break
            chunks.append(piece)
            if amt is not None and sum(len(c) for c in chunks) >= amt:
                break
        return b"".join(chunks)

    def read1(self, amt=_READ_SIZE):
        if self._eof or self._closed:
            return b""
        with self._lock:
            n = self._lib.CatpawNext(
                self._handle, self._buf, min(int(amt), _READ_SIZE),
                self._err, _ERR_SIZE)
            if n == 0:
                self._eof = True
                return b""
            if n < 0:
                self._eof = True
                msg = self._err.value.decode("utf-8", "replace") or "桥读取失败"
                raise BridgeError(msg)
            return self._buf.raw[:n]

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._lib.CatpawClose(self._handle)
        except Exception:       # 关闭失败不应掩盖原始错误
            pass

    def cancel(self):
        try:
            self._lib.CatpawCancel(self._handle)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def start_request(lib_path: str, cred: dict, body: bytes, conversation: str = "") -> IdeResponse:
    """发起一次 IDE 请求，返回可直接当作 urllib 响应使用的对象。"""
    lib = _Library.get(lib_path)
    auth_json = json.dumps(build_auth(cred), ensure_ascii=False).encode("utf-8")
    err = ctypes.create_string_buffer(_ERR_SIZE)
    handle = lib.CatpawStart(
        auth_json, body, (conversation or "").encode("utf-8"), err, _ERR_SIZE)
    if handle < 0:
        detail = err.value.decode("utf-8", "replace") or "桥启动失败"
        raise _as_upstream_error(BridgeError(detail))
    try:
        streaming = json.loads(body).get("stream") is True
    except Exception:
        streaming = False
    return IdeResponse(lib, handle, streaming)
