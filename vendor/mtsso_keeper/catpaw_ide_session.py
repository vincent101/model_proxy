#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""catpaw_ide_session.py — CatPaw IDE (OIDC) 凭据的登录与刷新

定位：与 CIBA 段、mcli 段平级的**第三类凭据**，由 keeper 独占管理；
model_proxy 只读本模块产出的 credential_file，不刷新、不登录。

与 mcli_token.py 的根本差异（不可复用其代码路径）：
  - mcli：客户端自造 uuid，服务端只"激活"，无签发秘密
  - IDE ：真 OAuth。access/refresh/attestation 三要素，刷新需三头

实测事实：access_token 寿命为 72h；正常有效期内只检查，不刷新。
上游提前返回 401/403 时按异常限速处理；寿命以 whoami 为准。
换 token 走 GET /websso/oauth/pkce（非标准 /token）。

用法：
  python3 catpaw_ide_session.py --check           # 只读健康检查
  python3 catpaw_ide_session.py                   # ensure：正常到期后刷新
  python3 catpaw_ide_session.py --force           # 兼容参数，不绕过门控
  python3 catpaw_ide_session.py --login           # 交互式重新登录（浏览器）

退出码：0 成功或已有效 / 1 刷新或校验失败 / 3 需人工登录 / 4 上游契约变更
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import math
import os
import subprocess
from dataclasses import dataclass
import secrets
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import mtsso_core as core  # noqa: E402
import file_lock  # noqa: E402

# ── 常量（已实测）────────────────────────────────────────────────────
SSO = "https://ssosv.sankuai.com"
CLIENT_ID = "2f73f89b68"
OIDC_PARAMS = {"client_id": CLIENT_ID, "mode": "cli", "loginProtocol": "OIDC"}

CRED_FILE = "~/.mtsso_keeper/catpaw_ide_session.json"
CALLBACK_PORTS = (8576, 9576)      # 与 IDE / Catboss 共用；占用即失败，不接管
HTTP_TIMEOUT = 20

# IDE 专用状态与互斥，不与 CIBA/mcli 的 state.json 共写。
STATUS_FILE = "~/.mtsso_keeper/catpaw_ide_status.json"
PENDING_FILE = "~/.mtsso_keeper/catpaw_ide_pending.json"
LOCK_FILE = "~/.mtsso_keeper/catpaw_ide.lock"
BACKUP_DIR = "~/.mtsso_keeper/backups/catpaw_ide"
PROBE_INTERVAL = 6 * 3600
RETRY_DELAYS = (60, 120, 240, 480, 900)
ALERT_AFTER = 300
BUDGET_SECONDS = 1800
EARLY_POST_INTERVAL = 900
NOTIFY_INTERVAL = 1800
# attestation 剩余低于该值报 WARN
ATTEST_WARN_DAYS = 2

EXIT_OK, EXIT_FAIL, EXIT_HUMAN, EXIT_UPSTREAM = 0, 1, 3, 4


class UpstreamChanged(Exception):
    """上游契约与实测快照不符（字段缺失/形态变化）。"""


class AuthExpired(PermissionError):
    """上游 whoami 明确返回 401/403。"""


class RefreshIndeterminate(Exception):
    """无法确认上游是否已签发，禁止盲目重试。"""


class GenerationConflict(Exception):
    """提交前主凭据被其他写入者替换。"""


@dataclass(frozen=True)
class CheckResult:
    verdict: str
    reason: str = ""
    old_expire: float | None = None
    new_expire: float | None = None
    old_issued_at: float | None = None
    new_issued_at: float | None = None
    remaining: float | None = None
    generation: str = ""


@dataclass(frozen=True)
class RecoveryProgress:
    generation: str = ""
    expire: float | None = None
    pending: bool = False
    pending_valid: bool = False
    pending_related: bool = False
    first_unavailable_at: float | None = None
    next_deadline: float | None = None
    budget_deadline: float | None = None
    early_invalid: bool = False
    event_id: str = ""
    stop_reason: str = ""
    verdict: str = ""
    post_in_flight: bool = False
    early_post_allowed_at: float | None = None


@dataclass(frozen=True)
class ObserveResult:
    verdict: str
    code: int
    reason: str = ""
    expire: float | None = None
    remaining: float | None = None
    generation: str = ""
    progress: RecoveryProgress | None = None


@dataclass(frozen=True)
class EnsureResult:
    verdict: str
    code: int
    reason: str = ""
    old_expire: float | None = None
    new_expire: float | None = None
    old_issued_at: float | None = None
    new_issued_at: float | None = None
    remaining: float | None = None
    generation: str = ""


# ── 网络 ─────────────────────────────────────────────────────────────
def _v(verbose: bool, msg: str) -> None:
    if verbose:
        print(f"[ide] {msg}", file=sys.stderr)


def _get(url: str, headers: dict | None = None, timeout: int = HTTP_TIMEOUT) -> tuple:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url: str, headers: dict | None = None, data: bytes = b"",
          timeout: int = HTTP_TIMEOUT) -> tuple:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _unwrap(body: bytes) -> dict:
    """统一解包 {code,data} 或裸对象。"""
    try:
        v = json.loads(body)
    except ValueError:
        raise UpstreamChanged("响应不是有效 JSON") from None
    if not isinstance(v, dict):
        raise UpstreamChanged("响应顶层不是对象")
    if isinstance(v.get("data"), dict):
        return v["data"]
    return v


def whoami(access_token: str) -> dict:
    """身份 + 寿命。这是唯一无副作用的续期判据来源。"""
    st, body = _get(f"{SSO}/websso/whoami?{urllib.parse.urlencode(OIDC_PARAMS)}",
                    {"Authorization": "Bearer " + access_token})
    if st in (401, 403):
        raise AuthExpired(f"whoami HTTP {st}")
    if st >= 500:
        raise OSError(f"whoami HTTP {st}")
    if st != 200:
        raise UpstreamChanged(f"whoami HTTP {st}")
    d = _unwrap(body)
    if d.get("introspectionResultEnum") != "SUCCESS":
        raise UpstreamChanged("whoami 非 SUCCESS，过期语义未知")
    for key in ("subject", "expire", "issuedAt"):
        if key not in d:
            raise UpstreamChanged(f"whoami 缺少 {key}（契约可能变化）")
    return d


def refresh(access_token: str, refresh_token: str, attestation: str) -> dict:
    """三头刷新。返回新的三要素 + 身份。"""
    h = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + access_token,
        "Authorization-Refresh": "Bearer " + refresh_token,
        "Authorization-Attestation": "Bearer " + attestation,
    }
    try:
        st, body = _post(f"{SSO}/websso/token/get?{urllib.parse.urlencode(OIDC_PARAMS)}", h)
    except (OSError, TimeoutError) as exc:
        raise RefreshIndeterminate("POST 结果不确定") from exc
    if st in (400, 401, 403):
        raise PermissionError("刷新被拒，需要人工登录")
    if st != 200:
        raise RefreshIndeterminate(f"token/get HTTP {st}，签发状态未知")
    d = _unwrap(body)
    for key in ("access_token", "refresh_token"):
        if not d.get(key):
            raise UpstreamChanged(f"刷新响应缺少 {key}")
    return d


# ── 凭据文件 ─────────────────────────────────────────────────────────
def read_cred(path: str = CRED_FILE) -> dict | None:
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def write_cred(cred: dict, path: str = CRED_FILE) -> None:
    """兼容入口：取得 IDE 独占锁后提交。"""
    os.makedirs(os.path.dirname(os.path.expanduser(LOCK_FILE)), mode=0o700, exist_ok=True)
    handle = file_lock.try_acquire(os.path.expanduser(LOCK_FILE))
    if handle is None:
        raise RuntimeError("IDE 锁忙")
    _HELD_IDE_LOCK.handle = handle
    try:
        current = read_cred(path)
        _commit_main_cred(handle, path, _generation(current) if _valid_cred(current) else "", cred)
    finally:
        _HELD_IDE_LOCK.handle = None
        file_lock.release(handle)


def _backup(path: str) -> str:
    """严格备份；备份失败不得刷新或覆盖目标。"""
    saved = core.backup_file(os.path.expanduser(path), os.path.expanduser(BACKUP_DIR))
    if not saved or not os.path.isfile(saved):
        raise OSError("备份失败")
    os.chmod(saved, 0o600)
    # 通用备份仅按秒命名；同一事务多次备份时保留每一个快照。
    unique = saved + "." + secrets.token_hex(8)
    os.replace(saved, unique)
    with open(unique, "rb") as backup:
        os.fsync(backup.fileno())
    _sync_dir(unique)
    return unique


