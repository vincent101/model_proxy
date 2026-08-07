"""
tools/model_proxy/core/server.py — 本地多协议路由代理主体

多协议 AI 模型代理主程序：HTTP server、路由决策、转发编排、协议转换、控制 API。
入口为 tools/model_proxy/model_proxy.py（thin wrapper 调用本模块 main()）。
与线上 proxy.py（18888）完全隔离并行：新端口 18889、新配置
tools/model_proxy/config/model_proxy_config.json（可用 MODEL_PROXY_CONFIG 环境变量覆盖）、
新进程锁 /tmp/claude_model_proxy.lock、新日志 tools/model_proxy/.claude_model_proxy.log。

仅使用 Python 标准库，不引入第三方依赖，也不 import proxy.py。
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
from .reasoning.capability import ModelReasoningCapability, abstract_encode, remap
from .reasoning.ladder import CanonicalEffort
from .reasoning.registry import apply_fields, get_codec, resolve_protocol

# ---------------------------------------------------------------------------
# L0 基座
# ---------------------------------------------------------------------------

# 日志固定落在包父目录 tools/model_proxy/（与 model_proxy_cli.sh 的 LOG_FILE 一致）
LOG_FILE = Path(__file__).resolve().parent.parent / ".claude_model_proxy.log"


def _trim_log(path: Path, keep: int = 5000) -> None:
    """启动时截断日志，只保留最后 keep 行。"""
    try:
        if not path.exists():
            return
        lines = path.read_bytes().splitlines(keepends=True)
        if len(lines) > keep:
            path.write_bytes(b"".join(lines[-keep:]))
    except OSError:
        pass


_trim_log(LOG_FILE)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger(__name__)

# access logger：单独 INFO 级别，复用同一日志文件、不向 root 传播（root 仍 WARNING，
# 避免误收其他 INFO 噪声）。固定前缀 ACCESS，key=value 单行文本，与现有 WARNING 行
# 风格一致，grep/awk 友好（见 docs/designs/2026-07-22-access-log-and-latency.md）。
_access_handler = logging.FileHandler(LOG_FILE)
_access_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
access_log = logging.getLogger("model_proxy.access")
access_log.setLevel(logging.INFO)
access_log.addHandler(_access_handler)
access_log.propagate = False

# ---------------------------------------------------------------------------
# 累计用量账本：独立于 access 日志文件，只增不截、不受 _trim_log 影响。
# 按天分桶 + supply×route×strategy 组合键，见
# docs/designs/2026-07-23-usage-totals-ledger.md
# ---------------------------------------------------------------------------

_CST = timezone(timedelta(hours=8))          # UTC+8，中国标准时间，固定偏移


def _cst_now() -> datetime:
    """显式带时区的当前时间，绝不用 naive datetime.now()。"""
    return datetime.now(_CST)


TOTALS_FILE = Path(__file__).resolve().parent.parent / ".claude_model_proxy_totals.json"
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
    """天桶/月归档/total 顶层的零值结构。"""
    return {"requests": 0, "ok": 0, "fail": 0, "sum_ms": 0, "combos": {}}


def _zero_combo() -> dict:
    """combos 单条目的零值结构（不存 sum_ms，见方案 §1）。"""
    return {"requests": 0, "ok": 0, "fail": 0, "usage_in": 0, "usage_out": 0}


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
                "version": 2,
                "since": _cst_now().strftime("%Y-%m-%d"),
                "keep_days": KEEP_DAYS,
                "total": _zero_bucket(),
                "months_archive": {},
                "days": {},
            }
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("usage totals ledger corrupt, resetting: %s", e)
            try:
                ts = int(time.time())
                corrupt_path = self._path.with_name(self._path.name + f".corrupt.{ts}")
                os.replace(str(self._path), str(corrupt_path))
            except OSError:
                pass
            return {
                "version": 2,
                "since": _cst_now().strftime("%Y-%m-%d"),
                "keep_days": KEEP_DAYS,
                "total": _zero_bucket(),
                "months_archive": {},
                "days": {},
            }

    @staticmethod
    def _combo_key(acc: dict) -> str:
        return (
            f"supply={acc.get('supply') or '(none)'}"
            f"|route={acc.get('route') or '(none)'}"
            f"|strategy={acc.get('strategy') or '(none)'}"
        )

    def record(self, acc: dict, ms: int) -> None:
        """核心记账方法：锁内累加内存 dict + 归档检查 + 原子落盘。"""
        with self._lock:
            day_key = _cst_now().strftime("%Y-%m-%d")
            days = self._data.setdefault("days", {})
            day_bucket = days.setdefault(day_key, _zero_bucket())
            total_bucket = self._data.setdefault("total", _zero_bucket())

            combo_key = self._combo_key(acc)
            ok = 1 if acc.get("status") == 200 else 0
            fail = 0 if ok else 1
            usage_in = acc.get("usage_in", 0) or 0
            usage_out = acc.get("usage_out", 0) or 0

            for bucket in (day_bucket, total_bucket):
                bucket["requests"] += 1
                bucket["ok"] += ok
                bucket["fail"] += fail
                bucket["sum_ms"] += ms
                combo = bucket.setdefault("combos", {}).setdefault(combo_key, _zero_combo())
                combo["requests"] += 1
                combo["ok"] += ok
                combo["fail"] += fail
                combo["usage_in"] += usage_in
                combo["usage_out"] += usage_out

            self._archive_if_needed()
            _atomic_write_json(self._path, self._data)

    def _archive_if_needed(self) -> None:
        """days 超过 KEEP_DAYS 时，把最旧的天桶按组合键汇总进 months_archive 后删除。
        必须持锁调用（由 record 内已持锁的调用点触发）。
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
            month_bucket["sum_ms"] += oldest_bucket.get("sum_ms", 0)
            month_combos = month_bucket.setdefault("combos", {})
            for combo_key, combo_val in oldest_bucket.get("combos", {}).items():
                dest = month_combos.setdefault(combo_key, _zero_combo())
                dest["requests"] += combo_val.get("requests", 0)
                dest["ok"] += combo_val.get("ok", 0)
                dest["fail"] += combo_val.get("fail", 0)
                dest["usage_in"] += combo_val.get("usage_in", 0)
                dest["usage_out"] += combo_val.get("usage_out", 0)


