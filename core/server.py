"""
tools/model_proxy/core/server.py — 本地多协议路由代理主体

多协议 AI 模型代理主程序：HTTP server、路由决策、转发编排、协议转换、控制 API。
入口为 tools/model_proxy/model_proxy.py（thin wrapper 调用本模块 main()）。
监听地址默认 127.0.0.1:18889，可经 config listen 段配置（回退矩阵与 allow_hosts
Host 白名单见 core/listen_config.py）、配置 tools/model_proxy/config/model_proxy_config.json
（可用 MODEL_PROXY_CONFIG 环境变量覆盖），默认运行数据位于项目 .runtime/。
（v1 proxy.py 于 2026-07-24 下线删除，本模块为唯一代理实现。）

仅使用 Python 标准库，不引入第三方依赖。
"""

import copy
import errno
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

# 合并后的双向协议转换器（core 包内相对导入）
from . import claudian_titles
from . import session_identity
from . import translate as pt
from .item_id_normalize import sanitize_responses_input_ids
from .thinking_guard import (
    CountTokensUnsupportedStore,
    ThinkingStripStore,
    ToolStrictStore,
    is_invalid_signature_rejection,
    is_strict_mode_rejection,
    strip_empty_signature_thinking,
)
from .listen_config import ListenConfigError, parse_host_authority, resolve_listen
from .config_snapshot import (ConfigSnapshot, ConfigSnapshotError, FileMarker,
                              current_file_marker, read_stable_config,
                              read_mac_business_config, mac_business_marker,
                              verify_mac_business_config, mac_config_error_code)
from .runtime_paths import PATH_KEYS, read_runtime_paths, RuntimePathsError
from .discovery import (inspect_runtime_identities, PreflightError, INSTANCE_SCHEMA,
                        process_start_identity, process_executable, write_instance_record,
                        read_instance_record, read_instance_set, initialize_installation,
                        verify_record_directory, verify_held_instance_locks,
                        delete_instance_record_if_matches, _verify_directory,
                        verify_no_extended_acl, verify_local_filesystem_fd)
from .platform_lock import acquire_lock, LockError, verify_local_access
from .commands import (
    CMD_PREFIX,
    COMMAND_HANDLERS,
    CommandContext,
    SessionOverridesSidecar,
    extract_last_user_input_content,
    extract_last_user_message_content,
    parse_route_command,
)
from .reasoning.capability import AbstractKind, ModelReasoningCapability, abstract_encode, remap
from .reasoning.ladder import CanonicalEffort
from .reasoning.registry import apply_fields, get_codec, resolve_protocol
from .protocol_hints import (
    OP_CHAT_COMPLETIONS,
    OP_COUNT_TOKENS,
    OP_MESSAGES,
    OP_RESPONSES,
    OP_UNKNOWN,
    PROTOCOL_HINT_WINDOW_DAYS,
    build_protocol_conversion_hints,
    operation_compatible,
    protocol_conversion_kind,
)

# ---------------------------------------------------------------------------
# L0 基座
# ---------------------------------------------------------------------------

# 运行时路径统一管理（单一真相源）：config/runtime_paths.json
# 路径常量与业务配置分离，Python/Bash 都从该文件读。详见
# docs/designs/2026-08-13-runtime-path-constants-unification.md
_PACKAGE_DIR = Path(__file__).resolve().parent.parent  # model_proxy/
_RUNTIME_PATHS_FILE = _PACKAGE_DIR / "config" / "runtime_paths.json"

# 版本号：VERSION 文件是单一真相源，tag/frontmatter 是镜像。
# 模块级默认 "unknown"，main() 启动时读 VERSION 文件覆盖。
_VERSION = "unknown"  # main() 启动时读 VERSION 文件覆盖
_VERSION_FILE = _PACKAGE_DIR / "VERSION"

# 模块级默认值（供 _DEFAULT_PATHS 引用 + 测试 import 时不触碰文件）
_LOG_FILE_DEFAULT = _PACKAGE_DIR / ".runtime" / "model_proxy.log"
_TOTALS_FILE_DEFAULT = _PACKAGE_DIR / ".runtime" / "model_proxy_totals.json"
_LOCK_FILE_DEFAULT = _PACKAGE_DIR / ".runtime" / "model_proxy.lock"

_DEFAULT_PATHS = {
    "log": str(_LOG_FILE_DEFAULT),
    "totals": str(_TOTALS_FILE_DEFAULT),
    "lock": str(_LOCK_FILE_DEFAULT),
    "pid": str(_PACKAGE_DIR / ".runtime" / "model_proxy.pid"),
    "ensure_log": str(_PACKAGE_DIR / ".runtime" / "model_proxy_ensure.log"),
    "start_lock": str(_PACKAGE_DIR / ".runtime" / "model_proxy_start.lock"),
}

# 运行时实际路径（main() 启动路径赋值，测试 import 时为 None）
_runtime_paths: dict[str, Path] | None = None


def resolve_runtime_paths(paths_file: Path = _RUNTIME_PATHS_FILE) -> dict[str, Path]:
    """Resolve once at startup; invalid or missing path files fail closed."""
    base = paths_file.parent.parent.resolve()
    parsed = read_runtime_paths(paths_file, project_root=base,
                                local_appdata=os.environ.get("LOCALAPPDATA"))
    return parsed.map_values(Path)


def _trim_log(path: Path, *, now: datetime | None = None,
              keep_days: int = PROTOCOL_HINT_WINDOW_DAYS + 1) -> None:
    """按记录时间保留日志；续行跟随最近一条可解析时间戳记录。"""
    if not path.exists():
        return
    cutoff = (now or datetime.now()) - timedelta(days=keep_days)
    fd = None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".logtrim")
        found_timestamp = False
        keep_record = False
        with open(path, "r", encoding="utf-8", errors="replace") as src, \
                os.fdopen(fd, "w", encoding="utf-8") as dst:
            fd = None
            for line in src:
                try:
                    ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                except ValueError:
                    if keep_record:
                        dst.write(line)
                    continue
                found_timestamp = True
                keep_record = ts >= cutoff
                if keep_record:
                    dst.write(line)
        if found_timestamp:
            os.replace(tmp, path)
            tmp = None
        else:
            log.warning("log trim skipped: no parseable timestamp in %s", path)
    except OSError as e:
        log.warning("log trim failed, preserving original: %s", e)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# req_id 全链关联（OPT-01）：threading.local 存当前请求的 req_id，Filter 注入到每条
# log record。适用前提（O-2 已核实）：所有 warn 与请求同线程（ThreadingHTTPServer、
# 流式同步内联、无子线程派生）。若未来出现子线程写日志路径，Filter 取默认值 '-'，
# req_id 链在该处断裂——届时需在子线程内手动 set/clear。
_req_local = threading.local()


class _ReqIdFilter(logging.Filter):
    """从 _req_local 读 req_id 注入 record.req_id；非请求线程默认 '-'。"""

    def filter(self, record):
        record.req_id = getattr(_req_local, "req_id", None) or "-"
        return True


_req_filter = _ReqIdFilter()

# 模块级只声明 logger，不装配 handler / 不调 basicConfig / 不截断日志。
# handler 装配与 _trim_log 延迟到 init_logging()（main() 启动路径调用），
# 避免测试 import core.server 时触碰生产日志文件（S1 修复）。
log = logging.getLogger(__name__)
access_log = logging.getLogger("model_proxy.access")
access_log.setLevel(logging.INFO)
access_log.propagate = False


def init_logging(log_path: Path, *, trim: bool = True) -> None:
    """生产启动路径调用：截断日志 + 装配 root/access handler 到 log_path。

    仅在 main() 里调用一次。测试 import 本模块时不执行，root logger 无 FileHandler，
    测试产生的 log 行不会写入生产日志文件。
    幂等：重复调用直接返回（否则 access handler 会重复追加导致双写）。
    """
    if access_log.handlers:
        return
    if trim:
        _trim_log(log_path)
    root_handler = logging.FileHandler(log_path)
    root_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s req_id=%(req_id)s %(message)s"))
    root_handler.addFilter(_req_filter)
    logging.basicConfig(level=logging.INFO, handlers=[root_handler])

    # access logger：单独 INFO 级别，复用同一日志文件、不向 root 传播（root 已开 INFO，
    # 但 access 独立 logger 避免相互 propagate 干扰）。固定前缀 ACCESS，key=value 单行文本，
    # 与现有 WARNING 行风格一致，grep/awk 友好（见 docs/designs/2026-07-22-access-log-and-latency.md）。
    # root 开 INFO 安全前提（已核实）：BaseHTTPRequestHandler.log_message 已屏蔽
    # （pass），stdlib 默认请求日志不会刷屏；进程内 INFO 级调用方只有本模块运维事件 +
    # translate.py 两条降级日志（OPT-03 已提 WARNING）。
    access_handler = logging.FileHandler(log_path)
    access_handler.setFormatter(logging.Formatter(
        "%(asctime)s req_id=%(req_id)s %(message)s"))
    access_handler.addFilter(_req_filter)
    access_log.addHandler(access_handler)

# ---------------------------------------------------------------------------
# 累计用量账本：独立于 access 日志文件，只增不截、不受 _trim_log 影响。
# 按天分桶 + supply×route×strategy 组合键，见
# docs/designs/2026-07-23-usage-totals-ledger.md
# ---------------------------------------------------------------------------

_CST = timezone(timedelta(hours=8))          # UTC+8，中国标准时间，固定偏移


def _cst_now() -> datetime:
    """显式带时区的当前时间，绝不用 naive datetime.now()。"""
    return datetime.now(_CST)


# TOTALS_FILE 旧硬编码已移除，运行时路径由 resolve_runtime_paths() 解析，
# 默认值见 _DEFAULT_PATHS["totals"]（即 _TOTALS_FILE_DEFAULT）。
KEEP_DAYS = 400  # 明细天桶保留窗口，超窗归档进 months_archive


@dataclass(frozen=True)
class LedgerUnavailable:
    """Content invalid after security checks; never equivalent to proven absence."""
    marker: FileMarker


@dataclass(frozen=True, repr=False)
class LedgerObservation:
    """Adapter-verified full content and opaque identity/protection evidence.

    ``data=None`` means independently verified absence. Evidence must bind the
    direct parent, target identity (or absence), protection and stable content.
    """
    data: dict | None
    evidence: Any


@dataclass(frozen=True, repr=False)
class LedgerCommitResult:
    """Adapter outcome: CONFIRMED, NOT_APPLIED or UNKNOWN.

    candidate is the protected staging object's identity/content evidence,
    retained even on UNKNOWN when available. confirmed is a postchecked target.
    """
    outcome: str
    candidate: LedgerObservation | None = None
    confirmed: LedgerObservation | None = None


# Runtime integration contract: guarded platform adapters are selected below.
# Windows has fake-backend coverage only; native deployment is not validated.
# bind_startup(path, snapshot) -> LedgerObservation; observe(path) -> observation;
# matches(observed, prior) -> bool; commit(path, prior, candidate_dict) ->
# LedgerCommitResult. bind_startup must revalidate startup identity/protection,
# including verified absence; matches must compare content AND identity, parent
# and protection (not content alone). commit must independently revalidate prior,
# roles, locks, staging/volume and target before atomic submission, then verify
# target and resource release. It must return UNKNOWN whenever submission,
# postcheck, cleanup or release is uncertain. No fake adapter is installed here.
# The injection seam is not itself authority; production must pass the real guard.


def _zero_bucket() -> dict:
    """天桶/月归档/total 顶层的零值结构。OPT-10: 新增 max_ms。"""
    return {"requests": 0, "ok": 0, "fail": 0, "client_disconnect": 0,
            "sum_ms": 0, "max_ms": 0, "combos": {}}


def _zero_combo() -> dict:
    """combos 单条目的零值结构（不存 sum_ms，见方案 §1）。
    OPT-10: 新增 attempts/attempt_fail（failover 口径，不含 budget 重试）。"""
    return {"requests": 0, "ok": 0, "fail": 0, "client_disconnect": 0,
            "usage_in": 0, "usage_out": 0,
            "attempts": 0, "attempt_fail": 0}


def validate_migration_ledger_snapshot(snapshot: ConfigSnapshot | None) -> None:
    """Pure semantic preflight for a previously captured stable ledger.

    None means the caller has independently proved absence. A present snapshot
    must come from a stable read with reject_duplicate_keys=True: a parsed dict
    cannot reveal keys already discarded by a permissive JSON parser.
    This function does not read files, authenticate paths or mutate/upgrade
    the snapshot. Existing v2 and v3 ledgers are accepted only when every
    bucket and combo can be incremented or archived by UsageTotalsStore.
    Historical optional extension fields remain optional, but must be valid
    when present.
    """
    def invalid():
        raise PreflightError("ledger_unverified", "Ledger schema is invalid or unsupported")

    if snapshot is None:
        return
    if not isinstance(snapshot, ConfigSnapshot) or type(snapshot.config) is not dict:
        invalid()
    data = snapshot.config
    # The original loader treats an absent version as the legacy v2 format.
    # An explicitly present null or other non-integer is still invalid.
    version = data.get("version", 2)
    if type(version) is not int or version not in (2, 3):
        invalid()

    def date_key(value, *, month=False):
        pattern = r"[0-9]{4}-[0-9]{2}" if month else r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
        if type(value) is not str or re.fullmatch(pattern, value) is None:
            invalid()
        try:
            datetime.fromisoformat(value + "-01" if month else value)
        except ValueError:
            invalid()

    if "since" in data:
        date_key(data["since"])
    if "keep_days" in data and (type(data["keep_days"]) is not int or data["keep_days"] <= 0):
        invalid()

    def numbers(mapping, required, optional):
        for field in required:
            if field not in mapping or type(mapping[field]) is not int or mapping[field] < 0:
                invalid()
        for field in optional:
            if field in mapping and (type(mapping[field]) is not int or mapping[field] < 0):
                invalid()

    def bucket(value):
        if type(value) is not dict:
            invalid()
        numbers(value, ("requests", "ok", "fail", "sum_ms"), ("client_disconnect", "max_ms"))
        if (value["ok"] + value["fail"] > value["requests"] or
                value.get("client_disconnect", 0) > value["requests"] or
                value.get("max_ms", 0) > value["sum_ms"]):
            invalid()
        combos = value.get("combos")
        if type(combos) is not dict:
            invalid()
        for key, combo in combos.items():
            if type(key) is not str or not key or type(combo) is not dict:
                invalid()
            numbers(combo, ("requests", "ok", "fail", "usage_in", "usage_out"),
                    ("client_disconnect", "attempts", "attempt_fail", "usage_reasoning"))
            if (combo["ok"] + combo["fail"] > combo["requests"] or
                    combo.get("client_disconnect", 0) > combo["requests"]):
                invalid()

    if "total" not in data:
        invalid()
    for name in ("days", "months_archive"):
        if name in data and type(data[name]) is not dict:
            invalid()
    bucket(data["total"])
    for key, value in data.get("days", {}).items():
        date_key(key)
        bucket(value)
    for key, value in data.get("months_archive", {}).items():
        date_key(key, month=True)
        bucket(value)