def attestation_remaining_days(attestation: str) -> float | None:
    """attestation 是 JWT（实测），可本地解 exp；不是 JWT 时返回 None。"""
    parts = (attestation or "").split(".")
    if len(parts) != 3:
        return None
    try:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            return (exp - time.time()) / 86400
    except Exception:
        pass
    return None


# ── 交互式登录 ───────────────────────────────────────────────────────
class _Callback(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/wait-for-auth":
            # IDE 侧探活兼容位；本模块不依赖，但保持应答一致
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(
                {"code": 1011, "data": {"clientId": CLIENT_ID}}).encode())
            return
        if u.path != "/websso/oauth/callback":
            self.send_response(404)
            self.end_headers()
            return
        state = (q.get("state") or [""])[0]
        code = (q.get("code") or [""])[0]
        if state != self.server.expect_state or not code:
            self.send_response(400)
            self.end_headers()
            self.wfile.write("state/code 校验失败".encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("登录完成，可以关闭此页。".encode())
        self.server.result = code
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def login(verbose: bool = False) -> dict:
    """按需启动本地回调并完成一次授权码 + PKCE 登录。"""
    verifier = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(16)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    srv = None
    for port in CALLBACK_PORTS:
        try:
            srv = socketserver.TCPServer(("127.0.0.1", port), _Callback)
            chosen = port
            break
        except OSError:
            _v(verbose, f"端口 {port} 被占用")
    if srv is None:
        raise RuntimeError(
            f"回调端口 {'/'.join(map(str, CALLBACK_PORTS))} 均被占用；"
            "请先关闭其他登录流程（IDE / Catboss）后重试")
    srv.expect_state, srv.result = state, None

    q = dict(OIDC_PARAMS)
    q.update({
        "response_type": "code", "scope": "profile", "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
        "redirect_uri": f"http://localhost:{chosen}/websso/oauth/callback?original-url=%2F",
    })
    url = f"{SSO}/sson/auth/oidc/v1/authorize?{urllib.parse.urlencode(q)}"
    print("请在浏览器打开以下链接完成 SSO（5 分钟内有效）：\n", file=sys.stderr)
    print(url + "\n", file=sys.stderr)

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deadline = time.time() + 300
    while srv.result is None and time.time() < deadline:
        time.sleep(0.3)
    srv.shutdown()
    srv.server_close()
    if not srv.result:
        raise TimeoutError("等待回调超时")

    eq = dict(OIDC_PARAMS)
    eq.update({"code": srv.result, "state": state,
               "code_verifier": verifier, "original-url": "/"})
    st, body = _get(f"{SSO}/websso/oauth/pkce?{urllib.parse.urlencode(eq)}")
    if st != 200:
        raise UpstreamChanged(f"pkce HTTP {st}")
    d = _unwrap(body)
    for key in ("access_token", "refresh_token", "token_exchange_attestation"):
        if not d.get(key):
            raise UpstreamChanged(f"登录响应缺少 {key}")
    return {
        "accessToken": d["access_token"],
        "refreshToken": d["refresh_token"],
        "attestation": d["token_exchange_attestation"],
    }


# ── ensure / check ───────────────────────────────────────────────────
_FIELDS = ("accessToken", "refreshToken", "attestation", "mis", "expire", "issuedAt", "updatedAt")
_SECRET_FIELDS = ("accessToken", "refreshToken", "attestation")
_HELD_IDE_LOCK = threading.local()


def _time_value(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 < value < 100_000_000_000)


def _valid_cred(cred) -> bool:
    return (isinstance(cred, dict)
            and all(isinstance(cred.get(k), str) and cred[k] for k in (*_SECRET_FIELDS, "mis"))
            and all(_time_value(cred.get(k)) for k in ("expire", "issuedAt"))
            and ("updatedAt" not in cred or _time_value(cred["updatedAt"]))
            and cred["issuedAt"] <= cred["expire"])


def _valid_whoami(w) -> bool:
    return (isinstance(w, dict) and isinstance(w.get("subject"), str) and bool(w["subject"])
            and _time_value(w.get("expire")) and _time_value(w.get("issuedAt"))
            and w["issuedAt"] <= w["expire"])


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generation(cred: dict) -> str:
    raw = json.dumps([cred[k] for k in _SECRET_FIELDS], ensure_ascii=False,
                     separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: str):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_status() -> dict:
    obj = _read_json(STATUS_FILE)
    return obj if isinstance(obj, dict) else {}


def _sync_dir(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(os.path.expanduser(path)))
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_file(path: str) -> None:
    with open(os.path.expanduser(path), "rb") as f:
        os.fsync(f.fileno())
    _sync_dir(path)


def _atomic_json(path: str, obj, expected_current=None) -> None:
    target = os.path.expanduser(path)
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    tmp = target + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if expected_current is not None and not expected_current(_read_json(path)):
            raise GenerationConflict("凭据已变化")
        os.replace(tmp, target)
        _sync_dir(target)
    finally:
        if fd != -1:
            os.close(fd)


def _save_status(st: dict) -> None:
    _atomic_json(STATUS_FILE, st)
    if _read_json(STATUS_FILE) != st:
        raise OSError("状态回读失败")


def _notify(reason: str) -> bool:
    labels = {
        "early_invalid": "上游提前失效",
        "rotation_only": "凭据轮换但到期时间未前移",
        "unavailable_too_long": "凭据过期超过五分钟仍未恢复",
        "need_human": "凭据需要人工处理",
        "upstream_changed": "上游凭据契约变化",
        "commit_failed": "凭据保存失败",
        "refresh_indeterminate": "刷新结果不确定",
        "identity_mismatch": "凭据身份不一致",
    }
    text = labels.get(reason)
    if not text:
        return False
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e",
                                 f'display notification "{text}" with title "CatPaw IDE"'],
                                capture_output=True, timeout=3, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _event(st: dict, kind: str, now: float, generation: str) -> dict:
    active = st.setdefault("active_events", {})
    events = st.setdefault("events", {})
    event_id = active.get(kind)
    event = events.get(event_id) if event_id else None
    if not isinstance(event, dict) or event.get("recovered_at") is not None:
        event_id = secrets.token_hex(16)
        event = {"id": event_id, "kind": kind, "generation": generation, "started_at": now}
        events[event_id] = event
        active[kind] = event_id
    return event


def _resolve_events(st: dict, now: float) -> None:
    for event_id in st.get("active_events", {}).values():
        event = st.get("events", {}).get(event_id)
        if event is not None:
            event["recovered_at"] = now
    st["active_events"] = {}


def _alert(st: dict, reason: str, now: float, generation: str) -> None:
    event = _event(st, reason, now, generation)
    if reason == "early_invalid":
        event.setdefault("verdict", "expired")
    st["alert"] = True
    last = event.get("last_notified_at")
    if last is None or now - last >= NOTIFY_INTERVAL:
        event["last_notified_at"] = now
        try:
            delivered = _notify(reason)
        except Exception:  # 通知失败不得中断凭据事务
            delivered = False
        if not delivered:
            event["notification_delivery_failed"] = True
            print("[ide] notification_delivery_failed", file=sys.stderr)


def _clear_recovery(st: dict, now: float) -> None:
    _resolve_events(st, now)
    st.pop("early_invalid", None)
    st.pop("early_recovered", None)
    st.pop("last_early_post_at", None)
    st.pop("first_unavailable_at", None)
    st.pop("stop_reason", None)
    st.update(recovered_at=now, alert=False, fail_count=0, next_retry_at=0)


def _retry(st: dict, now: float) -> None:
    st["fail_count"] = int(st.get("fail_count", 0)) + 1
    st["next_retry_at"] = now + RETRY_DELAYS[min(st["fail_count"] - 1, len(RETRY_DELAYS) - 1)]


def _result(verdict: str, code: int, st: dict, reason: str = "") -> EnsureResult:
    return EnsureResult(verdict, code, reason, st.get("old_expire"), st.get("new_expire"),
                        st.get("old_issued_at"), st.get("new_issued_at"),
                        st.get("remaining"), st.get("generation", ""))