usage_totals = UsageTotalsStore(TOTALS_FILE)

# ---------------------------------------------------------------------------
# reasoning debug 开关：默认关闭，不污染生产日志（沿用 MODEL_PROXY_CONFIG/
# MODEL_PROXY_PORT 的环境变量风格，进程启动时读取一次，不支持热切换）。
# 开启后把本模块 logger 的 effective level 调到 DEBUG（只影响 `log` 这一个具名
# logger，root 仍是 WARNING），调用点用 log.isEnabledFor(logging.DEBUG) 判断，
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
_LOCK_FILE = Path("/tmp/claude_model_proxy.lock")

# 控制路径前缀（v2，避免与 18888 的 /proxy 混淆）
_CONTROL_PATH_PREFIX = "/model_proxy"

# 顶层默认冷却时长（秒）
_DEFAULT_COOLDOWN_SECONDS = 300


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
        log.warning("reasoning_pref: model=%r → variant=%r (cached 48h)", model, variant)


# ---------------------------------------------------------------------------
# L1 配置：ConfigStore（拷贝 proxy.py 骨架 + 换 getter 适配新 schema）
# ---------------------------------------------------------------------------

class ConfigStore:
    """从 model_proxy_config.json 加载并热重载配置。

    热重载机制（mtime 比对、maybe_reload 双重检查、_reload_locked
    失败保留旧配置、reload 强制重载）拷贝自 proxy.py，getter 换成
    新 schema（supplies/routes/admin_token/default_cooldown_seconds）。
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

    def get_default_cooldown(self) -> int:
        with self._lock:
            return int(self._config.get("default_cooldown_seconds", _DEFAULT_COOLDOWN_SECONDS))

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

    def _reload_locked(self) -> bool:
        """持锁加载文件，替换 config 引用。解析失败时保留旧配置，返回 False。"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                new_config: dict[str, Any] = json.load(f)
            self._mtime = self._path.stat().st_mtime
            self._config = new_config
            return True
        except (json.JSONDecodeError, OSError) as e:
            log.warning("config reload failed, keeping old config: %s", e)
            return False


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
        self._lock = threading.Lock()

    def is_cooling(self, supply_id: str) -> bool:
        """当前是否处于冷却中（now < until）。"""
        now = time.time()
        with self._lock:
            until = self._until.get(supply_id, 0.0)
            return now < until

    def cooldown(self, supply_id: str, seconds: int) -> None:
        """将 supply 置入冷却：until = now + seconds。"""
        until = time.time() + seconds
        with self._lock:
            self._until[supply_id] = until

    def clear_all(self) -> None:
        """清空所有 supply 的冷却（仅手动 reload 调用，mtime 自动 reload 绝不调用）。"""
        with self._lock:
            self._until.clear()

    def snapshot(self) -> dict[str, float]:
        """返回 supply_id -> 剩余秒（仅含仍在冷却中的 supply，用于 status 展示）。"""
        now = time.time()
        with self._lock:
            items = list(self._until.items())
        result: dict[str, float] = {}
        for supply_id, until in items:
            remaining = until - now
            if remaining > 0:
                result[supply_id] = round(remaining, 1)
        return result


