"""
tools/model_proxy/core/server.py — 本地多协议路由代理主体

多协议 AI 模型代理主程序：HTTP server、路由决策、转发编排、协议转换、控制 API。
入口为 tools/model_proxy/model_proxy.py（thin wrapper 调用本模块 main()）。
端口 18889、配置 tools/model_proxy/config/model_proxy_config.json（可用 MODEL_PROXY_CONFIG
环境变量覆盖）、进程锁 /tmp/model_proxy.lock、日志 tools/model_proxy/.model_proxy.log。
（v1 proxy.py 于 2026-07-24 下线删除，本模块为唯一代理实现。）

仅使用 Python 标准库，不引入第三方依赖。
"""

import hashlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

# 合并后的双向协议转换器（core 包内相对导入）
from . import translate as pt
from .commands import (
    CMD_PREFIX,
    COMMAND_HANDLERS,
    CommandContext,
    SessionOverridesSidecar,
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
_LOG_FILE_DEFAULT = _PACKAGE_DIR / ".model_proxy.log"
_TOTALS_FILE_DEFAULT = _PACKAGE_DIR / ".model_proxy_totals.json"
_LOCK_FILE_DEFAULT = Path("/tmp/model_proxy.lock")

_DEFAULT_PATHS = {
    "log": str(_LOG_FILE_DEFAULT),
    "totals": str(_TOTALS_FILE_DEFAULT),
    "lock": str(_LOCK_FILE_DEFAULT),
    "pid": "/tmp/model_proxy.pid",
    "ensure_log": "/tmp/model_proxy_ensure.log",
    "start_lock": "/tmp/model_proxy_start.lock",
}

# 运行时实际路径（main() 启动路径赋值，测试 import 时为 None）
_runtime_paths: dict[str, Path] | None = None


def resolve_runtime_paths(paths_file: Path = _RUNTIME_PATHS_FILE) -> dict[str, Path]:
    """启动时一次性解析所有运行时路径。不参与热重载。

    runtime_paths.json 缺失/corrupt → 全部回退默认值。
    相对路径以 paths_file.parent.parent（即 model_proxy/）为基准。
    """
    paths = {}
    base = paths_file.parent.parent  # model_proxy/
    try:
        with open(paths_file, "r", encoding="utf-8") as f:
            raw_paths = json.load(f)
    except (json.JSONDecodeError, OSError):
        raw_paths = {}
    for key, default in _DEFAULT_PATHS.items():
        val = raw_paths.get(key, default)
        p = Path(val)
        if not p.is_absolute():
            p = base / p
        paths[key] = p
    return paths


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


def init_logging(log_path: Path) -> None:
    """生产启动路径调用：截断日志 + 装配 root/access handler 到 log_path。

    仅在 main() 里调用一次。测试 import 本模块时不执行，root logger 无 FileHandler，
    测试产生的 log 行不会写入生产日志文件。
    幂等：重复调用直接返回（否则 access handler 会重复追加导致双写）。
    """
    if access_log.handlers:
        return
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


def _atomic_write_json(path: Path, obj: dict) -> None:
    """mkstemp + os.replace 原子写盘，模式与 _config_ops.atomic_write 一致，
    但不跨包 import（依赖方向/形参语义不同，本函数在 server.py 内自持一份）。
    """
    _dir = str(path.parent)
    fd, tmp = tempfile.mkstemp(dir=_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


class UsageTotalsStore:
    """独立账本：按天分桶，桶内 supply×route×strategy 组合键累加。

    与 access 日志完全独立（无调用关系），不受 `_trim_log` 影响；文件只增不截。
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            return {
                "version": 3,
                "since": _cst_now().strftime("%Y-%m-%d"),
                "keep_days": KEEP_DAYS,
                "total": _zero_bucket(),
                "months_archive": {},
                "days": {},
            }
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("usage totals ledger corrupt, resetting: %s", e)
            try:
                ts = int(time.time())
                corrupt_path = self._path.with_name(self._path.name + f".corrupt.{ts}")
                os.replace(str(self._path), str(corrupt_path))
            except OSError:
                pass
            return {
                "version": 3,
                "since": _cst_now().strftime("%Y-%m-%d"),
                "keep_days": KEEP_DAYS,
                "total": _zero_bucket(),
                "months_archive": {},
                "days": {},
            }
        # OPT-10: v2→v3 迁移（补 0，断档但保真）
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
            _atomic_write_json(self._path, self._data)

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
_DEFAULT_CONFIG_PATH = Path(
    os.environ.get("MODEL_PROXY_CONFIG")
    or (Path(__file__).resolve().parent.parent / "config" / "model_proxy_config.json")
)
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

class ConfigStore:
    """从 model_proxy_config.json 加载并热重载配置。

    热重载机制（mtime 比对、maybe_reload 双重检查、_reload_locked
    失败保留旧配置、reload 强制重载）拷贝自 proxy.py，getter 换成
    新 schema（supplies/routes/admin_token/cooldown_rules）。
    """

    def __init__(
        self,
        config_path: str | os.PathLike | None = None,
        on_reload: Callable[[], None] | None = None,
    ):
        self._path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        self._config: dict[str, Any] = {}
        self._mtime: float = 0.0
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

    # ------------------------------------------------------------------
    # 热重载（拷贝 proxy.py 原样）
    # ------------------------------------------------------------------

    def maybe_reload(self) -> bool:
        """比对 mtime，有变化则重载，返回是否触发了重载。"""
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return False
        if mtime <= self._mtime:
            return False
        with self._lock:
            # 双重检查
            try:
                mtime = self._path.stat().st_mtime
            except FileNotFoundError:
                return False
            if mtime <= self._mtime:
                return False
            reloaded = self._reload_locked()
        # 锁外调用回调，避免锁嵌套；加载失败则不触发回调
        if reloaded and self._on_reload is not None:
            self._on_reload()
        return reloaded

    def reload(self) -> None:
        """强制重载配置文件，并触发 on_reload 回调。"""
        with self._lock:
            reloaded = self._reload_locked()
        if reloaded and self._on_reload is not None:
            self._on_reload()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        """初始化加载，失败直接上抛（不容错）。"""
        with self._lock:
            with open(self._path, "r", encoding="utf-8") as f:
                new_config: dict[str, Any] = json.load(f)
            self._mtime = self._path.stat().st_mtime
            self._config = new_config
            self._validate_config()

    def _reload_locked(self) -> bool:
        """持锁加载文件，替换 config 引用。解析失败时保留旧配置，返回 False。"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                new_config: dict[str, Any] = json.load(f)
            self._mtime = self._path.stat().st_mtime
            self._config = new_config
            log.info("config.reload.ok mtime=%s supplies=%d routes=%d strategies=%d",
                     self._mtime,
                     len(new_config.get("supplies", [])),
                     len(new_config.get("routes", [])),
                     len(new_config.get("strategies", [])))
            try:
                self._validate_config()
            except Exception as e:
                log.warning("config.validate raised (ignored, load still ok): %s", e)
            return True
        except (json.JSONDecodeError, OSError) as e:
            log.warning("config reload failed, keeping old config: %s", e)
            return False

    def _validate_config(self) -> None:
        """OPT-06：配置校验（告警 ≠ 拒绝加载）。

        在 _reload / _reload_locked 成功后各跑一次，替代热路径 extract_route_candidates
        内的每请求重复告警。校验项：
        - route_id 与 route_pool 互斥：每个 strategy 最多告警一次
        - route_pool 非法项：每个 strategy 最多告警一次（聚合非法 route_id 列表）
        - cooldown_rules 策略组格式校验（容错：非法策略组跳过 + log warning）
        容错语义：校验只告警，不影响加载成功（_reload_locked 仍返回 True）。
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

    def _validate_cooldown_rules(self, rules: list, ctx: str) -> None:
        """校验 cooldown_rules 策略组格式（容错：非法策略组跳过 + log warning，不阻断加载）。

        每条 rule 必须有 errorcode（非空 list，元素为 int 或 "URLError" 字符串）
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
                if not isinstance(c, int) and c != "URLError":
                    log.warning("config.validate: %s cooldown_rules[%d] errorcode has invalid "
                                "element %r (must be int or 'URLError'), skipped",
                                ctx, i, c)
            cs_val = rule.get("cooldown_seconds")
            if not isinstance(cs_val, int) or cs_val <= 0:
                log.warning("config.validate: %s cooldown_rules[%d] cooldown_seconds missing/invalid, skipped",
                            ctx, i)


# ---------------------------------------------------------------------------
# L1 状态：CooldownStore（全新，错误信号驱动，纯内存，不写盘）
# ---------------------------------------------------------------------------

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
                  tried_set: set, excluded_set: set | None = None) -> dict | None:
    """从 supplies 列表有序取第一个「未冷却且未试过」的 supply。

    跳过 cooling 的、tried_set 里已试的、以及 supply_map 中不存在的 id。
    返回 supply dict（非 id），无可用则 None。
    """
    excluded_set = excluded_set or set()
    for sid in supplies:
        if sid in tried_set or sid in excluded_set:
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


def pick_translator(source: str, target: str) -> str:
    """组合分发决策表。

    (anthropic,anthropic)|(responses,responses) → PASSTHROUGH
    (anthropic,chat) → ANTHROPIC_TO_CHAT；(responses,anthropic) → RESPONSES_TO_ANTHROPIC；
    (anthropic,responses) → ANTHROPIC_TO_RESPONSES；其余 → UNSUPPORTED
    """
    return _TRANSLATOR_TABLE.get((source, target), UNSUPPORTED)


@dataclass
class StreamProbeResult:
    ok: bool
    mode: str
    source: str
    raw_prefix: bytes = b""
    encoded_prefix: bytes = b""
    framer: Any = None
    consumer: Any = None
    error: "pt.TranslationError | None" = None
    bytes_read: int = 0
    first_event_ms: int | None = None


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

    def do_GET(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
            if self.path.startswith(_CONTROL_PATH_PREFIX):
                self._dispatch_control("GET")
            else:
                self._forward_logged("GET")
        finally:
            _req_local.req_id = None

    def do_POST(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
            if self.path.startswith(_CONTROL_PATH_PREFIX):
                self._dispatch_control("POST")
            else:
                self._forward_logged("POST")
        finally:
            _req_local.req_id = None

    def do_PUT(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
            self._forward_logged("PUT")
        finally:
            _req_local.req_id = None

    def do_DELETE(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
            self._forward_logged("DELETE")
        finally:
            _req_local.req_id = None

    def do_PATCH(self):
        _req_local.req_id = uuid.uuid4().hex[:8]
        try:
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
            # OPT-10: attempt_errors 记 failover 失败明细（supply_id, reason），
            # 仅 2 处 failover continue 前 append（不含 budget 重试的 4 处 continue）。
            "attempt_errors": [],
            # nudge 改写观测：入站 body 命中已知 SDK 注入文案改写时置 "1"
            # （含 $route 等未转发请求——命中即置位，属预期）。
            "nudge_rewritten": "",
            "response_committed": 0, "stream_integrity": "",
            "terminal_status": "", "terminal_reason": "", "first_event_ms": "",
        }
        t0 = time.monotonic()
        try:
            self._forward(method)
        finally:
            a = self._acc
            ms = int((time.monotonic() - t0) * 1000)
            access_log.info(
                "ACCESS ms=%d status=%s source=%s operation=%s route=%s tier=%s supply=%s "
                "target_protocol=%s conversion_kind=%s failover=%s attempts=%s "
                "usage_in=%s usage_out=%s token=%s session=%s "
                "route_failover=%s builtin=%s budget_retried=%s budget_truncated=%s "
                "stop_reason=%s final_error=%s nudge_rewritten=%s response_committed=%s "
                "stream_integrity=%s terminal_status=%s terminal_reason=%s first_event_ms=%s",
                ms, a["status"], a["source"], a["operation"],
                a["route"], a["tier"], a["supply"],
                a["target_protocol"], a["conversion_kind"], a["failover"], a["attempts"],
                a["usage_in"], a["usage_out"], a["token"], a["session"],
                a["route_failover"], a["builtin"], a["budget_retried"],
                a["budget_truncated"], a["stop_reason"],
                re.sub(r"\s+", "_", a["final_error"][:80]),
                a["nudge_rewritten"], a["response_committed"], a["stream_integrity"],
                a["terminal_status"], a["terminal_reason"], a["first_event_ms"])
            if usage_totals is not None:
                try:
                    usage_totals.record(a, ms)
                except Exception:
                    log.warning("usage_totals.record failed", exc_info=True)

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
            send_body = json.dumps(outgoing, ensure_ascii=False).encode("utf-8")
            appkey = supply.get("appkey", "")
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in {"host", "content-length", "authorization", "x-api-key"}}
            headers.update({"Authorization": f"Bearer {appkey}", "x-api-key": appkey,
                            "Content-Type": "application/json", "Content-Length": str(len(send_body))})
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
        self._acc["session"] = extract_session_key(body_json) or ""

        # 4. source 协议识别
        source = detect_source(self.path, body_json)
        self._acc["source"] = source
        operation = detect_operation(self.path)
        self._acc["operation"] = operation

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
        if (operation == OP_MESSAGES and source == "anthropic" and session_key and strategy is not None
                and isinstance(body_json, dict) and isinstance(body_json.get("messages"), list)):
            last_user_content = extract_last_user_message_content(body_json)
            is_cmd, cmd_arg = ((False, None) if last_user_content is None
                                else parse_route_command(last_user_content))
            if is_cmd:
                self._handle_builtin_command(cmd_arg, token, session_key, strategy,
                                              routes_map, sidecar, request_model, body_json)
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
        stream_failed_supply_ids: set[str] = set()
        last_retryable_error: dict | None = None
        saw_stream_timeout = False

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
                supply = select_supply(supplies_list, supply_map, cd, tried_set,
                                       stream_failed_supply_ids)
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
                        responses_body, fwd_ctx = pt.anthropic_to_responses_request(
                            body_json or {}, reasoning_fields=reasoning_wire)
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
                    send_body = json.dumps(anthropic_body, ensure_ascii=False).encode("utf-8")
                    # target_url 已在分支前统一算好（supply.url 现在已是完整 /v1/messages 端点）

                # ---- 注入出站 appkey（Authorization Bearer + x-api-key）----
                appkey = supply.get("appkey", "")
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
                    last_retryable_error = {"kind": "http", "reason": f"http_{resp_status}",
                                            "http_status": resp_status, "supply_id": supply_id}
                    resp_headers = list(e.headers.items())
                    resp_body = e.read()

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
                    last_retryable_error = {"kind": "network", "reason": "network_error",
                                            "http_status": 502, "supply_id": supply_id}
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

                # 四条流路径统一首事件预读；成功后才提交，失败可换 supply。
                if is_stream:
                    if mode == PASSTHROUGH:
                        adapter = None
                    elif mode == ANTHROPIC_TO_CHAT:
                        adapter = pt.OpenAIToAnthropicStreamAdapter(fwd_ctx, target_model or "")
                    elif mode == ANTHROPIC_TO_RESPONSES:
                        adapter = pt.ResponsesToAnthropicStreamAdapter(fwd_ctx, target_model or "")
                    else:
                        _r_effort = ((body_json or {}).get("reasoning") or {}).get("effort")
                        _tools_echo = (body_json or {}).get("tools") or []
                        adapter = pt.AnthropicToResponsesStreamAdapter(
                            model=target_model or "",
                            ctx={"tools": _tools_echo, "reasoning_effort": _r_effort})
                    probe = self._probe_upstream_stream(
                        resp, mode, source, adapter, cs.get_upstream_timeout())
                    if not probe.ok:
                        resp.close()
                        err = probe.error or pt.TranslationError("stream probe failed")
                        self._acc["first_event_ms"] = probe.first_event_ms
                        stream_failed_supply_ids.add(supply_id)
                        tried_set.add(supply_id)
                        self._acc["attempt_errors"].append((supply_id, f"stream_{err.reason}"))
                        last_retryable_error = {"kind": "stream", "reason": err.reason,
                                                "http_status": err.http_status,
                                                "supply_id": supply_id}
                        saw_stream_timeout = saw_stream_timeout or err.reason == "first_event_timeout"
                        if failover == "on":
                            self._acc["failover"] = 1
                            continue
                        self._acc["final_error"] = err.reason
                        self._write_buffered_response(
                            err.http_status, [], error_body_for_source(source, err.http_status, str(err)))
                        return
                    self._commit_and_write_probed_stream(
                        probe, resp, list(resp.getheaders()), cs.get_upstream_timeout())
                    adapter = probe.consumer
                    if mode != PASSTHROUGH:
                        self._acc["usage_in"], self._acc["usage_out"], _ = adapter.usage_tuple()
                        self._acc["stop_reason"] = getattr(adapter, "final_stop_reason", "") or ""
                    if (mode == PASSTHROUGH
                            and self._acc.get("stop_reason") in (
                                "max_tokens", "incomplete:max_output_tokens")
                            and not self._acc.get("stream_content")):
                        self._acc["budget_truncated"] = 1
                    return

                # ---- 按 mode 分派写回 ----
                if mode == PASSTHROUGH:
                    # 流式已由统一 probe 分支提前返回；此处仅处理非流式透传。
                    resp_body = resp.read()
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
                        # ④b 流式不重试（字节已下发），仅收口检测记日志：
                        # produced_content_block 不计 reasoning 累积/兜底填充，语义即
                        # 「真实正文是否产出」，与 is_budget_truncated 的「正文缺失」同义
                        if (adapter.final_stop_reason == "max_tokens"
                                and not adapter.produced_content_block):
                            self._acc["budget_truncated"] = 1
                            log.warning(
                                "budget_truncated(stream,不重试): supply=%s", supply_id)
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
                if saw_stream_timeout:
                    final_status = 504
                elif last_retryable_error and last_retryable_error.get("kind") == "stream":
                    final_status = 502
                else:
                    # 纯 HTTP/网络失败保持既有全耗尽 503 语义。
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
                                 routes_map, sidecar, request_model, body_json) -> None:
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
        if is_stream:
            self._write_builtin_stream_response(result.receipt_text, request_model)
        else:
            self._write_builtin_buffered_response(result.receipt_text, request_model)

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
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            "unconfigured_codes": _snapshot_unconfigured_hits(),
            "protocol_conversion_hints": build_protocol_conversion_hints({
                "supplies": supplies, "routes": cs.get_routes(),
            }),
            "version": _VERSION,
        })

    def _handle_reload(self, cs: "ConfigStore", cd: "CooldownStore"):
        """手动 reload：强制重载配置 + 无条件清空所有 supply 的冷却。

        与 mtime 驱动的自动 maybe_reload()（每请求经 _forward 调用）的关键区别：
        手动 reload 是运维显式动作（改配置后确认生效），清空 cooldown 是合理的用户预期；
        自动 reload 只是发现文件变了顺手换配置，不应该悄悄影响运行中的冷却状态。
        """
        cs.reload()
        cleared = len(cd.snapshot())
        cd.clear_all()
        log.info("admin.reload cleared_cooldowns=%d", cleared)
        self._send_json(200, {"ok": True})

    # ------------------------------------------------------------------
    # 写回（拷贝 proxy.py：流式 chunked 透传 / 缓冲响应）
    # ------------------------------------------------------------------

    _SKIP_RESP_HEADERS = {"transfer-encoding", "content-length"}
    _STREAM_PROBE_MAX_BYTES = 262144

    @staticmethod
    def _set_upstream_read_timeout(resp, timeout: float) -> None:
        """集中封装 urllib HTTPResponse 的底层 socket timeout 切换。"""
        fp = getattr(resp, "fp", None)
        raw = getattr(fp, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(timeout)

    def _probe_upstream_stream(self, resp, mode: str, source: str, adapter=None,
                               upstream_timeout: float = 1800) -> StreamProbeResult:
        """预读到首个合法业务 SSE 事件；失败时尚未提交客户端响应。

        首事件超时对齐 upstream_timeout（单一超时真相源），慢热上游不再被 30s 截断。
        """
        started = time.monotonic()
        framer = pt.SSEFramer()
        raw = bytearray()
        encoded = bytearray()
        consumer = (pt.PassthroughTerminalTracker(source, self._acc)
                    if mode == PASSTHROUGH else adapter)
        self._set_upstream_read_timeout(resp, upstream_timeout)
        try:
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= upstream_timeout:
                    raise pt.TranslationError("first stream event timed out",
                                              reason="first_event_timeout", http_status=504,
                                              retry_class="configured")
                self._set_upstream_read_timeout(resp, upstream_timeout - elapsed)
                try:
                    chunk = resp.read(4096)
                except TimeoutError as exc:
                    raise pt.TranslationError("first stream event timed out",
                                              reason="first_event_timeout", http_status=504,
                                              retry_class="configured") from exc
                except OSError as exc:
                    raise pt.TranslationError(f"stream probe network error: {exc}",
                                              reason="network_error", http_status=502,
                                              retry_class="configured") from exc
                if not chunk:
                    tail_events = framer.finish()
                    business = False
                    for event in tail_events:
                        if not event.is_comment:
                            business = True
                            self._probe_consume_event(mode, event, consumer, encoded)
                    if mode == PASSTHROUGH:
                        consumer.finalize()
                    else:
                        consumer.finalize()
                        if self._adapter_failed(consumer):
                            raise pt.TranslationError("stream ended without terminal",
                                                      reason="unexpected_eof")
                    if not business:
                        raise pt.TranslationError("empty stream", reason="empty_stream")
                    ms = int((time.monotonic() - started) * 1000)
                    return StreamProbeResult(True, mode, source, bytes(raw), bytes(encoded),
                                             framer, consumer, bytes_read=len(raw), first_event_ms=ms)
                raw.extend(chunk)
                events = framer.feed(chunk, max_events=1)
                if len(raw) + len(encoded) > self._STREAM_PROBE_MAX_BYTES:
                    raise pt.TranslationError("stream probe buffer exceeded",
                                              reason="frame_too_large")
                while events:
                    event = events[0]
                    if not event.is_comment:
                        self._probe_consume_event(mode, event, consumer, encoded)
                        if (mode == PASSTHROUGH and consumer.confirmed
                                and consumer.terminal.status == pt.TerminalStatus.FAILED) \
                                or (mode != PASSTHROUGH and self._adapter_failed(consumer)):
                            raise pt.TranslationError("upstream stream failed", reason="upstream_error")
                        self._set_upstream_read_timeout(resp, upstream_timeout)
                        ms = int((time.monotonic() - started) * 1000)
                        return StreamProbeResult(True, mode, source, bytes(raw), bytes(encoded),
                                                 framer, consumer, bytes_read=len(raw), first_event_ms=ms)
                    events = framer.feed(b"", max_events=1)
        except pt.TranslationError as exc:
            return StreamProbeResult(False, mode, source, bytes(raw), bytes(encoded),
                                     framer, consumer, exc, len(raw),
                                     int((time.monotonic() - started) * 1000))

    @staticmethod
    def _probe_consume_event(mode: str, event, consumer, encoded: bytearray) -> None:
        if mode == PASSTHROUGH:
            consumer.feed(event)
            return
        if event.is_done:
            outputs = consumer.finalize()
        elif mode == ANTHROPIC_TO_CHAT:
            outputs = consumer.feed(event.data)
        elif mode == ANTHROPIC_TO_RESPONSES:
            outputs = consumer.feed(event.event_type, event.data)
        else:  # RESPONSES_TO_ANTHROPIC
            outputs = consumer.feed(event.event_type, event.data)
        serializer = (pt.responses_sse_bytes if mode == RESPONSES_TO_ANTHROPIC
                      else pt.anthropic_sse_bytes)
        for output in outputs:
            encoded.extend(serializer(output))

    def _commit_and_write_probed_stream(self, result: StreamProbeResult, resp,
                                        headers: list[tuple[str, str]], upstream_timeout: float) -> None:
        self._acc["first_event_ms"] = result.first_event_ms
        if result.mode == PASSTHROUGH:
            self._write_streaming_response(200, headers, resp, result.source,
                                           prefix=result.raw_prefix,
                                           framer=result.framer, tracker=result.consumer)
            return
        self._begin_sse_chunked()
        self._acc["response_committed"] = 1
        try:
            if result.encoded_prefix:
                self._write_sse_chunk(result.encoded_prefix)
            done = False
            for event in result.framer.feed(b""):
                if event.is_comment:
                    continue
                if event.is_done:
                    outputs = result.consumer.finalize()
                    done = True
                else:
                    outputs = self._adapter_outputs(result.mode, result.consumer, event)
                for output in outputs:
                    self._write_sse_chunk(self._serialize_converted(result.mode, output))
                if done:
                    break
            while not done:
                chunk = resp.read(4096)
                if not chunk:
                    break
                for event in result.framer.feed(chunk):
                    if event.is_comment:
                        continue
                    if event.is_done:
                        outputs = result.consumer.finalize()
                        done = True
                    else:
                        outputs = self._adapter_outputs(result.mode, result.consumer, event)
                    for output in outputs:
                        self._write_sse_chunk(self._serialize_converted(result.mode, output))
                    if done:
                        break
            if not done:
                for event in result.framer.finish():
                    if not event.is_comment:
                        for output in self._adapter_outputs(result.mode, result.consumer, event):
                            self._write_sse_chunk(self._serialize_converted(result.mode, output))
                for output in result.consumer.finalize():
                    self._write_sse_chunk(self._serialize_converted(result.mode, output))
            terminal = self._adapter_terminal_state(result.consumer)
            self._record_stream_terminal(terminal)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._acc["stream_integrity"] = "client_disconnect"
        except Exception as exc:
            if self._adapter_has_terminal(result.consumer):
                terminal = self._adapter_terminal_state(result.consumer)
                self._record_stream_terminal(terminal)
            else:
                err = exc if isinstance(exc, pt.TranslationError) else pt.TranslationError(
                    f"stream interrupted: {exc}", reason="unexpected_eof")
                self._record_stream_error(err)
                try:
                    self._write_sse_chunk(stream_error_event_for_source(result.source, err))
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        finally:
            resp.close()

    @staticmethod
    def _adapter_outputs(mode: str, adapter, event) -> list:
        if mode == ANTHROPIC_TO_CHAT:
            return adapter.feed(event.data)
        return adapter.feed(event.event_type, event.data)

    @staticmethod
    def _serialize_converted(mode: str, output: dict) -> bytes:
        return (pt.responses_sse_bytes(output) if mode == RESPONSES_TO_ANTHROPIC
                else pt.anthropic_sse_bytes(output))

    @staticmethod
    def _adapter_failed(adapter) -> bool:
        return bool(getattr(adapter, "_failed", False) or getattr(adapter, "failed", False))

    @staticmethod
    def _adapter_has_terminal(adapter) -> bool:
        return bool(getattr(adapter, "_finalized", False) or getattr(adapter, "_failed", False)
                    or getattr(adapter, "completed", False) or getattr(adapter, "failed", False)
                    or getattr(adapter, "_completed", False))

    @staticmethod
    def _adapter_terminal_state(adapter) -> "pt.TerminalState":
        state = getattr(adapter, "terminal", None) or getattr(adapter, "_terminal", None)
        if isinstance(state, pt.TerminalState):
            return state
        reason = (getattr(adapter, "final_stop_reason", None) or
                  ("upstream_error" if (getattr(adapter, "_failed", False)
                                        or getattr(adapter, "failed", False)) else "end_turn"))
        try:
            if reason in ("end_turn", "stop_sequence", "tool_use", "max_tokens", "refusal", "pause_turn"):
                return pt.map_anthropic_terminal(reason)
        except pt.TranslationError:
            pass
        status = pt.TerminalStatus.FAILED if "error" in reason else pt.TerminalStatus.COMPLETED
        return pt.TerminalState(status, reason)

    def _write_streaming_response(self, status: int, headers: list[tuple[str, str]], resp,
                                  source: str = "", *, prefix: bytes = b"",
                                  framer=None, tracker=None) -> None:
        """PASSTHROUGH 原始字节回放；旁路严格跟踪协议终态。"""
        if hasattr(self, "_acc"):
            self._acc["status"] = status
            self._acc["response_committed"] = 1
        self.send_response(status)
        for hname, hval in headers:
            if hname.lower() not in self._SKIP_RESP_HEADERS:
                self.send_header(hname, hval)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        framer = framer or (pt.SSEFramer() if source else None)
        tracker = tracker or (pt.PassthroughTerminalTracker(
            source, getattr(self, "_acc", {})) if source else None)
        integrity_error = None
        upstream_read_error = None
        try:
            if prefix:
                self._write_sse_chunk(prefix)
            # probe 可能在一个 read 中读到多个事件，只消费了首事件；续写先排空 framer。
            if framer is not None:
                for event in framer.feed(b""):
                    tracker.feed(event)
            while True:
                try:
                    chunk = resp.read(8192)
                except OSError as exc:
                    upstream_read_error = exc
                    break
                if not chunk:
                    break
                self._write_sse_chunk(chunk)
                if integrity_error is None and framer is not None:
                    try:
                        for event in framer.feed(chunk):
                            tracker.feed(event)
                    except pt.TranslationError as exc:
                        integrity_error = exc
            if upstream_read_error is not None:
                if tracker is not None and tracker.confirmed:
                    self._record_stream_terminal(tracker.terminal)
                else:
                    integrity_error = pt.TranslationError(
                        f"stream interrupted: {upstream_read_error}", reason="unexpected_eof")
            elif integrity_error is None and framer is not None:
                try:
                    for event in framer.finish():
                        tracker.feed(event)
                    terminal = tracker.finalize()
                    self._record_stream_terminal(terminal)
                except pt.TranslationError as exc:
                    integrity_error = exc
            if integrity_error is not None:
                self._record_stream_error(integrity_error)
                self._write_sse_chunk(stream_error_event_for_source(source, integrity_error))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 上游 read 异常已在 read 点捕获；这里只可能来自客户端写。
            if hasattr(self, "_acc"):
                self._acc["stream_integrity"] = "client_disconnect"
        finally:
            resp.close()

    def _record_stream_terminal(self, terminal: "pt.TerminalState") -> None:
        if hasattr(self, "_acc"):
            self._acc["stream_integrity"] = "valid"
            self._acc["terminal_status"] = terminal.status.value
            self._acc["terminal_reason"] = terminal.reason

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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_sse_chunk(self, data: bytes) -> None:
        """把一段已序列化的 SSE 字节按 HTTP chunked 编码写出并 flush。"""
        if not data:
            return
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _write_translated_stream(self, upstream_resp, adapter) -> None:
        """正向流式（组合3）：上游 OpenAI chat SSE → Anthropic SSE。

        上游为 chat completions SSE：每事件一行 `data: {json}`，以 `data: [DONE]` 收尾。
        逐行解析 → adapter.feed(chunk) → 每个 Anthropic 事件经 pt.anthropic_sse_bytes 写出。
        """
        self._begin_sse_chunked()
        buf = b""
        done = False
        try:
            while not done:
                data = upstream_resp.read(4096)
                if not data:
                    break
                buf += data
                # 按行切分（chat SSE 单行 data:，事件间以空行分隔）
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.rstrip(b"\r").strip()
                    if not line:
                        continue
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[len(b"data:"):].lstrip()
                    if payload == b"[DONE]":
                        done = True
                        break
                    try:
                        chunk = json.loads(payload)
                    except (json.JSONDecodeError, ValueError) as e:
                        raise pt.TranslationError(
                            f"malformed Chat SSE frame: {e}", source_type="chat",
                            reason="malformed_stream") from e
                    for ev in adapter.feed(chunk):
                        self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            # 流结束：收尾（[DONE] 或上游断流都走 finalize，幂等）
            for ev in adapter.finalize():
                self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端断连不是上游 EOF：不 finalize、不制造失败终态、不触发 failover。
            pass
        except Exception as e:
            log.error("ANTHROPIC_TO_CHAT stream interrupted: %s", e)
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

        上游 Anthropic SSE 为 `event:xxx\\ndata:{json}`（冒号后无空格），事件间空行分隔。
        按 \\n\\n 切块，块内解析出 (event_type, data) → adapter.feed → pt.responses_sse_bytes 写出。
        message_stop 由 adapter.feed 内部触发 response.completed；断流则 finalize 补收尾。
        """
        self._begin_sse_chunked()
        buf = b""
        try:
            while True:
                data = upstream_resp.read(4096)
                if not data:
                    break
                buf += data
                # 按事件块（空行分隔）切分
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    ev_type, ev_data = self._parse_anthropic_sse_block(block)
                    if ev_type is None and ev_data is None:
                        continue  # SSE 注释/keep-alive 帧
                    if ev_type is None:
                        raise pt.TranslationError("malformed SSE frame", reason="malformed_stream")
                    for ev in adapter.feed(ev_type, ev_data):
                        self._write_sse_chunk(pt.responses_sse_bytes(ev))
            # 处理 buffer 残余块（末尾可能无空行）
            if buf.strip():
                ev_type, ev_data = self._parse_anthropic_sse_block(buf)
                if ev_type is None and ev_data is None:
                    pass  # SSE 注释/keep-alive 帧
                elif ev_type is None:
                    raise pt.TranslationError("malformed SSE frame", reason="malformed_stream")
                else:
                    for ev in adapter.feed(ev_type, ev_data):
                        self._write_sse_chunk(pt.responses_sse_bytes(ev))
            # 流意外结束（无 message_stop）时补收尾，幂等
            for ev in adapter.finalize():
                self._write_sse_chunk(pt.responses_sse_bytes(ev))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端断连不是上游 EOF。
            pass
        except Exception as e:
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

        上游 Responses SSE 为单行 `data:{json}`（无 event: 行、无 [DONE] 哨兵），
        event_type 从 data.type 取（_parse_anthropic_sse_block 的 event: 行缺失兜底逻辑
        自然覆盖此场景）。按 \\n\\n 切块 → adapter.feed(event_type, data) →
        pt.anthropic_sse_bytes 写出。断流则 finalize 补收尾。
        中断时补 `event: error`（输出侧已是 Anthropic 格式，不用 response.failed）。
        """
        self._begin_sse_chunked()
        buf = b""
        try:
            while True:
                data = upstream_resp.read(4096)
                if not data:
                    break
                buf += data
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    ev_type, ev_data = self._parse_anthropic_sse_block(block)
                    if ev_type is None and ev_data is None:
                        continue  # SSE 注释/keep-alive 帧
                    if ev_type is None:
                        raise pt.TranslationError("malformed SSE frame", reason="malformed_stream")
                    for ev in adapter.feed(ev_type, ev_data):
                        self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            # 处理 buffer 残余块（末尾可能无空行）
            if buf.strip():
                ev_type, ev_data = self._parse_anthropic_sse_block(buf)
                if ev_type is None and ev_data is None:
                    pass  # SSE 注释/keep-alive 帧
                elif ev_type is None:
                    raise pt.TranslationError("malformed SSE frame", reason="malformed_stream")
                else:
                    for ev in adapter.feed(ev_type, ev_data):
                        self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            # 流意外结束（无 response.completed）时补收尾，幂等
            for ev in adapter.finalize():
                self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端断连不是上游 EOF：不 finalize、不制造失败终态、不触发 failover。
            pass
        except Exception as e:
            log.error("ANTHROPIC_TO_RESPONSES stream interrupted: %s", e)
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

    def _sniff_passthrough_usage(self, block: bytes, source: str) -> None:
        """PASSTHROUGH 流式旁路：从一个完整 SSE 块里嗅探 usage，覆盖式写入 self._acc。
        字节预筛跳过绝大多数无关块，只对目标块做 json 解析。异常由调用方 try 兜住。

        ④b/⑤ 顺带嗅探（同为旁路，不影响转发字节）：
        - anthropic：message_delta 的 delta.stop_reason → _acc["stop_reason"]；
          content_block_start 的 text/tool_use 块 → _acc["stream_content"]=1（正文出现标记）。
        - responses：response.completed/response.incomplete 的 status（incomplete 带
          reason，形如 "incomplete:max_output_tokens"）→ _acc["stop_reason"]；
          output_item.added 的 message/function_call 项 → _acc["stream_content"]=1。
        供流式收口检测「budget 截断且正文缺失」记日志用（流式不重试）。
        """
        if source == "anthropic":
            if b"message_delta" in block:               # 字节预筛
                ev_type, data = self._parse_anthropic_sse_block(block)
                if ev_type != "message_delta" or not isinstance(data, dict):
                    return
                u = data.get("usage") or {}
                if u.get("output_tokens") is not None:
                    self._acc["usage_out"] = u.get("output_tokens") or 0
                if u.get("input_tokens") is not None:
                    self._acc["usage_in"] = u.get("input_tokens") or 0
                _sr = (data.get("delta") or {}).get("stop_reason")
                if _sr:
                    self._acc["stop_reason"] = _sr
            elif b"content_block_start" in block:       # 字节预筛
                ev_type, data = self._parse_anthropic_sse_block(block)
                if ev_type != "content_block_start" or not isinstance(data, dict):
                    return
                if (data.get("content_block") or {}).get("type") in ("text", "tool_use"):
                    self._acc["stream_content"] = 1
        elif source == "responses":
            if b"response.completed" in block or b"response.incomplete" in block:
                ev_type, data = self._parse_anthropic_sse_block(block)
                if ev_type not in ("response.completed", "response.incomplete") \
                        or not isinstance(data, dict):
                    return
                _resp = data.get("response") or {}
                u = _resp.get("usage") or {}
                if u.get("input_tokens") is not None:
                    self._acc["usage_in"] = u.get("input_tokens") or 0
                if u.get("output_tokens") is not None:
                    self._acc["usage_out"] = u.get("output_tokens") or 0
                _st = _resp.get("status")
                if _st:
                    _reason = (_resp.get("incomplete_details") or {}).get("reason")
                    self._acc["stop_reason"] = f"{_st}:{_reason}" if _reason else str(_st)
            elif b"output_item.added" in block:         # 字节预筛
                ev_type, data = self._parse_anthropic_sse_block(block)
                if not isinstance(data, dict):
                    return
                if (data.get("item") or {}).get("type") in ("message", "function_call"):
                    self._acc["stream_content"] = 1

    @staticmethod
    def _parse_anthropic_sse_block(block: bytes):
        """解析一个 Anthropic SSE 事件块 → (event_type, data_dict)。

        块形如 `event:TYPE\\ndata:{json}`（冒号后可能无空格）。
        无法解析返回 (None, None)。event: 行缺失时以 data.type 兜底。
        """
        ev_type = None
        payload = None
        for raw in block.split(b"\n"):
            raw = raw.rstrip(b"\r").strip()
            if not raw:
                continue
            if raw.startswith(b"event:"):
                ev_type = raw[len(b"event:"):].lstrip().decode("utf-8", "replace")
            elif raw.startswith(b"data:"):
                payload = raw[len(b"data:"):].lstrip()
        if payload is None:
            return None, None
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None, {}  # 有 data 但 JSON 非法；区别于合法注释帧 (None, None)
        if ev_type is None and isinstance(data, dict):
            ev_type = data.get("type")
        return ev_type, data

    # ------------------------------------------------------------------
    # helper
    # ------------------------------------------------------------------

    def _send_json(self, status: int, body: Any):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    config_path = _DEFAULT_CONFIG_PATH
    port = int(os.environ.get("MODEL_PROXY_PORT", "18889"))

    # 0. 解析运行时路径（bootstrap，不依赖 ConfigStore；文件缺失/corrupt 回退默认值）
    global _runtime_paths
    _runtime_paths = resolve_runtime_paths()

    # 0.1 读 VERSION 文件覆盖模块级默认值（文件不存在保持 "unknown"，不阻断启动）
    global _VERSION
    try:
        _VERSION = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass  # 保持 "unknown"

    # 1. 装配日志 handler + 截断日志（S1：从模块级挪到启动路径，
    # 避免测试 import 时触碰生产日志文件）
    init_logging(_runtime_paths["log"])

    # 2. 进程级互斥锁：同一时刻只允许一个 model_proxy.py 实例运行
    # （flock 提前到 UsageTotalsStore 之前，B10 确认是改善）
    import fcntl
    lock_path = _runtime_paths["lock"]
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        existing_pid = lock_path.read_text().strip() if lock_path.exists() else "unknown"
        log.warning("startup.lock_conflict existing_pid=%s", existing_pid)
        lock_fd.close()
        raise SystemExit(1)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()

    # 3. 实例化账本（S1：从模块级挪到启动路径，避免测试 import 时触碰账本文件）
    global usage_totals
    usage_totals = UsageTotalsStore(_runtime_paths["totals"])

    # 4. 实例化 ConfigStore
    config_store = ConfigStore(config_path, on_reload=None)

    # 2. 实例化 CooldownStore
    cooldown_store = CooldownStore()

    # 2.5 实例化 SyntaxPreferenceStore（reasoning 语法偏好，取代旧 _THINKING_FMT_CACHE）
    pref_store = SyntaxPreferenceStore()

    # 2.6 实例化 SessionOverridesSidecar（$route in-band 指令的 override 落盘，
    # 与主 config 同目录，代理独占写，见 docs/designs/2026-08-04-in-band-route-command-design.md §4.5）
    sidecar_store = SessionOverridesSidecar(config_path.parent / "session_overrides.json")

    # 3. 启动 ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", port), ModelProxyHandler)
    server.config_store = config_store    # type: ignore[attr-defined]
    server.cooldown_store = cooldown_store  # type: ignore[attr-defined]
    server.pref_store = pref_store        # type: ignore[attr-defined]
    server.sidecar_store = sidecar_store  # type: ignore[attr-defined]

    log.info("startup.listening port=%d pid=%d config_path=%s",
             port, os.getpid(), str(config_path))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown.signal KeyboardInterrupt")
    finally:
        server.shutdown()
        server.server_close()
        log.info("shutdown.complete")


if __name__ == "__main__":
    main()