def _finish(st: dict, verdict: str, code: int, reason: str = "") -> EnsureResult:
    if st.get("stop_reason") == "auto_budget_exhausted" and verdict not in ("renewed", "healthy"):
        verdict, code, reason = "need_human", EXIT_HUMAN, "auto_budget_exhausted"
    if (st.get("stop_reason") == "auto_budget_exhausted" and
            verdict == "healthy" and st.get("active_events", {}).get("rotation_only")):
        verdict, code, reason = "need_human", EXIT_HUMAN, "auto_budget_exhausted"
    if verdict == "retry_wait":
        # 等待是调度动作，不改变未完成事件的 verdict。
        st["last_run"] = time.time()
        _save_status(st)
        return _result(verdict, code, st, reason)
    active_rotation = st.get("active_events", {}).get("rotation_only")
    if active_rotation and verdict == "transient_error":
        _event(st, verdict, time.time(), st.get("generation", "")).update(verdict=verdict, reason=reason)
        verdict, code, reason = "rotation_only", EXIT_FAIL, "expire_not_advanced"
    if verdict == "healthy" and st.get("active_events", {}).get("rotation_only"):
        verdict, code, reason = "rotation_only", EXIT_FAIL, "expire_not_advanced"
    st["last_run"] = time.time()
    st["verdict"], st["reason"] = verdict, reason
    if verdict in ("rotation_only", "pending_verify", "transient_error"):
        event = _event(st, verdict, st["last_run"], st.get("generation", ""))
        event.update(verdict=verdict, reason=reason)
    if verdict in ("pending_verify", "transient_error", "rotation_only") and st.get("first_unavailable_at") is not None:
        event = _event(st, "unavailable", st["first_unavailable_at"], st.get("generation", ""))
        event.update(verdict=verdict, reason=reason)
    _save_status(st)
    return _result(verdict, code, st, reason)


def _fail(st: dict, verdict: str, code: int, reason: str, now: float,
          alert: str | None = None, stop: bool = False, retry: bool = False) -> EnsureResult:
    if retry:
        _retry(st, now)
    if stop:
        st["stop_reason"] = reason
    if alert:
        _alert(st, alert, now, st.get("generation", ""))
    return _finish(st, verdict, code, reason)


def _stop_for_budget(st: dict, now: float) -> EnsureResult:
    """预算到期不取代既有不可恢复停止原因。"""
    if st.get("stop_reason") and st["stop_reason"] != "auto_budget_exhausted":
        verdict = st.get("verdict", "need_human")
        return _finish(st, verdict, EXIT_FAIL if verdict in ("refresh_indeterminate", "commit_failed")
                       else EXIT_HUMAN, st["stop_reason"])
    return _fail(st, "need_human", EXIT_HUMAN, "auto_budget_exhausted", now, "need_human", True)


def check(path: str = CRED_FILE, verbose: bool = False) -> CheckResult:
    """纯只读检查；401/403 单独区分，其他拒绝不猜测为过期。"""
    del verbose
    cred = read_cred(path)
    if cred is None:
        return CheckResult("missing" if not os.path.exists(os.path.expanduser(path)) else "invalid_schema")
    if not _valid_cred(cred):
        return CheckResult("invalid_schema")
    now = time.time()
    base = dict(old_expire=cred["expire"], old_issued_at=cred["issuedAt"],
                remaining=cred["expire"] - now, generation=_generation(cred))
    try:
        w = whoami(cred["accessToken"])
    except AuthExpired:
        return CheckResult("expired", "upstream_unauthorized", **base)
    except PermissionError:
        return CheckResult("access_denied", "access_denied", **base)
    except UpstreamChanged:
        return CheckResult("upstream_changed", "whoami_contract", **base)
    except (OSError, TimeoutError):
        return CheckResult("transient_error", "whoami_network", **base)
    if not _valid_whoami(w):
        return CheckResult("upstream_changed", "whoami_schema", **base)
    base.update(new_expire=w["expire"], new_issued_at=w["issuedAt"], remaining=w["expire"] - now)
    if w["subject"] != cred["mis"]:
        return CheckResult("identity_mismatch", "identity_mismatch", **base)
    return CheckResult("healthy" if w["expire"] > now else "expired", "whoami", **base)


def _checked_write(path: str, obj: dict, validator, *, baseline: bool = False,
                   expected_current=None) -> None:
    _assert_ide_lock(getattr(_HELD_IDE_LOCK, "handle", None))
    if validator not in (_valid_pending, _valid_cred):
        raise ValueError("不支持的写入类型")
    if validator is _valid_pending and os.path.realpath(os.path.expanduser(path)) != os.path.realpath(os.path.expanduser(PENDING_FILE)):
        raise ValueError("候选写入路径错误")
    if validator is _valid_cred and not getattr(_HELD_IDE_LOCK, "committing_main", False):
        raise RuntimeError("主凭据必须经主写入入口")
    if not validator(obj):
        raise ValueError("schema 错误")
    target = os.path.expanduser(path)
    if os.path.exists(target):
        old = _read_json(path)
        if not validator(old):
            raise ValueError("目标 schema 错误")
        _backup(path)
    elif not baseline:
        raise OSError("缺少备份基线")
    _atomic_json(path, obj, expected_current)
    if _read_json(path) != obj or os.stat(target).st_mode & 0o077:
        raise OSError("完整回读或权限校验失败")


def _assert_ide_lock(handle) -> None:
    lock_path = os.path.expanduser(LOCK_FILE)
    if (handle is None or handle is not getattr(_HELD_IDE_LOCK, "handle", None) or handle.closed or
            os.path.realpath(handle.name) != os.path.realpath(lock_path) or
            os.fstat(handle.fileno()).st_ino != os.stat(lock_path).st_ino or
            os.fstat(handle.fileno()).st_dev != os.stat(lock_path).st_dev):
        raise RuntimeError("未持有 IDE 锁")


def _commit_main_cred(handle, path: str, expected_generation: str, cred: dict) -> None:
    """主凭据唯一写入口；keeper 独占写入，消费者只读，所有写入方持同一文件锁。

    锁内重读不符即中止；不是对不遵守锁的外部写入者的原子 CAS。
    """
    _assert_ide_lock(handle)
    if not _valid_cred(cred):
        raise ValueError("主凭据 schema 错误")
    current = read_cred(path)
    if current is None:
        if expected_generation or os.path.exists(os.path.expanduser(path)):
            raise GenerationConflict("凭据代际变化")
    elif not _valid_cred(current) or _generation(current) != expected_generation:
        raise GenerationConflict("凭据代际变化")
    if current == cred and not os.stat(os.path.expanduser(path)).st_mode & 0o077:
        _sync_file(path)
        latest = read_cred(path)
        if not _valid_cred(latest) or _generation(latest) != expected_generation:
            raise GenerationConflict("凭据代际变化")
        if latest != cred:
            raise OSError("凭据回读失败")
        return
    _HELD_IDE_LOCK.committing_main = True
    try:
        _checked_write(path, cred, _valid_cred, baseline=True,
                       expected_current=lambda value: value == current)
    finally:
        _HELD_IDE_LOCK.committing_main = False


def _valid_pending(p) -> bool:
    if not isinstance(p, dict) or p.get("phase") != "received" or not isinstance(p.get("path"), str):
        return False
    if p.get("operation") == "login":
        return (isinstance(p.get("candidate"), dict)
                and all(isinstance(p["candidate"].get(k), str) and p["candidate"][k] for k in _SECRET_FIELDS)
                and isinstance(p.get("old_generation"), str)
                and (p.get("expected_mis") is None or
                     isinstance(p.get("expected_mis"), str) and bool(p["expected_mis"])))
    return (_valid_cred(p.get("candidate")) and _time_value(p.get("old_expire"))
            and _time_value(p.get("old_issued_at")) and isinstance(p.get("old_generation"), str)
            and bool(p["old_generation"]) and isinstance(p.get("baseline"), str) and bool(p["baseline"]))


def _record_earlier_unavailable(st: dict, expire: float, now: float) -> None:
    """可信到期时间只能收紧已开始的不可用预算。"""
    if expire <= now:
        first = st.get("first_unavailable_at")
        st["first_unavailable_at"] = min(first, expire) if _time_value(first) else expire