# ---------------------------------------------------------------------------
# L2 路由决策（全新，纯函数 + 常量）
# ---------------------------------------------------------------------------

# 冷却触发状态码：参考 proxy.py 的 _FAILOVER_STATUSES（{401,403,429} ∪ 5xx）。
# 测试场景坏 key 返回 401，必须包含 401 才能触发 cooldown + failover。
_FAILOVER_STATUSES = frozenset([401, 403, 429]) | frozenset(range(500, 600))

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


def detect_source(path: str, body: dict | None) -> str:
    """识别入站 source 协议。

    path 尾 /v1/messages → anthropic；/v1/responses → responses；
    /chat/completions → chat；否则看 body 特征；都不中 → unknown。
    """
    clean = path.split("?", 1)[0].rstrip("/")
    clean_lower = clean.lower()
    if clean_lower.endswith("/v1/messages"):
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


def resolve_route(strategies: list, routes_map: dict, client_token: str) -> dict | None:
    """阶段1：client_token → strategy → route_id → route。"""
    s = resolve_strategy(strategies, client_token)
    return routes_map.get(s.get("route_id")) if s else None


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
        # 非法态：route_id 与 route_pool 互斥（见设计文档 §4 校验规则），
        # _config_ops.py 写入侧已用 _validate_strategy_route_fields 拒绝该态，
        # 但配置文件可能被手工/外部改动绕过写入侧校验，运行时兜底：不中断
        # 请求，按 route_pool 处理并忽略 route_id，但要留日志可见性。
        log.warning("strategy=%s 同时配置了 route_id 和 route_pool，已忽略 "
                    "route_id，按 route_pool 处理", strategy.get("client_token", ""))

    # 新写法：先校验 route_pool 每项引用合法性，跳过非法项。
    valid_pool: list[dict] = []
    for item in route_pool:
        rid = item.get("route_id") if isinstance(item, dict) else None
        if not rid or rid not in routes_map:
            log.warning("route_pool entry invalid, skip: strategy=%s route_id=%r",
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

    与 _MODEL_TIER_MAP/resolve_tier/resolve_route/select_supply 正交并存，不改动
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
    """从 supplies 列表有序取第一个「未冷却且未试过」的 supply。

    跳过 cooling 的、tried_set 里已试的、以及 supply_map 中不存在的 id。
    返回 supply dict（非 id），无可用则 None。
    """
    for sid in supplies:
        if sid in tried_set:
            continue
        if sid not in supply_map:
            continue
        if cooldown.is_cooling(sid):
            continue
        return supply_map[sid]
    return None


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

    # 静默 BrokenPipeError（拷贝 proxy.py）
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, BrokenPipeError):
            return
        super().handle_error(request, client_address)

    # 屏蔽默认日志
    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def do_GET(self):
        if self.path.startswith(_CONTROL_PATH_PREFIX):
            self._dispatch_control("GET")
        else:
            self._forward_logged("GET")

    def do_POST(self):
        if self.path.startswith(_CONTROL_PATH_PREFIX):
            self._dispatch_control("POST")
        else:
            self._forward_logged("POST")

    def do_PUT(self):    self._forward_logged("PUT")
    def do_DELETE(self): self._forward_logged("DELETE")
    def do_PATCH(self):  self._forward_logged("PATCH")

    # ------------------------------------------------------------------
    # access 日志：整请求一条，覆盖 _forward 的整个生命周期
    # ------------------------------------------------------------------

    def _forward_logged(self, method: str) -> None:
        """包一层 _forward：收集 self._acc 字段 + 打点耗时，finally 里统一 emit 一条
        ACCESS 记录。_dispatch_control 路径不设 self._acc，故不经过此包装，也不产生
        ACCESS 记录（见 _write_* 里的 hasattr 守卫）。
        """
        self._acc = {
            "status": 0, "source": "", "route": "", "tier": "",
            "supply": "", "failover": 0, "attempts": 0, "token": "",
            "usage_in": 0, "usage_out": 0,
            "strategy": "", "session": "", "route_failover": 0,
            "builtin": "",
        }
        t0 = time.monotonic()
        try:
            self._forward(method)
        finally:
            a = self._acc
            ms = int((time.monotonic() - t0) * 1000)
            access_log.info(
                "ACCESS ms=%d status=%s source=%s route=%s tier=%s supply=%s "
                "failover=%s attempts=%s usage_in=%s usage_out=%s token=%s session=%s "
                "route_failover=%s builtin=%s",
                ms, a["status"], a["source"],
                a["route"], a["tier"], a["supply"], a["failover"], a["attempts"],
                a["usage_in"], a["usage_out"], a["token"], a["session"],
                a["route_failover"], a["builtin"])
            try:
                usage_totals.record(a, ms)
            except Exception:
                log.warning("usage_totals.record failed", exc_info=True)

    # ------------------------------------------------------------------
    # 转发编排（阶段1：纯透传路由 + cooldown + failover）
    # ------------------------------------------------------------------

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
        self._acc["token"] = token[-4:] if token else ""

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

        # 5. 三阶段匹配：strategy → route候选列表（session_hash分配 + B选项跨route兜底）
        #    → tier → supplies 列表
        strategies = cs.get_strategies()
        routes_map = cs.get_routes_map()
        strategy = resolve_strategy(strategies, token)
        self._acc["strategy"] = strategy.get("client_token", "") if strategy else ""
        session_key = self._acc["session"] or None

        # ---- 内建命令层拦截点（docs/designs/2026-08-04-in-band-route-command-design.md）----
        # 插在 source 判定之后、route 选择之前：body_json/session_key/source 均已就绪，
        # 尚未产生任何转发副作用。门控（§2.1，全部满足才进入识别，任一不满足 fail-open
        # 照常转发）：source == "anthropic" + session_key 非空 + body_json 是 dict 且含
        # messages 列表 + 有匹配的 strategy（strategy 为 None 时无处落 override，不识别为
        # 命令，回退原逻辑，最终按现状 401，行为不变）。
        sidecar: SessionOverridesSidecar = self.server.sidecar_store
        sidecar.maybe_reload()
        if (source == "anthropic" and session_key and strategy is not None
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
            log.warning("no strategy/route matched: token_tail4=%s source=%s",
                        token[-4:] if token else "", source)
            self._write_buffered_response(
                401, [], error_body_for_source(source, 401, "no strategy/route matched"))
            return

        # tier 解析只与 request_model 有关，与候选哪个 route 无关，循环外只算一次。
        tier = resolve_tier(request_model)
        if tier is None:
            log.warning("unknown model tier: model=%s pinned_route=%s",
                        request_model, route_candidates[0].get("id"))
            self._write_buffered_response(
                400, [], error_body_for_source(source, 400, f"unknown model tier: {request_model}"))
            return
        self._acc["tier"] = tier

        supply_map = cs.get_supply_map()
        default_cd = cs.get_default_cooldown()

        _reasoning_retried = False   # reasoning 语法重试只做一次，作用域覆盖整个请求周期

        # raw_intent 必须在循环外、基于客户端原始 body_json 只 decode 一次。
        # body_json 在循环体内会被原地改写（model 改写为 target_model、reasoning_wire
        # 通过 apply_fields 写回），若在循环内重新 decode，第二轮起会对"已被上一轮写入
        # 结果污染过"的 body_json 解码，导致客户端原始意图被错误钳位/升档（bug 修复记录，
        # 见 docs/proxy_v2_buildplan.md 或 commit message）。remap() 仍需在循环内按每轮
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

        # 6. route 候选外层循环（§3 选项B：pin route/其tier全挂时，按 session_hash
        #    排好的候选顺序换下一个候选 route 重试；单候选（旧单值 route_id 配置）时
        #    该外层循环退化为只跑一轮，行为与改动前完全一致，不产生 route_failover）。
        num_candidates = len(route_candidates)
        for candidate_idx, route in enumerate(route_candidates):
            self._acc["route"] = route.get("id")
            is_last_candidate = candidate_idx == num_candidates - 1

            supplies_list = select_supply_list(route, tier)
            if not supplies_list:
                log.warning("route missing tier config: route=%s tier=%s", route.get("id"), tier)
                if not is_last_candidate:
                    log.warning(
                        "route_failover: route=%s missing tier=%s config, trying next candidate route",
                        route.get("id"), tier)
                    self._acc["route_failover"] = 1
                    continue
                self._write_buffered_response(
                    503, [], error_body_for_source(
                        source, 503, f"route {route.get('id')} missing tier {tier}"))
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
                    log.warning("detect_target failed: supply=%s err=%s", supply_id, e)
                    self._write_buffered_response(
                        500, [], error_body_for_source(source, 500, str(e)))
                    return
                mode = pick_translator(source, target)

                if mode == UNSUPPORTED:
                    self._write_buffered_response(
                        501, [], error_body_for_source(
                            source, 501,
                            f"unsupported combination source={source} target={target}"))
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

                elif mode == ANTHROPIC_TO_CHAT:
                    # 组合3：anthropic 请求 → chat 上游。转成 OpenAI body，打 native chat 端点。
                    # 请求转换失败（异常）→ 合法 Anthropic error，400（正向规格 §5.1）
                    try:
                        openai_body, fwd_ctx = pt.anthropic_to_openai_request(
                            body_json or {}, reasoning_fields=reasoning_wire)
                    except Exception as e:
                        log.warning("ANTHROPIC_TO_CHAT request translate failed: %s", e)
                        self._write_buffered_response(
                            400, [], error_body_for_source(
                                source, 400, f"proxy translate failed: {e}"))
                        return
                    if target_model:
                        openai_body["model"] = target_model
                        fwd_ctx["request_model"] = target_model  # 响应 model 字段回填 target_model
                    send_body = json.dumps(openai_body, ensure_ascii=False).encode("utf-8")
                    # target_url 已在分支前统一算好（supply.url 现在已是完整 /chat/completions 端点）

                elif mode == ANTHROPIC_TO_RESPONSES:
                    # 新组合：anthropic 请求 → responses 上游。转成 Responses body，打完整 /v1/responses。
                    # 请求转换失败（异常）→ 合法 Anthropic error，400
                    try:
                        responses_body, fwd_ctx = pt.anthropic_to_responses_request(
                            body_json or {}, reasoning_fields=reasoning_wire)
                    except Exception as e:
                        log.warning("ANTHROPIC_TO_RESPONSES request translate failed: %s", e)
                        self._write_buffered_response(
                            400, [], error_body_for_source(
                                source, 400, f"proxy translate failed: {e}"))
                        return
                    if target_model:
                        responses_body["model"] = target_model
                        fwd_ctx["request_model"] = target_model  # 响应 model 字段回填 target_model
                    send_body = json.dumps(responses_body, ensure_ascii=False).encode("utf-8")
                    # target_url 已在分支前统一算好（supply.url 已配到完整 /v1/responses 端点）
                    # Responses reasoning.effort 机制无 Anthropic thinking.type 400 拒绝问题
                    # （ResponsesReasoningCodec 单变体，interpret_rejection 恒 None），无需重试。

                else:  # RESPONSES_TO_ANTHROPIC
                    # 组合4：responses 请求 → anthropic 上游。转成 Anthropic body，打 /v1/messages。
                    # 请求转换失败（异常）→ 合法 Responses error，400（反向规格 §5.1）
                    try:
                        anthropic_body = pt.responses_to_anthropic_request(
                            body_json or {}, max_tokens_default=4096,
                            reasoning_fields=reasoning_wire)
                    except Exception as e:
                        log.warning("RESPONSES_TO_ANTHROPIC request translate failed: %s", e)
                        self._write_buffered_response(
                            400, [], error_body_for_source(
                                source, 400, f"proxy translate failed: {e}"))
                        return
                    if target_model:
                        anthropic_body["model"] = target_model
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

                cd_seconds = int(supply.get("cooldown_seconds", default_cd))

                try:
                    resp = urllib.request.urlopen(req, timeout=600)
                    resp_status = resp.status
                except urllib.error.HTTPError as e:
                    resp_status = e.code
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

                    if failover == "on" and resp_status in _FAILOVER_STATUSES:
                        log.warning("cooldown+failover: supply=%s status=%s key_tail4=%s",
                                    supply_id, resp_status, appkey[-4:] if appkey else "")
                        self._acc["failover"] = 1
                        cd.cooldown(supply_id, cd_seconds)
                        tried_set.add(supply_id)
                        continue
                    # 不 failover：按 source 协议包裹上游错误（阶段4 §5.2：不透传上游原始协议结构）
                    upstream_msg = _extract_upstream_error_message(resp_body)
                    self._write_buffered_response(
                        resp_status, [],
                        error_body_for_source(
                            source, resp_status,
                            f"upstream error {resp_status}: {upstream_msg}"))
                    return
                except (urllib.error.URLError, OSError) as e:
                    if failover == "on":
                        log.warning("cooldown+failover(net): supply=%s err=%s key_tail4=%s",
                                    supply_id, e, appkey[-4:] if appkey else "")
                        self._acc["failover"] = 1
                        cd.cooldown(supply_id, cd_seconds)
                        tried_set.add(supply_id)
                        continue
                    self._write_buffered_response(
                        502, [], error_body_for_source(source, 502, f"upstream error: {e}"))
                    return

                # 成功拿到响应：若为冷却信号码且允许 failover，则冷却后继续
                if failover == "on" and resp_status in _FAILOVER_STATUSES:
                    resp.close()
                    log.warning("cooldown+failover: supply=%s status=%s key_tail4=%s",
                                supply_id, resp_status, appkey[-4:] if appkey else "")
                    self._acc["failover"] = 1
                    cd.cooldown(supply_id, cd_seconds)
                    tried_set.add(supply_id)
                    continue

                is_stream = isinstance(body_json, dict) and body_json.get("stream") is True

                # ---- 按 mode 分派写回 ----
                if mode == PASSTHROUGH:
                    # 透传：流式 chunked，非流式 buffered
                    if is_stream:
                        self._write_streaming_response(
                            resp_status, list(resp.getheaders()), resp, source)
                    else:
                        resp_body = resp.read()
                        try:
                            _pu = (json.loads(resp_body) or {}).get("usage") or {}
                            # anthropic 侧: input_tokens/output_tokens；
                            # chat/openai 侧: prompt_tokens/completion_tokens
                            self._acc["usage_in"] = _pu.get(
                                "input_tokens", _pu.get("prompt_tokens", 0)) or 0
                            self._acc["usage_out"] = _pu.get(
                                "output_tokens", _pu.get("completion_tokens", 0)) or 0
                        except Exception:
                            pass   # 解析失败不影响透传主流程，usage 记 0
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
                    else:
                        try:
                            raw_resp_body = resp.read()
                        finally:
                            resp.close()
                        # 响应转换失败（JSON 非法/转换器异常）→ 合法 Anthropic error，500（正向规格 §5.1）
                        try:
                            openai_resp = json.loads(raw_resp_body)
                            anthropic_resp = pt.openai_to_anthropic_response(openai_resp, fwd_ctx)
                        except Exception as e:
                            log.warning("ANTHROPIC_TO_CHAT response translate failed: %s", e)
                            self._write_buffered_response(
                                500, [], error_body_for_source(
                                    source, 500, f"proxy translate failed: {e}"))
                            return
                        _u = anthropic_resp.get("usage") or {}
                        self._acc["usage_in"] = _u.get("input_tokens", 0)
                        self._acc["usage_out"] = _u.get("output_tokens", 0)
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
                    else:
                        try:
                            raw_resp_body = resp.read()
                        finally:
                            resp.close()
                        # 响应转换失败（JSON 非法/转换器异常）→ 合法 Anthropic error，500
                        try:
                            responses_resp = json.loads(raw_resp_body)
                            anthropic_resp = pt.responses_to_anthropic_response(
                                responses_resp, fwd_ctx)
                        except Exception as e:
                            log.warning("ANTHROPIC_TO_RESPONSES response translate failed: %s", e)
                            self._write_buffered_response(
                                500, [], error_body_for_source(
                                    source, 500, f"proxy translate failed: {e}"))
                            return
                        _u = anthropic_resp.get("usage") or {}
                        self._acc["usage_in"] = _u.get("input_tokens", 0)
                        self._acc["usage_out"] = _u.get("output_tokens", 0)
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
                else:
                    try:
                        raw_resp_body = resp.read()
                    finally:
                        resp.close()
                    # 响应转换失败（JSON 非法/转换器异常）→ 合法 Responses error，500（反向规格 §5.1）
                    try:
                        anthropic_resp = json.loads(raw_resp_body)
                        responses_resp = pt.anthropic_to_responses_response(
                            anthropic_resp, target_model or "",
                            reasoning_effort=_r_effort, tools_echo=_tools_echo)
                    except Exception as e:
                        log.warning("RESPONSES_TO_ANTHROPIC response translate failed: %s", e)
                        self._write_buffered_response(
                            500, [], error_body_for_source(
                                source, 500, f"proxy translate failed: {e}"))
                        return
                    _u = responses_resp.get("usage") or {}
                    self._acc["usage_in"] = _u.get("input_tokens", 0)
                    self._acc["usage_out"] = _u.get("output_tokens", 0)
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
                    log.warning(
                        "route_failover: route=%s tier=%s all supplies failed or cooling, "
                        "trying next candidate route", route.get("id"), tier)
                    self._acc["route_failover"] = 1
                    continue
                self._write_buffered_response(
                    503, [], error_body_for_source(
                        source, 503, "all upstream supplies failed or cooling"))
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
            self._send_json(401, {"error": "unauthorized"})
            return

        path = self.path.split("?", 1)[0]  # 去掉 query string

        # GET /model_proxy/status
        if method == "GET" and path == "/model_proxy/status":
            self._handle_status(cs, cd)
            return

        # POST /model_proxy/reload
        if method == "POST" and path == "/model_proxy/reload":
            self._handle_reload(cs, cd)
            return

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
            "default_cooldown_seconds": cs.get_default_cooldown(),
        })

    def _handle_reload(self, cs: "ConfigStore", cd: "CooldownStore"):
        """手动 reload：强制重载配置 + 无条件清空所有 supply 的冷却。

        与 mtime 驱动的自动 maybe_reload()（每请求经 _forward 调用）的关键区别：
        手动 reload 是运维显式动作（改配置后确认生效），清空 cooldown 是合理的用户预期；
        自动 reload 只是发现文件变了顺手换配置，不应该悄悄影响运行中的冷却状态。
        """
        cs.reload()
        cd.clear_all()
        self._send_json(200, {"ok": True})

    # ------------------------------------------------------------------
    # 写回（拷贝 proxy.py：流式 chunked 透传 / 缓冲响应）
    # ------------------------------------------------------------------

    _SKIP_RESP_HEADERS = {"transfer-encoding", "content-length"}

    def _write_streaming_response(self, status: int, headers: list[tuple[str, str]], resp, source: str = "") -> None:
        """流式回写上游响应，使用 chunked 编码（组合1/2 透传）。

        source 非空时（PASSTHROUGH 流式），转发之后旁路嗅探 SSE 里的 usage 事件
        写入 self._acc，透传字节本身不受影响（§7 方案）。
        """
        if hasattr(self, "_acc"):
            self._acc["status"] = status
        self.send_response(status)
        for hname, hval in headers:
            if hname.lower() in self._SKIP_RESP_HEADERS:
                continue
            self.send_header(hname, hval)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        sniff_buf = b""                      # 新增：usage 嗅探 buffer
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                # —— 转发在前、无条件（行为一字未改）——
                size_line = f"{len(chunk):X}\r\n".encode("ascii")
                self.wfile.write(size_line)
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                # —— 嗅探在后、纯旁路，异常绝不影响转发 ——
                try:
                    sniff_buf += chunk
                    while b"\n\n" in sniff_buf:
                        block, sniff_buf = sniff_buf.split(b"\n\n", 1)
                        self._sniff_passthrough_usage(block, source)
                except Exception:
                    pass
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                if sniff_buf.strip():
                    self._sniff_passthrough_usage(sniff_buf.strip(), source)
            except Exception:
                pass
            resp.close()

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
                    except (json.JSONDecodeError, ValueError):
                        continue
                    for ev in adapter.feed(chunk):
                        self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            # 流结束：收尾（[DONE] 或上游断流都走 finalize，幂等）
            for ev in adapter.finalize():
                self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端已断开，写不进去也无所谓，静默即可
            pass
        except Exception as e:
            # 上游连接中断/读取异常/adapter.feed 抛异常：200+chunked 头已发出，无法降级为
            # 非流式 error body，只能按正向规格 §5.1 补发一个 `event: error` 再体面收尾
            # （不调用 adapter.finalize()，避免在失败态下伪造 message_delta/message_stop）
            log.warning("ANTHROPIC_TO_CHAT stream interrupted: %s", e)
            try:
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
                    if ev_type is None:
                        continue
                    for ev in adapter.feed(ev_type, ev_data):
                        self._write_sse_chunk(pt.responses_sse_bytes(ev))
            # 处理 buffer 残余块（末尾可能无空行）
            if buf.strip():
                ev_type, ev_data = self._parse_anthropic_sse_block(buf)
                if ev_type is not None:
                    for ev in adapter.feed(ev_type, ev_data):
                        self._write_sse_chunk(pt.responses_sse_bytes(ev))
            # 流意外结束（无 message_stop）时补收尾，幂等
            for ev in adapter.finalize():
                self._write_sse_chunk(pt.responses_sse_bytes(ev))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端已断开，写不进去也无所谓，静默即可
            pass
        except Exception as e:
            # 上游连接中断/读取异常/adapter.feed 抛异常：200+chunked 头已发出，无法降级为
            # 非流式 error body，按反向规格 §5.1 补发一个 response.failed 事件再体面收尾
            log.warning("RESPONSES_TO_ANTHROPIC stream interrupted: %s", e)
            try:
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
                    if ev_type is None:
                        continue
                    for ev in adapter.feed(ev_type, ev_data):
                        self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            # 处理 buffer 残余块（末尾可能无空行）
            if buf.strip():
                ev_type, ev_data = self._parse_anthropic_sse_block(buf)
                if ev_type is not None:
                    for ev in adapter.feed(ev_type, ev_data):
                        self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            # 流意外结束（无 response.completed）时补收尾，幂等
            for ev in adapter.finalize():
                self._write_sse_chunk(pt.anthropic_sse_bytes(ev))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.warning("ANTHROPIC_TO_RESPONSES stream interrupted: %s", e)
            try:
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
        """
        if source == "anthropic":
            if b"message_delta" not in block:          # 字节预筛
                return
            ev_type, data = self._parse_anthropic_sse_block(block)
            if ev_type != "message_delta" or not isinstance(data, dict):
                return
            u = data.get("usage") or {}
            if u.get("output_tokens") is not None:
                self._acc["usage_out"] = u.get("output_tokens") or 0
            if u.get("input_tokens") is not None:
                self._acc["usage_in"] = u.get("input_tokens") or 0
        elif source == "responses":
            if b"response.completed" not in block:      # 字节预筛
                return
            ev_type, data = self._parse_anthropic_sse_block(block)
            if ev_type != "response.completed" or not isinstance(data, dict):
                return
            u = (data.get("response") or {}).get("usage") or {}
            self._acc["usage_in"] = u.get("input_tokens", 0) or 0
            self._acc["usage_out"] = u.get("output_tokens", 0) or 0

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
            return None, None
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

    # 进程级互斥锁：同一时刻只允许一个 model_proxy.py 实例运行
    import fcntl
    lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        existing_pid = _LOCK_FILE.read_text().strip() if _LOCK_FILE.exists() else "unknown"
        print(f"ERROR: another model_proxy.py is already running (pid {existing_pid}). Exiting.", flush=True)
        lock_fd.close()
        raise SystemExit(1)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()

    # 1. 实例化 ConfigStore
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

    print(f"model_proxy listening on 127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