class UsageTotalsStore:
    """独立账本：按天分桶，桶内 supply×route×strategy 组合键累加。

    与 access 日志完全独立（无调用关系），不受 `_trim_log` 影响；文件只增不截。
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = self._load()
        # Direct construction has no verified startup evidence and cannot write.
        self._init_persistence(None, None, initial_verified=False)

    @staticmethod
    def _empty_data() -> dict:
        return {
            "version": 3,
            "since": _cst_now().strftime("%Y-%m-%d"),
            "keep_days": KEEP_DAYS,
            "total": _zero_bucket(),
            "months_archive": {},
            "days": {},
        }

    @classmethod
    def from_verified_snapshot(cls, path: Path, snapshot, *, adapter=None):
        """Use the security-gated startup snapshot; never reset an unknown file.

        The caller injects the real guarded adapter on supported runtime paths;
        no adapter means persistence remains disabled, never an unsafe fallback.
        """
        if not isinstance(snapshot, LedgerUnavailable):
            validate_migration_ledger_snapshot(snapshot)
        store = cls.__new__(cls)
        store._path = path
        store._lock = threading.Lock()
        if snapshot is None or isinstance(snapshot, LedgerUnavailable):
            store._data = cls._empty_data()
        else:
            data = copy.deepcopy(snapshot.config)
            validate_migration_ledger_snapshot(ConfigSnapshot(data, snapshot.marker))
            store._data = cls._prepare_loaded(data)
        store._init_persistence(adapter, snapshot, initial_verified=True)
        return store

    def _init_persistence(self, adapter, snapshot, *, initial_verified):
        self._adapter = adapter
        self._startup_snapshot = copy.deepcopy(snapshot)
        self._startup_verified = initial_verified
        self._baseline = None
        self._candidate = None
        self._state = "UNAVAILABLE" if isinstance(snapshot, LedgerUnavailable) else "CLEAN"
        self._last_warning = None
        if self._state == "UNAVAILABLE":
            self._warn_persistence(force=True)
        elif adapter is None:
            log.warning("usage_totals.persistence protected adapter unavailable; new statistics will not be persisted")

    @property
    def persistence_state(self):
        with self._lock:
            return self._state

    def _warn_persistence(self, *, force=False):
        now = time.monotonic()
        if force or self._last_warning is None or now - self._last_warning >= 60:
            log.warning("usage_totals.persistence state=%s; statistics not confirmed on disk", self._state)
            self._last_warning = now

    def _set_persistence_state(self, state):
        if state != self._state:
            self._state = state
            if state == "CLEAN":
                log.info("usage_totals.persistence recovered; statistics confirmed on disk")
                self._last_warning = None
            else:
                # Rate-limit across transitions too, not just repeated states.
                self._warn_persistence()
        elif state != "CLEAN":
            self._warn_persistence()

    @staticmethod
    def _valid_observation(value):
        return (isinstance(value, LedgerObservation) and value.evidence is not None
                and (value.data is None or type(value.data) is dict))

    def _matches(self, observed, expected):
        # Content equality is necessary but NOT sufficient; adapter supplies
        # independent object/parent/protection identity evidence.
        return (self._valid_observation(observed) and self._valid_observation(expected)
                and observed.data == expected.data
                and self._adapter.matches(observed, expected) is True)

    def _flush_locked(self):
        """Accumulate once, then safely reconcile B/C before any new commit."""
        if self._state == "UNAVAILABLE":
            # Unknown starting contents cannot be merged safely with this
            # process's increments. Recovery needs a future proven merge path.
            self._warn_persistence()
            return
        if self._adapter is None or not self._startup_verified:
            self._set_persistence_state("PENDING")
            return
        try:
            if self._baseline is None:
                expected = (None if self._startup_snapshot is None
                            else self._startup_snapshot.config)
                bound = self._adapter.bind_startup(self._path, copy.deepcopy(self._startup_snapshot))
                if not self._valid_observation(bound) or bound.data != expected:
                    self._set_persistence_state("UNKNOWN")
                    return
                self._baseline = copy.deepcopy(bound)
            observed = self._adapter.observe(self._path)
            if self._state == "UNKNOWN" and self._candidate is not None and self._matches(observed, self._candidate):
                self._baseline = copy.deepcopy(observed)
                self._candidate = None
            elif self._matches(observed, self._baseline):
                self._candidate = None
            else:
                self._set_persistence_state("UNKNOWN")
                return
        except Exception:
            # Revalidation failures cannot establish current disk identity.
            self._set_persistence_state("UNKNOWN")
            return

        try:
            candidate_data = copy.deepcopy(self._data)
        except Exception:
            # No commit has been called; the in-memory increment is pending.
            self._set_persistence_state("PENDING")
            return
        try:
            result = self._adapter.commit(self._path, copy.deepcopy(self._baseline),
                                          copy.deepcopy(candidate_data))
        except Exception:
            # An unclassified exception may have happened after submission.
            self._candidate = None
            self._set_persistence_state("UNKNOWN")
            return
        if not isinstance(result, LedgerCommitResult):
            self._candidate = None
            self._set_persistence_state("UNKNOWN")
            return
        if result.outcome == "NOT_APPLIED":
            self._candidate = None
            self._set_persistence_state("PENDING")
            return
        try:
            candidate = result.candidate
            self._candidate = (copy.deepcopy(candidate) if self._valid_observation(candidate)
                               and candidate.data == candidate_data else None)
        except Exception:
            # Submission may have happened before the adapter returned.
            self._candidate = None
            self._set_persistence_state("UNKNOWN")
            return
        if result.outcome == "CONFIRMED" and self._candidate is not None:
            try:
                if self._matches(result.confirmed, self._candidate):
                    self._baseline = copy.deepcopy(result.confirmed)
                    self._candidate = None
                    self._set_persistence_state("CLEAN")
                    return
            except Exception:
                pass
        self._set_persistence_state("UNKNOWN")

    def _load(self) -> dict:
        """Load a stable ledger; never move/reset an unreadable existing file."""
        try:
            snapshot = read_stable_config(self._path, reject_duplicate_keys=True)
        except ConfigSnapshotError as exc:
            if exc.code == "config_read_error":
                try:
                    self._path.lstat()
                except FileNotFoundError:
                    return self._empty_data()
                except OSError:
                    pass
            raise PreflightError("ledger_unverified", "Ledger cannot be verified") from None
        validate_migration_ledger_snapshot(snapshot)
        return self._prepare_loaded(copy.deepcopy(snapshot.config))

    @staticmethod
    def _prepare_loaded(data: dict) -> dict:
        # OPT-10: v2→v3 migration remains in memory until normal recording.
        if data.get("version", 2) < 3:
            data["version"] = 3
            for bucket in [data.get("total"), *(data.get("months_archive") or {}).values(),
                           *(data.get("days") or {}).values()]:
                if not isinstance(bucket, dict):
                    continue
                bucket.setdefault("max_ms", 0)
                for combo in (bucket.get("combos") or {}).values():
                    if isinstance(combo, dict):
                        combo.setdefault("attempts", 0)
                        combo.setdefault("attempt_fail", 0)
            log.info("usage_totals.migrated v2→v3 (旧桶补 0: max_ms/attempts/attempt_fail)")
        return data

    @staticmethod
    def _combo_key(acc: dict) -> str:
        return (
            f"supply={acc.get('supply') or '(none)'}"
            f"|route={acc.get('route') or '(none)'}"
            f"|strategy={acc.get('strategy') or '(none)'}"
        )

    def record(self, acc: dict, ms: int) -> None:
        """核心记账方法：锁内累加内存 dict + 归档检查 + 原子落盘。
        OPT-10: 新增 max_ms（bucket 级）和 attempts/attempt_fail（combo 级）。"""
        with self._lock:
            day_key = _cst_now().strftime("%Y-%m-%d")
            days = self._data.setdefault("days", {})
            day_bucket = days.setdefault(day_key, _zero_bucket())
            total_bucket = self._data.setdefault("total", _zero_bucket())

            combo_key = self._combo_key(acc)
            integrity = acc.get("stream_integrity")
            disconnected = 1 if integrity == "client_disconnect" else 0
            if integrity:
                ok = 1 if acc.get("status") == 200 and integrity == "valid" else 0
                fail = 1 if integrity == "invalid" or acc.get("status") != 200 else 0
            else:
                ok = 1 if acc.get("status") == 200 else 0
                fail = 0 if ok else 1
            usage_in = acc.get("usage_in", 0) or 0
            usage_out = acc.get("usage_out", 0) or 0
            # OPT-10: attempts = ACCESS 的 attempts 字段（含 failover + budget 重试），
            # attempt_fail = attempt_errors 长度（仅 failover 口径，不含 budget 重试）
            attempts = acc.get("attempts", 0) or 0
            attempt_fail = len(acc.get("attempt_errors") or [])

            for bucket in (day_bucket, total_bucket):
                bucket["requests"] += 1
                bucket["ok"] += ok
                bucket["fail"] += fail
                bucket["client_disconnect"] = bucket.get("client_disconnect", 0) + disconnected
                bucket["sum_ms"] += ms
                # OPT-10: max_ms（bucket 级，跨 combo）
                if ms > bucket.get("max_ms", 0):
                    bucket["max_ms"] = ms
                combo = bucket.setdefault("combos", {}).setdefault(combo_key, _zero_combo())
                combo["requests"] += 1
                combo["ok"] += ok
                combo["fail"] += fail
                combo["client_disconnect"] = combo.get("client_disconnect", 0) + disconnected
                combo["usage_in"] += usage_in
                combo["usage_out"] += usage_out
                # OPT-10: attempts/attempt_fail（combo 级）
                combo["attempts"] = combo.get("attempts", 0) + attempts
                combo["attempt_fail"] = combo.get("attempt_fail", 0) + attempt_fail

            self._archive_if_needed()
            self._flush_locked()

    def _archive_if_needed(self) -> None:
        """days 超过 KEEP_DAYS 时，把最旧的天桶按组合键汇总进 months_archive 后删除。
        必须持锁调用（由 record 内已持锁的调用点触发）。
        OPT-10: 归档时传递 max_ms（取 max）和 attempts/attempt_fail（累加）。
        """
        days = self._data.setdefault("days", {})
        while len(days) > KEEP_DAYS:
            oldest_key = min(days.keys())
            oldest_bucket = days.pop(oldest_key)
            month_key = oldest_key[:7]  # "YYYY-MM"
            months_archive = self._data.setdefault("months_archive", {})
            month_bucket = months_archive.setdefault(month_key, _zero_bucket())
            month_bucket["requests"] += oldest_bucket.get("requests", 0)
            month_bucket["ok"] += oldest_bucket.get("ok", 0)
            month_bucket["fail"] += oldest_bucket.get("fail", 0)
            month_bucket["client_disconnect"] = (month_bucket.get("client_disconnect", 0)
                                                  + oldest_bucket.get("client_disconnect", 0))
            month_bucket["sum_ms"] += oldest_bucket.get("sum_ms", 0)
            # OPT-10: max_ms 归档取 max
            old_max = oldest_bucket.get("max_ms", 0)
            if old_max > month_bucket.get("max_ms", 0):
                month_bucket["max_ms"] = old_max
            month_combos = month_bucket.setdefault("combos", {})
            for combo_key, combo_val in oldest_bucket.get("combos", {}).items():
                dest = month_combos.setdefault(combo_key, _zero_combo())
                dest["requests"] += combo_val.get("requests", 0)
                dest["ok"] += combo_val.get("ok", 0)
                dest["fail"] += combo_val.get("fail", 0)
                dest["client_disconnect"] = (dest.get("client_disconnect", 0)
                                             + combo_val.get("client_disconnect", 0))
                dest["usage_in"] += combo_val.get("usage_in", 0)
                dest["usage_out"] += combo_val.get("usage_out", 0)
                # Preserve the legacy reasoning count only where it existed.
                if "usage_reasoning" in combo_val:
                    dest["usage_reasoning"] = (dest.get("usage_reasoning", 0)
                                               + combo_val["usage_reasoning"])
                # OPT-10: attempts/attempt_fail 归档累加
                dest["attempts"] = dest.get("attempts", 0) + combo_val.get("attempts", 0)
                dest["attempt_fail"] = dest.get("attempt_fail", 0) + combo_val.get("attempt_fail", 0)


# 模块级占位 None，main() 启动路径实例化（S1 修复：避免测试 import 时触碰账本文件）。
# _forward_logged 用 `if usage_totals is not None` 守卫，测试直驱 _forward 不会记账本。
usage_totals: "UsageTotalsStore | None" = None

# ---------------------------------------------------------------------------
# reasoning debug 开关：默认关闭，不污染生产日志（沿用 MODEL_PROXY_CONFIG/
# MODEL_PROXY_PORT 的环境变量风格，进程启动时读取一次，不支持热切换）。
# 开启后把本模块 logger 的 effective level 调到 DEBUG（只影响 `log` 这一个具名
# logger，root 已开 INFO），调用点用 log.isEnabledFor(logging.DEBUG) 判断，
# 关闭时不做任何字符串拼接。
# 手动开启：MODEL_PROXY_REASONING_DEBUG=1 再启动/重启进程（export 后运行
# model_proxy_cli.sh on 也会被 nohup 子进程继承）。
# ---------------------------------------------------------------------------
if os.environ.get("MODEL_PROXY_REASONING_DEBUG", "").strip().lower() in ("1", "true", "on", "yes"):
    log.setLevel(logging.DEBUG)

# 默认路径（全部 v2 命名）：锚定包目录 tools/model_proxy/config/model_proxy_config.json，
# 可用环境变量 MODEL_PROXY_CONFIG 覆盖（与 model_proxy_cli.sh 的 CONFIG_FILE 逻辑一致）。
_default_config_source = (os.environ.get("MODEL_PROXY_CONFIG")
                          or (Path(__file__).resolve().parent.parent / "config" / "model_proxy_config.json"))
# pathlib discards a trailing slash and '/.'; retain the original Mac business
# path until its filesystem semantics have been checked by the snapshot code.
_DEFAULT_CONFIG_PATH = (os.fspath(_default_config_source)
                        if sys.platform == "darwin" and os.name == "posix"
                        else Path(_default_config_source))
# _LOCK_FILE 旧硬编码已移除，运行时路径由 resolve_runtime_paths() 解析，
# 默认值见 _DEFAULT_PATHS["lock"]（即 _LOCK_FILE_DEFAULT）。

# 控制路径前缀（v2，避免与 18888 的 /proxy 混淆）
_CONTROL_PATH_PREFIX = "/model_proxy"

# 上游请求超时（秒），缺省 30min，对齐 API_TIMEOUT_MS；config 顶层
# upstream_timeout_seconds 可覆盖
_UPSTREAM_TIMEOUT_DEFAULT = 1800


# ---------------------------------------------------------------------------
# reasoning 语法偏好存储（取代旧 _THINKING_FMT_CACHE / _get/_set_thinking_fmt）。
# 记录"某 target_model 应该用哪种语法变体"的运行时学习结果（由 400 拒绝重试驱动）。
# 四种协议组合统一走 core.reasoning 链路，snapshot/learn 与协议无关，key 用 target_model。
# ---------------------------------------------------------------------------


class SyntaxPreferenceStore:
    """model → {"variant": str, "at": float}，带 TTL（沿用现有 48h）。"""

    _TTL = 48 * 3600

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def snapshot(self, model: str) -> dict:
        """返回该 model 当前的偏好 dict（供 codec.select_variant 用），过期或未学到返回 {}。"""
        with self._lock:
            entry = self._cache.get(model)
            if entry and time.time() - entry["at"] < self._TTL:
                return {"variant": entry["variant"]}
            return {}

    def learn(self, model: str, variant: str) -> None:
        with self._lock:
            self._cache[model] = {"variant": variant, "at": time.time()}
        log.info("reasoning_pref.learn: model=%r → variant=%r (cached 48h)", model, variant)


# ---------------------------------------------------------------------------
# L1 配置：ConfigStore（拷贝 proxy.py 骨架 + 换 getter 适配新 schema）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigReloadOutcome:
    """One attempt only; error values are fixed, non-sensitive enums."""

    loaded: bool
    error: str | None = None


class ConfigReloadCallbackError(RuntimeError):
    """Configuration was committed but its reload callback failed."""

    def __init__(self):
        super().__init__("config_callback_error")
        self.loaded = True


def _mac_business_enabled():
    return sys.platform == "darwin" and os.name == "posix"


def _business_path(path):
    """Anchor relative Mac paths without erasing symlink/.. or trailing slash."""
    if not _mac_business_enabled():
        return Path(path)
    raw = os.fspath(path)
    return raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)


def _windows_config_boundary(path, *, project_root=None):
    """Use the existing Windows preflight boundary for an established role."""
    import ntpath
    from .discovery import _windows_trusted_boundary
    try:
        project = ntpath.abspath(os.fspath(_PACKAGE_DIR if project_root is None else project_root))
        target = ntpath.abspath(os.fspath(path))
        return _windows_trusted_boundary(project, target)
    except PreflightError:
        # Ambiguous project aliases must remain fail-closed during hot reload.
        raise ConfigSnapshotError("config_read_error") from None


def _read_business_config(path):
    if _mac_business_enabled():
        return read_mac_business_config(path)
    if os.name == "nt":
        return read_stable_config(path, trusted_boundary=_windows_config_boundary(path))
    return read_stable_config(path)


def _business_marker(path):
    if _mac_business_enabled():
        return mac_business_marker(path)
    if os.name == "nt":
        return current_file_marker(path, trusted_boundary=_windows_config_boundary(path))
    return current_file_marker(path)


def _read_runtime_paths_config():
    if os.name == "nt":
        return read_stable_config(_RUNTIME_PATHS_FILE,
                                  trusted_boundary=_windows_config_boundary(_RUNTIME_PATHS_FILE))
    return read_stable_config(_RUNTIME_PATHS_FILE)


def _runtime_paths_marker():
    if os.name == "nt":
        return current_file_marker(_RUNTIME_PATHS_FILE,
                                   trusted_boundary=_windows_config_boundary(_RUNTIME_PATHS_FILE))
    return current_file_marker(_RUNTIME_PATHS_FILE)


def _verify_business_snapshot(snapshot):
    if _mac_business_enabled():
        verify_mac_business_config(snapshot)


class ConfigStore:
    """从 model_proxy_config.json 加载并热重载配置。

    文件身份、时间及大小标记不等时触发双重检查；本次稳定读取失败
    不替换快照、不推进标记。初始化保留 supply 扩展校验 fail-fast，
    热重载保留校验异常仅告警的既有行为。
    """

    def __init__(
        self,
        config_path: str | os.PathLike | None = None,
        on_reload: Callable[[], None] | None = None,
    ):
        self._path = _business_path(config_path if config_path is not None else _DEFAULT_CONFIG_PATH)
        self._config: dict[str, Any] = {}
        self._mtime: float = 0.0
        self._probe_marker = None
        self._business_snapshot = None
        self._lock = threading.Lock()
        self._on_reload = on_reload
        self._reload()

    # ------------------------------------------------------------------
    # Public API —— 新 getter（适配新 schema）
    # ------------------------------------------------------------------

    def get_supplies(self) -> list[dict]:
        """config["supplies"]（有序列表）的浅拷贝。"""
        with self._lock:
            return list(self._config.get("supplies", []))

    def get_supply_map(self) -> dict[str, dict]:
        """{supply["id"]: supply} 映射。"""
        with self._lock:
            supplies = self._config.get("supplies", [])
            return {s["id"]: s for s in supplies if "id" in s}

    def get_routes(self) -> list[dict]:
        """config["routes"]（有序列表）的浅拷贝。"""
        with self._lock:
            return list(self._config.get("routes", []))

    def get_routes_map(self) -> dict[str, dict]:
        """{route["id"]: route} 映射。"""
        with self._lock:
            routes = self._config.get("routes", [])
            return {r["id"]: r for r in routes if "id" in r}

    def get_strategies(self) -> list[dict]:
        """config["strategies"]（有序列表）的浅拷贝。"""
        with self._lock:
            return list(self._config.get("strategies", []))

    def get_admin_token(self) -> str:
        with self._lock:
            return str(self._config.get("admin_token", ""))

    def get_upstream_timeout(self) -> int:
        with self._lock:
            return int(self._config.get("upstream_timeout_seconds", _UPSTREAM_TIMEOUT_DEFAULT))

    def get_budget_retry(self) -> dict:
        """可选顶层块 budget_retry（④b 反应式预算重试）。缺省全开：
        {"enabled": True, "max_retries": _BUDGET_RETRY_MAX}。
        封顶 ceiling 硬编码为 _BUDGET_CEILING，不暴露到 config。
        无 per-supply 维度（③ output_budget 已撤销，见设计记录 §③/§④）。"""
        with self._lock:
            cfg = self._config.get("budget_retry") or {}
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "max_retries": int(cfg.get("max_retries", _BUDGET_RETRY_MAX)),
        }

    def get_cooldown_rules(self) -> list[dict]:
        """顶层 cooldown_rules 策略组列表（浅拷贝）。无 cooldown_rules → 返回 []。"""
        with self._lock:
            return list(self._config.get("cooldown_rules", []))

    def get_body_error_rules(self) -> list[dict]:
        """顶层 body_error_rules（浅拷贝）。无键/空数组 = 功能整体关闭（fail-open，
        对齐 budget_retry 缺省启停风格）。规则形状见 docs/designs/
        2026-10-10-正文错误识别与failover衔接方案.md §2.1。"""
        with self._lock:
            return list(self._config.get("body_error_rules", []))

    def get_log_upstream_error_body(self) -> dict:
        """可选顶层块 log_upstream_error_body。缺省关闭（不记错误体）。

        - enabled：是否记录非 2xx 上游错误体；
        - max_bytes：截断长度，正 int，缺省 4096，上限 65536；
        - redact_patterns：额外子串脱敏列表，缺省仅内置 token/appkey/key。
        """
        with self._lock:
            cfg = self._config.get("log_upstream_error_body") or {}
        if not isinstance(cfg, dict):
            return {"enabled": False, "max_bytes": 4096, "redact_patterns": []}
        try:
            max_bytes = int(cfg.get("max_bytes", 4096))
        except (TypeError, ValueError):
            max_bytes = 4096
        max_bytes = max(1, min(max_bytes, 65536))
        patterns = cfg.get("redact_patterns") or []
        if not isinstance(patterns, list):
            patterns = []
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "max_bytes": max_bytes,
            "redact_patterns": [str(x) for x in patterns if isinstance(x, str) and x],
        }

    # ------------------------------------------------------------------
    # 热重载（拷贝 proxy.py 原样）
    # ------------------------------------------------------------------

    def maybe_reload(self) -> bool:
        """Reload on marker inequality; failures do not advance the marker."""
        try:
            marker = _business_marker(self._path)
        except ConfigSnapshotError:
            return False
        if marker == self._probe_marker:
            return False
        with self._lock:
            try:
                marker = _business_marker(self._path)
            except ConfigSnapshotError:
                return False
            if marker == self._probe_marker:
                return False
            outcome = self._reload_locked()
        if outcome.loaded and self._on_reload is not None:
            self._on_reload()
        return outcome.loaded

    def reload_detailed(self) -> ConfigReloadOutcome:
        """One manual attempt, including a possible post-commit callback error."""
        with self._lock:
            outcome = self._reload_locked()
        if outcome.loaded and self._on_reload is not None:
            try:
                self._on_reload()
            except Exception:
                return ConfigReloadOutcome(True, "config_callback_error")
        return outcome

    def reload(self) -> bool:
        """Return this attempt's loading result; callback failure is post-commit."""
        outcome = self.reload_detailed()
        if outcome.error == "config_callback_error":
            raise ConfigReloadCallbackError() from None
        return outcome.loaded

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        """Initial stable read; retain the existing startup validation failure."""
        with self._lock:
            snapshot = _read_business_config(self._path)
            self._config = snapshot.config
            self._business_snapshot = snapshot
            self._probe_marker = snapshot.marker
            self._mtime = snapshot.marker.mtime_ns / 1_000_000_000
            # Startup remains fail-fast for invalid supply extension fields.
            self._validate_config()

    def _reload_locked(self) -> ConfigReloadOutcome:
        """Commit the read descriptor's snapshot and marker under one lock."""
        try:
            snapshot = _read_business_config(self._path)
        except ConfigSnapshotError as exc:
            allowed = {"config_parse_error", "config_read_error", "config_unstable",
                       "file_identity_unavailable", "filesystem_unverified"}
            code = exc.code if exc.code in allowed else "config_reload_error"
            try:
                log.warning("config.reload.failed reason=%s", code)
            except Exception:
                pass
            return ConfigReloadOutcome(False, code)

        # A later path change is detected against the marker of the bytes read.
        self._config = snapshot.config
        self._business_snapshot = snapshot
        self._probe_marker = snapshot.marker
        self._mtime = snapshot.marker.mtime_ns / 1_000_000_000
        try:
            self._validate_config()
        except Exception as exc:
            # Existing hot-reload behavior: validation warns, not rejection.
            try:
                log.warning("config.validate raised (ignored, load still ok): %s", exc)
            except Exception:
                pass
        try:
            def safe_count(value):
                try:
                    return len(value)
                except (TypeError, AttributeError):
                    return 0
            log.info("config.reload.ok mtime=%s supplies=%d routes=%d strategies=%d",
                     self._mtime, safe_count(snapshot.config.get("supplies", [])),
                     safe_count(snapshot.config.get("routes", [])),
                     safe_count(snapshot.config.get("strategies", [])))
        except Exception:
            # Logging failure cannot make an already committed snapshot old.
            pass
        return ConfigReloadOutcome(True)

    def _validate_config(self) -> None:
        """OPT-06：配置校验（告警 ≠ 拒绝加载）。

        在 _reload / _reload_locked 成功后各跑一次，替代热路径 extract_route_candidates
        内的每请求重复告警。校验项：
        - route_id 与 route_pool 互斥：每个 strategy 最多告警一次
        - route_pool 非法项：每个 strategy 最多告警一次（聚合非法 route_id 列表）
        - cooldown_rules 策略组格式校验（容错：非法策略组跳过 + log warning）
        容错语义：热重载校验只告警，不影响本次加载成功结果。
        """
        routes_map = {r["id"]: r for r in self._config.get("routes", [])
                      if isinstance(r, dict) and "id" in r}
        for st in self._config.get("strategies", []):
            if not isinstance(st, dict):
                continue
            ct = st.get("client_token", "")
            if st.get("route_id") and st.get("route_pool"):
                log.warning("config.validate: strategy=%s has both route_id and route_pool, "
                            "route_id will be ignored", ct)
            pool = st.get("route_pool") or []
            invalid = [item.get("route_id") for item in pool
                       if isinstance(item, dict)
                       and (not item.get("route_id") or item.get("route_id") not in routes_map)]
            if invalid:
                log.warning("config.validate: strategy=%s route_pool has invalid route_ids=%s",
                            ct, invalid)
        self._validate_cooldown_rules(self._config.get("cooldown_rules", []), "top")
        self._validate_body_error_rules(self._config.get("body_error_rules", []))
        # supply 层扩展字段校验（extra_headers / system_inject / appkey_file）：
        # 不同于上方容错校验，此处 raise——启动路径 fail-fast，热重载路径被
        # _reload_locked 吞为 warning（已知降级，见 validate_supply_ext docstring）。
        for s in self._config.get("supplies", []):
            if isinstance(s, dict):
                validate_supply_ext(s)

    def _validate_cooldown_rules(self, rules: list, ctx: str) -> None:
        """校验 cooldown_rules 策略组格式（容错：非法策略组跳过 + log warning，不阻断加载）。

        每条 rule 必须有 errorcode（非空 list，元素为 int 或任意非空字符串）
        和 cooldown_seconds（正 int）。非法 rule log warning + 跳过，不阻断加载。
        """
        if not isinstance(rules, list):
            log.warning("config.validate: %s cooldown_rules not a list, skipped", ctx)
            return
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                log.warning("config.validate: %s cooldown_rules[%d] not a dict, skipped",
                            ctx, i)
                continue
            ec = rule.get("errorcode")
            if not isinstance(ec, list) or not ec:
                log.warning("config.validate: %s cooldown_rules[%d] errorcode missing/empty, skipped",
                            ctx, i)
                continue
            for c in ec:
                # 元素校验放开为「int 或任意非空字符串」，不特判 "URLError"/body_* 前缀
                # （设计 2026-10-10 复核修订中1）：特判前缀则每引入新虚拟 code 都要同步
                # 改校验，违背「加规则一行成本」目标；errorcode 命名空间本就开放
                # （_record_unconfigured/resolve_cooldown_seconds 均按任意值处理），
                # 校验收紧无安全收益。
                if not isinstance(c, int) and not (isinstance(c, str) and c):
                    log.warning("config.validate: %s cooldown_rules[%d] errorcode has invalid "
                                "element %r (must be int or non-empty string), skipped",
                                ctx, i, c)
            cs_val = rule.get("cooldown_seconds")
            if not isinstance(cs_val, int) or cs_val <= 0:
                log.warning("config.validate: %s cooldown_rules[%d] cooldown_seconds missing/invalid, skipped",
                            ctx, i)

    def _validate_body_error_rules(self, rules: list) -> None:
        """校验 body_error_rules 格式（容错：非法 rule 跳过 + log warning，不阻断加载，
        同 _validate_cooldown_rules 风格；运行期由 match_body_error 对非法 rule 容错跳过）。

        每条 rule 必须有 pattern（非空 str）与 errorcode（非空 str）；
        match 可选（contains 默认 | exact | prefix，不做 regex）；
        max_text_len 可选（正 int，缺省 _BODY_ERROR_MAX_TEXT_LEN_DEFAULT）。
        """
        if not isinstance(rules, list):
            log.warning("config.validate: top body_error_rules not a list, skipped")
            return
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                log.warning("config.validate: top body_error_rules[%d] not a dict, skipped", i)
                continue
            if not isinstance(rule.get("pattern"), str) or not rule.get("pattern"):
                log.warning("config.validate: top body_error_rules[%d] pattern missing/empty, skipped", i)
                continue
            if not isinstance(rule.get("errorcode"), str) or not rule.get("errorcode"):
                log.warning("config.validate: top body_error_rules[%d] errorcode missing/empty, skipped", i)
                continue
            match = rule.get("match") or "contains"
            if match not in ("contains", "exact", "prefix"):
                log.warning("config.validate: top body_error_rules[%d] match=%r invalid "
                            "(must be contains/exact/prefix), skipped", i, match)
                continue
            mtl = rule.get("max_text_len")
            if mtl is not None and (not isinstance(mtl, int) or isinstance(mtl, bool)
                                    or mtl <= 0):
                log.warning("config.validate: top body_error_rules[%d] max_text_len=%r invalid "
                            "(must be positive int), skipped", i, mtl)


# ---------------------------------------------------------------------------
# L1 状态：CooldownStore（全新，错误信号驱动，纯内存，不写盘）
# ---------------------------------------------------------------------------

class StreamHealthStore:
    """流完整性连续失败旁路计数；首版固定只观测，不执行冷却。"""

    enforcement = False
    window_seconds = 600
    threshold = 3

    def __init__(self):
        self._state: dict[str, dict[str, float | int]] = {}
        self._lock = threading.Lock()

    def record(self, supply_id: str, integrity: str) -> None:
        if not supply_id or integrity not in {"valid", "invalid"}:
            return
        now = time.time()
        with self._lock:
            if integrity == "valid":
                self._state.pop(supply_id, None)
                return
            current = self._state.get(supply_id)
            count = (int(current["consecutive"]) + 1
                     if current and now - float(current["last_at"]) <= self.window_seconds else 1)
            self._state[supply_id] = {"consecutive": count, "last_at": now}

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            items = {sid: dict(value) for sid, value in self._state.items()
                     if now - float(value["last_at"]) <= self.window_seconds}
        return {"enforcement": self.enforcement, "window_seconds": self.window_seconds,
                "threshold": self.threshold, "supplies": items}


class CooldownStore:
    """按 supply 记录冷却截止时间。

    与旧 StateStore 的本质区别：不记账、不轮转游标、不写盘。
    冷却完全由上游错误信号驱动。ThreadingHTTPServer 多线程，必须加锁。
    """

    def __init__(self):
        self._until: dict[str, float] = {}   # supply_id -> cooldown_until(epoch 秒)
        self._reason: dict[str, str] = {}    # supply_id -> errorcode（当前冷却周期的触发原因）
        self._lock = threading.Lock()

    def is_cooling(self, supply_id: str) -> bool:
        """当前是否处于冷却中（now < until）。"""
        now = time.time()
        with self._lock:
            until = self._until.get(supply_id, 0.0)
            return now < until

    def cooldown(self, supply_id: str, seconds: int, reason: str = "") -> None:
        """将 supply 置入冷却：until = now + seconds。reason 为触发此冷却的 errorcode（如 "http_429"）。"""
        until = time.time() + seconds
        with self._lock:
            self._until[supply_id] = until
            if reason:
                self._reason[supply_id] = reason

    def clear_all(self) -> None:
        """清空所有 supply 的冷却（仅手动 reload 调用，mtime 自动 reload 绝不调用）。"""
        with self._lock:
            self._until.clear()
            self._reason.clear()

    def snapshot(self) -> dict[str, dict[str, float | str]]:
        """返回 supply_id -> {"remain": 剩余秒, "reason": errorcode}（仅含仍在冷却中的 supply）。"""
        now = time.time()
        with self._lock:
            items = list(self._until.items())
            reasons = dict(self._reason)
        result: dict[str, dict] = {}
        for supply_id, until in items:
            remaining = until - now
            if remaining > 0:
                result[supply_id] = {
                    "remain": round(remaining, 1),
                    "reason": reasons.get(supply_id, ""),
                }
        return result


# ---------------------------------------------------------------------------
# L2 路由决策（全新，纯函数 + 常量）
# ---------------------------------------------------------------------------


def resolve_cooldown_seconds(errorcode, cs: "ConfigStore") -> int | None:
    """按 errorcode 查顶层 cooldown_rules，首条命中返回 cooldown_seconds，未命中返回 None。

    errorcode: int(HTTP 状态码) 或 "URLError" 字符串。
    无 supply 级覆盖，所有 supply 共用顶层策略组。
    """
    for rule in cs.get_cooldown_rules():
        if errorcode in rule.get("errorcode", []):
            return int(rule["cooldown_seconds"])
    return None


# 未配置策略的 errorcode 命中计数（线程安全，纯内存，重启清零）。
# 让用户通过 /model_proxy/status 看到"哪些 code 撞了但没配策略"，据此补 cooldown_rules。
_unconfigured_hits: dict[str, int] = {}
_unconfigured_lock = threading.Lock()


def _record_unconfigured(code) -> None:
    """记录未命中 cooldown_rules 的 errorcode（int 或 "URLError"），全局计数。"""
    key = str(code)
    with _unconfigured_lock:
        _unconfigured_hits[key] = _unconfigured_hits.get(key, 0) + 1


def _snapshot_unconfigured_hits() -> dict[str, int]:
    """返回当前 unconfigured_hits 快照（浅拷贝）。"""
    with _unconfigured_lock:
        return dict(_unconfigured_hits)


# ---------------------------------------------------------------------------
# 正文错误识别（body error）：200 + 正文携带错误文本 → 虚拟 errorcode（body_*）
# 走既有 resolve_cooldown_seconds → cd.cooldown 链路（设计 2026-10-10 §2.2）
# ---------------------------------------------------------------------------

_BODY_ERROR_MAX_TEXT_LEN_DEFAULT = 200   # text 全文长度上限（rule 级 max_text_len 可覆盖）


def match_body_error(text: str, rules: list | None, *,
                     saw_tool_or_thinking: bool, text_len: int) -> str | None:
    """正文错误识别纯函数：返回命中的虚拟 errorcode，未命中/结构条件不满足返回 None。

    误伤防护——三条结构条件全部满足才判错误（设计 §2.2）：
    1. 响应无 tool_use、无 thinking 块（正常回复一旦开始思考/调工具即排除）；
    2. text 全文长度 ≤ max_text_len（缺省 200，rule 级可覆盖）——条件 2 先行短路，
       超限一次整数比较即出，短响应才做个位数模式匹配；
    3. 模式按 match 档命中（contains 默认 | exact | prefix，不做 regex）。

    非法 rule（非 dict / 缺 pattern / 缺 errorcode）容错跳过，不抛异常。
    """
    if not rules or saw_tool_or_thinking or not isinstance(text, str):
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        code = rule.get("errorcode")
        if not isinstance(code, str) or not code:
            continue
        max_len = rule.get("max_text_len", _BODY_ERROR_MAX_TEXT_LEN_DEFAULT)
        if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len <= 0:
            max_len = _BODY_ERROR_MAX_TEXT_LEN_DEFAULT
        if text_len > max_len:
            continue
        match = rule.get("match") or "contains"
        if match == "exact":
            hit = text == pattern
        elif match == "prefix":
            hit = text.startswith(pattern)
        else:   # contains（默认，未知取值同按 contains 容错）
            hit = pattern in text
        if hit:
            return code
    return None


def _extract_body_error_text(resp_body: bytes) -> tuple[str, bool]:
    """非流式响应的正文错误 text 提取（设计 §2.3 + 复核轻5）。

    返回 (text, saw_tool_or_thinking)：
    - JSON 合法且有 content text 块 → 拼接块文本；content 含 tool_use/thinking 块
      时置结构标记；
    - 无 content text（content 空 / 错误文本在 error 等顶层字段 / JSON 解析失败）
      → raw UTF-8 全文兜底（兜底条件是「无 content text」而非仅「解析失败」，
      覆盖 JSON 合法的错误字段形态）。
    """
    saw = False
    try:
        pj = json.loads(resp_body)
    except Exception:
        return resp_body.decode("utf-8", "replace"), False
    texts: list[str] = []
    if isinstance(pj, dict):
        content = pj.get("content")
        if isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype in ("tool_use", "thinking"):
                    saw = True
                elif btype == "text" and isinstance(blk.get("text"), str):
                    texts.append(blk["text"])
    if texts:
        return "".join(texts), saw
    return resp_body.decode("utf-8", "replace"), saw


def _match_body_error_from_response(resp_body: bytes, rules: list | None) -> str | None:
    """非流式响应字节 → 命中的虚拟 errorcode（无规则/未命中返回 None）。"""
    if not rules:
        return None
    text, saw = _extract_body_error_text(resp_body)
    return match_body_error(text, rules, saw_tool_or_thinking=saw, text_len=len(text))


# 组合分发模式（精确组合命名）
PASSTHROUGH = "passthrough"
ANTHROPIC_TO_CHAT = "anthropic_to_chat"            # 原 FORWARD
RESPONSES_TO_ANTHROPIC = "responses_to_anthropic"  # 原 REVERSE
ANTHROPIC_TO_RESPONSES = "anthropic_to_responses"  # 新增
UNSUPPORTED = "unsupported"

_TRANSLATOR_TABLE = {
    ("anthropic", "anthropic"): PASSTHROUGH,
    ("responses", "responses"): PASSTHROUGH,
    ("anthropic", "chat"): ANTHROPIC_TO_CHAT,
    ("responses", "anthropic"): RESPONSES_TO_ANTHROPIC,
    ("anthropic", "responses"): ANTHROPIC_TO_RESPONSES,
}

# ---------------------------------------------------------------------------
# ④b 预算治理：反应式 ×2 阶梯重试（③ 撤销后的唯一预算机制，见
# docs/designs/2026-08-07-reasoning-thinking-truncation-and-protocol-consistency.md §④）
# ---------------------------------------------------------------------------
_BUDGET_CEILING = 131072       # 放大封顶（硬编码，不暴露到 config）
_BUDGET_RETRY_MAX = 5          # 放大重试次数上限（config budget_retry.max_retries 可覆盖）

# ② 反向缺省预算（responses→anthropic 客户端不传 max_tokens 时）：按 remap 结果分档——
# 本请求将产生 thinking 用 16384（4096 对 reasoning 模型是陷阱默认值，思考会占满预算
# 挤出正文）；非 thinking 维持 4096。
_THINKING_MAX_TOKENS_DEFAULT = 16384
_NON_THINKING_MAX_TOKENS_DEFAULT = 4096

# ④b 出站预算字段名按协议分（架构审查 R2）：PASSTHROUGH responses→responses 是
# max_output_tokens 不是 max_tokens。
_BUDGET_FIELD_BY_PROTOCOL = {
    "anthropic": "max_tokens",
    "chat": "max_completion_tokens",
    "responses": "max_output_tokens",
}


def extract_client_token(headers) -> str:
    """从入站请求头提取 client_token。

    Anthropic 原生 API 标准鉴权方式是 x-api-key（无 Bearer 前缀）；OpenAI Chat
    Completions/Responses API 标准方式是 Authorization: Bearer <key>。不同生态的
    客户端可能只发其中一种，均需支持，否则某些合法客户端（如仅发 x-api-key 的
    Anthropic 风格客户端）会因 token 解析为空而 401（no strategy/route matched）。

    优先级：Authorization: Bearer 优先，缺失则回退 x-api-key，都无则返回空串。
    两者都提供但值不同时取 Authorization（此处 client_token 只是路由查表键，
    无密钥校验语义，不报错；与出站转发同一 appkey 双发 Authorization+x-api-key
    保持对称）。

    两个边界处理（RFC 6750 规定 Bearer scheme 大小写不敏感；x-api-key 常见客户端
    实现可能带首尾空白）：
    - "Bearer " 前缀判断大小写不敏感（如 "bearer xxx"/"BEARER xxx" 也要识别）。
    - x-api-key 取值 strip 首尾空白，避免带空白的 token 直接进查表导致查不到 strategy。
    """
    auth_header = headers.get("Authorization", "") or ""
    if auth_header[:7].lower() == "bearer ":
        return auth_header[7:].strip()
    return (headers.get("x-api-key", "") or "").strip()


def extract_session_key(body_json):
    """从 metadata.user_id 提取 session_id，取不到返回 None。

    临时观测函数，用于验证 session_route_dispatch 方案的 session_key 假设
    （见 docs/designs/2026-07-28-session-route-dispatch-design.md §1/§4b）。

    实测修正（2026-07-28 沙箱实测）：真实 CC 请求的 metadata.user_id 不是设计文档
    最初假设的拼接字符串 "user_..._session_<uuid>"，而是一个 JSON 字符串，形如：
      '{"device_id":"...","account_uuid":"","session_id":"<uuid>"}'
    需要二次 json.loads 后取 "session_id" 字段。
    """
    if not isinstance(body_json, dict):
        return None
    user_id = (body_json.get("metadata") or {}).get("user_id")
    if not isinstance(user_id, str):
        return None
    try:
        inner = json.loads(user_id)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(inner, dict):
        return None
    session_id = inner.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def extract_responses_session_key(body_json):
    """从 Responses 请求体提取稳定 session 标识，取不到返回 None。

    候选顺序（宁缺勿错，全部要求非空字符串）：
    1. prompt_cache_key —— codex-cli 实测会带（docs/designs/
       2026-08-22-跨协议静默丢弃审计与修复方案.md），且按会话稳定；
    2. previous_response_id —— 服务端有状态续接时的响应链根；
    3. conversation.id —— 部分网关/SDK 会显式带，同一会话稳定。

    注意：conversation.id 在请求侧并非 OpenAI Responses 官方必填字段，
    只作为最后兜底；三者都缺时保持 None，不制造伪 session（否则会破坏
    route_pool 一致性哈希语义）。
    """
    if not isinstance(body_json, dict):
        return None
    for key in ("prompt_cache_key", "previous_response_id"):
        value = body_json.get(key)
        if isinstance(value, str) and value:
            return value
    conversation = body_json.get("conversation")
    if isinstance(conversation, dict):
        value = conversation.get("id")
        if isinstance(value, str) and value:
            return value
    return None


# 已知 SDK 注入文案改写表（docs/designs/2026-08-09-cli-thinking-only-nudge文案proxy改写.md）。
# 键：claude CLI thinking_only_retry 注入的 nudge 原文（2.1.197 硬编码孤立字符串，
# CLI 升级若改文案则静默失配——fail-open 原样透传，无崩溃风险；失配主动核对方式见
# 验证方式 E3）。
_NUDGE_TEXT_ORIG = ("[Your previous response had no visible output. "
                    "Please continue and produce a user-visible response.]")
_NUDGE_TEXT_REWRITTEN = (
    "[Harness auto-retry notice — generated by the client runtime, NOT by the user. "
    "The user did not send an empty message. "
    "Your previous turn contained only internal reasoning with no visible output, "
    "so nothing was executed. Skip any apology or meta-commentary: "
    "if you intended to call a tool, emit the tool call now; "
    "otherwise produce your visible reply now.]"
)