def _apply_verified_lifetime(st: dict, path: str, cred: dict, checked: CheckResult,
                             now: float, handle) -> None:
    """可信 whoami 元数据先持锁提交回读，调用方之后才能判停止/告警。"""
    if checked.new_expire != cred["expire"] or checked.new_issued_at != cred["issuedAt"]:
        _commit_main_cred(handle, path, _generation(cred),
                          dict(cred, expire=checked.new_expire, issuedAt=checked.new_issued_at))
    elif os.stat(os.path.expanduser(path)).st_mode & 0o077:
        _commit_main_cred(handle, path, _generation(cred), cred)
    st.update(new_expire=checked.new_expire, new_issued_at=checked.new_issued_at,
              remaining=checked.remaining)


def _resolve_unavailable(st: dict, now: float) -> None:
    """结束不可用预算，保留 rotation_only 历史结论和告警。"""
    for kind in ("unavailable", "unavailable_too_long", "transient_error", "early_invalid"):
        event_id = st.get("active_events", {}).pop(kind, None)
        if event_id and event_id in st.get("events", {}):
            st["events"][event_id]["recovered_at"] = now
    st.pop("first_unavailable_at", None)
    if st.get("stop_reason") == "auto_budget_exhausted":
        st.pop("stop_reason", None)
        event_id = st.get("active_events", {}).pop("need_human", None)
        if event_id and event_id in st.get("events", {}):
            st["events"][event_id]["recovered_at"] = now
    st.pop("early_invalid", None)
    st.pop("early_recovered", None)
    st.pop("last_early_post_at", None)
    st.update(recovered_at=now, fail_count=0, next_retry_at=0)


def _recover_login(st: dict, path: str, p: dict, now: float, handle) -> EnsureResult:
    candidate = p["candidate"]
    try:
        w = whoami(candidate["accessToken"])
    except (OSError, TimeoutError, AuthExpired):
        return _fail(st, "pending_verify", EXIT_FAIL, "login_unverified", now, retry=True)
    except PermissionError:
        return _fail(st, "need_human", EXIT_HUMAN, "login_denied", now, "need_human", True)
    except UpstreamChanged:
        return _fail(st, "upstream_changed", EXIT_UPSTREAM, "login_contract", now, "upstream_changed", True)
    if not _valid_whoami(w):
        return _fail(st, "upstream_changed", EXIT_UPSTREAM, "login_schema", now, "upstream_changed", True)
    if p.get("expected_mis") and w["subject"] != p["expected_mis"]:
        return _fail(st, "need_human", EXIT_HUMAN, "login_identity_mismatch", now, "identity_mismatch", True)
    if w["expire"] <= now:
        return _fail(st, "pending_verify", EXIT_FAIL, "login_expired", now, retry=True)
    new = {k: candidate[k] for k in _SECRET_FIELDS}
    new.update(mis=w["subject"], expire=w["expire"], issuedAt=w["issuedAt"], updatedAt=int(now))
    try:
        latest = read_cred(path)
        if latest is None:
            allowed = not p["old_generation"] and not os.path.exists(os.path.expanduser(path))
        else:
            allowed = _valid_cred(latest) and _generation(latest) in (p["old_generation"], _generation(candidate))
        if not allowed:
            raise GenerationConflict("凭据已变化")
        _commit_main_cred(handle, path, _generation(latest) if latest else "", new)
        # 人工登录成功建立新代际，清除旧事务、事件及告警进展。
        st.clear()
        st.update(generation=_generation(new), last_probe=now, fail_count=0, next_retry_at=0,
                  alert=False, new_expire=new["expire"], new_issued_at=new["issuedAt"])
        result = _finish(st, "healthy", EXIT_OK, "login")
        os.unlink(os.path.expanduser(PENDING_FILE))
        return result
    except GenerationConflict:
        return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", now, "need_human", True)
    except (OSError, ValueError):
        return _fail(st, "commit_failed", EXIT_FAIL, "login_commit", now, "commit_failed", retry=True)


def _recover_pending(st: dict, path: str, now: float, handle) -> EnsureResult:
    p = _read_json(PENDING_FILE)
    if not _valid_pending(p):
        return _fail(st, "commit_failed", EXIT_FAIL, "pending_schema", now, "commit_failed", True)
    if p["path"] != os.path.abspath(os.path.expanduser(path)):
        return _fail(st, "need_human", EXIT_HUMAN, "pending_path_mismatch", now, "need_human", True)
    if p.get("operation") == "login":
        return _recover_login(st, path, p, now, handle)
    candidate = p["candidate"]
    current = read_cred(path)
    if not _valid_cred(current):
        return _fail(st, "commit_failed", EXIT_FAIL, "credential_schema", now, "commit_failed", True)
    if _generation(current) not in (p["old_generation"], _generation(candidate)):
        return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", now, "need_human", True)
    st.update(old_expire=p["old_expire"], old_issued_at=p["old_issued_at"])
    try:
        w = whoami(candidate["accessToken"])
    except (OSError, TimeoutError, AuthExpired):
        return _fail(st, "pending_verify", EXIT_FAIL, "candidate_unverified", now, retry=True)
    except PermissionError:
        return _fail(st, "need_human", EXIT_HUMAN, "candidate_denied", now, "need_human", True)
    except UpstreamChanged:
        return _fail(st, "upstream_changed", EXIT_UPSTREAM, "candidate_contract", now, "upstream_changed", True)
    if not _valid_whoami(w):
        return _fail(st, "upstream_changed", EXIT_UPSTREAM, "candidate_schema", now, "upstream_changed", True)
    if w["subject"] != candidate["mis"]:
        return _fail(st, "need_human", EXIT_HUMAN, "identity_mismatch", now, "identity_mismatch", True)
    new = dict(candidate, expire=w["expire"], issuedAt=w["issuedAt"])
    try:
        latest = read_cred(path)
        if not _valid_cred(latest) or _generation(latest) not in (p["old_generation"], _generation(candidate)):
            raise GenerationConflict("凭据已变化")
        _commit_main_cred(handle, path, _generation(latest), new)
    except GenerationConflict:
        return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", now, "need_human", True)
    except (OSError, ValueError):
        return _fail(st, "commit_failed", EXIT_FAIL, "credential_commit", now, "commit_failed", retry=True)
    now = time.time()
    if (st.get("first_unavailable_at") is not None
            and now - st["first_unavailable_at"] >= ALERT_AFTER):
        _alert(st, "unavailable_too_long", now, st.get("generation", ""))
    st.update(generation=_generation(new), new_expire=new["expire"], new_issued_at=new["issuedAt"],
              remaining=new["expire"] - now, last_probe=now, post_in_flight=False)
    if new["expire"] > p["old_expire"] and new["expire"] > now:
        _resolve_events(st, now)
        st.update(fail_count=0, next_retry_at=0, alert=False, recovered_at=now,
                  gap_s=max(0, now - p["old_expire"]))
        if st.get("early_invalid"):
            st["early_recovered"] = True
            st.pop("last_early_post_at", None)
        st.pop("first_unavailable_at", None)
        st.pop("stop_reason", None)
        result = _finish(st, "renewed", EXIT_OK, "renewed")
    elif (st.get("first_unavailable_at") is not None and
            now - st["first_unavailable_at"] >= BUDGET_SECONDS):
        return _stop_for_budget(st, now)
    elif new["expire"] <= p["old_expire"]:
        _retry(st, now)
        _alert(st, "rotation_only", now, st["generation"])
        result = _finish(st, "rotation_only", EXIT_FAIL, "expire_not_advanced")
    else:
        _retry(st, now)
        result = _finish(st, "pending_verify", EXIT_FAIL, "new_expire_not_valid")
        return result
    try:
        os.unlink(os.path.expanduser(PENDING_FILE))
    except OSError:
        return _fail(st, "commit_failed", EXIT_FAIL, "pending_cleanup", now, "commit_failed", retry=True)
    return result


def _discard_stale_pending(st: dict, path: str, p: dict, now: float, handle) -> EnsureResult | None:
    current = read_cred(path)
    if not _valid_cred(current):
        return None
    expected = p.get("expected_mis") if p.get("operation") == "login" else p["candidate"]["mis"]
    if expected and current["mis"] != expected:
        return None
    old_rt_digest = p.get("old_rt_digest")
    if not old_rt_digest:
        # 首次登录无旧 RT；旧格式无法确认时必须保留候选。
        if p.get("operation") == "login" and not p.get("old_generation"):
            old_rt_digest = _token_digest("")
        else:
            baseline = _read_json(p.get("baseline", "")) if p.get("baseline") else None
            if not _valid_cred(baseline):
                return None
            old_rt_digest = _token_digest(baseline["refreshToken"])
    if _token_digest(current["refreshToken"]) == old_rt_digest:
        # 旧 RT 仍在：继续处理候选；正常的代际冲突检查会保护外部修改。
        return None
    # 已明确转人工的契约冲突不得在下一 tick 自动清候选。
    if st.get("stop_reason") == "generation_conflict":
        return _finish(st, "need_human", EXIT_HUMAN, "generation_conflict")
    # 候选 RT 相同不代表候选 AT 已验证；三要素有差异时保留双方并转人工。
    if _token_digest(current["refreshToken"]) == _token_digest(p["candidate"]["refreshToken"]):
        if _generation(current) == _generation(p["candidate"]):
            return None
        return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", now, "need_human", True)
    verified = check(path)
    if verified.verdict != "healthy":
        return None
    try:
        _backup(PENDING_FILE)
        latest = read_cred(path)
        if latest != current:
            raise GenerationConflict("凭据已变化")
        if verified.new_expire != current["expire"] or verified.new_issued_at != current["issuedAt"]:
            updated = dict(current, expire=verified.new_expire, issuedAt=verified.new_issued_at,
                           updatedAt=int(now))
            _commit_main_cred(handle, path, _generation(current), updated)
            current = updated
        else:
            _commit_main_cred(handle, path, _generation(current), current)
        if read_cred(path) != current:
            raise GenerationConflict("凭据已变化")
        _resolve_events(st, now)
        st.update(generation=_generation(current), last_probe=now, alert=False,
                  fail_count=0, recovered_at=now, new_expire=verified.new_expire,
                  new_issued_at=verified.new_issued_at)
        st.pop("stop_reason", None)
        st.pop("first_unavailable_at", None)
        result = _finish(st, "healthy", EXIT_OK, "new_generation")
        os.unlink(os.path.expanduser(PENDING_FILE))
        return result
    except GenerationConflict:
        return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", now, "need_human", True)
    except (OSError, ValueError):
        return _fail(st, "commit_failed", EXIT_FAIL, "pending_supersede", now, "commit_failed", True)


def acquire_lock():
    os.makedirs(os.path.dirname(os.path.expanduser(LOCK_FILE)), mode=0o700, exist_ok=True)
    handle = file_lock.try_acquire(os.path.expanduser(LOCK_FILE))
    if handle is not None:
        _HELD_IDE_LOCK.handle = handle
    return handle


def release_lock(handle) -> None:
    if handle is getattr(_HELD_IDE_LOCK, "handle", None):
        _HELD_IDE_LOCK.handle = None
    file_lock.release(handle)


def recovery_progress(handle, path: str = CRED_FILE, owner_revision: str = "",
                      now: float | None = None) -> RecoveryProgress:
    """锁内返回非秘密恢复事实；不执行网络、通知或凭据写入。"""
    _assert_ide_lock(handle)
    now = time.time() if now is None else now
    st = load_status()
    cred = read_cred(path)
    generation = _generation(cred) if _valid_cred(cred) else ""
    pending_exists = os.path.exists(os.path.expanduser(PENDING_FILE))
    pending = _read_json(PENDING_FILE) if pending_exists else None
    valid = (_valid_pending(pending) and pending.get("path") == os.path.abspath(os.path.expanduser(path)))
    related = False
    if valid and owner_revision and pending.get("old_generation") == owner_revision:
        candidate_generation = _generation(pending["candidate"])
        related = generation in (owner_revision, candidate_generation)
        if pending.get("operation") == "login" and not generation and not owner_revision:
            related = True
    if valid and pending.get("operation") == "login" and not owner_revision:
        related = (not pending.get("old_generation") and not generation)
    first = st.get("first_unavailable_at")
    if not _time_value(first):
        first = cred["expire"] if _valid_cred(cred) and cred["expire"] <= now else None
    budget = first + BUDGET_SECONDS if first is not None else None
    deadlines = []
    retry = st.get("next_retry_at")
    if _time_value(retry):
        if (st.get("early_invalid") and not st.get("early_recovered")
                and _time_value(st.get("last_early_post_at")) and not pending_exists):
            retry = max(retry, st["last_early_post_at"] + EARLY_POST_INTERVAL)
        if retry > now:
            deadlines.append(retry)
    if first is not None:
        event_id = st.get("active_events", {}).get("unavailable_too_long")
        notified = st.get("events", {}).get(event_id, {}).get("last_notified_at") if event_id else None
        if not notified and first + ALERT_AFTER > now:
            deadlines.append(first + ALERT_AFTER)
        if budget > now:
            deadlines.append(budget)
    early = bool(st.get("early_invalid") and not st.get("early_recovered"))
    event_id = st.get("active_events", {}).get("early_invalid", "") if early else ""
    early_allowed = (st["last_early_post_at"] + EARLY_POST_INTERVAL
                     if early and _time_value(st.get("last_early_post_at")) else None)
    if early_allowed is not None and early_allowed > now and not pending_exists:
        deadlines.append(early_allowed)
    return RecoveryProgress(generation, cred["expire"] if _valid_cred(cred) else None,
                            pending_exists, bool(valid), related, first,
                            min(deadlines) if deadlines else None, budget, early,
                            event_id, str(st.get("stop_reason") or ""),
                            str(st.get("verdict") or ""), bool(st.get("post_in_flight")), early_allowed)


def _observe_locked(path: str, handle) -> ObserveResult:
    """观察先于恢复分支；不触发通知、POST 或 pending 提交。"""
    _assert_ide_lock(handle)
    now = time.time()
    st = load_status()
    cred = read_cred(path)
    checked = check(path)
    generation = checked.generation
    st["last_observation"] = now
    st["observation_verdict"] = checked.verdict
    st["observation_reason"] = checked.reason
    trusted = (checked.verdict in ("healthy", "expired") and checked.reason == "whoami"
               and _valid_cred(cred) and generation == _generation(cred))
    if trusted:
        try:
            _apply_verified_lifetime(st, path, cred, checked, now, handle)
        except (OSError, ValueError, GenerationConflict):
            st["observation_verdict"] = "commit_failed"
            st["observation_reason"] = "metadata_commit"
            st["check_fail_count"] = int(st.get("check_fail_count", 0)) + 1
            _save_status(st)
            return ObserveResult("commit_failed", EXIT_FAIL, "metadata_commit", generation=generation,
                                 progress=recovery_progress(handle, path, generation, now))
        st["last_observation_error"] = ""
        st["check_fail_count"] = 0
        st["last_probe"] = now
        st.setdefault("generation", generation)
        st.setdefault("old_expire", cred["expire"])
        st.setdefault("old_issued_at", cred["issuedAt"])
        if checked.new_expire <= now:
            _record_earlier_unavailable(st, checked.new_expire, now)
            _event(st, "unavailable", st["first_unavailable_at"], generation).setdefault("verdict", "expired")
        if (st.get("first_unavailable_at") is not None
                and now - st["first_unavailable_at"] >= BUDGET_SECONDS
                and not st.get("stop_reason")
                and not os.path.exists(os.path.expanduser(PENDING_FILE))):
            st.update(verdict="need_human", reason="auto_budget_exhausted", stop_reason="auto_budget_exhausted")
            _event(st, "need_human", now, generation).update(verdict="need_human", reason="auto_budget_exhausted")
    elif checked.verdict == "expired" and checked.reason == "upstream_unauthorized" and _valid_cred(cred):
        st["last_observation_error"] = ""
        st["check_fail_count"] = 0
        st.setdefault("generation", generation)
        st.setdefault("old_expire", cred["expire"])
        st.setdefault("old_issued_at", cred["issuedAt"])
        if cred["expire"] > now:
            if not st.get("early_invalid") or st.get("early_recovered"):
                st.update(early_invalid=True, early_recovered=False, first_unavailable_at=now)
                st.pop("last_early_post_at", None)
            _event(st, "early_invalid", st["first_unavailable_at"], generation).setdefault("verdict", "expired")
        else:
            st.setdefault("first_unavailable_at", cred["expire"])
        _event(st, "unavailable", st["first_unavailable_at"], generation).setdefault("verdict", "expired")
        if (now - st["first_unavailable_at"] >= BUDGET_SECONDS
                and not st.get("stop_reason")
                and not os.path.exists(os.path.expanduser(PENDING_FILE))):
            st.update(verdict="need_human", reason="auto_budget_exhausted", stop_reason="auto_budget_exhausted")
            _event(st, "need_human", now, generation).update(verdict="need_human", reason="auto_budget_exhausted")
    else:
        st["last_observation_error"] = checked.reason or checked.verdict
        st["check_fail_count"] = int(st.get("check_fail_count", 0)) + 1
    _save_status(st)
    progress = recovery_progress(handle, path, generation, now)
    expire = checked.new_expire if trusted else (cred["expire"] if checked.reason == "upstream_unauthorized" and _valid_cred(cred) else None)
    code = EXIT_OK if checked.verdict in ("healthy", "expired") else EXIT_FAIL
    return ObserveResult(checked.verdict, code, checked.reason, expire,
                         expire - now if expire is not None else None, generation, progress)