def _rewrite_known_injected_texts(body: dict) -> bool:
    """精确改写已知 SDK 注入文案（user 消息的 string / text block 两种形态）。

    纯函数，返回是否发生改写（调用方据此重序列化 raw_body + 置 _acc 观测字段）。
    精确匹配（strip 后全等），不做子串替换；不匹配一律原样保留（fail-open）。
    """
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        return False
    dirty = False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip() == _NUDGE_TEXT_ORIG:
                msg["content"] = _NUDGE_TEXT_REWRITTEN
                dirty = True
        elif isinstance(content, list):
            for blk in content:
                if (isinstance(blk, dict) and blk.get("type") == "text"
                        and isinstance(blk.get("text"), str)
                        and blk["text"].strip() == _NUDGE_TEXT_ORIG):
                    blk["text"] = _NUDGE_TEXT_REWRITTEN
                    dirty = True
    return dirty


def detect_operation(path: str) -> str:
    """按规范化精确路径识别操作，不从 body 猜测。"""
    clean = urllib.parse.urlparse(path).path.rstrip("/").lower()
    if clean.endswith("/v1/messages/count_tokens"):
        return OP_COUNT_TOKENS
    if clean.endswith("/v1/messages"):
        return OP_MESSAGES
    if clean.endswith("/v1/responses"):
        return OP_RESPONSES
    if clean.endswith("/chat/completions"):
        return OP_CHAT_COMPLETIONS
    return OP_UNKNOWN


def detect_source(path: str, body: dict | None) -> str:
    """识别入站 source 协议。

    path 尾 /v1/messages → anthropic；/v1/responses → responses；
    /chat/completions → chat；否则看 body 特征；都不中 → unknown。
    """
    clean = path.split("?", 1)[0].rstrip("/")
    clean_lower = clean.lower()
    if (clean_lower.endswith("/v1/messages")
            or clean_lower.endswith("/v1/messages/count_tokens")):
        return "anthropic"
    if clean_lower.endswith("/v1/responses"):
        return "responses"
    if clean_lower.endswith("/chat/completions"):
        return "chat"
    # body 特征兜底
    if isinstance(body, dict):
        if "input" in body:            # Responses API 用 input
            return "responses"
        if "messages" in body:
            # Anthropic 用 max_tokens + system 顶层；Chat 用 messages（无 max_tokens 亦可）
            if "max_tokens" in body or "system" in body:
                return "anthropic"
            return "chat"
    return "unknown"


_UPSTREAM_ERROR_REDACT_BUILTINS = ("appkey", "token", "api-key", "authorization", "secret")


def _redact_upstream_error_text(text: str, patterns: list[str]) -> str:
    """对错误体文本做保守脱敏：内置敏感键 + 配置额外子串。

    只做子串替换，不解析 JSON——错误体结构不可控，解析反而可能失效。
    """
    out = text
    for pattern in [*_UPSTREAM_ERROR_REDACT_BUILTINS, *(patterns or [])]:
        if pattern:
            out = out.replace(pattern, "***")
    return out


def _log_upstream_error_body(resp_body: bytes, status: int, supply_id: str,
                             target: str, cfg: dict) -> None:
    """按配置记录上游非 2xx 错误体（截断 + 脱敏）。fail-open：异常只跳过。"""
    if not (cfg and cfg.get("enabled")):
        return
    try:
        max_bytes = int(cfg.get("max_bytes", 4096))
        raw = resp_body[:max_bytes].decode("utf-8", "replace")
        text = re.sub(r"\s+", " ", raw).strip()
        text = _redact_upstream_error_text(text, cfg.get("redact_patterns") or [])
        log.warning(
            "UPSTREAM_ERROR status=%s supply=%s target=%s body=%r",
            status, supply_id, target, text[:max_bytes])
    except Exception:
        pass


def build_target_url(supply_url: str, request_path: str, operation: str) -> str:
    """构造终态 URL；count_tokens 只接受 messages 端点并追加一次后缀。"""
    parsed = urllib.parse.urlsplit(supply_url)
    base_path = parsed.path.rstrip("/")
    if operation == OP_COUNT_TOKENS:
        lower = base_path.lower()
        if lower.endswith("/count_tokens"):
            raise ValueError("count_tokens supply url must not include /count_tokens")
        if not lower.endswith("/v1/messages"):
            raise ValueError("count_tokens supply url must end with /v1/messages")
        base_path += "/count_tokens"
    request_query = _sanitize_forward_query(request_path).removeprefix("?")
    merged_query = urllib.parse.urlencode(
        urllib.parse.parse_qsl(parsed.query) + urllib.parse.parse_qsl(request_query))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, base_path, merged_query, parsed.fragment))


def _sanitize_forward_query(path: str) -> str:
    """出站 URL 的 query 净化：从客户端 path 的 query 里剔除 beta 参数后重新编码。

    返回值形如 "?foo=1"（含 query 时）或 ""（无 query 时），供调用方拼在完整终态
    端点 url 之后。原名 _build_passthrough_target_url，因四个转发分支现在统一复用
    同一条 target_url 计算逻辑（supply.url 已是完整终态端点，不再需要按分支拼接
    协议相关后缀），故只保留其中仍然必须的 query 净化部分，改名去掉
    "passthrough" 专属语义，其余拼接逻辑不再需要（见 core/server.py 中 target_url
    的统一计算处）。

    为什么统一丢弃客户端 path：客户端 SDK 把 base_url 配到本代理后，各家自己拼接
    path 的方式不统一（有的会拼 /v1/messages，有的拼 /v1/responses，有的直连根
    路径），代理入站不应该对这些差异敏感；只用配置好的 supply.url 发出真实上游
    请求，才能保证转发目标稳定，不受客户端拼接方式影响，避免路径重复（如
    /v1/responses/v1/responses 这种旧 bug）。

    为什么不怕丢 path 里的信息：PASSTHROUGH 只在 source ∈ {anthropic, responses}
    时触发（见 _TRANSLATOR_TABLE），而 detect_source 仅在 path 精确以
    /v1/messages 或 /v1/responses 结尾时才判定为这两种 source。带资源 ID 的子
    路径（如 /v1/messages/{id}、/v1/files/{id}）尾缀不匹配、body 也无对应特征
    时会落到 unknown，pick_translator 返回 UNSUPPORTED 直接 501 拒绝，根本进不
    了 PASSTHROUGH 分支。因此走到这里的请求，其 path 里除标准端点尾缀外不包含
    任何有意义信息，丢弃是安全的。非 PASSTHROUGH 分支的客户端 path 同理不包含
    有意义信息，一并统一净化 query 不影响正确性。
    """
    parsed = urllib.parse.urlparse(path)
    qs = {k: v for k, v in urllib.parse.parse_qsl(parsed.query) if k not in {"beta"}}
    return "?" + urllib.parse.urlencode(qs) if qs else ""


_MODEL_TIER_MAP = {
    "claude-opus": "opus",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
    "claude-fable": "fable",
}


def resolve_strategy(strategies: list, client_token: str) -> "dict | None":
    """client_token → 匹配的第一条 strategy 记录本身（不解析 route）。"""
    for s in strategies:
        if s.get("client_token") == client_token:
            return s
    return None


def extract_route_candidates(strategy: "dict | None", session_key: "str | None",
                              routes_map: dict) -> list:
    """给定 strategy，返回按 session_hash 排好的 route 候选顺序列表（route dict 列表）。

    用于 §3 选项B「pin route 全挂时跨 route 兜底」：候选列表第一项是主选（pin）
    route，后续项按顺序作为兜底候选。设计见
    docs/designs/2026-07-28-session-route-dispatch-design.md §2/§4/§4b。

    - strategy 为 None → 返回 []（无匹配）。
    - strategy 只有旧字段 route_id（无 route_pool）→ 返回长度<=1 的列表，完全
      向后兼容现状（route_id 不在 routes_map 中则返回 []）。
    - strategy 有 route_pool（新写法）：
      1. route_pool 每项 route_id 须存在于 routes_map，非法项跳过并 log.warning，
         不因一条脏配置拖垒整个 strategy。
      2. 若 dispatch.session_overrides[session_key] 命中且该 route_id 存在于
         routes_map → 该 route 放第一候选，其余候选按一致性哈希排出的顺序
         （命中项排除）跟在后面作为兜底。
      3. 未命中 override 且 session_key 非空 → 一致性哈希
         idx = md5(session_key) % 权重总和 定位主选 route，其余按权重区间顺序
         （从主选处开始整体旋转）跟在后面兜底。
      4. session_key 为空/缺失 → dispatch.fallback（当前只支持
         "on_missing_first"）：route_pool 首项为主选，其余按原顺序跟随。
    """
    if not strategy:
        return []

    route_pool = strategy.get("route_pool")
    if not route_pool:
        # 旧写法：单值 route_id，行为与改动前完全一致。
        route = routes_map.get(strategy.get("route_id"))
        return [route] if route else []

    if strategy.get("route_id"):
        # OPT-06：校验已挪到 reload 时统一跑（ConfigStore._validate_config），热路径降 DEBUG 保底。
        log.debug("strategy=%s has both route_id and route_pool, ignoring route_id",
                  strategy.get("client_token", ""))

    # 新写法：先校验 route_pool 每项引用合法性，跳过非法项。
    valid_pool: list[dict] = []
    for item in route_pool:
        rid = item.get("route_id") if isinstance(item, dict) else None
        if not rid or rid not in routes_map:
            # OPT-06：校验已挪到 reload 时统一跑，热路径降 DEBUG 保底。
            log.debug("route_pool entry invalid, skip: strategy=%s route_id=%r",
                      strategy.get("client_token", ""), rid)
            continue
        weight = item.get("weight", 1)
        if not isinstance(weight, int) or weight <= 0:
            weight = 1
        valid_pool.append({"route_id": rid, "weight": weight})
    if not valid_pool:
        return []

    def _hash_rotate(pool: list, key: str) -> list:
        """按一致性哈希定位主选项在 pool 中的位置，整体旋转后返回（主选在首位）。"""
        total_weight = sum(p["weight"] for p in pool)
        idx = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % total_weight
        cum = 0
        primary_pos = 0
        for i, p in enumerate(pool):
            cum += p["weight"]
            if idx < cum:
                primary_pos = i
                break
        return pool[primary_pos:] + pool[:primary_pos]

    dispatch = strategy.get("dispatch") or {}
    session_overrides = dispatch.get("session_overrides") or {}

    if session_key:
        override_rid = session_overrides.get(session_key)
        ordered = _hash_rotate(valid_pool, session_key)
        if override_rid and override_rid in routes_map:
            rest = [routes_map[p["route_id"]] for p in ordered if p["route_id"] != override_rid]
            return [routes_map[override_rid]] + rest
        return [routes_map[p["route_id"]] for p in ordered]

    # session_key 缺失 → dispatch.fallback（当前只实现 on_missing_first）：
    # route_pool 首项为主选，其余按原顺序跟随。
    return [routes_map[p["route_id"]] for p in valid_pool]


def resolve_tier(model: str | None) -> str | None:
    """阶段2：model字符串精确查表 → tier名。不做子串猜测。"""
    if not model:
        return None
    return _MODEL_TIER_MAP.get(model)


def resolve_source_capability(strategy: "dict | None", tier: "str | None") -> ModelReasoningCapability:
    """source 侧能力建模：从 strategy 记录的 tiers_source_capability[tier] 取 source 侧能力；
    strategy 为 None、无该字段、或该 tier 未声明 → 回退默认全档序列（与 target 侧"未配置时走
    默认5档"的处理原则一致）。

    tiers_source_capability 挂在 strategy（client_token 归属）下，而不是顶层独立表——
    client_token 才是真正代表"哪个客户端接入"的身份标识（request_model 字面值被多个 SDK
    共享，不代表客户端身份，见 README「tiers_source_capability」一节说明）。

    单条 tier entry 结构 {"effort_enum":[...], "off_alias":...} 与 target 侧
    supply["reasoning_capability"] 同构，包一层 "reasoning_capability" 键后复用同一个
    ModelReasoningCapability.from_config 解析，不重复实现解析逻辑。

    与 _MODEL_TIER_MAP/resolve_tier/resolve_strategy/select_supply 正交并存，不改动
    既有 tier 路由逻辑。
    """
    tier_map = (strategy or {}).get("tiers_source_capability") if strategy else None
    entry = tier_map.get(tier) if isinstance(tier_map, dict) and tier else None
    return ModelReasoningCapability.from_config({"reasoning_capability": entry} if entry else None)


def select_supply_list(route: dict, tier: str) -> list | None:
    """阶段3：route的tiers字典按tier名取出supplies列表。"""
    return (route.get("tiers") or {}).get(tier)


def select_supply(supplies: list, supply_map: dict, cooldown: "CooldownStore",
                  tried_set: set) -> dict | None:
    """从 supplies 列表有序取第一个未冷却且未试过的 supply。"""
    for sid in supplies:
        if sid in tried_set:
            continue
        if sid not in supply_map:
            continue
        if cooldown.is_cooling(sid):
            continue
        return supply_map[sid]
    return None


def select_supply_for_operation(supplies: list, supply_map: dict, cooldown: "CooldownStore",
                                operation: str, failover: str) -> tuple[dict | None, str | None]:
    """选择 operation 兼容 supply；第二项为已解析 target，None 表示无兼容项。"""
    for sid in supplies:
        supply = supply_map.get(sid)
        if supply is None or cooldown.is_cooling(sid):
            continue
        target = detect_target(supply)
        if operation_compatible(operation, target):
            return supply, target
        if failover != "on":
            return None, None
    return None, None


def detect_target(supply: dict) -> str:
    """target 协议：优先显式 supply["protocol"]，否则从 supply["url"] 尾缀推断
    （唯一权威实现见 core.reasoning.registry.resolve_protocol，本函数不重复判断逻辑）。
    推断失败时抛 ValueError，由调用方（_forward）捕获并转为合法错误响应。
    """
    return resolve_protocol(supply)


# ---------------------------------------------------------------------------
# supply 层扩展字段（extra_headers / system_inject / appkey_file）
# 设计记录：docs/designs/2026-09-20-model_proxy接入mcli的supply层扩展.md
# ---------------------------------------------------------------------------

# extra_headers 禁键（小写比较）：鉴权归 appkey/appkey_file 管，避免双来源
# 冲突；content-length 由 body 长度决定，不可覆盖。
_EXTRA_HEADER_FORBIDDEN_KEYS = {"authorization", "x-api-key", "content-length"}


def validate_supply_ext(supply: dict) -> None:
    """supply 层扩展字段校验（extra_headers / system_inject / appkey_file）。

    与 _validate_config 既有容错校验（log.warning 不阻断加载）不同，本组
    校验 raise ValueError：启动路径 fail-fast 阻断加载；热重载路径由
    _reload_locked 吞异常降级为 warning（已知接受）。_config_ops.py 的
    supply_add/supply_edit 写盘前调用同款校验拒写盘（单一真相源）。
    """
    sid = supply.get("id", "")
    eh = supply.get("extra_headers")
    if eh is not None:
        if not isinstance(eh, dict):
            raise ValueError(f"supply {sid}: extra_headers 必须为 dict")
        for k, v in eh.items():
            if not isinstance(v, str) or not v:
                raise ValueError(f"supply {sid}: extra_headers[{k!r}] 值必须为非空 string")
            if k.lower() in _EXTRA_HEADER_FORBIDDEN_KEYS:
                raise ValueError(
                    f"supply {sid}: extra_headers 禁止键 {k!r}（authorization/x-api-key "
                    "鉴权归 appkey/appkey_file 管；content-length 由 body 长度决定）")
    if supply.get("system_inject"):
        # 终态协议以 resolve_protocol 为准（显式 protocol 优先，缺省从 url 推断）
        protocol = resolve_protocol(supply)
        if protocol != "anthropic":
            raise ValueError(
                f"supply {sid}: system_inject 仅支持 anthropic 协议 supply"
                f"（当前 {protocol}）")
    has_appkey = bool(supply.get("appkey"))
    has_appkey_file = bool(supply.get("appkey_file"))
    if has_appkey and has_appkey_file:
        raise ValueError(f"supply {sid}: appkey 与 appkey_file 互斥，只能配其一")
    if not has_appkey and not has_appkey_file:
        raise ValueError(f"supply {sid}: 须配置 appkey 或 appkey_file 之一")


def inject_system_block(body_json: dict, text: str) -> bool:
    """system_inject：向 body["system"] 前置合并注入块（幂等，原地修改）。

    合并矩阵（设计记录 §2）：
    - system 缺省 / 空数组 → [inject_block]
    - 字符串 → [inject_block, {"type":"text","text":原值}]
    - 非空数组 → 首块 text == 注入文本 → 跳过（幂等硬约束：budget_retry /
      failover 重试复用同一 body_json，重复注入会叠加计费标识块）；
      否则前置插入
    - 首块畸形（非 dict）→ 视为不匹配 → 前置插入
    返回是否发生修改（False = 幂等跳过；未知形态不动）。
    """
    inject_block = {"type": "text", "text": text}
    system = body_json.get("system")
    if system is None or (isinstance(system, list) and not system):
        body_json["system"] = [inject_block]
        return True
    if isinstance(system, str):
        body_json["system"] = [inject_block, {"type": "text", "text": system}]
        return True
    if isinstance(system, list):
        first = system[0]
        if isinstance(first, dict) and first.get("text") == text:
            return False
        body_json["system"] = [inject_block] + system
        return True
    return False


def resolve_appkey(supply: dict) -> str:
    """出站鉴权值：appkey 直取，或 appkey_file 每请求读取解析。

    - appkey_file 支持 ~ 展开（os.path.expanduser）；逐行 strip 后精确匹配
      "AUTHORIZATION:" 前缀取值（目标文件非标准 YAML，勿用 yaml 库；
      与 CatpawCLI token store 同款策略，无缓存）。
    - 失败（文件不存在 / 无 AUTHORIZATION 行 / 值为空）抛 ValueError（消息含
      supply id 与原因），调用方出站前 500 返回、不进 cooldown（配置问题
      非上游问题，冷却会误伤 failover）。
    - appkey 与 appkey_file 均缺 → 返回 ""（维持历史空串照发行为；必填由
      validate_supply_ext 在加载/写盘层把关，热重载降级路径不额外拦截）。
    """
    appkey = supply.get("appkey")
    if appkey:
        return appkey
    appkey_file = supply.get("appkey_file")
    if not appkey_file:
        return ""
    sid = supply.get("id", "")
    path = os.path.expanduser(appkey_file)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        raise ValueError(f"supply {sid} appkey_file 读取失败: {e}") from e
    for line in lines:
        line = line.strip()
        if line.startswith("AUTHORIZATION:"):
            token = line[len("AUTHORIZATION:"):].strip()
            if token:
                return token
            raise ValueError(f"supply {sid} appkey_file 读取失败: AUTHORIZATION 值为空")
    raise ValueError(f"supply {sid} appkey_file 读取失败: 无 AUTHORIZATION 行")


def pick_translator(source: str, target: str) -> str:
    """组合分发决策表。

    (anthropic,anthropic)|(responses,responses) → PASSTHROUGH
    (anthropic,chat) → ANTHROPIC_TO_CHAT；(responses,anthropic) → RESPONSES_TO_ANTHROPIC；
    (anthropic,responses) → ANTHROPIC_TO_RESPONSES；其余 → UNSUPPORTED
    """
    return _TRANSLATOR_TABLE.get((source, target), UNSUPPORTED)


def translation_error_for_upstream(error: dict | None) -> "pt.TranslationError":
    """统一上游业务错误分类；供非流状态码与流内错误事件共用。"""
    return pt.classify_upstream_error(error)


def stream_error_event_for_source(source: str, error: "pt.TranslationError") -> bytes:
    """按客户端协议生成流内失败事件，不伪造成功终态。"""
    message = str(error)
    if source == "anthropic":
        return pt.anthropic_sse_bytes({
            "type": "error", "error": {"type": "api_error", "message": message}})
    if source == "responses":
        return pt.responses_sse_bytes({
            "type": "response.failed",
            "response": {"status": "failed", "error": {
                "type": "server_error", "message": message}}})
    return b"data: " + json.dumps({"error": {"message": message}}).encode() + b"\n\n"


def error_body_for_source(source: str, http_status: int, message: str) -> bytes:
    """按 source 协议构造合法 error body（基础版，阶段4 再完善上游错误包裹）。

    http_status 决定 error.type：4xx → invalid_request_error，5xx → api_error/server_error。
    """
    if 400 <= http_status < 500:
        anthropic_type = "invalid_request_error"
        responses_type = "invalid_request_error"
    else:
        anthropic_type = "api_error"
        responses_type = "server_error"

    if source == "anthropic":
        body = {"type": "error", "error": {"type": anthropic_type, "message": message}}
    elif source == "responses":
        body = {"error": {"message": message, "type": responses_type,
                          "code": None, "param": None}}
    else:
        body = {"error": {"message": message}}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _extract_upstream_error_message(resp_body: bytes, fallback_limit: int = 500) -> str:
    """从上游 error body 提取核心 message 文本（阶段4 §5.2）。

    OpenAI/Anthropic 的 error 结构都是 `{"error": {"message": ...}}`（Anthropic 外层多包一层
    `type:"error"`，形状仍兼容）。解析失败或结构不含 message 时，回退为截断原文。
    """
    try:
        parsed = json.loads(resp_body)
    except (json.JSONDecodeError, ValueError):
        return resp_body[:fallback_limit].decode("utf-8", "replace")
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(parsed.get("message"), str):
            return parsed["message"]
    return resp_body[:fallback_limit].decode("utf-8", "replace")


def _fmt_effort(level) -> str:
    """CanonicalEffort|None → 可读字符串，如 'XHIGH(5)' / 'None'。"""
    if level is None:
        return "None"
    return f"{level.name}({int(level)})"


def _log_reasoning_debug(
    supply_id: str,
    target_model: "str | None",
    source_cap: "ModelReasoningCapability",
    target_cap: "ModelReasoningCapability",
    raw_intent: "Any",
    target_effort: "Any",
    abstract: "Any",
    reasoning_variant: str,
    reasoning_wire: dict,
) -> None:
    """reasoning debug 旁路日志：拼出"客户端意图 → 相对映射结果"的一眼可读对比。

    只在 log.isEnabledFor(logging.DEBUG) 为真时才被调用（调用点已判断一次，这里
    再判一次防御性重复调用），避免关闭开关时仍做字符串拼接。

    相对映射下 target_effort.level 可能高于、低于或等于 raw_intent.level（跨模型
    思考档数不同时会出现"increased"，这是相对排名映射的正常结果，不再像旧版绝对钳位
    那样"raised"代表异常）。
    """
    if not log.isEnabledFor(logging.DEBUG):
        return
    intent_str = _fmt_effort(raw_intent.level)
    target_str = _fmt_effort(target_effort.level)
    if raw_intent.level is not None and target_effort.level is not None:
        if target_effort.level == raw_intent.level:
            tag = "unchanged"
        elif target_effort.level < raw_intent.level:
            tag = "decreased"
        else:
            tag = "increased"  # 相对映射的正常结果（非异常），见方案文档 §3.4 haiku 例子
    elif target_effort.stripped:
        tag = "stripped"
    elif raw_intent.level == CanonicalEffort.OFF:
        tag = "off"
    else:
        tag = "n/a"
    src_cap_str = ",".join(e.name for e in source_cap.enum) if source_cap.enum else "()"
    tgt_cap_str = ",".join(e.name for e in target_cap.enum) if target_cap.enum else "()"
    log.debug(
        "reasoning_debug: supply=%s target_model=%s src_cap=[%s] tgt_cap=[%s] "
        "intent=%s(source_budget=%s,present=%s) -> target=%s [%s] abstract_kind=%s "
        "variant=%s wire=%s",
        supply_id, target_model, src_cap_str, tgt_cap_str,
        intent_str, raw_intent.source_budget, raw_intent.present,
        target_str, tag, abstract.kind.value,
        reasoning_variant, reasoning_wire,
    )


def _responses_failed_event(adapter: "pt.AnthropicToResponsesStreamAdapter", message: str) -> dict:
    """构造反向流式中途出错的 response.failed 收尾事件（反向规格 §5.1）。

    只读取 adapter 的公开状态属性（response_id/model/...）拼装骨架，不调用、不修改
    translate.py 的内部逻辑。
    """
    return {
        "type": "response.failed",
        "response": {
            "id": adapter.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "failed",
            "background": False,
            "model": adapter.model,
            "output": list(adapter.completed_items),
            "parallel_tool_calls": True,
            "conversation": {"id": adapter.conversation_id},
            "reasoning": {"effort": adapter.reasoning_effort, "summary": None},
            "service_tier": "default",
            "store": True,
            "text": {"format": {"type": "text"}, "verbosity": "medium"},
            "tool_choice": "auto",
            "tools": adapter.tools_echo,
            "truncation": "disabled",
            "error": {"message": message, "type": "server_error"},
        },
        "sequence_number": adapter.seq,
    }


# ---------------------------------------------------------------------------
# Host 头校验（listen.host 非 loopback 时启用）
#
# 安全定位（必须如实认知）：Host 头是客户端可控的 HTTP 头，allow_hosts 仅缓解浏览器
# DNS rebinding 攻击，不是客户端鉴权，也不能限制局域网设备访问——任意可达设备都可
# 伪造 Host 通过。真正的边界是"可信 LAN + 无公网映射 + client_token"，不得把本白名单
# 描述为网络边界。loopback Host（localhost/127.0.0.1/[::1]，含带端口）代码内置恒放行，
# 不进配置——本机 cli.sh / ensure 脚本 / Claude Code（MODEL_PROXY_BASE 固定 127.0.0.1）
# 永不因白名单漏配而断。
# ---------------------------------------------------------------------------

_LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def is_host_allowed(host_value: str, *, listen_port: int,
                    allow_hosts: list[str]) -> bool:
    """单个 Host 头值是否放行：loopback 恒放行；其余端口必须存在且与监听端口一致，
    且命中 allow_hosts 条目（hostname 小写比较，条目端口缺省匹配任意端口）。
    畸形 authority 一律不放行。"""
    try:
        host, port = parse_host_authority(host_value)
    except ValueError:
        return False
    if host in _LOOPBACK_HOST_NAMES:
        return True  # loopback 恒放行（含带端口形式；端口值不参与判定）
    if port is None or port != listen_port:
        return False  # 端口必须存在且与监听端口一致（host:* 形式不支持）
    for entry in allow_hosts:
        try:
            e_host, e_port = parse_host_authority(entry)
        except ValueError:
            continue  # 畸形条目不匹配（类型/结构/端口合法性已在启动时由 listen_config 校验）
        if host == e_host and (e_port is None or e_port == listen_port):
            return True
    return False


def check_request_host(host_values: list[str], *, listen_port: int,
                       allow_hosts: list[str]) -> bool:
    """Host 头集合校验：缺失（空列表）或重复（多于一个）拒绝，不取第一个；
    单值走 is_host_allowed。"""
    if len(host_values) != 1:
        return False
    return is_host_allowed(host_values[0], listen_port=listen_port,
                           allow_hosts=allow_hosts)


class HostHeaderValidator:
    """Host 头白名单校验器（仅 DNS rebinding 防护，非访问控制）。

    main() 在 listen.host 非 loopback 时装配到 server.host_validator；
    handler 层所有路由之前调用（含 /model_proxy 控制接口），与 client_token/
    admin_token 认证正交。
    """

    def __init__(self, listen_port: int, allow_hosts: list[str]):
        self.listen_port = listen_port
        self.allow_hosts = list(allow_hosts)

    def check(self, host_values: list[str]) -> bool:
        return check_request_host(host_values, listen_port=self.listen_port,
                                  allow_hosts=self.allow_hosts)


# ---------------------------------------------------------------------------
# ModelProxyHandler（控制 API + 转发编排）
# ---------------------------------------------------------------------------

class ModelProxyHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。

    实例化时需要外部注入 config_store / cooldown_store，
    通过 server 属性访问（ThreadingHTTPServer 持有引用）。
    """

    protocol_version = "HTTP/1.1"
    timeout = 30

    # 屏蔽默认日志
    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def _close_if_body_unconsumed(self) -> None:
        """Do not reuse a connection when this route leaves a possible body unread."""
        lengths = self.headers.get_all("Content-Length") or []
        encodings = self.headers.get_all("Transfer-Encoding") or []
        # Only no framing, or one strictly decimal zero, proves there is no body.
        # Do not read/discard it: a client may not send the declared bytes.
        if (encodings or len(lengths) > 1
                or (lengths and re.fullmatch(r"0+", lengths[0].strip()) is None)):
            self.close_connection = True
            self._unconsumed_body_close = True

    def _host_check(self) -> bool:
        """Host 头校验守卫：通过返回 True；不通过已回 403 并返回 False。

        仅 listen.host 非 loopback 时启用（server.host_validator 由 main() 装配，
        None 即未启用，行为与旧版完全一致）。置于所有路由之前（含控制接口），
        缺失/重复 Host、畸形 authority、白名单外一律拒绝。
        """
        validator = getattr(self.server, "host_validator", None)
        if validator is None:
            return True
        values = self.headers.get_all("Host") or []
        if validator.check(values):
            return True
        log.warning("host_check.reject req_id=%s host=%r path=%s",
                    _req_local.req_id, values, self.path)
        self._close_if_body_unconsumed()
        self._send_json(403, {"error": "host not allowed"})
        return False

    def do_GET(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
            if not self._host_check():
                return
            if self.path.startswith(_CONTROL_PATH_PREFIX):
                self._close_if_body_unconsumed()
                self._dispatch_control("GET")
            else:
                self._forward_logged("GET")
        finally:
            _req_local.req_id = None

    def do_POST(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
            if not self._host_check():
                return
            if self.path.startswith(_CONTROL_PATH_PREFIX):
                self._close_if_body_unconsumed()
                self._dispatch_control("POST")
            else:
                self._forward_logged("POST")
        finally:
            _req_local.req_id = None

    def do_PUT(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
            if not self._host_check():
                return
            self._forward_logged("PUT")
        finally:
            _req_local.req_id = None

    def do_DELETE(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
            if not self._host_check():
                return
            self._forward_logged("DELETE")
        finally:
            _req_local.req_id = None

    def do_PATCH(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
            if not self._host_check():
                return
            self._forward_logged("PATCH")
        finally:
            _req_local.req_id = None

    # ------------------------------------------------------------------
    # access 日志：整请求一条，覆盖 _forward 的整个生命周期
    # ------------------------------------------------------------------

    def _forward_logged(self, method: str) -> None:
        """包一层 _forward：收集 self._acc 字段 + 打点耗时，finally 里统一 emit 一条
        ACCESS 记录。_dispatch_control 路径不设 self._acc，故不经过此包装，也不产生
        ACCESS 记录（见 _write_* 里的 hasattr 守卫）。
        """
        self._acc = {
            "status": 0, "source": "", "operation": "", "route": "", "tier": "",
            "supply": "", "target_protocol": "", "conversion_kind": "",
            "failover": 0, "attempts": 0, "token": "",
            "usage_in": 0, "usage_out": 0,
            "strategy": "", "session": "", "route_failover": 0,
            "builtin": "",
            # ④b/⑤ 预算治理观测：budget_retried 为放大轨迹（"16000→32000[,…]"），
            # budget_truncated=1 为最终仍截断（含流式收口检测），stop_reason 为
            # 响应停止原因（能拿到才记，拿不到留空）。
            "budget_retried": "", "budget_truncated": 0, "stop_reason": "",
            # OPT-04: final_error 记错误出口的短 reason（截断 80 字符、空格转下划线）。
            # budget 截断终态（status=200 + budget_truncated=1）不写 final_error，两者正交。
            "final_error": "",
            # 正文错误识别观测：命中 body_error_rules 时记虚拟 errorcode
            # （body_rate_limited 等；最终成功的 failover 请求同样保留首命中 code）。
            "body_error": "",
            # OPT-10: attempt_errors 记 failover 失败明细（supply_id, reason），
            # 仅 2 处 failover continue 前 append（不含 budget 重试的 4 处 continue）。
            "attempt_errors": [],
            # nudge 改写观测：入站 body 命中已知 SDK 注入文案改写时置 "1"
            # （含 $route 等未转发请求——命中即置位，属预期）。
            "nudge_rewritten": "",
            # thinking 剥离重试观测：签名 400 触发 learn+剥离重试时置 1
            # （低频高价值，对齐 body_error/nudge_rewritten 先例）。
            "thinking_strip": 0,
            "response_committed": 0, "stream_integrity": "",
            "terminal_status": "", "terminal_reason": "", "first_event_ms": "",
        }
        t0 = time.monotonic()
        self._forward_started_monotonic = t0
        try:
            self._forward(method)
        except (BrokenPipeError, ConnectionResetError):
            self._acc["stream_integrity"] = "client_disconnect"
            log.info("client disconnected before request completion")
        except Exception as exc:
            a = self._acc
            a["final_error"] = "internal_escape"
            log.exception("request internal escape: %s", type(exc).__name__)
            if not a.get("response_committed"):
                try:
                    self._write_buffered_response(
                        502, [], error_body_for_source(
                            a.get("source") or "anthropic", 502, "internal proxy error"))
                except (BrokenPipeError, ConnectionResetError, OSError):
                    a["stream_integrity"] = "client_disconnect"
            else:
                self.close_connection = True
        finally:
            a = self._acc
            if a.get("response_committed") and not a.get("stream_integrity"):
                a["stream_integrity"] = "invalid"
                a["terminal_status"] = a.get("terminal_status") or "open"
                a["terminal_reason"] = a.get("terminal_reason") or "internal_escape"
                a["final_error"] = a.get("final_error") or "internal_escape"
            health = getattr(self.server, "stream_health_store", None)
            if health is not None:
                health.record(a.get("supply", ""), a.get("stream_integrity", ""))
            ms = int((time.monotonic() - t0) * 1000)
            access_log.info(
                "ACCESS ms=%d status=%s source=%s operation=%s route=%s tier=%s supply=%s "
                "target_protocol=%s conversion_kind=%s failover=%s attempts=%s "
                "usage_in=%s usage_out=%s token=%s session=%s "
                "route_failover=%s builtin=%s budget_retried=%s budget_truncated=%s "
                "stop_reason=%s final_error=%s body_error=%s nudge_rewritten=%s "
                "thinking_strip=%s "
                "response_committed=%s "
                "stream_integrity=%s terminal_status=%s terminal_reason=%s first_event_ms=%s",
                ms, a["status"], a["source"], a["operation"],
                a["route"], a["tier"], a["supply"],
                a["target_protocol"], a["conversion_kind"], a["failover"], a["attempts"],
                a["usage_in"], a["usage_out"], a["token"], a["session"],
                a["route_failover"], a["builtin"], a["budget_retried"],
                a["budget_truncated"], a["stop_reason"],
                re.sub(r"\s+", "_", a["final_error"][:80]),
                a["body_error"],
                a["nudge_rewritten"], a["thinking_strip"],
                a["response_committed"],
                a["stream_integrity"],
                a["terminal_status"], a["terminal_reason"], a["first_event_ms"])
            if usage_totals is not None:
                try:
                    usage_totals.record(a, ms)
                except Exception:
                    # Do not expose ledger data or adapter exception details.
                    log.warning("usage_totals.record failed; statistics not confirmed on disk")

    # ------------------------------------------------------------------
    # 转发编排（阶段1：纯透传路由 + cooldown + failover）
    # ------------------------------------------------------------------

    def _forward_count_tokens(self, method: str, cs: "ConfigStore", cd: "CooldownStore",
                              route: dict, tier: str, supply_map: dict,
                              body_json: dict | None) -> None:
        """count_tokens 独立非 SSE 转发，不进入生成请求 codec/translator/预算链。"""
        supplies = select_supply_list(route, tier) or []
        failover = route.get("failover", "off")
        remaining = list(supplies)
        while remaining:
            try:
                supply, target = select_supply_for_operation(
                    remaining, supply_map, cd, OP_COUNT_TOKENS, failover)
            except ValueError as e:
                self._acc["final_error"] = str(e)
                self._write_buffered_response(
                    500, [], error_body_for_source("anthropic", 500, str(e)))
                return
            if supply is None:
                if self._acc["attempts"]:
                    errs = self._acc.get("attempt_errors") or []
                    summary = "; ".join(f"{s}={e}" for s, e in errs) or "all attempts failed"
                    msg = f"all upstream supplies failed or cooling: {summary}"
                    status = 503
                else:
                    msg = "count_tokens requires an anthropic supply"
                    status = 501
                self._acc["final_error"] = msg
                self._write_buffered_response(
                    status, [], error_body_for_source("anthropic", status, msg))
                return
            sid = supply.get("id", "")
            self._acc["supply"] = sid
            self._acc["attempts"] += 1
            self._acc["target_protocol"] = target
            self._acc["conversion_kind"] = protocol_conversion_kind("anthropic", target)
            # 该 supply 已学到不支持 count_tokens → 本地直接 404，不打上游
            # （实测 mcli 返 404；客户端拿不到即本地估算，行为等价，省一次往返）。
            _ct_store = getattr(self.server, "count_tokens_store", None)
            if _ct_store is not None and _ct_store.snapshot(sid):
                self._acc["final_error"] = "count_tokens_unsupported_local"
                self._write_buffered_response(
                    404, [], error_body_for_source(
                        "anthropic", 404,
                        f"count_tokens not supported by supply {sid} (short-circuited)"))
                return
            try:
                target_url = build_target_url(supply.get("url", ""), self.path, OP_COUNT_TOKENS)
            except ValueError as e:
                self._acc["final_error"] = str(e)
                self._write_buffered_response(500, [], error_body_for_source("anthropic", 500, str(e)))
                return
            outgoing = dict(body_json or {})
            if supply.get("target_model") and "model" in outgoing:
                outgoing["model"] = supply["target_model"]
            outgoing.pop("stream", None)
            # system_inject（count_tokens 同样注入，取与主转发一致性；见设计记录 §2）
            if target == "anthropic" and supply.get("system_inject"):
                inject_system_block(outgoing, supply["system_inject"])
            send_body = json.dumps(outgoing, ensure_ascii=False).encode("utf-8")
            # appkey 直取 / appkey_file 每请求读取；失败出站前 500，不进 cooldown
            try:
                appkey = resolve_appkey(supply)
            except ValueError as e:
                self._acc["final_error"] = str(e)
                self._write_buffered_response(
                    500, [], error_body_for_source("anthropic", 500, str(e)))
                return
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in {"host", "content-length", "authorization", "x-api-key"}}
            headers.update({"Authorization": f"Bearer {appkey}", "x-api-key": appkey,
                            "Content-Type": "application/json", "Content-Length": str(len(send_body))})
            # extra_headers：supply 声明的出站 header，最高优先（跳过 content-length）
            for k, v in (supply.get("extra_headers") or {}).items():
                if k.lower() == "content-length":
                    continue
                headers[k] = v
            req = urllib.request.Request(target_url, data=send_body, headers=headers, method=method)
            try:
                resp = urllib.request.urlopen(req, timeout=cs.get_upstream_timeout())
                status = resp.status
                resp_headers = list(resp.getheaders())
                resp_body = resp.read()
                resp.close()
            except urllib.error.HTTPError as e:
                status = e.code
                resp_headers = list(e.headers.items())
                resp_body = e.read()
                # count_tokens 404 = 该 supply 不支持该端点（实测 mcli）→ learn，
                # 后续请求本地短接（不打上游、不 cooldown/failover——非故障）。
                if status == 404:
                    _ct_store = getattr(self.server, "count_tokens_store", None)
                    if _ct_store is not None:
                        _ct_store.learn(sid)
                        log.info("count_tokens_unsupported.learn: supply=%s (404)", sid)
                secs = resolve_cooldown_seconds(status, cs)
                if failover == "on" and secs is not None:
                    self._acc["failover"] = 1
                    self._acc["attempt_errors"].append((sid, f"http_{status}"))
                    cd.cooldown(sid, secs, f"http_{status}")
                    remaining = [x for x in remaining if x != sid]
                    self._acc["supply"] = ""
                    self._acc["target_protocol"] = ""
                    self._acc["conversion_kind"] = ""
                    continue
                try:
                    parsed = json.loads(resp_body)
                except (json.JSONDecodeError, ValueError):
                    parsed = None
                valid_error = (isinstance(parsed, dict) and parsed.get("type") == "error"
                               and isinstance(parsed.get("error"), dict)
                               and isinstance(parsed["error"].get("type"), str)
                               and isinstance(parsed["error"].get("message"), str))
                if valid_error:
                    self._write_buffered_response(status, resp_headers, resp_body)
                else:
                    msg = _extract_upstream_error_message(resp_body)
                    self._write_buffered_response(
                        status, [], error_body_for_source("anthropic", status, msg or "upstream error"))
                self._acc["final_error"] = f"upstream_error {status}"
                return
            except (urllib.error.URLError, OSError) as e:
                secs = resolve_cooldown_seconds("URLError", cs)
                if failover == "on" and secs is not None:
                    self._acc["failover"] = 1
                    self._acc["attempt_errors"].append((sid, f"net_error:{e}"))
                    cd.cooldown(sid, secs, f"net_error:{e}")
                    remaining = [x for x in remaining if x != sid]
                    self._acc["supply"] = ""
                    self._acc["target_protocol"] = ""
                    self._acc["conversion_kind"] = ""
                    continue
                self._acc["final_error"] = f"upstream net error: {e}"
                self._write_buffered_response(
                    502, [], error_body_for_source("anthropic", 502, f"upstream error: {e}"))
                return
            try:
                parsed = json.loads(resp_body)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if not (isinstance(parsed, dict) and type(parsed.get("input_tokens")) is int
                    and parsed["input_tokens"] >= 0):
                self._acc["final_error"] = "invalid count_tokens response"
                self._write_buffered_response(
                    502, [], error_body_for_source("anthropic", 502, "invalid count_tokens response"))
                return
            self._write_buffered_response(status, resp_headers, resp_body)
            return
        if self._acc["attempts"]:
            errs = self._acc.get("attempt_errors") or []
            summary = "; ".join(f"{sid}={reason}" for sid, reason in errs) or "all attempts failed"
            msg = f"all upstream supplies failed or cooling: {summary}"
            status = 503
        else:
            msg = "count_tokens requires an available anthropic supply"
            status = 501
        self._acc["final_error"] = msg
        self._write_buffered_response(status, [], error_body_for_source("anthropic", status, msg))

    def _forward(self, method: str):
        cs: ConfigStore = self.server.config_store
        cd: CooldownStore = self.server.cooldown_store
        pref_store: SyntaxPreferenceStore = self.server.pref_store
        thinking_strip_store: ThinkingStripStore = self.server.thinking_strip_store
        # 1. 热重载 + 读 body
        cs.maybe_reload()
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        # 2. client_token（Authorization: Bearer 优先，回退 x-api-key，见 extract_client_token 注释）
        token = extract_client_token(self.headers)
        # client_token 只是路由查表键（无密钥校验语义，见 extract_client_token），记全名。
        # 历史行的尾4位值不做迁移/反查：旧行滚动消失，跨协议聚合段只认带 operation
        # 字段的新格式行（存量行均无 operation，不受混合期影响）。
        self._acc["token"] = token or ""

        # 3. 解析 body 拿 model
        body_json: dict[str, Any] | None = None
        try:
            if raw_body:
                body_json = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            body_json = None
        request_model = body_json.get("model") if isinstance(body_json, dict) else None

        # 4. source 协议识别（须先于 session 提取：Responses/Anthropic 用不同字段）
        source = detect_source(self.path, body_json)
        self._acc["source"] = source
        operation = detect_operation(self.path)
        self._acc["operation"] = operation

        # 3b. session 提取：Responses 优先 prompt_cache_key / previous_response_id /
        # conversation.id；Anthropic 维持 metadata.user_id。取不到则 None（不制造伪
        # session，避免 route_pool 一致性哈希被空串串扰）。
        session_key = None
        if source == "responses" or operation == OP_RESPONSES:
            session_key = extract_responses_session_key(body_json)
        if session_key is None:
            session_key = extract_session_key(body_json)
        self._acc["session"] = session_key or ""

        # 5. 三阶段匹配：strategy → route候选列表（session_hash分配 + B选项跨route兜底）
        #    → tier → supplies 列表
        strategies = cs.get_strategies()
        routes_map = cs.get_routes_map()
        strategy = resolve_strategy(strategies, token)
        self._acc["strategy"] = strategy.get("client_token", "") if strategy else ""
        session_key = self._acc["session"] or None

        # ---- 已知注入文案改写层（docs/designs/2026-08-09-cli-thinking-only-nudge文案proxy改写.md）----
        # 插在 source 判定之后、内建命令门控之前：CLI nudge 等注入文案在此改写。
        # 改写后重序列化 raw_body，使 PASSTHROUGH 分支（send_body = raw_body，1436 行）
        # 在无其他 body 变更时也能带出改写；re-dump 与既有 model 改写路径（1439 行）同款操作，安全。
        # fail-open：不匹配则原样透传。
        if operation == OP_MESSAGES and source == "anthropic" and isinstance(body_json, dict):
            if _rewrite_known_injected_texts(body_json):
                raw_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                self._acc["nudge_rewritten"] = "1"   # 语义：入站 body 命中改写（含未转发的命令请求）

        # ---- 内建命令层拦截点（docs/designs/2026-08-04-in-band-route-command-design.md）----
        # 插在 source 判定之后、route 选择之前：body_json/session_key/source 均已就绪，
        # 尚未产生任何转发副作用。门控（§2.1，全部满足才进入识别，任一不满足 fail-open
        # 照常转发）：source == "anthropic" + session_key 非空 + body_json 是 dict 且含
        # messages 列表 + 有匹配的 strategy（strategy 为 None 时无处落 override，不识别为
        # 命令，回退原逻辑，最终按现状 401，行为不变）。
        sidecar: SessionOverridesSidecar = self.server.sidecar_store
        sidecar.maybe_reload()
        _builtin_command_source = None
        if (operation == OP_MESSAGES and source == "anthropic" and session_key
                and strategy is not None and isinstance(body_json, dict)
                and isinstance(body_json.get("messages"), list)):
            last_user_content = extract_last_user_message_content(body_json)
            _builtin_command_source = "anthropic"
        elif (operation == OP_RESPONSES and source == "responses" and session_key
                and strategy is not None and isinstance(body_json, dict)
                and isinstance(body_json.get("input"), list)):
            last_user_content = extract_last_user_input_content(body_json)
            _builtin_command_source = "responses"
        else:
            last_user_content = None

        if last_user_content is not None:
            is_cmd, cmd_arg = parse_route_command(last_user_content)
            if is_cmd:
                self._handle_builtin_command(cmd_arg, token, session_key, strategy,
                                              routes_map, sidecar, request_model, body_json,
                                              protocol=_builtin_command_source)
                return

        # 命中 override 的普通请求：读 sidecar 后构造浅拷贝视图再选 route，
        # 确保 $route 写入 sidecar 的最新值立刻对后续请求生效（不等 ConfigStore 重载）。
        # 浅拷贝 strategy 与 dispatch 两层，不 deepcopy、不改 ConfigStore 内部对象引用
        # （§4.2 别名污染要求），view 只是这次调用的局部变量。
        if strategy is not None:
            overrides = sidecar.get_overrides_for(strategy.get("client_token", ""))
            view = dict(strategy)
            view_dispatch = dict(strategy.get("dispatch") or {})
            view_dispatch["session_overrides"] = overrides
            view["dispatch"] = view_dispatch
        else:
            overrides = {}
            view = strategy

        route_candidates = extract_route_candidates(view, session_key, routes_map)
        if (strategy is not None and session_key
                and overrides.get(session_key) in routes_map
                and route_candidates and route_candidates[0].get("id") == overrides.get(session_key)):
            # 命中 override：只更新内存 last_seen，不写盘（热路径无 IO，见 §5.4 / V13）。
            sidecar.touch(strategy.get("client_token", ""), session_key)

        if not route_candidates:
            log.warning("no strategy/route matched: token=%s source=%s",
                        token or "", source)
            self._acc["final_error"] = "no strategy or route matched"
            self._write_buffered_response(
                401, [], error_body_for_source(source, 401, "no strategy/route matched"))
            return

        # tier 解析只与 request_model 有关，与候选哪个 route 无关，循环外只算一次。
        tier = resolve_tier(request_model)
        if tier is None:
            log.warning("unknown model tier: model=%s pinned_route=%s",
                        request_model, route_candidates[0].get("id"))
            self._acc["final_error"] = f"unknown model tier: {request_model}"
            self._write_buffered_response(
                400, [], error_body_for_source(source, 400, f"unknown model tier: {request_model}"))
            return
        self._acc["tier"] = tier

        supply_map = cs.get_supply_map()

        if operation == OP_COUNT_TOKENS:
            route = route_candidates[0]
            self._acc["route"] = route.get("id", "")
            self._forward_count_tokens(method, cs, cd, route, tier, supply_map, body_json)
            return

        _reasoning_retried = False   # reasoning 语法重试只做一次，作用域覆盖整个请求周期
        _thinking_strip_retried = False   # thinking 签名 400 剥离重试只做一次（与语法重试独立互不消耗）
        _tool_strict_retried = False      # Responses strict:false 被拒后的 strict:true 重试只做一次
        # raw_intent 必须在循环外、基于客户端原始 body_json 只 decode 一次。
        # body_json 在循环体内会被原地改写（model 改写为 target_model、reasoning_wire
        # 通过 apply_fields 写回），若在循环内重新 decode，第二轮起会对"已被上一轮写入
        # 结果污染过"的 body_json 解码，导致客户端原始意图被错误钳位/升档（bug 修复记录，
        # 见 docs/archive/model_proxy_buildplan.md 或 commit message）。remap() 仍需在循环内按每轮
        # supply 的 target capability 重新计算，因为不同 supply 的能力上限不同；
        # source capability 只依赖 strategy+tier（strategy 才代表客户端身份，
        # request_model 字面值会被多个 SDK 共享，见 resolve_source_capability 注释），
        # 同样在循环外只算一次（strategy 已在阶段5解析过，这里直接复用，不重新查找）。
        src_codec = get_codec(source)
        raw_intent = src_codec.decode(body_json or {})
        source_cap = resolve_source_capability(strategy, tier)

        # 400 自适应重试优化（方案文档 §4.3）：reasoning 语法重试的 continue 只改变
        # variant，不改变 intent/两侧 cap，remap+abstract_encode 结果可复用；只有
        # failover 换 supply 的 continue 才需要重新计算（target_cap 可能不同）。
        _reasoning_cache_supply_id: "str | None" = None
        _cached_target_effort = None
        _cached_abstract = None

        # ---- ④b 预算治理：反应式 ×2 阶梯重试（唯一预算机制，设计记录 §④）----
        # _budget_retries 是计数器（int，≤max_retries；架构审查 R3：不是布尔位），与
        # 语法重试 _reasoning_retried 状态独立、可同请求先后发生（先 400 语法重试、
        # 后 200 截断预算重试），互不消耗对方次数。
        # _budget_current 为当前生效预算，0=未定（首轮 stamp 时从出站 body 读回客户端
        # 有效值；反向缺省场景由 ② 的 max_tokens_default 先行填入，读回的即是有效值）。
        # R4 声明（有意行为）：两者都是请求周期作用域——爬升途中触发 failover 换 supply
        # 时，放大后的预算被下一 supply 继承（预算不足是模型属性，同 tier 换 supply
        # 不必从起点重爬）。
        budget_cfg = cs.get_budget_retry()
        _budget_retries = 0
        _budget_current = 0

        def _stamp_budget(outgoing: dict, target_protocol: str) -> None:
            """④b 出站预算 stamp（构建完 outgoing body 后、json.dumps 前调用）：
            首轮读回客户端有效预算作爬升起点（写回原值，无行为变化）；重试轮覆写为
            放大值。字段名分协议（R2，_BUDGET_FIELD_BY_PROTOCOL）。"""
            nonlocal _budget_current
            field = _BUDGET_FIELD_BY_PROTOCOL.get(target_protocol)
            if not field or not isinstance(outgoing, dict):
                return
            if _budget_current:
                outgoing[field] = _budget_current
            else:
                try:
                    _budget_current = int(outgoing.get(field) or 0)
                except (TypeError, ValueError):
                    _budget_current = 0

        def _maybe_budget_retry(raw_resp_bytes: bytes, target_protocol: str, sid: str) -> bool:
            """④b 截断检测 + 放大决策（仅非流式成功响应、resp.read() 之后/转换之前调用）。

            在原始上游响应上判定（R1 纯函数 pt.is_budget_truncated；chat 方向必须在
            转换前判，否则被 _ENABLE_REASONING_FALLBACK 填 text 掩盖）。命中且未达上限
            → 预算 ×2（封顶 ceiling）、计数 +1、返回 True，调用方随后 resp.close() +
            continue 重进 while：同 supply 重选（不 cooldown、不进 tried_set、不计
            failover），remap 缓存经 _reasoning_cache_supply_id 复用，重试只改预算
            不改档。命中但已到上限/次数耗尽/预算基线未知 → 记 budget_truncated=1、
            返回 False，调用方如实写回截断响应。
            """
            nonlocal _budget_retries, _budget_current
            if not budget_cfg["enabled"]:
                return False
            if not pt.is_budget_truncated(target_protocol, raw_resp_bytes):
                return False
            nxt = 0
            if _budget_current > 0 and _budget_retries < budget_cfg["max_retries"]:
                nxt = min(_budget_current * 2, _BUDGET_CEILING)
                if nxt <= _budget_current:
                    nxt = 0   # 已达封顶（next==current），停止
            if not nxt:
                self._acc["budget_truncated"] = 1
                log.warning(
                    "budget_truncated: supply=%s budget=%s retries=%d "
                    "（到上限/无预算基线，如实返回截断响应）",
                    sid, _budget_current, _budget_retries)
                return False
            old = _budget_current
            _budget_current = nxt
            _budget_retries += 1
            trail = self._acc.get("budget_retried") or ""
            self._acc["budget_retried"] = (
                f"{trail},{old}→{nxt}" if trail else f"{old}→{nxt}")
            log.warning("budget_retry: supply=%s budget %s→%s (%d/%d)",
                        sid, old, nxt, _budget_retries, budget_cfg["max_retries"])
            return True

        # 6. route 候选外层循环（§3 选项B：pin route/其tier全挂时，按 session_hash
        #    排好的候选顺序换下一个候选 route 重试；单候选（旧单值 route_id 配置）时
        #    该外层循环退化为只跑一轮，行为与改动前完全一致，不产生 route_failover）。
        num_candidates = len(route_candidates)
        for candidate_idx, route in enumerate(route_candidates):
            self._acc["route"] = route.get("id")
            is_last_candidate = candidate_idx == num_candidates - 1

            supplies_list = select_supply_list(route, tier)
            if not supplies_list:
                if not is_last_candidate:
                    # OPT-05 双发合并：非末候选只打动作行（消息内含原因），吞并条件行
                    log.warning(
                        "route_failover: route=%s missing tier=%s config, trying next candidate route",
                        route.get("id"), tier)
                    self._acc["route_failover"] = 1
                    continue
                # 末候选只打条件行
                log.warning("route missing tier config: route=%s tier=%s", route.get("id"), tier)
                self._write_buffered_response(
                    503, [], error_body_for_source(
                        source, 503, f"route {route.get('id')} missing tier {tier}"))
                self._acc["final_error"] = f"route {route.get('id')} missing tier {tier}"
                return

            failover = route.get("failover", "off")

            # tried_set 为请求内局部集合，每个候选 route 重置（不同 route 下 supply id
            # 命名空间独立，冷却/已试状态不应跨 route 互相污染）；不改全局状态。
            tried_set: set[str] = set()
            route_exhausted = False

            while True:
                supply = select_supply(supplies_list, supply_map, cd, tried_set)
                if supply is None:
                    log.warning("all supplies failed or cooling: route=%s tier=%s",
                                route.get("id"), tier)
                    route_exhausted = True
                    break

                supply_id = supply.get("id", "")
                self._acc["supply"] = supply_id
                self._acc["attempts"] += 1
                try:
                    target = detect_target(supply)
                except ValueError as e:
                    log.error("detect_target failed: supply=%s err=%s", supply_id, e)
                    self._write_buffered_response(
                        500, [], error_body_for_source(source, 500, str(e)))
                    return
                mode = pick_translator(source, target)
                self._acc["target_protocol"] = target
                self._acc["conversion_kind"] = protocol_conversion_kind(source, target)

                if mode == UNSUPPORTED:
                    log.warning("request.reject source=%s target=%s mode=UNSUPPORTED",
                                source, target)
                    self._write_buffered_response(
                        501, [], error_body_for_source(
                            source, 501,
                            f"unsupported combination source={source} target={target}"))
                    self._acc["final_error"] = f"unsupported source={source} target={target}"
                    return

                target_model = supply.get("target_model")
                # supply["url"] 现在语义是完整终态请求端点（不再是 base），代码侧零拼接。
                # 四个转发分支统一用这个 target_url，不再各自拼接协议相关后缀（见
                # _sanitize_forward_query 函数级注释）。
                base_url = supply.get("url", "").rstrip("/")
                target_url = base_url + _sanitize_forward_query(self.path)

                # ---- reasoning 统一链路：resolve_source_capability → remap → select_variant →
                # ---- abstract_encode → syntax_adapt ----
                # 四种协议组合（PASSTHROUGH anthropic→anthropic / PASSTHROUGH responses→responses /
                # ANTHROPIC_TO_CHAT / ANTHROPIC_TO_RESPONSES / RESPONSES_TO_ANTHROPIC）全部走这同一条
                # 链路，差异只在 get_codec 拿到哪个 codec。PASSTHROUGH anthropic→anthropic 现在也用
                # 目标 Claude 模型的 capability 做相对映射（现状缺失的能力）。
                tgt_codec = get_codec(target)
                target_cap = ModelReasoningCapability.from_config(supply)
                if supply_id == _reasoning_cache_supply_id and _cached_abstract is not None:
                    # 同一 supply 的 reasoning 语法重试（continue 未加入 tried_set）：intent/两侧
                    # cap 均未变，复用已算好的 target_effort/abstract，只重跑
                    # select_variant+syntax_adapt（方案文档 §4.3）。
                    target_effort = _cached_target_effort
                    abstract = _cached_abstract
                else:
                    target_effort = remap(raw_intent, source_cap, target_cap)
                    abstract = abstract_encode(target_effort)
                    _cached_target_effort = target_effort
                    _cached_abstract = abstract
                    _reasoning_cache_supply_id = supply_id
                reasoning_variant = tgt_codec.select_variant(pref_store.snapshot(target_model or ""))
                reasoning_wire = tgt_codec.syntax_adapt(abstract, reasoning_variant)
                if log.isEnabledFor(logging.DEBUG):
                    _log_reasoning_debug(
                        supply_id, target_model, source_cap, target_cap, raw_intent,
                        target_effort, abstract, reasoning_variant, reasoning_wire)

                # ---- 按 mode 计算 send_body / target_url / 转换上下文 ----
                # fwd_ctx：请求转换上下文（tool_name_mapping/request_model），
                # ANTHROPIC_TO_CHAT 与 ANTHROPIC_TO_RESPONSES 用
                fwd_ctx: dict[str, Any] | None = None
                if mode == PASSTHROUGH:
                    # 改写 model → target_model；target_url 已在分支前统一算好（完整端点 + 净化后 query）
                    send_body = raw_body
                    if target_model and isinstance(body_json, dict) and "model" in body_json:
                        body_json["model"] = target_model
                        send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                    # thinking 剥离：aigc 验签 supply（learn 到的）出站前删空签名历史 thinking 块。
                    # fail-open：未 learn 的 supply 首轮请求不剥离（mcli 不验签，零行为变化；
                    # 剥离后 failover 到未 learn supply 的场景见设计风险 R7，后果良性）。
                    # 注意 send_body 强制 re-dump：raw_body 逃逸坑的核心修复——若 supply 无
                    # target_model/reasoning_wire/system_inject 且预算无变化，send_body 停留在
                    # 原始字节，只改 body_json 不 re-dump 会静默丢弃剥离结果（推演·具象3）。
                    if (target == "anthropic" and isinstance(body_json, dict)
                            and thinking_strip_store.snapshot(supply_id)):
                        _n = strip_empty_signature_thinking(body_json)
                        if _n:
                            log.warning("thinking_strip: supply=%s removed %d empty-signature thinking blocks",
                                        supply_id, _n)
                            send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                    # responses 上游：剥离 input item 上无法兑现的 id（2026-11-11）。
                    # 上游对 item id 有两条独立校验：前缀（fc_/msg_/rs_）与存在性/绑定
                    # （encrypted_content 内部 item_id 必须与声明 id 一致）。改名只能过第一条，
                    # 而代理生成的 id 与密文内部 id 永不可能匹配（gen_reasoning_id 是随机值），
                    # 故改名是把不合规伪装得更隐蔽，剥离才是诚实解。
                    # 实测（terra）：伪造 rs_ id + 真实 encrypted_content → 400
                    # "item_id did not match"；同一密文不带 id → 200。
                    # 引用形态（item_reference / 裸 id）豁免：剥离会变成空 item。
                    # 门控按目标协议（非 supply/store）：协议级不变量；store 缺省 true 且
                    # failover 换 supply 后 id 必悬空，按 store 分流会让 failover 失效。
                    if target == "responses" and isinstance(body_json, dict):
                        _stripped, _kept_refs = sanitize_responses_input_ids(body_json)
                        if _stripped:
                            log.info("input_item_id_strip: supply=%s stripped %d item ids",
                                     supply_id, _stripped)
                            send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                        if _kept_refs:
                            # 引用形态不剥离（会变空 item），但不保证可解析：
                            # 交上游 fail-fast，仅以 warning 暴露。
                            log.warning("input_item_id_strip: supply=%s kept %d reference-shaped item ids",
                                        supply_id, _kept_refs)
                    # reasoning 字段按目标 capability 相对映射后原地 merge（含 anthropic→anthropic 与
                    # responses→responses：source==target 时 syntax_adapt 的 variant 就是该协议唯一/
                    # 学到的语法，PASSTHROUGH 不代表"不处理 reasoning"，只代表 body 结构本身不用转换）
                    if isinstance(body_json, dict) and reasoning_wire:
                        apply_fields(body_json, reasoning_wire)
                        send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                    # ④b 预算 stamp（R2：透传按 target 子协议分字段；首轮写回原值不 re-dump，
                    # 重试轮覆写放大值后需重新序列化）
                    if isinstance(body_json, dict):
                        _bf = _BUDGET_FIELD_BY_PROTOCOL.get(target)
                        _pre_budget = body_json.get(_bf) if _bf else None
                        _stamp_budget(body_json, target)
                        if _bf and body_json.get(_bf) != _pre_budget:
                            send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                    # system_inject：anthropic 出站 body 前置合并计费 system 块
                    # （幂等，重试复用 body_json 不重复注入；见 inject_system_block）
                    if target == "anthropic" and isinstance(body_json, dict) \
                            and supply.get("system_inject"):
                        if inject_system_block(body_json, supply["system_inject"]):
                            send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")

                elif mode == ANTHROPIC_TO_CHAT:
                    # 组合3：anthropic 请求 → chat 上游。转成 OpenAI body，打 native chat 端点。
                    # 请求转换失败（异常）→ 合法 Anthropic error，400（正向规格 §5.1）
                    try:
                        openai_body, fwd_ctx = pt.anthropic_to_openai_request(
                            body_json or {}, reasoning_fields=reasoning_wire)
                    except Exception as e:
                        log.error("ANTHROPIC_TO_CHAT request translate failed: %s", e)
                        self._write_buffered_response(
                            400, [], error_body_for_source(
                                source, 400, f"proxy translate failed: {e}"))
                        return
                    if target_model:
                        openai_body["model"] = target_model
                        fwd_ctx["request_model"] = target_model  # 响应 model 字段回填 target_model
                    _stamp_budget(openai_body, target)   # ④b（chat 字段 max_completion_tokens）
                    send_body = json.dumps(openai_body, ensure_ascii=False).encode("utf-8")
                    # target_url 已在分支前统一算好（supply.url 现在已是完整 /chat/completions 端点）

                elif mode == ANTHROPIC_TO_RESPONSES:
                    # 新组合：anthropic 请求 → responses 上游。转成 Responses body，打完整 /v1/responses。
                    # 请求转换失败（异常）→ 合法 Anthropic error，400
                    try:
                        _ts_store = getattr(self.server, "tool_strict_store", None)
                        _ts = _ts_store.snapshot(supply_id) if _ts_store else False
                        responses_body, fwd_ctx = pt.anthropic_to_responses_request(
                            body_json or {}, reasoning_fields=reasoning_wire,
                            tool_strict=_ts)
                    except Exception as e:
                        log.error("ANTHROPIC_TO_RESPONSES request translate failed: %s", e)
                        self._write_buffered_response(
                            400, [], error_body_for_source(
                                source, 400, f"proxy translate failed: {e}"))
                        return
                    if target_model:
                        responses_body["model"] = target_model
                        fwd_ctx["request_model"] = target_model  # 响应 model 字段回填 target_model
                    _stamp_budget(responses_body, target)   # ④b（responses 字段 max_output_tokens）
                    send_body = json.dumps(responses_body, ensure_ascii=False).encode("utf-8")
                    # target_url 已在分支前统一算好（supply.url 已配到完整 /v1/responses 端点）
                    # Responses reasoning.effort 机制无 Anthropic thinking.type 400 拒绝问题
                    # （ResponsesReasoningCodec 单变体，interpret_rejection 恒 None），无需重试。

                else:  # RESPONSES_TO_ANTHROPIC
                    # 组合4：responses 请求 → anthropic 上游。转成 Anthropic body，打 /v1/messages。
                    # 请求转换失败（异常）→ 合法 Responses error，400（反向规格 §5.1）
                    try:
                        # ② 反向缺省预算按 remap 结果分档：本请求将产生 thinking → 16384
                        # （4096 对 reasoning 模型是陷阱默认值，思考会占满预算挤出正文），
                        # 非 thinking 维持 4096。即便 16384 仍不够，④b 反应式爬升兜底。
                        _mt_default = (_THINKING_MAX_TOKENS_DEFAULT
                                       if abstract.kind == AbstractKind.THINKING
                                       else _NON_THINKING_MAX_TOKENS_DEFAULT)
                        anthropic_body = pt.responses_to_anthropic_request(
                            body_json or {}, max_tokens_default=_mt_default,
                            reasoning_fields=reasoning_wire)
                    except Exception as e:
                        log.error("RESPONSES_TO_ANTHROPIC request translate failed: %s", e)
                        self._write_buffered_response(
                            400, [], error_body_for_source(
                                source, 400, f"proxy translate failed: {e}"))
                        return
                    if target_model:
                        anthropic_body["model"] = target_model
                    _stamp_budget(anthropic_body, target)   # ④b（anthropic 字段 max_tokens）
                    # system_inject：转换后 anthropic body 同样前置注入（幂等）
                    if target == "anthropic" and supply.get("system_inject"):
                        inject_system_block(anthropic_body, supply["system_inject"])
                    send_body = json.dumps(anthropic_body, ensure_ascii=False).encode("utf-8")
                    # target_url 已在分支前统一算好（supply.url 现在已是完整 /v1/messages 端点）

                # ---- 注入出站 appkey（Authorization Bearer + x-api-key）----
                # appkey 直取 / appkey_file 每请求读取；失败出站前 500、不进
                # cooldown（配置问题非上游问题，同 detect_target 失败先例）
                try:
                    appkey = resolve_appkey(supply)
                except ValueError as e:
                    log.error("resolve_appkey failed: supply=%s err=%s", supply_id, e)
                    self._write_buffered_response(
                        500, [], error_body_for_source(source, 500, str(e)))
                    return
                _skip_req_headers = {"host", "content-length", "authorization", "x-api-key"}
                fwd_headers: dict[str, str] = {}
                for key, val in self.headers.items():
                    if key.lower() in _skip_req_headers:
                        continue
                    fwd_headers[key] = val
                fwd_headers["Authorization"] = f"Bearer {appkey}"
                fwd_headers["x-api-key"] = appkey
                fwd_headers["Content-Length"] = str(len(send_body))
                if mode != PASSTHROUGH:
                    # 转换后 body 一律 JSON
                    fwd_headers["Content-Type"] = "application/json"
                # extra_headers：supply 声明的出站 header，最高优先——覆盖透传值
                # 与内置注入（含转换路径的 Content-Type）；content-length 由 body
                # 长度决定，跳过不覆盖
                for k, v in (supply.get("extra_headers") or {}).items():
                    if k.lower() == "content-length":
                        continue
                    fwd_headers[k] = v

                req = urllib.request.Request(
                    url=target_url,
                    data=send_body if send_body else None,
                    headers=fwd_headers,
                    method=method,
                )

                try:
                    resp = urllib.request.urlopen(req, timeout=cs.get_upstream_timeout())  # 缺省30min,对齐 API_TIMEOUT_MS
                    resp_status = resp.status
                except urllib.error.HTTPError as e:
                    resp_status = e.code
                    resp_headers = list(e.headers.items())
                    resp_body = e.read()


                    _log_cfg_getter = getattr(cs, "get_log_upstream_error_body", None)
                    _log_upstream_error_body(
                        resp_body, resp_status, supply_id, target,
                        _log_cfg_getter() if _log_cfg_getter else None)

                    # thinking 签名 400 自适应剥离重试（仅一次，不 rotate/cooldown/tried_set，
                    # 与 reasoning 语法重试同模式；分类器文本与 interpret_rejection 各 pattern 互斥，
                    # 单个 400 至多命中其一。置于语法重试之前：签名错误更特定且不依赖 reasoning_wire）。
                    if (resp_status == 400 and not _thinking_strip_retried
                            and mode == PASSTHROUGH and target == "anthropic"
                            and is_invalid_signature_rejection(resp_body)):
                        thinking_strip_store.learn(supply_id)
                        _thinking_strip_retried = True
                        self._acc["thinking_strip"] = 1
                        log.warning("thinking_strip_retry: supply=%s learned, stripping and retrying once",
                                    supply_id)
                        continue  # 重进 while：select_supply 复选同 supply，D 点命中刚 learn 的偏好自动剥离重发

                    # Responses strict:false 被上游拒绝（400 提及 strict/additionalProperties）
                    # → learn 该 supply 需要 strict:true，同模式重试一次（出站改写 strict 策略）。
                    # 仅 ANTHROPIC_TO_RESPONSES 组合：r2a/passthrough 无本字段。
                    _ts_store = getattr(self.server, "tool_strict_store", None)
                    if (resp_status == 400 and not _tool_strict_retried and _ts_store
                            and mode == ANTHROPIC_TO_RESPONSES
                            and not _ts_store.snapshot(supply_id)
                            and is_strict_mode_rejection(resp_body)):
                        _ts_store.learn(supply_id, True)
                        _tool_strict_retried = True
                        log.warning("tool_strict_retry: supply=%s learned strict=true, retrying once",
                                    supply_id)
                        continue  # 重进 while：出站转换读取 store，strict:true 重发

                    # reasoning 语法自适应重试（仅一次，不 rotate/cooldown supply，
                    # 与 failover 重试的关键区别：不加入 tried_set、不调用 cd.cooldown）。
                    # 单变体 codec（Chat/Responses）interpret_rejection 恒 None，天然不触发重试；
                    # 只有 AnthropicReasoningCodec（双变体）在 tgt_codec 为它时才可能返回新变体。
                    # _reasoning_cache_supply_id 保留不变（不重置）：下一轮循环命中同一 supply_id
                    # 时会复用已缓存的 target_effort/abstract，只重跑 select_variant+syntax_adapt
                    # （方案文档 §4.3，remap/abstract_encode 结果与 variant 无关，无需重算）。
                    if (resp_status == 400 and not _reasoning_retried and target_model
                            and reasoning_wire):
                        next_variant = tgt_codec.interpret_rejection(resp_body, reasoning_variant)
                        if next_variant:
                            pref_store.learn(target_model, next_variant)
                            _reasoning_retried = True
                            continue  # 重新走 while 循环：select_supply 会再次选中同一 supply，
                                      # 重新算 reasoning_wire 时 select_variant 命中刚学到的偏好，
                                      # 自动改对语法后重发

                    secs = resolve_cooldown_seconds(resp_status, cs)
                    if failover == "on" and secs is not None:
                        log.warning("cooldown+failover: supply=%s status=%s secs=%s",
                                    supply_id, resp_status, secs)
                        self._acc["failover"] = 1
                        self._acc["attempt_errors"].append(
                            (supply_id, f"http_{resp_status}"))
                        cd.cooldown(supply_id, secs, f"http_{resp_status}")
                        tried_set.add(supply_id)
                        continue
                    # 未命中策略 → 透传 + 告警 + 累计 unconfigured_hits
                    log.warning("unconfigured upstream status: supply=%s status=%s (not in cooldown_rules, passing through)",
                                supply_id, resp_status)
                    _record_unconfigured(resp_status)
                    upstream_msg = _extract_upstream_error_message(resp_body)
                    self._write_buffered_response(
                        resp_status, [],
                        error_body_for_source(
                            source, resp_status,
                            f"upstream error {resp_status}: {upstream_msg}"))
                    self._acc["final_error"] = f"upstream_error {resp_status} {upstream_msg}"
                    return
                except (urllib.error.URLError, OSError) as e:
                    secs = resolve_cooldown_seconds("URLError", cs)
                    if failover == "on" and secs is not None:
                        log.warning("cooldown+failover(net): supply=%s err=%s secs=%s",
                                    supply_id, e, secs)
                        self._acc["failover"] = 1
                        self._acc["attempt_errors"].append(
                            (supply_id, f"net_error:{e}"))
                        cd.cooldown(supply_id, secs, f"net_error:{e}")
                        tried_set.add(supply_id)
                        continue
                    # 未配 URLError 策略 → 透传 502 + 告警
                    log.warning("unconfigured net error: supply=%s err=%s (URLError not in cooldown_rules, passing through)",
                                supply_id, e)
                    _record_unconfigured("URLError")
                    self._write_buffered_response(
                        502, [], error_body_for_source(source, 502, f"upstream error: {e}"))
                    self._acc["final_error"] = f"upstream net error: {e}"
                    return

                is_stream = isinstance(body_json, dict) and body_json.get("stream") is True

                # HTTP 2xx 后立即提交；流内失败不再切换 supply。
                if is_stream and mode == PASSTHROUGH:
                    self._write_streaming_response(
                        resp_status, list(resp.getheaders()), resp, source)
                    if (self._acc.get("stop_reason") in (
                            "max_tokens", "incomplete:max_output_tokens")
                            and not self._acc.get("stream_content")):
                        self._acc["budget_truncated"] = 1
                    return

                # ---- 按 mode 分派写回 ----
                if mode == PASSTHROUGH:
                    # PASSTHROUGH 流式已在即时提交分支返回；此处仅处理非流式透传。
                    resp_body = resp.read()
                    # 正文错误识别（Phase 1，设计 2026-10-10 §2.3）：200+语义错误
                    # → 虚拟 errorcode 走既有 cooldown/failover 链路（④b 同款位置，
                    # 判定在 _maybe_budget_retry 之前）。
                    _be_code = _match_body_error_from_response(
                        resp_body, cs.get_body_error_rules())
                    if _be_code is not None:
                        _be_secs = resolve_cooldown_seconds(_be_code, cs)
                        if failover == "on" and _be_secs is not None:
                            log.warning("cooldown+failover(body_error): supply=%s code=%s secs=%s",
                                        supply_id, _be_code, _be_secs)
                            self._acc["failover"] = 1
                            self._acc["attempt_errors"].append(
                                (supply_id, _be_code))
                            self._acc["body_error"] = _be_code
                            cd.cooldown(supply_id, _be_secs, _be_code)
                            tried_set.add(supply_id)
                            resp.close()   # 复核轻4：continue 前 close（对标 budget_retry）
                            continue
                        # 命中但 cooldown_rules 未配该 code → _record_unconfigured +
                        # 原样写回 resp_body（复核中3：真 fail-open，不走
                        # error_body_for_source——它需真实非 2xx status，传 200 会产出
                        # 200+proxy error 体的畸形响应）。
                        log.warning("unconfigured body error: supply=%s code=%s "
                                    "(not in cooldown_rules, passing through)",
                                    supply_id, _be_code)
                        _record_unconfigured(_be_code)
                        self._acc["body_error"] = _be_code
                        self._acc["final_error"] = f"body_error {_be_code} passthrough"
                        self._write_buffered_response(
                            resp_status, list(resp.getheaders()), resp_body)
                        resp.close()
                        return
                    if _maybe_budget_retry(resp_body, target, supply_id):
                        resp.close()
                        continue
                    try:
                        _pj = json.loads(resp_body) or {}
                        _pu = _pj.get("usage") or {}
                        self._acc["usage_in"] = _pu.get(
                            "input_tokens", _pu.get("prompt_tokens", 0)) or 0
                        self._acc["usage_out"] = _pu.get(
                            "output_tokens", _pu.get("completion_tokens", 0)) or 0
                        _st = _pj.get("stop_reason") or ""
                        if not _st and _pj.get("status"):
                            _reason = (_pj.get("incomplete_details") or {}).get("reason")
                            _st = (f"{_pj['status']}:{_reason}" if _reason
                                   else str(_pj["status"]))
                        self._acc["stop_reason"] = _st
                    except Exception:
                        pass
                    self._write_buffered_response(
                        resp_status, list(resp.getheaders()), resp_body)
                    resp.close()
                    return

                if mode == ANTHROPIC_TO_CHAT:
                    # chat 响应 → Anthropic
                    if is_stream:
                        adapter = pt.OpenAIToAnthropicStreamAdapter(fwd_ctx, target_model or "")
                        self._write_translated_stream(resp, adapter)
                        (self._acc["usage_in"], self._acc["usage_out"], _) = adapter.usage_tuple()
                        # ⑤ 可选 stop_reason（final_stop_reason 已经 map_finish_reason
                        # 映射，"length"→"max_tokens"）
                        self._acc["stop_reason"] = adapter.final_stop_reason or ""
                        # 跨协议流维持既有观测语义：不做 PASSTHROUGH 截断标记。
                    else:
                        try:
                            raw_resp_body = resp.read()
                        finally:
                            resp.close()
                        # ④b 截断检测：必须在原始 chat 响应上判（转换前）——转换的
                        # reasoning fallback 会把 reasoning_content 填成 text，转换后
                        # 再判「无 text」恒为假，检测被兜底掩盖（④a/R1）
                        if _maybe_budget_retry(raw_resp_body, target, supply_id):
                            continue
                        # 响应转换失败（JSON 非法/转换器异常）→ 合法 Anthropic error，500（正向规格 §5.1）
                        try:
                            openai_resp = json.loads(raw_resp_body)
                            anthropic_resp = pt.openai_to_anthropic_response(openai_resp, fwd_ctx)
                        except pt.TranslationError as e:
                            log.error("ANTHROPIC_TO_CHAT response translate failed: %s", e)
                            self._write_buffered_response(
                                e.http_status, [], error_body_for_source(
                                    source, e.http_status, f"proxy translate failed: {e}"))
                            return
                        except Exception as e:
                            log.error("ANTHROPIC_TO_CHAT response translate failed: %s", e)
                            self._write_buffered_response(
                                500, [], error_body_for_source(
                                    source, 500, f"proxy translate failed: {e}"))
                            return
                        _u = anthropic_resp.get("usage") or {}
                        self._acc["usage_in"] = _u.get("input_tokens") or 0
                        self._acc["usage_out"] = _u.get("output_tokens") or 0
                        self._acc["stop_reason"] = anthropic_resp.get("stop_reason") or ""
                        self._write_buffered_response(
                            200, [("Content-Type", "application/json")],
                            json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8"))
                    return

                if mode == ANTHROPIC_TO_RESPONSES:
                    # responses 响应 → Anthropic
                    if is_stream:
                        adapter = pt.ResponsesToAnthropicStreamAdapter(fwd_ctx, target_model or "")
                        self._write_translated_stream_from_responses(resp, adapter)
                        (self._acc["usage_in"], self._acc["usage_out"], _) = adapter.usage_tuple()
                        # 观测补齐：与 chat/r2a 流式分支对齐，读取 adapter 推断的
                        # Anthropic 域 stop_reason（completed 才有值，失败留空）。
                        self._acc["stop_reason"] = adapter.final_stop_reason or ""
                        # ④b 流式不重试；本 adapter 未捕获 response.incomplete 状态，
                        # 流式截断检测此处不做（状态不允许，仅非流式生效）
                    else:
                        try:
                            raw_resp_body = resp.read()
                        finally:
                            resp.close()
                        # ④b 截断检测（原始 responses 响应上判，转换前）
                        if _maybe_budget_retry(raw_resp_body, target, supply_id):
                            continue
                        # 响应转换失败（JSON 非法/转换器异常）→ 合法 Anthropic error，500
                        try:
                            responses_resp = json.loads(raw_resp_body)
                            anthropic_resp = pt.responses_to_anthropic_response(
                                responses_resp, fwd_ctx)
                        except pt.TranslationError as e:
                            log.error("ANTHROPIC_TO_RESPONSES response translate failed: %s", e)
                            self._write_buffered_response(
                                e.http_status, [], error_body_for_source(
                                    source, e.http_status, f"proxy translate failed: {e}"))
                            return
                        except Exception as e:
                            log.error("ANTHROPIC_TO_RESPONSES response translate failed: %s", e)
                            self._write_buffered_response(
                                500, [], error_body_for_source(
                                    source, 500, f"proxy translate failed: {e}"))
                            return
                        _u = anthropic_resp.get("usage") or {}
                        self._acc["usage_in"] = _u.get("input_tokens") or 0
                        self._acc["usage_out"] = _u.get("output_tokens") or 0
                        self._acc["stop_reason"] = anthropic_resp.get("stop_reason") or ""
                        self._write_buffered_response(
                            200, [("Content-Type", "application/json")],
                            json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8"))
                        # 非流式成功也补终态观测，避免 usage 有值但 terminal 字段全空。
                        try:
                            self._record_stream_terminal(pt.map_anthropic_terminal(
                                anthropic_resp.get("stop_reason")))
                        except pt.TranslationError:
                            self._record_stream_terminal(pt.TerminalState(
                                pt.TerminalStatus.FAILED, "upstream_error"))
                    return

                # RESPONSES_TO_ANTHROPIC：anthropic 响应 → Responses
                _r_effort = ((body_json or {}).get("reasoning") or {}).get("effort")
                _tools_echo = (body_json or {}).get("tools") or []
                if is_stream:
                    adapter = pt.AnthropicToResponsesStreamAdapter(
                        model=target_model or "",
                        ctx={"tools": _tools_echo, "reasoning_effort": _r_effort})
                    self._write_responses_stream(resp, adapter)
                    (self._acc["usage_in"], self._acc["usage_out"], _) = adapter.usage_tuple()
                    # ⑤ 可选 stop_reason（anthropic 原生值）；adapter 无「正文是否产出」
                    # 状态，流式截断检测此处不做（仅非流式生效）
                    self._acc["stop_reason"] = adapter.final_stop_reason or ""
                else:
                    try:
                        raw_resp_body = resp.read()
                    finally:
                        resp.close()
                    # ④b 截断检测（原始 anthropic 响应上判，转换前）
                    if _maybe_budget_retry(raw_resp_body, target, supply_id):
                        continue
                    # 响应转换失败（JSON 非法/转换器异常）→ 合法 Responses error，500（反向规格 §5.1）
                    try:
                        anthropic_resp = json.loads(raw_resp_body)
                        responses_resp = pt.anthropic_to_responses_response(
                            anthropic_resp, target_model or "",
                            reasoning_effort=_r_effort, tools_echo=_tools_echo)
                    except pt.TranslationError as e:
                        log.error("RESPONSES_TO_ANTHROPIC response translate failed: %s", e)
                        self._write_buffered_response(
                            e.http_status, [], error_body_for_source(
                                source, e.http_status, f"proxy translate failed: {e}"))
                        return
                    except Exception as e:
                        log.error("RESPONSES_TO_ANTHROPIC response translate failed: %s", e)
                        self._write_buffered_response(
                            500, [], error_body_for_source(
                                source, 500, f"proxy translate failed: {e}"))
                        return
                    _u = responses_resp.get("usage") or {}
                    self._acc["usage_in"] = _u.get("input_tokens") or 0
                    self._acc["usage_out"] = _u.get("output_tokens") or 0
                    self._acc["stop_reason"] = anthropic_resp.get("stop_reason") or ""
                    self._write_buffered_response(
                        200, [("Content-Type", "application/json")],
                        json.dumps(responses_resp, ensure_ascii=False).encode("utf-8"))
                return

            # while 循环仅在 route_exhausted=True 时 break 到此处（其余分支均直接
            # return 或 continue 留在 while 内）。§3 选项B：当前候选 route 全部
            # supply 已冷却/失败，非最后一个候选则换下一个候选 route 重试；是最后
            # 一个候选（含单候选、即旧单值 route_id 配置的现状行为）才最终 503。
            if route_exhausted:
                if not is_last_candidate:
                    # OPT-05 双发合并：非末候选只打动作行（消息内含 exhausted 原因），吞并条件行
                    log.warning(
                        "route_failover: route=%s tier=%s all supplies failed or cooling, "
                        "trying next candidate route", route.get("id"), tier)
                    self._acc["route_failover"] = 1
                    continue
                # 末候选只打条件行
                log.warning("all supplies failed or cooling: route=%s tier=%s",
                            route.get("id"), tier)
                errs = self._acc.get("attempt_errors") or []
                err_summary = "; ".join(f"{sid}={reason}" for sid, reason in errs) if errs else "no attempts"
                msg = f"all upstream supplies failed or cooling: {err_summary}"
                # HTTP/网络失败全耗尽保持既有 503 语义。
                final_status = 503
                self._write_buffered_response(
                    final_status, [], error_body_for_source(source, final_status, msg))
                self._acc["final_error"] = msg
                return

    # ------------------------------------------------------------------
    # 内建命令层（docs/designs/2026-08-04-in-band-route-command-design.md §7.3）
    #
    # 边界约束（必须遵守，不是建议）：本层及其分发到的 handler 只允许操作代理
    # 自身的路由/观测状态，且只允许纯本地操作。禁止执行外部命令、读写代理配置/
    # sidecar 以外的文件、代理请求转发、任何需要网络的动作。首版只有 $route 一个
    # 命令（core/commands.py::COMMAND_HANDLERS），新增命令前必须重新确认这条边界。
    # ------------------------------------------------------------------

    def _handle_builtin_command(self, cmd_arg, client_token, session_key, strategy,
                                 routes_map, sidecar, request_model, body_json,
                                 protocol: str = "anthropic") -> None:
        """命令层统一入口：分发 → 统一响应合成 → 统一 ACCESS 记录。

        目前唯一命令 $route 的匹配已在调用方 `_forward` 完成（parse_route_command
        返回 is_cmd=True 才会走到这里），本方法固定分发到 COMMAND_HANDLERS["$route"]，
        分发表的存在是为未来扩展命令预留（届时按首 token 查表即可，不改这里的骨架）。
        """
        self._acc["builtin"] = "route"
        self._acc["supply"] = "(builtin)"

        # 查询命令需要"若无 override 会落到哪个候选 route"，复用既有一致性哈希算法，
        # 不在 commands.py 里重复实现（避免与 server.py 侧算法出现第二份漂移）。
        overrides = sidecar.get_overrides_for(strategy.get("client_token", ""))
        view = dict(strategy)
        view_dispatch = dict(strategy.get("dispatch") or {})
        view_dispatch["session_overrides"] = overrides
        view["dispatch"] = view_dispatch
        resolved_candidates = extract_route_candidates(view, session_key, routes_map)
        resolved_route_id = resolved_candidates[0].get("id") if resolved_candidates else None

        ctx = CommandContext(
            arg=cmd_arg,
            client_token=client_token,
            session_key=session_key,
            strategy=strategy,
            routes_map=routes_map,
            sidecar=sidecar,
            resolved_route_id=resolved_route_id,
        )
        handler = COMMAND_HANDLERS[CMD_PREFIX]
        result = handler(ctx)

        # 回执内嵌当前请求 session 的身份：把各行 "session <uuid8>" 升级为
        # "session <name · uuid8>"（注册表命中 name 时），再补 claudian 会话标题
        # 成 "session <name · uuid8>（<标题>）"（meta 命中标题时）。身份解析限定
        # 在 server 层完成，commands.py 维持"只操作代理自身路由/观测状态"的命令层边界。
        receipt_text = result.receipt_text
        if session_key:
            identity = session_identity.format_session_identity(session_key)
            title = claudian_titles.lookup_session_title(session_key)
            display = f"{identity}（{title}）" if title else identity
            short = session_key[:8]
            if display != short:
                receipt_text = receipt_text.replace(f"session {short}", f"session {display}")

        # ACCESS route= 记「本次命令操作/查询后的生效 route」以便核对（§3.3）：
        # 切换成功后就是目标 route；reset 成功后重新算一次候选（写操作已完成，
        # sidecar 已更新）；查询/切换失败（如 route 不存在）则用查询时算好的候选。
        if result.wrote and cmd_arg not in (None, "reset"):
            self._acc["route"] = cmd_arg
        elif result.wrote and cmd_arg == "reset":
            post_overrides = sidecar.get_overrides_for(strategy.get("client_token", ""))
            post_view = dict(strategy)
            post_view_dispatch = dict(strategy.get("dispatch") or {})
            post_view_dispatch["session_overrides"] = post_overrides
            post_view["dispatch"] = post_view_dispatch
            post_candidates = extract_route_candidates(post_view, session_key, routes_map)
            self._acc["route"] = post_candidates[0].get("id") if post_candidates else ""
        else:
            self._acc["route"] = resolved_route_id or ""

        is_stream = isinstance(body_json, dict) and body_json.get("stream") is True
        if protocol == "responses":
            if is_stream:
                self._write_builtin_responses_stream_response(receipt_text, request_model)
            else:
                self._write_builtin_responses_buffered_response(receipt_text, request_model)
        else:
            if is_stream:
                self._write_builtin_stream_response(receipt_text, request_model)
            else:
                self._write_builtin_buffered_response(receipt_text, request_model)

    def _write_builtin_responses_stream_response(self, receipt_text: str,
                                                    request_model) -> None:
        """自造 Responses 流式回执：事件序列对齐 OpenAI Responses SSE。

        语义与 Anthropic 版一致：零上游消耗，usage 全 0；终态用
        response.completed（不是 response.failed）。
        """
        now = int(time.time())
        response_id = pt.gen_response_id()
        conversation_id = pt.gen_conversation_id()
        message_id = pt.gen_message_id()

        def _response(status: str, completed_at=None, usage=None):
            return {
                "id": response_id,
                "object": "response",
                "created_at": now,
                "status": status,
                "background": False,
                "completed_at": completed_at,
                "model": request_model or "",
                "output": [] if usage is None else [{
                    "id": message_id,
                    "type": "message",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": receipt_text}],
                    "role": "assistant",
                }],
                "parallel_tool_calls": True,
                "conversation": {"id": conversation_id},
                "reasoning": {"effort": None, "summary": None},
                "service_tier": "default" if usage is not None else "auto",
                "store": False,
                "usage": usage,
                "metadata": {},
            }

        events = [
            {"type": "response.created", "response": _response("in_progress")},
            {"type": "response.in_progress", "response": _response("in_progress")},
            {"type": "response.output_item.added", "output_index": 0, "item": {
                "id": message_id, "type": "message", "status": "in_progress",
                "content": [], "role": "assistant"}},
            {"type": "response.content_part.added", "item_id": message_id,
             "output_index": 0, "content_index": 0,
             "part": {"type": "output_text", "text": ""}},
            {"type": "response.output_text.delta", "item_id": message_id,
             "output_index": 0, "content_index": 0, "delta": receipt_text},
            {"type": "response.output_text.done", "item_id": message_id,
             "output_index": 0, "content_index": 0, "text": receipt_text},
            {"type": "response.content_part.done", "item_id": message_id,
             "output_index": 0, "content_index": 0,
             "part": {"type": "output_text", "text": receipt_text}},
            {"type": "response.output_item.done", "output_index": 0, "item": {
                "id": message_id, "type": "message", "status": "completed",
                "content": [{"type": "output_text", "text": receipt_text}],
                "role": "assistant"}},
            {"type": "response.completed", "response": _response(
                "completed", completed_at=now,
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})},
        ]

        self._begin_sse_chunked()
        try:
            for i, ev in enumerate(events):
                ev["sequence_number"] = i
                self._write_sse_chunk(pt.responses_sse_bytes(ev))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            # Responses builtin 回执成功封口：与 Anthropic builtin 同口径。
            self._acc["stop_reason"] = "end_turn"
            self._acc["response_committed"] = 1
            self._acc["stream_integrity"] = "valid"
            self._acc["terminal_status"] = "completed"
            self._acc["terminal_reason"] = "end_turn"
        except (BrokenPipeError, ConnectionResetError):
            self._acc["response_committed"] = 1
            self._acc["stream_integrity"] = "client_disconnect"

    def _write_builtin_responses_buffered_response(self, receipt_text: str,
                                                     request_model) -> None:
        """自造 Responses 非流式回执。"""
        now = int(time.time())
        resp = {
            "id": pt.gen_response_id(),
            "object": "response",
            "created_at": now,
            "status": "completed",
            "background": False,
            "completed_at": now,
            "model": request_model or "",
            "output": [{
                "id": pt.gen_message_id(),
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": receipt_text}],
                "role": "assistant",
            }],
            "parallel_tool_calls": True,
            "conversation": {"id": pt.gen_conversation_id()},
            "reasoning": {"effort": None, "summary": None},
            "service_tier": "default",
            "store": False,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "metadata": {},
        }
        self._write_buffered_response(
            200, [("Content-Type", "application/json")],
            json.dumps(resp, ensure_ascii=False).encode("utf-8"))
        # Responses builtin 非流式成功收口：与流式同口径。
        self._acc["stop_reason"] = "end_turn"
        self._acc["response_committed"] = 1
        self._acc["stream_integrity"] = "valid"
        self._acc["terminal_status"] = "completed"
        self._acc["terminal_reason"] = "end_turn"

    def _write_builtin_stream_response(self, receipt_text: str, request_model) -> None:
        """自造 anthropic 流式回执（§3.2 事件序列），复用 translate.py 既有事件构造
        helper（OpenAIToAnthropicStreamAdapter 的实例方法），不另写事件字典。
        usage 全填 0（§3.3 决策：零上游消耗如实反映）。
        """
        adapter = pt.OpenAIToAnthropicStreamAdapter(ctx={}, model=request_model or "")
        adapter.input_tokens = 0
        adapter.output_tokens = 0
        adapter.final_stop_reason = "end_turn"

        events = [
            adapter._message_start_event(),
            adapter._ping_event(),
            adapter._content_block_start_text(0),
            adapter._content_block_delta_text(0, receipt_text),
            adapter._content_block_stop(0),
            adapter._message_delta_event(),
            adapter._message_stop_event(),
        ]

        self._begin_sse_chunked()
        try:
            for ev in events:
                self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            # builtin 回执成功封口：显式记终态，避免统一 finally 兜底误标
            # invalid/open/internal_escape（2026-11-06 实测发现）。
            self._acc["stop_reason"] = "end_turn"
            self._acc["response_committed"] = 1
            self._acc["stream_integrity"] = "valid"
            self._acc["terminal_status"] = "completed"
            self._acc["terminal_reason"] = "end_turn"
        except (BrokenPipeError, ConnectionResetError):
            self._acc["response_committed"] = 1
            self._acc["stream_integrity"] = "client_disconnect"

    def _write_builtin_buffered_response(self, receipt_text: str, request_model) -> None:
        """自造 anthropic 非流式回执（§3.4）。"""
        resp = {
            "id": pt.gen_msg_id(),
            "type": "message",
            "role": "assistant",
            "model": request_model or "",
            "content": [{"type": "text", "text": receipt_text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
        self._write_buffered_response(
            200, [("Content-Type", "application/json")],
            json.dumps(resp, ensure_ascii=False).encode("utf-8"))
        # builtin 回执成功收口：同流式口径（§ P1 观测修复）。
        self._acc["stop_reason"] = "end_turn"
        self._acc["response_committed"] = 1
        self._acc["stream_integrity"] = "valid"
        self._acc["terminal_status"] = "completed"
        self._acc["terminal_reason"] = "end_turn"

    # ------------------------------------------------------------------
    # control API dispatch
    # ------------------------------------------------------------------

    def _dispatch_control(self, method: str):
        cs: ConfigStore = self.server.config_store
        cd: CooldownStore = self.server.cooldown_store

        # 鉴权（X-Proxy-Admin-Token）
        admin_token = cs.get_admin_token()
        if not admin_token:
            self._send_json(503, {"error": "admin_token not configured"})
            return
        request_token = self.headers.get("X-Proxy-Admin-Token", "")
        if not hmac.compare_digest(request_token, admin_token):
            log.warning("admin.auth_fail: unauthorized control API access (token mismatch)")
            self._send_json(401, {"error": "unauthorized"})
            return

        path = self.path.split("?", 1)[0]  # 去掉 query string

        # GET /model_proxy/status
        if method == "GET" and path == "/model_proxy/status":
            log.info("admin.status")
            self._handle_status(cs, cd)
            return

        # POST /model_proxy/reload
        if method == "POST" and path == "/model_proxy/reload":
            self._handle_reload(cs, cd)
            return

        log.info("admin.404 path=%s method=%s", path, method)
        self._send_json(404, {"error": "not found"})

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------

    def _handle_status(self, cs: "ConfigStore", cd: "CooldownStore"):
        """回显 supplies/routes + cooldown 剩余秒。

        supplies 屏蔽 appkey（只留尾4位），避免明文暴露。
        每条 strategy 额外补 sidecar_overrides_count（session override 迁移到 sidecar
        单一存储后，dispatch.session_overrides 恒为空，CLI 需读此字段展示覆盖数）。
        """
        supplies = cs.get_supplies()
        safe_supplies: list[dict] = []
        for s in supplies:
            item = {k: v for k, v in s.items() if k != "appkey"}
            appkey = s.get("appkey", "")
            item["appkey_tail4"] = appkey[-4:] if appkey else ""
            safe_supplies.append(item)

        strategies = cs.get_strategies()
        sidecar: SessionOverridesSidecar = self.server.sidecar_store
        strategies_out = []
        for st in strategies:
            st_copy = dict(st) if isinstance(st, dict) else {}
            ct = st_copy.get("client_token", "")
            st_copy["sidecar_overrides_count"] = sidecar.count_overrides_for(ct)
            strategies_out.append(st_copy)

        self._send_json(200, {
            "supplies": safe_supplies,
            "routes": cs.get_routes(),
            "strategies": strategies_out,
            "cooldown": cd.snapshot(),
            "stream_health": getattr(
                self.server, "stream_health_store", StreamHealthStore()).snapshot(),
            "unconfigured_codes": _snapshot_unconfigured_hits(),
            "protocol_conversion_hints": build_protocol_conversion_hints({
                "supplies": supplies, "routes": cs.get_routes(),
            }),
            "version": _VERSION,
        })

    def _handle_reload(self, cs: "ConfigStore", cd: "CooldownStore"):
        """Authenticated manual reload: report the actual attempt and clear cooldown."""
        loaded: bool | None = None
        load_error: str | None = None
        allowed = {"config_parse_error", "config_read_error", "config_unstable",
                   "file_identity_unavailable", "filesystem_unverified", "config_reload_error",
                   "config_callback_error", "config_reload_unknown"}
        try:
            outcome = cs.reload_detailed()
            if not isinstance(outcome, ConfigReloadOutcome) or not isinstance(outcome.loaded, bool):
                load_error = "config_reload_unknown"
            else:
                loaded = outcome.loaded
                load_error = outcome.error if outcome.error in allowed else (
                    "config_reload_unknown" if outcome.error is not None else None)
        except Exception:
            # An unexpected failure may have happened before or after commit.
            loaded = None
            load_error = "config_reload_unknown"

        try:
            count = len(cd.snapshot())
        except Exception:
            count = None
        try:
            cd.clear_all()
            cooldown_cleared: bool | None = True
        except Exception:
            # A partial clear cannot be distinguished from an unperformed one.
            cooldown_cleared = None
        try:
            if cooldown_cleared is True:
                log.info("admin.reload cleared_cooldowns=%s", count if count is not None else "unknown")
            else:
                log.warning("admin.reload cooldown_clear_failed")
        except Exception:
            pass

        if loaded is True and load_error is None and cooldown_cleared is True:
            self._send_json(200, {"ok": True})
            return
        if cooldown_cleared is None:
            error = "cooldown_clear_failed"
        else:
            error = load_error or "config_reload_unknown"
        body = {"ok": False, "error": error, "loaded": loaded,
                "cooldown_cleared": cooldown_cleared}
        if cooldown_cleared is None and load_error is not None:
            body["load_error"] = load_error
        self._send_json(422 if error == "config_parse_error" else 500, body)

    # ------------------------------------------------------------------
    # 写回（拷贝 proxy.py：流式 chunked 透传 / 缓冲响应）
    # ------------------------------------------------------------------

    _SKIP_RESP_HEADERS = {"transfer-encoding", "content-length"}
    def _write_streaming_response(self, status: int, headers: list[tuple[str, str]], resp,
                                  source: str = "") -> None:
        """PASSTHROUGH 即时字节转发；observer 仅在正常 EOF 后决定是否补 error。"""
        if hasattr(self, "_acc"):
            self._acc["status"] = status
            self._acc["response_committed"] = 1
        self.send_response(status)
        for hname, hval in headers:
            if hname.lower() not in self._SKIP_RESP_HEADERS:
                self.send_header(hname, hval)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        observer = pt.PassthroughStreamObserver(source, getattr(self, "_acc", {})) if source else None
        try:
            while True:
                try:
                    chunk = self._read_stream_chunk(resp, 8192)
                except OSError as exc:
                    self._record_stream_error(pt.TranslationError(
                        f"upstream read failed: {exc}", reason="upstream_read_error"))
                    log.warning("passthrough upstream read failed: %s", exc)
                    self.close_connection = True
                    return
                if not chunk:
                    break
                self._write_sse_chunk(chunk)
                if observer is not None:
                    observer.feed(chunk)
            if observer is None:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            result = observer.finish()
            self._acc["first_event_ms"] = (result.first_event_ms
                                             if result.first_event_ms is not None else "")
            if result.kind == "terminal":
                self._record_stream_terminal(result.terminal_state)
            elif result.kind == "observer_error":
                self._acc["stream_integrity"] = "observer_error"
                self._acc["terminal_status"] = "open"
                self._acc["terminal_reason"] = "observer_error"
                log.warning("passthrough observer failed: source=%s", source)
            else:
                err = pt.TranslationError(
                    f"stream ended without terminal: {result.reason}", reason=result.reason)
                self._record_stream_error(err)
                self._write_sse_chunk(stream_error_event_for_source(source, err))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            if hasattr(self, "_acc"):
                self._acc["stream_integrity"] = "client_disconnect"
            self.close_connection = True
            log.info("passthrough client disconnected")
        finally:
            resp.close()
            # 正文错误收口判定（设计 §2.4 + 复核中2）：挂 finally 覆盖客户端断开 /
            # 上游读错提前退出（挂 finish() 后则这两类退出不判定不冷却）。
            # 当前请求不救：已透传字节零改动（B+ 边界）。
            self._maybe_body_error_cooldown(observer)

    def _maybe_body_error_cooldown(self, observer) -> None:
        """流式收口正文错误判定 + 跨请求冷却（_write_streaming_response 专用）。

        命中 → cd.cooldown(supply_id, secs, code)（supply_id 取 _acc["supply"]，
        复核轻7：本函数无 supply_id 参数，:2094 每轮 attempt 已写入）+ ACCESS
        body_error 字段 + log.warning；未配 cooldown → _record_unconfigured 只记
        不冷却（对齐 HTTPError 语义）。不补 error 事件、不改已透传字节、不重试。
        直接构造的测试 handler 可能无 server/config_store（同 stream_health_store
        的 getattr 容忍先例）；无规则时零行为。
        """
        if observer is None or not hasattr(self, "_acc"):
            return
        acc = self._acc
        if acc.get("body_error"):
            return   # 幂等护栏（修订中2）：判定单点挂 finally，护栏防任何重复进入
        _server = getattr(self, "server", None)
        cs = getattr(_server, "config_store", None)
        _getter = getattr(cs, "get_body_error_rules", None)
        rules = _getter() if callable(_getter) else []
        if not rules:
            return
        text, text_len, saw = observer.body_text_state()
        code = match_body_error(text, rules, saw_tool_or_thinking=saw,
                                text_len=text_len)
        if code is None:
            return
        supply_id = acc.get("supply", "")
        acc["body_error"] = code
        acc["final_error"] = f"body_error {code}"
        secs = resolve_cooldown_seconds(code, cs)
        if secs is None:
            log.warning("unconfigured body error: supply=%s code=%s "
                        "(not in cooldown_rules, no cooldown)", supply_id, code)
            _record_unconfigured(code)
            return
        log.warning("body_error cooldown: supply=%s code=%s secs=%s",
                    supply_id, code, secs)
        cd = getattr(_server, "cooldown_store", None)
        if cd is not None:
            cd.cooldown(supply_id, secs, code)

    def _record_stream_terminal(self, terminal: "pt.TerminalState") -> None:
        if hasattr(self, "_acc"):
            if terminal.status is pt.TerminalStatus.OPEN:
                # 成功写回路径结束但 adapter 无终态：不允许伪装成 valid。
                # 归为 unexpected_eof，保留观测证据，便于后续修复。
                self._acc["stream_integrity"] = "invalid"
                self._acc["terminal_status"] = "open"
                self._acc["terminal_reason"] = "unexpected_eof"
                self._acc["final_error"] = "unexpected_eof"
                return
            self._acc["stream_integrity"] = "valid"
            self._acc["terminal_status"] = terminal.status.value
            self._acc["terminal_reason"] = terminal.reason
            if terminal.status is pt.TerminalStatus.FAILED:
                # 流内上游失败是合法完整终态（valid/failed），但要保留短 reason
                # 到 final_error，避免只看 ACCESS 时误以为无错误出口。
                self._acc["final_error"] = terminal.reason or "upstream_error"

    def _record_stream_error(self, error: "pt.TranslationError") -> None:
        if hasattr(self, "_acc"):
            self._acc["stream_integrity"] = "invalid"
            self._acc["terminal_status"] = "open"
            self._acc["terminal_reason"] = error.reason
            self._acc["final_error"] = error.reason

    def _write_buffered_response(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        """回写已完整读取的 buffer 响应（非流式 / 错误响应用）。"""
        if hasattr(self, "_acc"):
            self._acc["status"] = status
        self.send_response(status)
        for hname, hval in headers:
            if hname.lower() in self._SKIP_RESP_HEADERS:
                continue
            self.send_header(hname, hval)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------------
    # 转换流式写回（全新：组合3 正向 / 组合4 反向）
    # 与透传 _write_streaming_response 的区别：不是字节透传，而是先按上游
    # SSE 格式逐事件解析，喂给状态机 adapter，再把 adapter 产出的目标协议
    # 事件序列化后 chunked 写出。两套写回互不混用。
    # ------------------------------------------------------------------

    def _begin_sse_chunked(self) -> None:
        """发 200 + text/event-stream 响应头，启用 chunked 逐事件写出。

        本方法是 _write_translated_stream / _write_responses_stream /
        _write_translated_stream_from_responses 三个转换流式写回函数唯一的共同
        入口，三者只在成功路径调用（无显式 status 参数），故在此统一固定填 200。
        """
        if hasattr(self, "_acc"):
            self._acc["status"] = 200
            self._acc["response_committed"] = 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_sse_chunk(self, data: bytes) -> None:
        """把一段已序列化的 SSE 字节按 HTTP chunked 编码写出并 flush。"""
        if not data:
            return
        # 转换流式原缺 first_event_ms（此前只有 passthrough observer 会补）。
        # 首个实际写出的 SSE chunk 即可代表转换链路首包；passthrough 后续会用
        # observer 的业务事件时间覆写，转换路径保留这里的值。
        started = getattr(self, "_forward_started_monotonic", None)
        if started is not None and self._acc.get("first_event_ms", "") == "":
            self._acc["first_event_ms"] = max(
                0, int((time.monotonic() - started) * 1000))
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _write_translated_stream(self, upstream_resp, adapter) -> None:
        """正向流式（组合3）：上游 OpenAI chat SSE → Anthropic SSE。

        SSEFramer 逐事件解析 → adapter.feed(data) → pt.anthropic_sse_bytes 写出。
        Chat [DONE] 立即停止消费尾流并 finalize；只有正常 EOF 接纳无空行末帧。
        """
        self._begin_sse_chunked()
        framer = pt.SSEFramer(allow_event_only=True)
        done = False

        def consume(frame):
            if frame.is_comment:
                return False
            if frame.is_done:
                return True
            for ev in adapter.feed(frame.data):
                self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            return False

        try:
            while not done:
                data = self._read_stream_chunk(upstream_resp, 4096)
                if not data:
                    for frame in framer.finish():
                        if consume(frame):
                            break
                    break
                frames = framer.feed(data, max_events=1)
                while frames:
                    done = consume(frames[0])
                    if done:
                        break
                    frames = framer.feed(b"", max_events=1)
            # 正常 EOF 或 Chat [DONE] 收尾；读取异常/客户端断连不 finalize。
            for ev in adapter.finalize():
                self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            self._record_stream_terminal(adapter.terminal_state())
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._acc["stream_integrity"] = "client_disconnect"
            # 客户端断连不是上游 EOF：不 finalize、不制造失败终态、不触发 failover。
            pass
        except Exception as e:
            err = e if isinstance(e, pt.TranslationError) else pt.TranslationError(
                f"internal translation error: {e}", reason="internal_translation_error")
            self._record_stream_error(err)
            log.error("ANTHROPIC_TO_CHAT stream interrupted: %s", err)
            try:
                if not (getattr(adapter, "_finalized", False)
                        or getattr(adapter, "_failed", False)):
                    err_event = {
                        "type": "error",
                        "error": {"type": "api_error", "message": f"stream interrupted: {e}"},
                    }
                    self._write_sse_chunk(pt.anthropic_sse_bytes(err_event))
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            upstream_resp.close()

    def _write_responses_stream(self, upstream_resp, adapter) -> None:
        """反向流式（组合4）：上游网关 Anthropic SSE → Responses SSE。

        SSEFramer 逐事件解析出 event_type/data → adapter.feed → pt.responses_sse_bytes 写出。
        message_stop 由 adapter.feed 内部触发 response.completed；断流则 finalize 补收尾。
        """
        self._begin_sse_chunked()
        framer = pt.SSEFramer(allow_event_only=True)

        def consume(frame):
            if frame.is_comment:
                return
            if frame.is_done:
                # 已确认终态后的冗余 DONE 容忍；终态前仍属 malformed。
                if getattr(adapter, "completed", False) or getattr(adapter, "failed", False):
                    return
                raise pt.TranslationError("unexpected [DONE]", reason="malformed_stream")
            if frame.event_type is None:
                raise pt.TranslationError("malformed SSE frame", reason="malformed_stream")
            for ev in adapter.feed(frame.event_type, frame.data):
                self._write_sse_chunk(pt.responses_sse_bytes(ev))

        try:
            while True:
                data = self._read_stream_chunk(upstream_resp, 4096)
                if not data:
                    break
                frames = framer.feed(data, max_events=1)
                while frames:
                    consume(frames[0])
                    frames = framer.feed(b"", max_events=1)
            # 只有正常 EOF 接纳无空行末帧；读取异常/客户端断连不走此路径。
            for frame in framer.finish():
                consume(frame)
            # 流意外结束（无 message_stop）时补收尾，幂等
            for ev in adapter.finalize():
                self._write_sse_chunk(pt.responses_sse_bytes(ev))
            self._record_stream_terminal(adapter.terminal_state())
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._acc["stream_integrity"] = "client_disconnect"
            # 客户端断连不是上游 EOF。
            pass
        except Exception as e:
            err = e if isinstance(e, pt.TranslationError) else pt.TranslationError(
                f"internal translation error: {e}", reason="internal_translation_error")
            self._record_stream_error(err)
            # 上游连接中断/读取异常/adapter.feed 抛异常：200+chunked 头已发出，无法降级为
            # 非流式 error body，按反向规格 §5.1 补发一个 response.failed 事件再体面收尾
            log.error("RESPONSES_TO_ANTHROPIC stream interrupted: %s", e)
            try:
                if not (getattr(adapter, "completed", False)
                        or getattr(adapter, "failed", False)):
                    self._write_sse_chunk(pt.responses_sse_bytes(
                        _responses_failed_event(adapter, f"stream interrupted: {e}")))
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            upstream_resp.close()

    def _write_translated_stream_from_responses(self, upstream_resp, adapter) -> None:
        """新组合流式：上游 Responses SSE → Anthropic SSE。

        SSEFramer 支持 event: 行缺失时以 data.type 兜底。
        逐事件 → adapter.feed(event_type, data) →
        pt.anthropic_sse_bytes 写出。断流则 finalize 补收尾。
        中断时补 `event: error`（输出侧已是 Anthropic 格式，不用 response.failed）。
        """
        self._begin_sse_chunked()
        framer = pt.SSEFramer(allow_event_only=True)

        def consume(frame):
            if frame.is_comment:
                return
            if frame.is_done:
                if getattr(adapter, "_completed", False) or getattr(adapter, "_failed", False):
                    return
                raise pt.TranslationError("unexpected [DONE]", reason="malformed_stream")
            if frame.event_type is None:
                raise pt.TranslationError("malformed SSE frame", reason="malformed_stream")
            for ev in adapter.feed(frame.event_type, frame.data):
                self._write_sse_chunk(pt.anthropic_sse_bytes(ev))

        try:
            while True:
                data = self._read_stream_chunk(upstream_resp, 4096)
                if not data:
                    break
                frames = framer.feed(data, max_events=1)
                while frames:
                    consume(frames[0])
                    frames = framer.feed(b"", max_events=1)
            for frame in framer.finish():
                consume(frame)
            # 流意外结束（无 response.completed）时补收尾，幂等
            for ev in adapter.finalize():
                self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            self._record_stream_terminal(adapter.terminal_state())
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._acc["stream_integrity"] = "client_disconnect"
            # 客户端断连不是上游 EOF：不 finalize、不制造失败终态、不触发 failover。
            pass
        except Exception as e:
            err = e if isinstance(e, pt.TranslationError) else pt.TranslationError(
                f"internal translation error: {e}", reason="internal_translation_error")
            self._record_stream_error(err)
            log.error("ANTHROPIC_TO_RESPONSES stream interrupted: %s", err)
            try:
                if not (getattr(adapter, "_completed", False)
                        or getattr(adapter, "_failed", False)):
                    err_event = {
                        "type": "error",
                        "error": {"type": "api_error", "message": f"stream interrupted: {e}"},
                    }
                    self._write_sse_chunk(pt.anthropic_sse_bytes(err_event))
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            upstream_resp.close()


    # ------------------------------------------------------------------
    # helper
    # ------------------------------------------------------------------

    @staticmethod
    def _read_stream_chunk(resp, amt: int = 4096) -> bytes:
        """Phase D：单次读取流数据，有啥读啥，不跨 chunk 累积。

        背景（2026-11-06 实测）：http.client.HTTPResponse.read(amt) 在 chunked
        响应上会跨 chunk 循环累积直到凑满 amt 或 EOF，转换链路（a2r/r2a）因此
        首事件被压到流接近结束才交付（线上 first_event_ms/total p50≈0.86）。

        read1(n) 语义（CPython stdlib）：
        - chunked：读当前 chunk 的最多 n 字节（或整 chunk，取小者），不跨 chunk；
        - 非 chunked：底层 fp.read1(n)，至多一次系统调用；
        - 返回 b'' 仅表示 EOF；chunk 中途连接断抛 IncompleteRead。

        兼容边界：自定义/fake response 无 read1 属性时回退 read(amt)（现有测试
        fake 即此形态）；属性存在但不可调用明确报错（fail-closed，不静默走 read）。
        """
        read1 = getattr(resp, "read1", None)
        if read1 is None:
            return resp.read(amt)
        if not callable(read1):
            raise TypeError(f"stream response read1 is not callable: {type(resp).__name__}")
        data = read1(amt)
        # 防 fake/mock reader：read1 返回非 bytes 时不能当有效流数据
        # （MagicMock 自动生成 read1 属性会绕过 read fallback 造成死循环）。
        # 非正确类型 → 回退 read(amt)，由既有路径处理。
        if not isinstance(data, (bytes, bytearray)):
            return resp.read(amt)
        return bytes(data)

    def _send_json(self, status: int, body: Any):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if getattr(self, "_unconsumed_body_close", False):
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _windows_check_config_targets(runtime_paths, config_path, *, project_root=None,
                                  initial_missing_parents=()):
    """Use held Windows path evidence for configs, sidecar, and all roles."""
    import ntpath
    from pathlib import PureWindowsPath
    from core import windows_paths
    from core.discovery import (_windows_open_preflight, _windows_close_leases,
                                _windows_trusted_boundary)
    scope = None
    inputs = []
    try:
        project = ntpath.abspath(os.fspath(_PACKAGE_DIR if project_root is None else project_root))
        business = PureWindowsPath(ntpath.abspath(os.fspath(config_path)))
        path_config = PureWindowsPath(ntpath.join(project, "config", "runtime_paths.json"))
        sidecar = business.parent / "session_overrides.json"
        _, scope = _windows_open_preflight(project, runtime_paths, light=True,
                                           initial_missing_parents=initial_missing_parents)
        backend = scope["__root__"]._backend
        for path in (business, path_config, sidecar):
            lease = windows_paths.inspect(
                str(path), backend=backend, kind="file", private=True,
                trusted_boundary=_windows_trusted_boundary(project, path))
            inputs.append(lease)
            if not lease.evidence.direct_parent_observed:
                raise LockError("config_path_unknown")
        state = scope["state_dir"]
        for item in inputs:
            if windows_paths.may_contain(item, state):
                raise LockError("config_state_conflict")
        for index, item in enumerate(inputs):
            for other in inputs[index + 1:]:
                if windows_paths.may_alias(item, other):
                    raise LockError("config_path_conflict")
            for role, other in scope.items():
                if role in ("__root__", "state_dir"):
                    continue
                if windows_paths.may_contain(item, other) or windows_paths.may_contain(other, item):
                    raise LockError("config_path_conflict")
        for lease in (*scope.values(), *inputs):
            lease.revalidate()
    except LockError:
        raise
    except Exception as exc:
        # The opening preflight may fail while closing an internal HANDLE,
        # before it hands us any leases. Preserve that unknown outcome rather
        # than downgrading it to an ordinary path-verification failure.
        if (getattr(exc, "release_unknown", False) is True or
                getattr(exc, "close_unknown", False) is True or
                getattr(exc, "code", None) in
                ("release_unknown", "lock_release_unknown", "close_failed",
                 "handle_close_failed", "resource_release_failed")):
            raise LockError("lock_release_unknown") from None
        raise LockError("config_path_unknown") from None
    finally:
        all_leases = list(inputs)
        if scope is not None:
            all_leases.extend(scope.values())
        try:
            _windows_close_leases(all_leases)
        except PreflightError:
            raise LockError("lock_release_unknown") from None


def _check_config_targets(runtime_paths, config_path):
    """Reject overlaps among runtime targets, both configs and the sidecar."""
    if os.name == "nt":
        return _windows_check_config_targets(runtime_paths, config_path)
    config = Path(config_path)
    protected = (config, _RUNTIME_PATHS_FILE, config.parent / "session_overrides.json")
    try:
        inputs = tuple(path if path.is_absolute() else Path.cwd() / path for path in protected)
        # Reuse the same identity and unresolved-name rules as the runtime
        # preflight. Probe both roles so no original runtime role is omitted.
        # lock/pid preserve the already-supported configuration symlink path.
        for source in inputs:
            for role in ("lock", "pid"):
                probe = dict(runtime_paths)
                probe[role] = source
                inspect_runtime_identities(_PACKAGE_DIR, probe)
        for index, source in enumerate(inputs):
            for other in inputs[index + 1:]:
                probe = dict(runtime_paths)
                probe["lock"], probe["pid"] = source, other
                inspect_runtime_identities(_PACKAGE_DIR, probe)
    except LockError:
        raise
    except PreflightError as exc:
        raise LockError("config_path_conflict" if exc.code in
                        ("same_identity", "nested_targets", "state_conflict")
                        else "config_path_unknown") from None
    except (OSError, RuntimeError, ValueError):
        raise LockError("config_path_unknown") from None


def _check_legacy_presence(snapshot):
    """Old PID or starter-lock artifacts are UNKNOWN, never auto-cleared."""
    identities = snapshot.as_dict()
    if identities["pid"].exists or identities["start_lock"].exists:
        raise PreflightError("legacy_state_unknown", "Legacy startup state is unknown")


def _check_windows_legacy_observations(snapshot):
    """Read-only Windows role/metadata preflight; never sends a signal."""
    from .discovery import WindowsPathPreflight
    if not isinstance(snapshot, WindowsPathPreflight):
        raise PreflightError("legacy_state_unknown", "Runtime path identity unavailable")
    identities = snapshot.as_dict()
    try:
        paths = {key: identities[key].requested for key in PATH_KEYS}
        light = "install.json" not in identities
        current = (inspect_runtime_identities(snapshot.project_root, paths, windows_light=True)
                   if light else inspect_runtime_identities(snapshot.project_root, paths))
        _check_legacy_presence(current)
        if current != snapshot:
            raise PreflightError("legacy_state_unknown", "Runtime path identity changed")
        observations = []
        for key in PATH_KEYS:
            evidence = identities[key]
            marker = (current_file_marker(
                evidence.canonical,
                trusted_boundary=_windows_config_boundary(evidence.canonical, project_root=snapshot.project_root))
                if evidence.exists else None)
            observations.append((key, marker))
        # Bracket the observation by a second held-chain/path preflight and
        # re-probe existing file IDs/ChangeTime without reading their contents.
        current = (inspect_runtime_identities(snapshot.project_root, paths, windows_light=True)
                   if light else inspect_runtime_identities(snapshot.project_root, paths))
        if current != snapshot:
            raise PreflightError("legacy_state_unknown", "Runtime path identity changed")
        for key, marker in observations:
            target = identities[key].canonical
            if (marker is not None and current_file_marker(
                    target, trusted_boundary=_windows_config_boundary(target, project_root=snapshot.project_root))
                    != marker):
                raise PreflightError("legacy_state_unknown", "Runtime target changed")
        return tuple(observations)
    except PreflightError:
        raise
    except (ConfigSnapshotError, OSError, ValueError, AttributeError, KeyError):
        raise PreflightError("legacy_state_unknown", "Runtime protection unavailable") from None


def _check_legacy_observations(snapshot):
    """Read-only equivalent of CLI legacy preflight; no ownership inference."""
    if os.name == "nt":
        return _check_windows_legacy_observations(snapshot)
    from .secure_files import SecureSnapshotError, mac_private_protection
    identities = snapshot.as_dict()
    try:
        paths = {key: identities[key].requested for key in PATH_KEYS}
        current = inspect_runtime_identities(snapshot.project_root, paths)
        _check_legacy_presence(current)
        if current != snapshot:
            raise PreflightError("legacy_state_unknown", "Runtime path identity changed")
        observations = []
        verified_parents = set()
        for key in PATH_KEYS:
            identity = identities[key]
            target = Path(identity.canonical)
            parent = target.parent
            if parent.resolve(strict=True) != parent:
                raise PreflightError("legacy_state_unknown", "Runtime parent is unknown")
            for part in (parent,):
                if part not in verified_parents:
                    # Match the CLI's bounded retries for transient snapshots.
                    for attempt in range(4):
                        try:
                            _verify_directory(part, private=False)
                            break
                        except PreflightError as exc:
                            if exc.code not in ("protection_changed", "record_changed", "records_changed") or attempt == 3:
                                raise
                            time.sleep(0.01)
                    verified_parents.add(part)
            if not identity.exists:
                observations.append((key, None))
                continue
            before = target.lstat()
            if stat.S_ISDIR(before.st_mode):
                mac_private_protection(str(target), before)
            else:
                fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    opened = os.fstat(fd)
                    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                            or opened.st_uid not in (os.geteuid(), 0) or opened.st_mode & 0o022
                            or (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid, opened.st_gid)
                            != (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid)):
                        raise PreflightError("legacy_state_unknown", "Runtime protection unavailable")
                    verify_no_extended_acl(fd)
                    verify_local_filesystem_fd(fd)
                finally:
                    os.close(fd)
            after = target.lstat()
            def marker(info):
                return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if marker(before) != marker(after):
                raise PreflightError("legacy_state_unknown", "Runtime target changed")
            observations.append((key, marker(after)))
        final = inspect_runtime_identities(snapshot.project_root, paths)
        _check_legacy_presence(final)
        if final != snapshot:
            raise PreflightError("legacy_state_unknown", "Runtime path identity changed")
        return tuple(observations)
    except PreflightError:
        raise
    except (OSError, ValueError, RuntimeError, SecureSnapshotError, AttributeError):
        raise PreflightError("legacy_state_unknown", "Runtime protection unavailable") from None


def _listen_address_family(host, port):
    """Select IPv4 for localhost/dual-stack names, otherwise the IP family."""
    if host.lower() == "localhost":
        return socket.AF_INET  # Preserve the previous IPv4-only localhost behavior.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        candidates = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM,
                                        socket.IPPROTO_TCP)
        families = {item[0] for item in candidates if item[1] == socket.SOCK_STREAM}
        if socket.AF_INET in families:
            return socket.AF_INET
        if socket.AF_INET6 in families:
            return socket.AF_INET6
        raise OSError("listen address family unavailable")
    return socket.AF_INET6 if address.version == 6 else socket.AF_INET


class ProxyHTTPServer(ThreadingHTTPServer):
    """Preserve the baseline outside Windows; use Windows candidate binding."""

    allow_reuse_address = os.name != "nt"

    def __init__(self, server_address, handler_class, bind_and_activate=True):
        if os.name == "nt":
            host, port = server_address[:2]
            self.address_family = _listen_address_family(host, port)
            if self.address_family == socket.AF_INET6:
                server_address = (host, port, 0, 0)
        super().__init__(server_address, handler_class, bind_and_activate)

    def server_bind(self):
        if os.name == "nt":
            if self.address_family == socket.AF_INET6:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY,
                                       0 if self.server_address[0] == "::" else 1)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    config_path = _business_path(_DEFAULT_CONFIG_PATH)
    try:
        if sys.platform == "win32" and os.name == "nt":
            _windows_main(config_path)
            return
        # Match the Mac control authority before records, locks or listeners.
        # Windows retains its separate native startup contract.
        initial_private = _mac_private_config_snapshot(config_path)
        listen_cfg = resolve_listen(config_path, os.environ.get("MODEL_PROXY_PORT"))
        if initial_private is not None:
            _verify_mac_private_config(initial_private)
        global _runtime_paths
        _runtime_paths = resolve_runtime_paths()
        _check_config_targets(_runtime_paths, config_path)
        # Detect invalid/conflicting targets before creating the state directory.
        snapshot = inspect_runtime_identities(_PACKAGE_DIR, _runtime_paths)
        # Only the two platforms with native protection adapters may proceed.
        if not ((sys.platform == "darwin" and os.name == "posix") or
                (sys.platform == "win32" and os.name == "nt")):
            raise PreflightError("protection_unavailable", "ACL verification unavailable")
        state = Path(snapshot.as_dict()["state_dir"].canonical)
        if os.name == "nt":
            # mkdir(mode=0o700) does not establish a verified Windows DACL.
            # Do not create an unprotected state directory and hope to fix it.
            if not snapshot.as_dict()["state_dir"].exists:
                raise PreflightError("windows_state_directory_unverified",
                                     "A verified fixed state directory is required")
        elif not snapshot.as_dict()["state_dir"].exists:
            _verify_directory(state.parent, private=False)
            state.mkdir(mode=0o700)
        verify_record_directory(state)
        snapshot = inspect_runtime_identities(_PACKAGE_DIR, _runtime_paths)
        _check_config_targets(_runtime_paths, config_path)
        initial_records = (read_instance_set(state)
                           if snapshot.as_dict()["install.json"].exists else None)
        first_start = initial_records is None or initial_records["active"] is None
        initial_legacy = _check_legacy_observations(snapshot) if first_start else None
        initial_snapshot = snapshot
        with ExitStack() as locks:
            anchor = locks.enter_context(acquire_lock(
                snapshot, "anchor.lock", create=True, access_verifier=verify_local_access))
            if resolve_runtime_paths() != _runtime_paths:
                raise LockError("runtime_paths_changed")
            legacy = locks.enter_context(acquire_lock(
                anchor.snapshot, "lock", create=True, access_verifier=verify_local_access,
                anchor=anchor))
            # Check once more while both locks are held, before any log or ledger.
            if (resolve_runtime_paths() != _runtime_paths or
                    inspect_runtime_identities(_PACKAGE_DIR, _runtime_paths) != legacy.snapshot):
                raise LockError("preflight_changed")
            _check_config_targets(_runtime_paths, config_path)
            verify_held_instance_locks(anchor, legacy)
            if first_start:
                if read_instance_set(state) != initial_records:
                    raise PreflightError("legacy_state_unknown", "Initialization state changed")
                checked = _check_legacy_observations(legacy.snapshot)
                before = dict(initial_legacy)
                after = dict(checked)
                old_ids, new_ids = initial_snapshot.as_dict(), legacy.snapshot.as_dict()
                if any(before[key] != after[key] or old_ids[key] != new_ids[key]
                       for key in PATH_KEYS if key != "lock"):
                    raise PreflightError("legacy_state_unknown", "Legacy runtime observations changed")
            if sys.platform == "darwin" and os.name == "posix":
                _run_server(config_path, listen_cfg, anchor, legacy,
                            initial_private=initial_private)
            else:
                _run_server(config_path, listen_cfg, anchor, legacy)
    except (ListenConfigError, RuntimePathsError, PreflightError, LockError, OSError) as exc:
        # Do not disclose configuration values or raw paths.
        code = getattr(exc, "code", type(exc).__name__)
        detail = ": runtime paths changed; migration required" if code == "runtime_drift" else ""
        if code == "config_unverified" and sys.platform == "darwin" and os.name == "posix":
            detail = ": private local configuration could not be verified"
        if os.name == "nt":
            from core.discovery import WindowsRecordCommitError
            if isinstance(exc, WindowsRecordCommitError):
                detail = _windows_record_error_detail(exc)
            elif isinstance(exc, WindowsStartupRecoveryError):
                detail = _windows_startup_recovery_detail(exc)
        print(f"model_proxy: startup failed ({code}){detail}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("model_proxy: startup failed (unexpected_error)", file=sys.stderr)
        raise SystemExit(1) from None


def _windows_record_error_detail(exc):
    """Fixed enumerations only; never print paths, ACLs or raw OS messages."""
    stages = {"precommit", "api_failed", "reported_success", "unknown"}
    states = {"original", "replacement", "original_changed", "replacement_changed", "missing", "other", "unknown"}
    stage = exc.stage if exc.stage in stages else "unknown"
    number = exc.winerror if type(exc.winerror) is int and 0 <= exc.winerror <= 0xFFFFFFFF else "unknown"
    recovery = exc.recovery
    target = recovery.target if recovery is not None and recovery.target in states else "unknown"
    source = recovery.source if recovery is not None and recovery.source in states else "unknown"
    old = recovery.old_other_name if recovery is not None and recovery.old_other_name in ("unknown", "not_assumed") else "unknown"
    temp_data = exc.temporary_may_contain_data if type(exc.temporary_may_contain_data) is bool else "unknown"
    temp_created = exc.temporary_created if type(exc.temporary_created) is bool else "unknown"
    return (f": stage={stage} winerror={number} target={target} source={source} "
            f"old_other_name={old} temp_created={temp_created} temp_data={temp_data}")


_WINDOWS_STARTUP_CAUSES = frozenset({
    "startup_config_changed", "runtime_drift", "preflight_changed",
    "ledger_changed", "ledger_unverified", "records_changed",
    "records_inconsistent", "locks_unverified", "lock_identity_changed",
    "security_changed", "protection_unavailable", "process_unverified",
    "process_still_active", "identity_changed", "identity_unknown",
    "protection_changed", "release_unknown", "lock_release_unknown",
})


def _windows_startup_cause(exc):
    """Expose only a fixed code from known startup errors, never raw details."""
    if not isinstance(exc, (PreflightError, LockError, RuntimePathsError, ListenConfigError)):
        return "unknown"
    code = getattr(exc, "code", None)
    return code if type(code) is str and code in _WINDOWS_STARTUP_CAUSES else "unknown"


class WindowsStartupRecoveryError(PreflightError):
    """Records may have changed; no Windows rollback was attempted."""

    def __init__(self, original):
        super().__init__("record_cleanup_failed", "Startup records require manual review")
        self.cause_code = _windows_startup_cause(original)


def _windows_startup_recovery_detail(exc):
    cause = exc.cause_code
    if type(cause) is not str or cause not in _WINDOWS_STARTUP_CAUSES:
        cause = "unknown"
    return f": cause={cause} records=unverified rollback=not_attempted"


def _instance_observation(server, anchor, legacy, install):
    """Protected record data, not proof of current process or peer ownership."""
    pid = os.getpid()
    nonce = os.environ.get("MODEL_PROXY_START_NONCE")
    if nonce is None:
        nonce = uuid.uuid4().hex
    elif not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise PreflightError("invalid_start_nonce", "Invalid startup nonce")
    identities = legacy.snapshot.as_dict()
    if os.name == "nt":
        locks = _lock_identities(anchor, legacy)
    else:
        locks = {}
        for name, held in (("anchor", anchor), ("legacy", legacy)):
            info = os.fstat(held.fileno())
            locks[name] = {"device": info.st_dev, "inode": info.st_ino}
    host, port = server.server_address[:2]
    return {
        "schema": INSTANCE_SCHEMA, "record": "active", "state": "STARTING",
        "instance_nonce": nonce, "pid": pid,
        "process_start": process_start_identity(pid),
        "executable": process_executable(pid),
        "project_root": legacy.snapshot.project_root,
        "listen": {"host": host, "port": port},
        "runtime_paths": {key: identities[key].canonical for key in _runtime_paths},
        "locks": locks,
        "install_id": install["install_id"],
    }


def _capture_confirmation_snapshot(config_path, config_store, listen_cfg, legacy):
    """Capture exactly the inputs to be confirmed, while instance locks are held."""
    try:
        business = _read_business_config(config_path)
        paths = _read_runtime_paths_config()
        with config_store._lock:
            if (config_store._probe_marker != business.marker or config_store._config != business.config
                    or (_mac_business_enabled() and config_store._business_snapshot != business)):
                raise PreflightError("startup_config_changed", "Startup configuration changed")
        legacy_observations = _check_legacy_observations(legacy.snapshot)
        snapshot = (business, paths, os.environ.get("MODEL_PROXY_PORT"), legacy_observations)
        _verify_confirmation_snapshot(snapshot, config_path, config_store, listen_cfg, legacy)
        return snapshot
    except ConfigSnapshotError:
        raise PreflightError("startup_config_changed", "Startup configuration unavailable") from None


def _verify_confirmation_snapshot(snapshot, config_path, config_store, listen_cfg, legacy):
    """Recheck input bytes, identities, parsed paths and listen before committing."""
    try:
        business, paths, port, legacy_observations = snapshot
        _verify_business_snapshot(business)
        if (os.environ.get("MODEL_PROXY_PORT") != port or
                _read_business_config(config_path) != business or
                _read_runtime_paths_config() != paths):
            raise PreflightError("startup_config_changed", "Startup configuration changed")
        with config_store._lock:
            if (config_store._probe_marker != business.marker or config_store._config != business.config
                    or (_mac_business_enabled() and config_store._business_snapshot != business)):
                raise PreflightError("startup_config_changed", "Startup configuration changed")
        if (resolve_listen(config_path, port) != listen_cfg or
                resolve_runtime_paths() != _runtime_paths):
            raise PreflightError("startup_config_changed", "Startup configuration changed")
        _check_config_targets(_runtime_paths, config_path)
        if _check_legacy_observations(legacy.snapshot) != legacy_observations:
            raise PreflightError("legacy_state_unknown", "Legacy runtime observations changed")
        if (_business_marker(config_path) != business.marker or
                _runtime_paths_marker() != paths.marker):
            raise PreflightError("startup_config_changed", "Startup configuration changed")
        _verify_business_snapshot(business)
    except ConfigSnapshotError:
        raise PreflightError("startup_config_changed", "Startup configuration unavailable") from None


def _confirm_initialization():
    """Explicit operator attestation; absence never implies a new install."""
    if not sys.stdin.isatty():
        raise PreflightError("initialization_required", "Interactive initialization required")
    try:
        source = input("Installation/recovery source: ").strip()
        confirm = input("Confirm no old instance, shared/synchronized files or enabled legacy autostart (type YES): ").strip()
    except (EOFError, KeyboardInterrupt):
        raise PreflightError("initialization_unconfirmed", "Initialization not confirmed") from None
    if not source or confirm != "YES":
        raise PreflightError("initialization_unconfirmed", "Initialization not confirmed")
    return source


def _lock_identities(anchor, legacy):
    if os.name == "nt":
        from core import platform_lock
        from core import platform_windows
        result = {}
        try:
            for name, held in (("anchor", anchor), ("legacy", legacy)):
                platform_lock._windows_verify_guard(held._windows_guard, held._windows_lease)
                handle = platform_windows._NativeFDAPI().handle(held.fileno())
                identity = platform_windows.file_identity(handle)
                if identity != held._windows_guard.object_evidence.identity:
                    raise PreflightError("locks_unverified", "Lock identity changed")
                result[name] = {"device": identity.volume_serial, "inode": identity.file_index}
            return result
        except PreflightError:
            raise
        except Exception:
            raise PreflightError("locks_unverified", "Lock identity unavailable") from None
    result = {}
    for name, held in (("anchor", anchor), ("legacy", legacy)):
        info = os.fstat(held.fileno())
        result[name] = {"device": info.st_dev, "inode": info.st_ino}
    return result


def _ensure_stale_instance(records, current_paths, current_listen, lock_ids):
    """Reject path/lock drift and require the previous PID to have exited or changed."""
    active = records["active"]
    if active is None:
        return
    if active["runtime_paths"] != current_paths:
        raise PreflightError("runtime_drift", "Runtime paths changed; migration required")
    if active["locks"] != lock_ids:
        raise PreflightError("lock_identity_changed", "Previous lock identities changed")
    pid = active["pid"]
    if os.name == "nt":
        # Windows os.kill is not a safe existence probe. A stale record
        # cannot authorize a signal or a new instance without native proof.
        raise PreflightError("process_unverified", "Previous process identity unavailable")
    try:
        os.kill(pid, 0)  # Existence/permission probe only; never stop the process.
    except ProcessLookupError:
        return
    except OSError:
        raise PreflightError("process_unverified", "Previous process identity unavailable") from None
    current = process_start_identity(pid)
    if sys.platform == "darwin" and (current is None or current == active["process_start"]):
        # proc_pidinfo may return ESRCH for an unreaped zombie even though
        # kill(pid, 0) still succeeds. Require the same stable PID/start and
        # kernel status evidence used by CLI recovery; never reap here.
        from .platform_process import darwin_recorded_state, ProcessEvidenceError
        try:
            state = darwin_recorded_state(pid, active["process_start"])
        except ProcessEvidenceError:
            raise PreflightError("process_unverified", "Previous process identity unavailable") from None
        if state == 'ZOMBIE':
            return
    if current is None:
        raise PreflightError("process_unverified", "Previous process identity unavailable")
    if current == active["process_start"]:
        raise PreflightError("process_still_active", "Previous process is still active")


def _same_configured_listener(configured, previous):
    host = configured["host"]
    return (configured["port"] == previous["port"] and
            (host == previous["host"] or host.lower() == "localhost" and previous["host"] == "127.0.0.1"))


@contextmanager
def _reserve_previous_listener(listen):
    """Temporarily bind the recorded numeric endpoint; never connect or signal."""
    try:
        host, port = listen["host"], listen["port"]
        ip = ipaddress.ip_address(host)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid port")
        if ip.version == 6 and (ip.is_link_local or getattr(ip, "scope_id", None)):
            raise ValueError("record lacks verifiable IPv6 scope")
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
        listener = socket.socket(family, socket.SOCK_STREAM)
    except (OSError, ValueError, TypeError, KeyError):
        raise PreflightError("old_listener_unverified", "Previous listener cannot be verified") from None
    try:
        try:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if host == "::" else 1)
            listener.bind(address)
            listener.listen(5)
        except OSError as exc:
            code = "old_listener_busy" if exc.errno == errno.EADDRINUSE else "old_listener_unverified"
            raise PreflightError(code, "Previous listener cannot be verified free") from None
        yield listener
    finally:
        listener.close()


def _capture_migration_ledger(snapshot):
    """Capture a security-checked ledger, or explicit content UNAVAILABLE.

    Only stable JSON/schema failure is degradable. Identity, protection, I/O,
    close and race failures remain startup errors, not a missing/empty ledger.
    """
    identity = snapshot.as_dict()["totals"]
    target = Path(identity.canonical)

    def validated_or_unavailable(data, marker):
        if data is None:
            return LedgerUnavailable(marker)
        try:
            validate_migration_ledger_snapshot(data)
        except PreflightError as exc:
            if exc.code != "ledger_unverified":
                raise
            return LedgerUnavailable(marker)
        return data

    try:
        if not identity.exists:
            try:
                target.lstat()
            except FileNotFoundError:
                return None
            raise PreflightError("ledger_changed", "Ledger appeared during startup")
        if os.name == "nt":
            # The startup ledger is the already-preflighted totals role. Other
            # ledger callers and unproven snapshots retain the strict default.
            from .discovery import WindowsPathPreflight
            boundary = (_windows_config_boundary(target, project_root=snapshot.project_root)
                        if isinstance(snapshot, WindowsPathPreflight) else None)
            # A parse failure alone must not waive target/parent protection.
            before = current_file_marker(target, trusted_boundary=boundary)
            if (identity.link_count != 1 or
                    (before.device, before.inode) != (identity.device, identity.inode)):
                raise PreflightError("ledger_changed", "Ledger identity changed")
            try:
                data = read_stable_config(target, reject_duplicate_keys=True,
                                          trusted_boundary=boundary)
            except ConfigSnapshotError as exc:
                if exc.code != "config_parse_error":
                    raise
                data = None
            after = current_file_marker(target, trusted_boundary=boundary)
            if before != after or (data is not None and data.marker != before):
                raise PreflightError("ledger_changed", "Ledger changed during startup")
            return validated_or_unavailable(data, before)
        before = target.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in (os.geteuid(), 0) or before.st_mode & 0o022
                or (before.st_dev, before.st_ino) != (identity.device, identity.inode)):
            raise PreflightError("ledger_unverified", "Ledger identity unavailable")
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            def marker(value):
                return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
                        value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if marker(before) != marker(opened):
                raise PreflightError("ledger_changed", "Ledger changed during startup")
            verify_no_extended_acl(fd)
            verify_local_filesystem_fd(fd)
            try:
                data = read_stable_config(target, reject_duplicate_keys=True)
            except ConfigSnapshotError as exc:
                if exc.code != "config_parse_error":
                    raise
                data = None
            after = os.fstat(fd)
            current = target.lstat()
            file_marker = FileMarker(opened.st_dev, opened.st_ino, opened.st_mtime_ns,
                                     opened.st_ctime_ns, opened.st_size)
            if (marker(opened) != marker(after) or marker(after) != marker(current)
                    or (data is not None and data.marker != file_marker)):
                raise PreflightError("ledger_changed", "Ledger changed during startup")
            return validated_or_unavailable(data, file_marker)
        finally:
            os.close(fd)
    except PreflightError:
        raise
    except (OSError, ConfigSnapshotError, ValueError, AttributeError):
        raise PreflightError("ledger_unverified", "Ledger cannot be verified") from None


def _mac_private_config_snapshot(config_path):
    """Capture the protected Mac business path, never CLI authority internals."""
    if not _mac_business_enabled():
        return None
    from .mac_config_path import MacConfigError, capture_config
    try:
        return capture_config(config_path)
    except MacConfigError as exc:
        raise PreflightError(mac_config_error_code(exc), "Private configuration unavailable") from None


def _verify_mac_private_config(snapshot):
    from .mac_config_path import MacConfigError, verify_config
    try:
        verify_config(snapshot)
    except MacConfigError:
        raise PreflightError("startup_config_changed", "Configuration protection changed") from None


def _check_mac_private_business(private, business):
    """Bind exact chain, identity, bytes, parsed content and probe marker."""
    if (business.evidence is None or private != business.evidence
            or business.marker != private.marker):
        raise PreflightError("startup_config_changed", "Configuration identity changed")
    _verify_mac_private_config(private)


def _capture_migration_guard(config_path, config_store, listen_cfg, legacy, initial_private=None):
    try:
        private = _mac_private_config_snapshot(config_path)
        if sys.platform == "darwin" and os.name == "posix":
            if initial_private is None or private != initial_private:
                raise PreflightError("startup_config_changed", "Configuration changed before guard")
        business = _read_business_config(config_path)
        paths = _read_runtime_paths_config()
        with config_store._lock:
            if (config_store._probe_marker != business.marker or config_store._config != business.config
                    or (_mac_business_enabled() and config_store._business_snapshot != business)):
                raise PreflightError("startup_config_changed", "Startup configuration changed")
        if private is not None:
            _check_mac_private_business(private, business)
            if _mac_private_config_snapshot(config_path) != private:
                raise PreflightError("startup_config_changed", "Configuration protection changed")
        guard = (business, paths, os.environ.get("MODEL_PROXY_PORT"), _capture_migration_ledger(legacy.snapshot))
        return (*guard, private) if private is not None else guard
    except ConfigSnapshotError:
        raise PreflightError("startup_config_changed", "Startup configuration unavailable") from None


def _verify_migration_guard(guard, config_path, config_store, listen_cfg, anchor, legacy,
                            expected_records, *, allow_log_change=False):
    """Validate the same config, paths, held locks, records and current ledger."""
    try:
        business, paths, port, ledger = guard[:4]
        if sys.platform == "darwin" and os.name == "posix":
            if len(guard) != 5 or guard[4] is None:
                raise PreflightError("config_unverified", "Private configuration unavailable")
            private = guard[4]
            if _mac_private_config_snapshot(config_path) != private:
                raise PreflightError("startup_config_changed", "Configuration protection changed")
            _check_mac_private_business(private, business)
        else:
            private = None
        if (os.environ.get("MODEL_PROXY_PORT") != port or
                _read_business_config(config_path) != business or
                _read_runtime_paths_config() != paths):
            raise PreflightError("startup_config_changed", "Startup configuration changed")
        with config_store._lock:
            if (config_store._probe_marker != business.marker or config_store._config != business.config
                    or (_mac_business_enabled() and config_store._business_snapshot != business)):
                raise PreflightError("startup_config_changed", "Startup configuration changed")
        if resolve_listen(config_path, port) != listen_cfg:
            raise PreflightError("startup_config_changed", "Listen configuration changed")
        if resolve_runtime_paths() != _runtime_paths:
            raise PreflightError("runtime_drift", "Runtime paths changed; migration required")
        verify_held_instance_locks(anchor, legacy)
        windows_light = (os.name == "nt" and isinstance(expected_records, dict)
                         and set(expected_records) == {"windows_active"})
        fresh = (inspect_runtime_identities(_PACKAGE_DIR, _runtime_paths, windows_light=True)
                 if windows_light else inspect_runtime_identities(_PACKAGE_DIR, _runtime_paths)).as_dict()
        previous = legacy.snapshot.as_dict()
        for key in (*PATH_KEYS, "state_dir", "anchor.lock"):
            if key == "log" and allow_log_change:
                # init_logging may create or atomically trim this same path.
                if (fresh[key].canonical != previous[key].canonical or
                        fresh[key].requested != previous[key].requested or fresh[key].final_was_link):
                    raise PreflightError("preflight_changed", "Runtime log path changed")
            elif fresh[key] != previous[key]:
                raise PreflightError("preflight_changed", "Runtime path identity changed")
        _check_config_targets(_runtime_paths, config_path)
        state = Path(legacy.snapshot.as_dict()["state_dir"].canonical)
        if (os.name == "nt" and isinstance(expected_records, dict)
                and set(expected_records) == {"windows_active"}):
            from .discovery import read_windows_active
            current_records = {"windows_active": read_windows_active(state)}
        else:
            current_records = read_instance_set(state)
        if current_records != expected_records:
            raise PreflightError("records_changed", "Instance records changed")
        if _capture_migration_ledger(legacy.snapshot) != ledger:
            raise PreflightError("ledger_changed", "Ledger changed during startup")
        if (_business_marker(config_path) != business.marker or
                _runtime_paths_marker() != paths.marker):
            raise PreflightError("startup_config_changed", "Startup configuration changed")
        if private is not None and _mac_private_config_snapshot(config_path) != private:
            raise PreflightError("startup_config_changed", "Configuration protection changed")
    except ConfigSnapshotError:
        raise PreflightError("startup_config_changed", "Startup configuration unavailable") from None


def _restore_prestart_records(state_dir, before, attempted, anchor, legacy):
    """Restore only our own intended changes; uncertain rollback stays UNKNOWN."""
    if os.name == "nt" and attempted:
        # No approved Windows delete/restore contract: never guess which
        # partially committed record still belongs to this startup.
        raise PreflightError("record_cleanup_failed", "Startup records require manual review")
    for name in ("install.json", "active.json", "last_good_paths.json"):
        if name not in attempted:
            continue
        old, new_values = before[name], attempted[name]
        current = read_instance_record(state_dir, name)
        if current == old:
            continue
        if current not in new_values:
            raise PreflightError("record_cleanup_failed", "Record cleanup could not be verified")
        if old is None:
            delete_instance_record_if_matches(state_dir, name, current, anchor=anchor, legacy=legacy)
        else:
            write_instance_record(state_dir, name, old, anchor=anchor, legacy=legacy)
            if read_instance_record(state_dir, name) != old:
                raise PreflightError("record_cleanup_failed", "Record cleanup could not be verified")


def _require_windows_listener_validation():
    """Require the actual exclusive-bind primitive; server_bind applies it."""
    if os.name == "nt" and not isinstance(getattr(socket, "SO_EXCLUSIVEADDRUSE", None), int):
        raise PreflightError("windows_listener_exclusivity_unavailable",
                             "Windows exclusive listener option is unavailable")


def _require_windows_service_launch(config_path, listen_cfg):
    """Do not start an instance the controller cannot identify from argv."""
    from .cli.windows_command_line import verify_shared_launch_argv
    try:
        verify_shared_launch_argv(
            getattr(sys, "orig_argv", ()),
            entry=str(_PACKAGE_DIR / "core" / "cli" / "model_proxy.py"),
            config=str(config_path), port=listen_cfg["port"])
    except Exception:
        raise PreflightError("windows_launch_unverified", "Service launch entry cannot be verified") from None


def _windows_main(config_path):
    """Windows startup using the two OS locks and one non-authoritative locator."""
    import ntpath
    from .discovery import (read_windows_active, prepare_windows_runtime_directories,
                            prepare_windows_log, windows_initial_missing_parents)
    from . import windows_process
    global _runtime_paths
    config_path = Path(ntpath.abspath(os.fspath(config_path)))
    listen_cfg = resolve_listen(config_path, os.environ.get("MODEL_PROXY_PORT"))
    _require_windows_service_launch(config_path, listen_cfg)
    _runtime_paths = resolve_runtime_paths()
    _windows_check_config_targets(
        _runtime_paths, config_path,
        initial_missing_parents=windows_initial_missing_parents(
            _runtime_paths, os.environ.get("LOCALAPPDATA")))
    prepare_windows_runtime_directories(_PACKAGE_DIR, _runtime_paths, os.environ.get("LOCALAPPDATA"))
    _check_config_targets(_runtime_paths, config_path)
    snapshot = inspect_runtime_identities(_PACKAGE_DIR, _runtime_paths, windows_light=True)
    state = Path(snapshot.as_dict()["state_dir"].canonical)
    if not snapshot.as_dict()["state_dir"].exists:
        raise PreflightError("windows_state_directory_unverified",
                             "A privately created state directory is required")
    verify_record_directory(state)
    with ExitStack() as locks:
        anchor = locks.enter_context(acquire_lock(
            snapshot, "anchor.lock", create=True, access_verifier=verify_local_access))
        legacy = locks.enter_context(acquire_lock(
            anchor.snapshot, "lock", create=True, access_verifier=verify_local_access,
            anchor=anchor))
        verify_held_instance_locks(anchor, legacy)
        if resolve_runtime_paths() != _runtime_paths:
            raise LockError("runtime_paths_changed")
        _check_config_targets(_runtime_paths, config_path)
        previous = read_windows_active(state)
        if previous is not None:
            try:
                status = windows_process.recorded_process_state(
                    previous["pid"], previous["process_start"], previous["executable"])
            except windows_process.ProcessEvidenceError as exc:
                raise PreflightError("previous_process_unknown", "Previous process is unknown") from exc
            if status == "running":
                raise PreflightError("previous_process_alive", "Previous process is still running")
            # A path change is not silently taken over, even if the old PID exited.
            current = {key: legacy.snapshot.as_dict()[key].canonical for key in PATH_KEYS}
            if current != previous["runtime_paths"]:
                raise PreflightError("runtime_drift", "Runtime paths changed")
        else:
            _check_legacy_observations(legacy.snapshot)
        # Ordinary log creation is a service-side effect: neither contender may
        # create it until *both* instance locks are held.  The new path evidence
        # must differ only by this one created file, never by another role.
        before_log = legacy.snapshot
        after_log = prepare_windows_log(_PACKAGE_DIR, _runtime_paths, before_log)
        if after_log != before_log:
            from .platform_lock import _windows_compare_post
            _windows_compare_post(before_log, after_log, "log")
        # The fresh snapshot is admissible only after the single-log delta
        # check.  Held-lock validation itself compares against the snapshot,
        # so publish it to both held locks *before* that validation.
        anchor.snapshot = legacy.snapshot = after_log
        verify_held_instance_locks(anchor, legacy)
        _check_config_targets(_runtime_paths, config_path)
        config_store = ConfigStore(config_path, on_reload=None)
        guard = _capture_migration_guard(config_path, config_store, listen_cfg, legacy)
        expected = {"windows_active": previous}
        _verify_migration_guard(guard, config_path, config_store, listen_cfg, anchor, legacy, expected)
        _require_windows_listener_validation()
        with ProxyHTTPServer((listen_cfg["host"], listen_cfg["port"]), ModelProxyHandler) as server:
            _verify_migration_guard(guard, config_path, config_store, listen_cfg, anchor, legacy, expected)
            pid = os.getpid()
            record = {
                "schema": 3, "record": "active", "state": "STARTING",
                "instance_nonce": os.environ.get("MODEL_PROXY_START_NONCE") or uuid.uuid4().hex,
                "project_root": legacy.snapshot.project_root, "pid": pid,
                "process_start": process_start_identity(pid), "executable": process_executable(pid),
                "entry": str(_PACKAGE_DIR / "core" / "cli" / "model_proxy.py"),
                "config": str(config_path),
                "listen": {"host": server.server_address[0], "port": server.server_address[1]},
                "runtime_paths": {key: legacy.snapshot.as_dict()[key].canonical for key in PATH_KEYS},
            }
            from .discovery import _valid_windows_active
            if not _valid_windows_active(record, state):
                raise PreflightError("process_unverified", "Current process identity unavailable")
            write_instance_record(state, "active.json", record, anchor=anchor, legacy=legacy)
            expected = {"windows_active": record}
            _verify_migration_guard(guard, config_path, config_store, listen_cfg, anchor, legacy, expected)
            ready, attempted = {"serving_started": False}, {"active.json": [record]}
            _run_bound_server(server, config_path, listen_cfg, config_store, state, record,
                              ready, attempted, anchor, legacy, guard, expected, None)


def _run_server(config_path, listen_cfg, anchor, legacy, *, initial_private=None):
    config_store = ConfigStore(config_path, on_reload=None)
    if resolve_listen(config_path, os.environ.get("MODEL_PROXY_PORT")) != listen_cfg:
        raise PreflightError("listen_changed", "Listen configuration changed")
    if (resolve_runtime_paths() != _runtime_paths or
            inspect_runtime_identities(_PACKAGE_DIR, _runtime_paths) != legacy.snapshot):
        raise LockError("preflight_changed")
    _check_config_targets(_runtime_paths, config_path)
    state_dir = Path(legacy.snapshot.as_dict()["state_dir"].canonical)
    records = read_instance_set(state_dir)
    active = None if records is None else records["active"]
    current_paths = {key: legacy.snapshot.as_dict()[key].canonical for key in PATH_KEYS}
    lock_ids = _lock_identities(anchor, legacy)
    if active is not None:
        _ensure_stale_instance(records, current_paths, listen_cfg, lock_ids)
    migration = (active is not None and not _same_configured_listener(listen_cfg, active["listen"]))
    # Every startup must revalidate the stopped/current ledger, even if the
    # listen endpoint did not change or no earlier instance exists.
    if sys.platform == "darwin" and os.name == "posix":
        guard = _capture_migration_guard(config_path, config_store, listen_cfg, legacy, initial_private)
    else:
        guard = _capture_migration_guard(config_path, config_store, listen_cfg, legacy)

    # Old port is reserved while checking and, for different ports, through
    # the new bind. Same-port address changes require releasing it first.
    with ExitStack() as listeners:
        old_listener = None
        _verify_migration_guard(guard, config_path, config_store, listen_cfg, anchor, legacy, records)
        _require_windows_listener_validation()
        if migration:
            old_listener = listeners.enter_context(_reserve_previous_listener(active["listen"]))
            _ensure_stale_instance(records, current_paths, listen_cfg, lock_ids)
            if active["listen"]["port"] == listen_cfg["port"]:
                old_listener.close()
        server = ProxyHTTPServer((listen_cfg["host"], listen_cfg["port"]), ModelProxyHandler)
        listeners.callback(server.server_close)
        _verify_migration_guard(guard, config_path, config_store, listen_cfg, anchor, legacy, records)
        if migration:
            _ensure_stale_instance(records, current_paths, listen_cfg, lock_ids)
            old_listener.close()
        if (resolve_runtime_paths() != _runtime_paths or
                inspect_runtime_identities(_PACKAGE_DIR, _runtime_paths) != legacy.snapshot):
            raise LockError("preflight_changed")
        _check_config_targets(_runtime_paths, config_path)
        if read_instance_set(state_dir) != records:
            raise PreflightError("records_changed", "Instance records changed")
        if records is None or records["active"] is None:
            _check_legacy_observations(legacy.snapshot)
        if records is None:
            confirmation = _capture_confirmation_snapshot(config_path, config_store, listen_cfg, legacy)
            source = _confirm_initialization()
            def recheck_confirmed_inputs():
                _verify_confirmation_snapshot(confirmation, config_path, config_store, listen_cfg, legacy)
            recheck_confirmed_inputs()
            initialize_installation(anchor, legacy, server.socket, listen=listen_cfg,
                                    source=source, confirmed=True, precommit_check=recheck_confirmed_inputs)
            records = read_instance_set(state_dir)
            if records is None:
                raise PreflightError("initialization_unverified", "Initialization did not verify")
        install = records["install"]
        record = _instance_observation(server, anchor, legacy, install)
        if record["process_start"] is None or record["executable"] is None:
            raise PreflightError("process_unverified", "Current process identity unavailable")
        _ensure_stale_instance(records, record["runtime_paths"], record["listen"], record["locks"])
        if active is not None and record["listen"] != active["listen"] and not migration:
            raise PreflightError("listen_changed", "Resolved listener changed during startup")
        _verify_migration_guard(guard, config_path, config_store, listen_cfg, anchor, legacy, records)
        last_good = {key: record[key] for key in ("schema", "project_root", "instance_nonce",
                                                    "listen", "runtime_paths", "install_id")}
        last_good["record"] = "last_good_paths"
        updated_install = dict(install, ever_started=True, last_instance_nonce=record["instance_nonce"])
        before = {"install.json": install, "active.json": records["active"],
                  "last_good_paths.json": records["last_good_paths"]}
        writes = (("last_good_paths.json", last_good), ("active.json", record),
                  ("install.json", updated_install))
        attempted = {}
        ready = {"serving_started": False}
        expected = {"install": updated_install, "active": record, "last_good_paths": last_good}
        try:
            for name, value in writes:
                attempted[name] = [value]  # Include post-replace failures in rollback.
                write_instance_record(state_dir, name, value, anchor=anchor, legacy=legacy)
            if read_instance_set(state_dir) is None:
                raise PreflightError("records_inconsistent", "Instance records did not verify")
            _verify_migration_guard(guard, config_path, config_store, listen_cfg, anchor, legacy, expected)
            _run_bound_server(server, config_path, listen_cfg, config_store, state_dir, record,
                              ready, attempted, anchor, legacy, guard, expected, records)
        except BaseException as exc:
            if os.name == "nt":
                from core.discovery import WindowsRecordCommitError
                if isinstance(exc, WindowsRecordCommitError):
                    # A failed ReplaceFileW may already have changed names or
                    # ACLs; preserve recovery evidence and never blind-rollback.
                    raise
            if not ready["serving_started"]:
                if os.name == "nt" and attempted:
                    # Replacement may have committed despite a reported error.
                    # Preserve the original fixed failure category without
                    # claiming that any record has been restored.
                    raise WindowsStartupRecoveryError(exc) from None
                try:
                    _restore_prestart_records(state_dir, before, attempted, anchor, legacy)
                except Exception:
                    raise PreflightError("record_cleanup_failed", "Startup record cleanup failed") from None
            raise


def _close_windows_startup_diagnostic_pipe():
    """Return a controller-launched service's stderr to NUL before serving.

    The marker only controls diagnostic plumbing; it conveys no authority.
    Direct launches and all non-Windows behavior are unchanged.
    """
    if os.name != "nt" or os.environ.pop("MODEL_PROXY_STARTUP_DIAGNOSTIC_PIPE", None) != "1":
        return
    try:
        sys.stderr.flush()
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stderr.fileno())
    except Exception:
        raise PreflightError("startup_diagnostics_unavailable", "Startup diagnostic pipe unavailable") from None


class _LedgerGuardUnverified(PreflightError):
    """The shared guard failed without a complete release-result contract."""
    def __init__(self):
        super().__init__("ledger_guard_unverified", "Ledger guard unavailable")
        # Existing guard helpers may collapse a close failure into a generic
        # protection error. Conservatively quarantine until process restart.
        self.close_unknown = True
        self.release_unknown = True


class _LedgerRuntimeGuard:
    """Revalidate actual held locks, selected path and all protected roles.

    The configured totals path is the authorization source; the existing
    role preflight and the platform writer separately verify identities.
    """
    def __init__(self, config_path, anchor, legacy, runtime_paths):
        if not ((sys.platform == "darwin" and os.name == "posix") or
                (sys.platform == "win32" and os.name == "nt")):
            raise PreflightError("ledger_unverified", "Ledger runtime adapter unavailable")
        self.root = _PACKAGE_DIR
        self.config_path = Path(config_path)
        self.anchor, self.legacy = anchor, legacy
        self.paths = {key: Path(runtime_paths[key]) for key in PATH_KEYS}
        self.target = self.paths["totals"]
        self.state = self.root / ".runtime"
        self.staging = self.state / "tmp"
        initial = legacy.snapshot.as_dict()["totals"]
        if (not self.root.is_absolute() or not self.target.is_absolute()
                or ".." in self.target.parts or str(self.target) != initial.canonical
                or str(self.target) != initial.requested or initial.final_was_link):
            raise PreflightError("ledger_unverified", "Ledger target not supported")
        if os.name == "nt":
            from .discovery import _windows_trusted_boundary
            self.boundary = Path(_windows_trusted_boundary(self.root, self.target))
            self.external = self.boundary != self.root
        else:
            self.external = os.path.commonpath((str(self.root), str(self.target))) != str(self.root)
            self.boundary = self.target.parent if self.external else self.root

    def _check_mac_external_boundary(self):
        if not self.external or os.name != "posix":
            return
        try:
            root_info = self.root.stat()
            current = self.target.parent
            while True:
                info = current.stat()
                if (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino):
                    raise PreflightError("ledger_unverified", "External boundary ambiguous")
                if current == current.parent:
                    break
                current = current.parent
        except PreflightError:
            raise
        except (OSError, ValueError, AttributeError):
            raise PreflightError("ledger_unverified", "External boundary unavailable") from None

    def _check_mac_external_namespace(self, roles):
        """Prove hidden random target-parent names do not overlap any role."""
        if not self.external or os.name != "posix":
            return
        from .discovery import _identity_contains, _path_identity
        parent = self.target.parent
        try:
            parent_id = _path_identity(parent, fixed=False, kind="directory")
            protected = list(roles.values())
            for source in (self.config_path, _RUNTIME_PATHS_FILE,
                           self.config_path.parent / "session_overrides.json"):
                path = source if source.is_absolute() else Path.cwd() / source
                protected.append(_path_identity(path, fixed=False, kind="file"))
            for item in protected:
                component = None
                for i, entry in enumerate(item.trace):
                    if (entry[1], entry[2]) == (parent_id.device, parent_id.inode):
                        if i + 1 < len(item.trace):
                            component = Path(item.trace[i + 1][0]).name
                        elif item.unresolved:
                            component = item.unresolved[0]
                        else:
                            raise PreflightError("ledger_unverified", "Staging namespace overlaps a role")
                        break
                if component is None and _identity_contains(parent_id, item):
                    # Alias/containment without a physically bound first child.
                    raise PreflightError("ledger_unverified", "Staging namespace ambiguous")
                if component is not None and (not component.isascii() or re.fullmatch(
                        r"\.model-proxy-runtime-[0-9a-f]{24}", component, flags=re.IGNORECASE)):
                    raise PreflightError("ledger_unverified", "Staging namespace overlaps a role")
        except PreflightError:
            raise
        except (OSError, ValueError, AttributeError):
            raise PreflightError("ledger_unverified", "Staging namespace unavailable") from None

    def _verify_locks(self):
        try:
            verify_held_instance_locks(self.anchor, self.legacy)
        except Exception:
            # The shared lock helper can collapse a close failure into a
            # generic protection error, so release cannot be inferred here.
            raise _LedgerGuardUnverified() from None

    def __call__(self):
        self._verify_locks()
        try:
            configured = resolve_runtime_paths(_RUNTIME_PATHS_FILE)
        except Exception:
            # The shared snapshot reader can similarly hide a close failure.
            raise _LedgerGuardUnverified() from None
        if configured != self.paths:
            raise PreflightError("runtime_drift", "Runtime paths changed")
        fresh = (inspect_runtime_identities(self.root, self.paths, windows_light=True)
                 if os.name == "nt" else inspect_runtime_identities(self.root, self.paths)).as_dict()
        target = fresh["totals"]
        if (target.canonical != str(self.target) or target.requested != str(self.target)
                or target.final_was_link or fresh["state_dir"].canonical != str(self.state)):
            raise PreflightError("ledger_changed", "Ledger target changed")
        # The shared preflight checks all runtime/fixed roles, tmp/backups,
        # config, path-config and sidecar, including missing-name alias rules.
        _check_config_targets(self.paths, self.config_path)
        self._check_mac_external_boundary()
        self._verify_locks()
        return True

    def check_mac_target_staging(self):
        """Authorize hidden target-parent names only for the cross-volume case."""
        if os.name != "posix" or not self.external:
            raise PreflightError("ledger_unverified", "External staging not authorized")
        self()
        fresh = inspect_runtime_identities(self.root, self.paths).as_dict()
        self._check_mac_external_namespace(fresh)
        self._verify_locks()
        return True

    def check_windows_artifact(self, stage, artifact, *, require_absent=False):
        """Bind the exact native staging name to live role/input leases."""
        if os.name != "nt":
            raise PreflightError("ledger_unverified", "Windows guard unavailable")
        import ntpath
        from . import windows_paths
        from .discovery import _windows_open_preflight, _windows_close_leases, _windows_trusted_boundary
        stage, artifact = os.fspath(stage), os.fspath(artifact)
        if (stage not in (str(self.staging), str(self.target.parent))
                or (stage == str(self.target.parent) and not self.external)
                or ntpath.dirname(artifact) != stage
                or re.fullmatch(r"\.model-proxy-stage-[0-9a-f]{24}", ntpath.basename(artifact)) is None):
            raise PreflightError("ledger_unverified", "Staging artifact is not authorized")
        self()
        scope = None
        extras = []
        try:
            _, scope = _windows_open_preflight(str(self.root), self.paths, light=True)
            backend = scope["__root__"]._backend
            candidate = windows_paths.inspect(
                artifact, backend=backend, kind="file", private=True, single_link=True,
                trusted_boundary=_windows_trusted_boundary(self.root, artifact))
            extras.append(candidate)
            absent = not candidate.evidence.exists and candidate.evidence.direct_parent_observed
            for source in (self.config_path, _RUNTIME_PATHS_FILE,
                           self.config_path.parent / "session_overrides.json"):
                lease = windows_paths.inspect(str(source), backend=backend, kind="file", private=True,
                                              trusted_boundary=_windows_trusted_boundary(self.root, source))
                extras.append(lease)
            for name, lease in scope.items():
                if name in ("__root__", "state_dir") or (name == "tmp_dir" and stage == str(self.staging)):
                    continue
                if windows_paths.may_contain(candidate, lease) or windows_paths.may_contain(lease, candidate):
                    raise PreflightError("ledger_unverified", "Staging artifact overlaps a role")
            for lease in extras[1:]:
                if windows_paths.may_contain(candidate, lease) or windows_paths.may_contain(lease, candidate):
                    raise PreflightError("ledger_unverified", "Staging artifact overlaps an input")
            for lease in (*scope.values(), *extras):
                lease.revalidate()
        finally:
            try:
                _windows_close_leases([*extras, *(scope.values() if scope is not None else ())])
            except Exception:
                raise _LedgerGuardUnverified() from None
        self()
        return absent if require_absent else True

    def prepare_staging(self):
        """Create only the fixed private tmp directory, under held locks."""
        self()
        if os.name == "nt":
            from .platform_windows import ensure_private_directory, WindowsPrivateCreationError
            try:
                ensure_private_directory(str(self.staging), trusted_boundary=str(self.root))
            except WindowsPrivateCreationError as exc:
                if exc.created is not False or exc.release_unknown:
                    unknown = PreflightError("ledger_staging_unknown", "Staging creation unverified")
                    unknown.cleanup_unknown = True
                    unknown.release_unknown = bool(exc.release_unknown)
                    raise unknown from None
                raise PreflightError("ledger_unverified", "Staging unavailable") from None
            self()
            return self.staging
        from .secure_files import capture_private_directory
        expected = self.legacy.snapshot.as_dict()["state_dir"]
        try:
            verify_record_directory(self.state)
        except Exception:
            raise _LedgerGuardUnverified() from None
        fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (expected.device, expected.inode):
                raise PreflightError("ledger_changed", "Runtime directory changed")
            try:
                os.stat("tmp", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    # A competing creation is not silently adopted.
                    os.mkdir("tmp", 0o700, dir_fd=fd)
                except FileExistsError:
                    raise PreflightError("ledger_changed", "Staging directory appeared") from None
                except OSError:
                    # The creation outcome and any residual are not established.
                    exc = PreflightError("ledger_staging_unknown", "Staging creation unverified")
                    exc.cleanup_unknown = True
                    raise exc from None
        finally:
            try:
                os.close(fd)
            except OSError:
                raise PreflightError("release_unknown", "Staging directory release unknown") from None
        self()
        return capture_private_directory(self.staging, purpose="staging")


def _ledger_runtime_adapter(config_path, anchor, legacy):
    """Install only a real platform adapter with its real runtime guard."""
    try:
        from .ledger_secure_io import MacLedgerSecureIO, WindowsLedgerSecureIO
        guard = _LedgerRuntimeGuard(config_path, anchor, legacy, _runtime_paths)
        adapter = WindowsLedgerSecureIO if os.name == "nt" else MacLedgerSecureIO
        return adapter(root=guard.root, target=guard.target, guard=guard)
    except Exception:
        # Lack of a supported secure platform/path is a persistence downgrade,
        # never permission to fall back to an unchecked replace.
        return None


def _sidecar_runtime_store(config_path, anchor, legacy):
    """Build the protected sidecar; failure never falls back to legacy writes."""
    from .sidecar_runtime_guard import SidecarRuntimeGuard, UnavailableSidecarIO
    path = Path(config_path).parent / "session_overrides.json"
    try:
        from .sidecar_secure_io import SidecarSecureIO, _source
        selected = os.environ.get("MODEL_PROXY_CONFIG")
        explicit = bool(selected) and _source(selected) == _source(config_path)
        guard = SidecarRuntimeGuard(root=_PACKAGE_DIR, config_path=config_path,
                                    anchor=anchor, legacy=legacy, runtime_paths=_runtime_paths,
                                    explicit_external_config=explicit)
        adapter = SidecarSecureIO(project_root=_PACKAGE_DIR, target=path, guard=guard,
                                   allow_external_target_staging=guard.allow_external_target_staging)
        try:
            guard.prepare_staging()
        except Exception:
            # Guard retains any resource uncertainty. An ordinary unavailable
            # staging directory need not prevent protected reads/conversation.
            log.warning("sidecar: staging unavailable; protected writes may be blocked")
    except Exception as exc:
        log.warning("sidecar: protected adapter unavailable; overrides remain unavailable")
        adapter = UnavailableSidecarIO(exc)
    return SessionOverridesSidecar(path, secure_io=adapter)


def _run_bound_server(server, config_path, listen_cfg, config_store, state_dir, record, ready, attempted,
                      anchor, legacy, migration_guard=None, expected_records=None, previous_records=None):
    global _VERSION
    try:
        _VERSION = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    global usage_totals
    if migration_guard is None:
        raise PreflightError("ledger_unverified", "Validated ledger snapshot required")
    _verify_migration_guard(migration_guard, config_path, config_store, listen_cfg,
                            anchor, legacy, expected_records)
    windows_light = (os.name == "nt" and isinstance(expected_records, dict)
                     and set(expected_records) == {"windows_active"})
    if windows_light:
        init_logging(_runtime_paths["log"], trim=False)
    else:
        init_logging(_runtime_paths["log"])
    _verify_migration_guard(migration_guard, config_path, config_store, listen_cfg,
                            anchor, legacy, expected_records, allow_log_change=True)
    ledger_adapter = _ledger_runtime_adapter(config_path, anchor, legacy)
    usage_totals = UsageTotalsStore.from_verified_snapshot(
        _runtime_paths["totals"], migration_guard[3], adapter=ledger_adapter)

    # 2. 实例化 CooldownStore
    cooldown_store = CooldownStore()
    stream_health_store = StreamHealthStore()

    # 2.5 实例化 SyntaxPreferenceStore（reasoning 语法偏好，取代旧 _THINKING_FMT_CACHE）
    pref_store = SyntaxPreferenceStore()

    # 2.5b 实例化 ThinkingStripStore（thinking 空签名剥离的 supply 级学习，
    # 见 core/thinking_guard.py 与 docs/designs/2026-10-28-thinking空签名剥离与supply级自适应.md）
    thinking_strip_store = ThinkingStripStore()

    # 2.6 实例化 SessionOverridesSidecar（$route in-band 指令的 override 落盘，
    # 与主 config 同目录，代理独占写，见 docs/designs/2026-08-04-in-band-route-command-design.md §4.5）
    sidecar_store = _sidecar_runtime_store(config_path, anchor, legacy)

    # Socket is already exclusively bound; publish handlers only after stores exist.
    server.config_store = config_store    # type: ignore[attr-defined]
    server.cooldown_store = cooldown_store  # type: ignore[attr-defined]
    server.stream_health_store = stream_health_store  # type: ignore[attr-defined]
    server.pref_store = pref_store        # type: ignore[attr-defined]
    server.thinking_strip_store = thinking_strip_store  # type: ignore[attr-defined]
    tool_strict_store = ToolStrictStore()
    server.tool_strict_store = tool_strict_store  # type: ignore[attr-defined]
    count_tokens_store = CountTokensUnsupportedStore()
    server.count_tokens_store = count_tokens_store  # type: ignore[attr-defined]
    server.sidecar_store = sidecar_store  # type: ignore[attr-defined]
    # Host 头校验仅 listen.host 非 loopback 时装配（None = 未启用，loopback 无暴露面）
    server.host_validator = (  # type: ignore[attr-defined]
        HostHeaderValidator(listen_cfg["port"], listen_cfg["allow_hosts"])
        if listen_cfg["host_check_enabled"] else None)

    if migration_guard is not None:
        _verify_migration_guard(migration_guard, config_path, config_store, listen_cfg,
                                anchor, legacy, expected_records, allow_log_change=True)
        if previous_records is not None:
            _ensure_stale_instance(previous_records, record["runtime_paths"], record["listen"], record["locks"])
    record = dict(record, state="READY")
    # A post-replace failure may have committed READY. Include both our
    # STARTING and READY values in pre-serve cleanup's allowed identities.
    attempted["active.json"].append(record)
    write_instance_record(state_dir, "active.json", record, anchor=anchor, legacy=legacy)
    windows_light = (os.name == "nt" and isinstance(expected_records, dict)
                     and set(expected_records) == {"windows_active"})
    if windows_light:
        from .discovery import read_windows_active
        ready_records = {"windows_active": read_windows_active(state_dir)}
    else:
        ready_records = read_instance_set(state_dir)
    if ready_records is None:
        raise PreflightError("records_inconsistent", "Ready record did not verify")
    if os.name == "nt" or _mac_business_enabled():
        # A self-consistent record set is not necessarily the one just written.
        # Recheck after publication and before serving. Mac cleanup attempts
        # only the existing identity-checked pre-start rollback; failure is UNKNOWN.
        if not isinstance(expected_records, dict):
            raise PreflightError("records_inconsistent", "Ready record did not verify")
        expected_ready = ({"windows_active": record} if windows_light
                          else dict(expected_records, active=record))
        if ready_records != expected_ready:
            raise PreflightError("records_changed", "Ready records changed")
        _verify_migration_guard(migration_guard, config_path, config_store, listen_cfg,
                                anchor, legacy, expected_ready, allow_log_change=True)
        if previous_records is not None:
            _ensure_stale_instance(previous_records, record["runtime_paths"],
                                   record["listen"], record["locks"])

    log.info("startup.listening host=%s port=%d pid=%d config_path=%s "
             "host_check=%s allow_hosts=%d",
             listen_cfg["host"], listen_cfg["port"], os.getpid(),
             str(config_path),
             "on" if listen_cfg["host_check_enabled"] else "off",
             len(listen_cfg["allow_hosts"]))
    if windows_light:
        _close_windows_startup_diagnostic_pipe()
    try:
        ready["serving_started"] = True
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown.signal KeyboardInterrupt")
    finally:
        server.shutdown()
        log.info("shutdown.complete")


if __name__ == "__main__":
    main()