def record_observation_error_locked(handle, reason: str) -> None:
    _assert_ide_lock(handle)
    st = load_status()
    st.update(last_observation=time.time(), last_observation_error=reason,
              check_fail_count=int(st.get("check_fail_count", 0)) + 1)
    _save_status(st)


def observe_locked(handle, path: str = CRED_FILE) -> ObserveResult:
    return _observe_locked(path, handle)


def advance_deadlines_locked(handle, path: str = CRED_FILE) -> EnsureResult:
    """只推进本地告警/预算截止，不发 GET 或 POST。"""
    _assert_ide_lock(handle)
    now = time.time()
    st = load_status()
    progress = recovery_progress(handle, path, now=now)
    if progress.first_unavailable_at is not None:
        st.setdefault("first_unavailable_at", progress.first_unavailable_at)
        _event(st, "unavailable", progress.first_unavailable_at, progress.generation).setdefault("verdict", "expired")
        if now - progress.first_unavailable_at >= ALERT_AFTER:
            _alert(st, "unavailable_too_long", now, progress.generation)
        if now - progress.first_unavailable_at >= BUDGET_SECONDS and not progress.pending:
            return _stop_for_budget(st, now)
    return _finish(st, "retry_wait", EXIT_FAIL, "deadline_wait")


def expire_budget_locked(handle, path: str = CRED_FILE) -> EnsureResult:
    """无 pending 且墙钟预算已尽时仅推进既有本地事件，不联网。"""
    _assert_ide_lock(handle)
    now = time.time()
    st = load_status()
    progress = recovery_progress(handle, path, now=now)
    if (progress.pending or progress.budget_deadline is None or now < progress.budget_deadline):
        raise ValueError("budget_not_exhausted")
    st.setdefault("generation", progress.generation)
    st.setdefault("first_unavailable_at", progress.first_unavailable_at)
    _event(st, "unavailable", st["first_unavailable_at"], progress.generation).setdefault("verdict", "expired")
    _alert(st, "unavailable_too_long", now, progress.generation)
    return _stop_for_budget(st, now)


def observe(path: str = CRED_FILE, verbose: bool = False) -> ObserveResult:
    del verbose
    handle = acquire_lock()
    if handle is None:
        return ObserveResult("retry_wait", EXIT_FAIL, "lock_busy")
    try:
        return _observe_locked(path, handle)
    finally:
        release_lock(handle)


def _ensure_locked(path: str, verbose: bool, local_gate: bool, handle,
                   verification_only: bool = False) -> EnsureResult:
    del verbose
    now = time.time()
    st = load_status()
    if not st.get("active_events") and st.get("verdict") == "rotation_only":
        event = _event(st, "rotation_only", st.get("last_run", now), st.get("generation", ""))
        event.update(verdict="rotation_only", reason="expire_not_advanced")
    cred = read_cred(path)
    if (not st.get("first_unavailable_at") and not os.path.exists(os.path.expanduser(PENDING_FILE))
            and _valid_cred(cred) and cred["expire"] <= now):
        st["first_unavailable_at"] = cred["expire"]
    if now < st.get("next_retry_at", 0):
        if st.get("first_unavailable_at") is not None:
            if now - st["first_unavailable_at"] >= ALERT_AFTER:
                _alert(st, "unavailable_too_long", now, st.get("generation", ""))
            if now - st["first_unavailable_at"] >= BUDGET_SECONDS:
                if os.path.exists(os.path.expanduser(PENDING_FILE)):
                    # 到截止仅允许验证已签发候选，不再等待验证退避。
                    st["next_retry_at"] = 0
                else:
                    return _stop_for_budget(st, now)
        if now < st.get("next_retry_at", 0):
            return _finish(st, "retry_wait", EXIT_FAIL, "probe_backoff")
    if (_valid_cred(cred) and st.get("generation")
            and st["generation"] != _generation(cred)
            and not os.path.exists(os.path.expanduser(PENDING_FILE))):
        verified = check(path)
        if verified.verdict == "healthy":
            _clear_recovery(st, now)
            st.update(generation=_generation(cred), last_probe=now)
            st.pop("post_in_flight", None)
            _save_status(st)
    # 现有事件可恢复后再告警；30 分钟禁 POST 在确认分支仍严格执行。
    if st.get("first_unavailable_at") is not None:
        event = _event(st, "unavailable", st["first_unavailable_at"], st.get("generation", ""))
        event.setdefault("verdict", "expired")
        if now - st["first_unavailable_at"] >= ALERT_AFTER:
            _alert(st, "unavailable_too_long", now, st.get("generation", ""))
        if now - st["first_unavailable_at"] >= BUDGET_SECONDS and not st.get("stop_reason"):
            st["stop_reason"] = "auto_budget_exhausted"
            _alert(st, "need_human", now, st.get("generation", ""))
    if st.get("verdict") == "commit_failed" and st.get("reason") in ("credential_commit", "login_commit", "pending_cleanup"):
        _alert(st, "commit_failed", now, st.get("generation", ""))
    if st.get("stop_reason") and st.get("verdict") == "need_human":
        _alert(st, "need_human", now, st.get("generation", ""))
    if st.get("stop_reason") and st.get("verdict") == "upstream_changed":
        _alert(st, "upstream_changed", now, st.get("generation", ""))
    if os.path.exists(os.path.expanduser(PENDING_FILE)):
        pending = _read_json(PENDING_FILE)
        if _valid_pending(pending) and pending["path"] == os.path.abspath(os.path.expanduser(path)):
            try:
                _sync_file(PENDING_FILE)
            except OSError:
                return _fail(st, "commit_failed", EXIT_FAIL, "pending_durability", now, "commit_failed", True)
            superseded = _discard_stale_pending(st, path, pending, now, handle)
            if superseded is not None:
                return superseded
        if (_valid_pending(pending) and pending.get("operation") != "login"
                and st.get("first_unavailable_at") is None and pending["old_expire"] <= now):
            st["first_unavailable_at"] = pending["old_expire"]
            if now - pending["old_expire"] >= ALERT_AFTER:
                _alert(st, "unavailable_too_long", now, st.get("generation", ""))
            if now - pending["old_expire"] >= BUDGET_SECONDS and not st.get("stop_reason"):
                st["stop_reason"] = "auto_budget_exhausted"
                _alert(st, "need_human", now, st.get("generation", ""))
        if st.get("stop_reason") == "auto_budget_exhausted":
            st["verdict"] = "need_human"
            st["reason"] = "auto_budget_exhausted"
        if (st.get("stop_reason") and st["stop_reason"] != "auto_budget_exhausted"
                and st.get("reason") not in ("pending_commit", "login_pending_commit", "pending_durability")):
            return _finish(st, st.get("verdict", "need_human"),
                           EXIT_UPSTREAM if st.get("verdict") == "upstream_changed" else EXIT_HUMAN,
                           st["stop_reason"])
        if st.get("reason") in ("pending_commit", "login_pending_commit", "pending_durability"):
            st.pop("stop_reason", None)
        if now < st.get("next_retry_at", 0):
            return _finish(st, "retry_wait", EXIT_FAIL, "pending_backoff")
        return _recover_pending(st, path, now, handle)
    if st.get("post_in_flight"):
        st["post_in_flight"] = False
        st.update(verdict="refresh_indeterminate", stop_reason="interrupted_post")
        _alert(st, "refresh_indeterminate", now, st.get("generation", ""))
    if (st.get("verdict") in ("refresh_indeterminate", "commit_failed")
            and st.get("stop_reason")):
        # 停止 POST 不妨碍只读核验与可信寿命写回；下方统一先写回再返回停止状态。
        _alert(st, st["verdict"], now, st.get("generation", ""))
    if verification_only and not os.path.exists(os.path.expanduser(PENDING_FILE)):
        if st.get("stop_reason"):
            verdict = st.get("verdict", "need_human")
            return _finish(st, verdict, EXIT_FAIL if verdict in ("refresh_indeterminate", "commit_failed")
                           else EXIT_HUMAN, st["stop_reason"])
        if st.get("active_events", {}).get("rotation_only"):
            _retry(st, now)
            return _finish(st, "rotation_only", EXIT_FAIL, "expire_not_advanced")
        return _finish(st, "need_human", EXIT_HUMAN, "verification_only")
    if cred is None:
        verdict = "missing" if not os.path.exists(os.path.expanduser(path)) else "invalid_schema"
        return _finish(st, verdict, EXIT_HUMAN, verdict)
    if not _valid_cred(cred):
        return _fail(st, "need_human", EXIT_HUMAN, "invalid_schema", now, "need_human", True)
    generation = _generation(cred)
    changed = bool(st.get("generation") and st["generation"] != generation)
    if changed:
        # 新代际必须先经 whoami 确认身份才能清旧禁令。
        local_gate = False
    st.setdefault("generation", generation)
    st.setdefault("old_expire", cred["expire"])
    st.setdefault("old_issued_at", cred["issuedAt"])
    if (local_gate and not changed and not st.get("stop_reason") and not st.get("alert")
            and not st.get("active_events")
            and not st.get("next_retry_at") and not st.get("first_unavailable_at")
            and (not st.get("early_invalid") or st.get("early_recovered"))
            and cred["expire"] > now and st.get("last_probe", 0) + PROBE_INTERVAL > now):
        return _result("healthy", EXIT_OK, st, "local_gate")
    c = check(path)
    st["last_probe"] = now
    st["remaining"] = c.remaining
    if c.verdict == "healthy":
        # 顺序不变量：先提交可信回写并回读，再判停止类状态与事件。
        try:
            _apply_verified_lifetime(st, path, cred, c, now, handle)
        except GenerationConflict:
            return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", now, "need_human", True)
        except (OSError, ValueError):
            return _fail(st, "commit_failed", EXIT_FAIL, "metadata_commit", now, "commit_failed", True)
        if changed:
            _clear_recovery(st, now)
            st.update(generation=generation, last_probe=now)
        if st.get("active_events", {}).get("rotation_only") and not changed:
            # 已确认寿命前移只结束不可用预算，不把历史轮换记为 renewed。
            if c.new_expire > cred["expire"]:
                _resolve_unavailable(st, now)
            if st.get("stop_reason"):
                return _finish(st, "need_human", EXIT_HUMAN, st["stop_reason"])
            if st.get("first_unavailable_at") is not None:
                if now - st["first_unavailable_at"] >= ALERT_AFTER:
                    _alert(st, "unavailable_too_long", now, generation)
                if now - st["first_unavailable_at"] >= BUDGET_SECONDS:
                    return _stop_for_budget(st, now)
            _alert(st, "rotation_only", now, generation)
            return _finish(st, "rotation_only", EXIT_FAIL, "expire_not_advanced")
        if st.get("stop_reason"):
            stop = st["stop_reason"]
            verdict = st.get("verdict", "need_human")
            return _finish(st, verdict, EXIT_FAIL if verdict in ("refresh_indeterminate", "commit_failed")
                           else EXIT_HUMAN, stop)
        if st.get("early_invalid") and not st.get("early_recovered"):
            _clear_recovery(st, now)
        else:
            _resolve_events(st, now)
            st.pop("first_unavailable_at", None)
            st.update(fail_count=0, next_retry_at=0, alert=False)
        return _finish(st, "healthy", EXIT_OK, "healthy")
    if c.verdict == "identity_mismatch":
        return _fail(st, "need_human", EXIT_HUMAN, c.reason, now, "identity_mismatch", True)
    if c.verdict in ("upstream_changed", "invalid_schema"):
        return _fail(st, "upstream_changed", EXIT_UPSTREAM, c.reason, now, "upstream_changed", True)
    if c.verdict == "access_denied":
        return _fail(st, "need_human", EXIT_HUMAN, c.reason, now, "need_human", True)
    if c.verdict == "transient_error":
        if now >= cred["expire"]:
            st.setdefault("first_unavailable_at", cred["expire"])
        if st.get("first_unavailable_at") is not None:
            if now - st["first_unavailable_at"] >= ALERT_AFTER:
                _alert(st, "unavailable_too_long", now, generation)
            if now - st["first_unavailable_at"] >= BUDGET_SECONDS:
                return _stop_for_budget(st, now)
        if st.get("stop_reason"):
            verdict = st.get("verdict", "need_human")
            return _finish(st, verdict, EXIT_FAIL if verdict in ("refresh_indeterminate", "commit_failed")
                           else EXIT_HUMAN, st["stop_reason"])
        if now < st.get("next_retry_at", 0):
            return _finish(st, "retry_wait", EXIT_FAIL, "probe_backoff")
        return _fail(st, "transient_error", EXIT_FAIL, c.reason, now, retry=True)
    if (c.verdict == "expired" and c.reason == "whoami" and c.new_expire != cred["expire"]):
        updated = dict(cred, expire=c.new_expire, issuedAt=c.new_issued_at)
        try:
            _commit_main_cred(handle, path, generation, updated)
        except GenerationConflict:
            return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", now, "need_human", True)
        except (OSError, ValueError):
            return _fail(st, "commit_failed", EXIT_FAIL, "metadata_commit", now, "commit_failed", True)
        cred = updated
        st["old_expire"], st["old_issued_at"] = cred["expire"], cred["issuedAt"]
    if c.verdict == "expired" and c.reason == "whoami":
        _record_earlier_unavailable(st, c.new_expire, time.time())
    if changed:
        return _fail(st, "need_human", EXIT_HUMAN, "unverified_generation", now, "need_human", True)
    early = cred["expire"] > now and c.reason == "upstream_unauthorized"
    if early and (not st.get("early_invalid") or st.get("early_recovered")):
        st["early_invalid"] = True
        st["early_recovered"] = False
        st["first_unavailable_at"] = now
        st.pop("last_early_post_at", None)
        _alert(st, "early_invalid", now, generation)
    if st.get("early_invalid") and not st.get("early_recovered"):
        _alert(st, "early_invalid", now, generation)
    if st.get("active_events", {}).get("rotation_only"):
        _alert(st, "rotation_only", now, generation)
    st.setdefault("first_unavailable_at", min(now, cred["expire"]))
    _event(st, "unavailable", st["first_unavailable_at"], generation)
    if now - st["first_unavailable_at"] >= ALERT_AFTER:
        _alert(st, "unavailable_too_long", now, generation)
    if now - st["first_unavailable_at"] >= BUDGET_SECONDS:
        return _stop_for_budget(st, now)
    if st.get("stop_reason"):
        verdict = st.get("verdict", "need_human")
        return _finish(st, verdict, EXIT_FAIL if verdict in ("refresh_indeterminate", "commit_failed")
                       else EXIT_HUMAN, st["stop_reason"])
    retry_at = st.get("next_retry_at", 0)
    if st.get("early_invalid") and not st.get("early_recovered") and st.get("last_early_post_at") is not None:
        retry_at = max(retry_at, st["last_early_post_at"] + EARLY_POST_INTERVAL)
    if now < retry_at:
        return _finish(st, "retry_wait", EXIT_FAIL, "backoff")
    # 再读当前代际，再只读确认，避免人工替换和上游恢复后误刷。
    current = read_cred(path)
    if not _valid_cred(current) or _generation(current) != generation:
        return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", now, "need_human", True)
    confirm = check(path)
    if confirm.reason == "upstream_unauthorized" and cred["expire"] > now and not early:
        early = True
        st["early_invalid"] = True
        st["early_recovered"] = False
        st["first_unavailable_at"] = now
        st.pop("last_early_post_at", None)
        _alert(st, "early_invalid", now, generation)
    if confirm.verdict == "healthy":
        try:
            _apply_verified_lifetime(st, path, cred, confirm, now, handle)
        except GenerationConflict:
            return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", now, "need_human", True)
        except (OSError, ValueError):
            return _fail(st, "commit_failed", EXIT_FAIL, "metadata_commit", now, "commit_failed", True)
        if st.get("active_events", {}).get("rotation_only"):
            if confirm.new_expire > cred["expire"]:
                _resolve_unavailable(st, now)
            _alert(st, "rotation_only", now, generation)
            return _finish(st, "rotation_only", EXIT_FAIL, "expire_not_advanced")
        _clear_recovery(st, now)
        return _finish(st, "healthy", EXIT_OK, "healthy")
    if confirm.verdict != "expired":
        if confirm.verdict == "transient_error":
            return _fail(st, "transient_error", EXIT_FAIL, "prepost_probe", now, retry=True)
        return _fail(st, "need_human", EXIT_HUMAN, "prepost_rejected", now, "need_human", True)
    if confirm.reason == "whoami":
        try:
            _apply_verified_lifetime(st, path, cred, confirm, time.time(), handle)
        except GenerationConflict:
            return _fail(st, "need_human", EXIT_HUMAN, "generation_conflict", time.time(), "need_human", True)
        except (OSError, ValueError):
            return _fail(st, "commit_failed", EXIT_FAIL, "metadata_commit", time.time(), "commit_failed", True)
        cred = dict(cred, expire=confirm.new_expire, issuedAt=confirm.new_issued_at)
        _record_earlier_unavailable(st, confirm.new_expire, time.time())
    now = time.time()
    if now - st["first_unavailable_at"] >= BUDGET_SECONDS:
        return _stop_for_budget(st, now)
    try:
        baseline = _backup(path)
        # 先持久化 POST 意图；中断时不能假设 RT 未轮换。
        st.update(post_in_flight=True, old_expire=cred["expire"], old_issued_at=cred["issuedAt"])
        if early:
            st["last_early_post_at"] = now
        _save_status(st)
    except (OSError, ValueError):
        return _fail(st, "commit_failed", EXIT_FAIL, "prepost_backup", now, "commit_failed", True)
    now = time.time()
    if now - st["first_unavailable_at"] >= BUDGET_SECONDS:
        st["post_in_flight"] = False
        return _stop_for_budget(st, now)
    try:
        response = refresh(cred["accessToken"], cred["refreshToken"], cred["attestation"])
    except PermissionError:
        st["post_in_flight"] = False
        return _fail(st, "need_human", EXIT_HUMAN, "refresh_rejected", now, "need_human", True)
    except UpstreamChanged:
        st["post_in_flight"] = False
        return _fail(st, "upstream_changed", EXIT_UPSTREAM, "refresh_contract", now, "upstream_changed", True)
    except (RefreshIndeterminate, OSError, TimeoutError):
        return _fail(st, "refresh_indeterminate", EXIT_FAIL, "refresh_indeterminate", now,
                     "refresh_indeterminate", True)
    candidate = dict(cred, accessToken=response.get("access_token"),
                     refreshToken=response.get("refresh_token"),
                     attestation=response.get("token_exchange_attestation") or cred["attestation"],
                     updatedAt=int(time.time()))
    pending = {"candidate": candidate, "old_expire": cred["expire"],
               "old_issued_at": cred["issuedAt"], "old_generation": generation,
               "old_rt_digest": _token_digest(cred["refreshToken"]),
               "baseline": baseline, "path": os.path.abspath(os.path.expanduser(path)),
               "phase": "received"}
    try:
        _checked_write(PENDING_FILE, pending, _valid_pending, baseline=True)
    except (OSError, ValueError):
        return _fail(st, "commit_failed", EXIT_FAIL, "pending_commit", now, "commit_failed", True)
    st["post_in_flight"] = False
    return _recover_pending(st, path, time.time(), handle)


def maintain_locked(handle, path: str = CRED_FILE, verbose: bool = False,
                    verification_only: bool = False) -> EnsureResult:
    _assert_ide_lock(handle)
    return _ensure_locked(path, verbose, False, handle, verification_only=verification_only)


def ensure(force: bool = False, path: str = CRED_FILE, verbose: bool = False,
           local_gate: bool = False) -> EnsureResult:
    if force:
        _v(True, "--force 已废弃，仍遵守刷新门控")
    handle = acquire_lock()
    if handle is None:
        return EnsureResult("retry_wait", EXIT_FAIL, "lock_busy")
    try:
        return _ensure_locked(path, verbose, local_gate, handle)
    finally:
        release_lock(handle)


def attestation_warning(cred: dict | None) -> str | None:
    """attestation 到期是唯一硬边界；不足 ATTEST_WARN_DAYS 天时给出人工提示。"""
    if not cred:
        return None
    days = attestation_remaining_days(cred.get("attestation", ""))
    if days is not None and days < ATTEST_WARN_DAYS:
        return (f"attestation 剩余 {days:.1f} 天，即将到期且是否自动续签未验证；"
                "请在到期前准备一次人工登录")
    return None


def login_and_commit(path: str = CRED_FILE, verbose: bool = False) -> EnsureResult:
    os.makedirs(os.path.dirname(os.path.expanduser(LOCK_FILE)), mode=0o700, exist_ok=True)
    handle = file_lock.try_acquire(os.path.expanduser(LOCK_FILE))
    if handle is None:
        return EnsureResult("retry_wait", EXIT_FAIL, "lock_busy")
    _HELD_IDE_LOCK.handle = handle
    try:
        current = read_cred(path)
        existing_pending = None
        if os.path.exists(os.path.expanduser(PENDING_FILE)):
            existing_pending = _read_json(PENDING_FILE)
            if not _valid_pending(existing_pending):
                return EnsureResult("commit_failed", EXIT_FAIL, "pending_schema")
            if existing_pending["path"] != os.path.abspath(os.path.expanduser(path)):
                return EnsureResult("need_human", EXIT_HUMAN, "pending_path_mismatch")
        if os.path.exists(os.path.expanduser(path)) and not _valid_cred(current):
            return EnsureResult("need_human", EXIT_HUMAN, "invalid_schema")
        try:
            if current:
                _backup(path)
            if existing_pending:
                _backup(PENDING_FILE)
        except OSError:
            return EnsureResult("commit_failed", EXIT_FAIL, "login_backup")
        tokens = login(verbose)
        pending = {"operation": "login", "phase": "received",
                   "candidate": {k: tokens.get(k) for k in _SECRET_FIELDS},
                   "old_generation": _generation(current) if current else "",
                   "old_rt_digest": _token_digest(current["refreshToken"]) if current else "",
                   "path": os.path.abspath(os.path.expanduser(path)),
                   "expected_mis": current["mis"] if current else None}
        st = load_status()
        try:
            _checked_write(PENDING_FILE, pending, _valid_pending, baseline=True)
        except (OSError, ValueError):
            return _fail(st, "commit_failed", EXIT_FAIL, "login_pending_commit", time.time(),
                         "commit_failed", True)
        st.pop("stop_reason", None)
        st.pop("reason", None)
        st["verdict"] = "healthy"
        st["next_retry_at"] = 0
        return _recover_pending(st, path, time.time(), handle)
    finally:
        _HELD_IDE_LOCK.handle = None
        file_lock.release(handle)


def main() -> int:
    ap = argparse.ArgumentParser(description="CatPaw IDE 凭据管理")
    ap.add_argument("--check", action="store_true", help="只读健康检查")
    ap.add_argument("--force", action="store_true", help="兼容参数，不绕过到期门控")
    ap.add_argument("--login", action="store_true", help="交互式重新登录")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        if args.check:
            result = check(verbose=args.verbose)
            print(f"{result.verdict}\t{result.reason}")
            return EXIT_OK if result.verdict == "healthy" else EXIT_FAIL
        if args.login:
            result = login_and_commit(verbose=args.verbose)
        else:
            result = ensure(force=args.force, verbose=args.verbose)
        print(f"{result.verdict}\t{result.reason}")
        if not args.login:
            warn = attestation_warning(read_cred())
            if warn:
                print(f"警告: {warn}", file=sys.stderr)
        return result.code
    except TimeoutError as e:
        print(f"需要人工：{e}", file=sys.stderr)
        return EXIT_HUMAN
    except RuntimeError as e:
        print(f"需要人工：{e}", file=sys.stderr)
        return EXIT_HUMAN
    except UpstreamChanged as e:
        print(f"上游契约变化：{e}", file=sys.stderr)
        return EXIT_UPSTREAM


if __name__ == "__main__":
    sys.exit(main())
