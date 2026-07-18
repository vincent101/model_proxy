"""
tools/model_proxy/core/server.py — 本地多协议路由代理主体

多协议 AI 模型代理主程序：HTTP server、路由决策、转发编排、协议转换、控制 API。
入口为 tools/model_proxy/model_proxy.py（thin wrapper 调用本模块 main()）。
与线上 proxy.py（18888）完全隔离并行：新端口 18889、新配置
~/.claude/model_proxy_config.json、新进程锁 /tmp/claude_model_proxy.lock、
新日志 tools/model_proxy/.claude_model_proxy.log。

仅使用 Python 标准库，不引入第三方依赖，也不 import proxy.py。
"""

import hmac
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

# 合并后的双向协议转换器（core 包内相对导入）
from . import translate as pt

# ---------------------------------------------------------------------------
# L0 基座
# ---------------------------------------------------------------------------

# 日志固定落在包父目录 tools/model_proxy/（与 model_proxy_cli.sh 的 LOG_FILE 一致）
LOG_FILE = Path(__file__).resolve().parent.parent / ".claude_model_proxy.log"


def _trim_log(path: Path, keep: int = 1000) -> None:
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

# 默认路径（全部 v2 命名）
_DEFAULT_CONFIG_PATH = Path.home() / ".claude" / "model_proxy_config.json"
_LOCK_FILE = Path("/tmp/claude_model_proxy.lock")

# 控制路径前缀（v2，避免与 18888 的 /proxy 混淆）
_CONTROL_PATH_PREFIX = "/model_proxy"

# 顶层默认冷却时长（秒）
_DEFAULT_COOLDOWN_SECONDS = 300


# ---------------------------------------------------------------------------
# Thinking 格式自适应缓存（原样拷贝自 proxy.py，不改内部逻辑）
# 组合1（PASSTHROUGH, anthropic→anthropic）与组合4（REVERSE, responses→anthropic
# 转换后打真实 Claude 上游）共用：缓存 key 用 target_model，同进程内同一真实模型
# 命中同一条缓存。这四个函数只认 thinking/output_config 两个 key，不关心 dict 来源。
# ---------------------------------------------------------------------------

_THINKING_FMT_CACHE: dict[str, dict] = {}          # model → {format, at}
_THINKING_CACHE_TTL = 48 * 3600                     # 48 小时


def _get_thinking_fmt(model: str) -> str | None:
    entry = _THINKING_FMT_CACHE.get(model)
    if entry and time.time() - entry["at"] < _THINKING_CACHE_TTL:
        return entry["format"]
    return None


def _set_thinking_fmt(model: str, fmt: str) -> None:
    _THINKING_FMT_CACHE[model] = {"format": fmt, "at": time.time()}
    log.warning("thinking_cache: model=%r → format=%r (cached 48h)", model, fmt)


def _parse_thinking_error(body: bytes) -> str | None:
    """从 400 响应体识别应换用的 thinking 格式；返回 'adaptive' 或 'enabled'，无法识别返回 None。"""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return None
    t = text.lower()
    if "thinking.type.enabled" in t and "not supported" in t:
        return "adaptive"
    if "budget_tokens" in t and "not supported" in t:
        return "adaptive"
    if "thinking.type.adaptive" in t and "not supported" in t:
        return "enabled"
    # GLM 等部分网关返回泛化错误（不含具体 thinking 关键词），
    # 当请求带了 adaptive thinking 时，转回 enabled 尝试
    # output_config 不被支持（如 haiku-4.5 不接受 adaptive 的 output_config.effort）
    if "output_config" in t and ("not permitted" in t or "not allowed" in t):
        return "enabled"
    # GLM 等部分上游返回泛化中文错误（调用方带了 thinking 才走到此分支）
    if "参数有误" in t or "invalid parameter" in t.lower():
        return "enabled"
    return None


def _apply_thinking_fmt(body: dict, fmt: str) -> dict:
    """将 body 里的 thinking/output_config 转换为目标格式，返回修改后的 body（原地修改）。"""
    thinking = body.get("thinking")
    current = thinking.get("type") if isinstance(thinking, dict) else None
    if fmt == "adaptive":
        # enabled+budget → adaptive+effort
        if current == "enabled":
            budget = thinking.get("budget_tokens", 10000)
            effort = "low" if budget < 2000 else "high" if budget >= 32000 else "medium"
            body["thinking"] = {"type": "adaptive"}
            body.setdefault("output_config", {})["effort"] = effort
    elif fmt == "enabled":
        # adaptive → enabled+budget；并清除游离的 output_config.effort
        # （haiku 等模型不接受 output_config.effort，即使 thinking 为 None 也要清）
        if current == "adaptive":
            effort = body.get("output_config", {}).get("effort", "medium")
            budget = {"low": 2000, "medium": 10000, "high": 40000}.get(effort, 10000)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # 无论 thinking 形态如何，只要目标是 enabled 就移除 output_config
        # （游离的 output_config.effort 是 haiku 报错主因）
        if isinstance(body.get("output_config"), dict):
            body["output_config"].pop("effort", None)
            if not body["output_config"]:
                body.pop("output_config", None)
    return body


def _maybe_precvt_thinking(carrier: dict | None, model: str | None) -> bool:
    """查缓存，命中则对 carrier 原地应用 thinking 格式转换。

    carrier 为 PASSTHROUGH(anthropic→anthropic) 的 body_json 或 REVERSE 的
    anthropic_body；两处预转换逻辑复用同一份判断，避免重复代码。
    返回是否发生了转换（供调用方决定要不要重新序列化 send_body）。
    """
    if not carrier or not model:
        return False
    has_thinking_related = carrier.get("thinking") or carrier.get("output_config")
    if not has_thinking_related:
        return False
    cached_fmt = _get_thinking_fmt(model)
    if not cached_fmt:
        return False
    _apply_thinking_fmt(carrier, cached_fmt)
    return True


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

    def clear(self, supply_id: str) -> None:
        """手动清除某 supply 的冷却（控制 API 用）。"""
        with self._lock:
            self._until.pop(supply_id, None)

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

# 四组合分发模式
PASSTHROUGH = "passthrough"
FORWARD = "forward"
REVERSE = "reverse"
UNSUPPORTED = "unsupported"


def detect_source(path: str, body: dict | None) -> str:
    """识别入站 source 协议。

    path 尾 /v1/messages → anthropic；/v1/responses → responses；
    /chat/completions → chat；否则看 body 特征；都不中 → unknown。
    """
    clean = path.split("?", 1)[0].rstrip("/")
    if clean.endswith("/v1/messages"):
        return "anthropic"
    if clean.endswith("/v1/responses"):
        return "responses"
    if clean.endswith("/chat/completions"):
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


_MODEL_TIER_MAP = {
    "claude-opus": "opus",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
}


def resolve_route(strategies: list, routes_map: dict, client_token: str) -> dict | None:
    """阶段1：client_token → strategy → route_id → route。"""
    for s in strategies:
        if s.get("client_token") == client_token:
            return routes_map.get(s.get("route_id"))
    return None


def resolve_tier(model: str | None) -> str | None:
    """阶段2：model字符串精确查表 → tier名。不做子串猜测。"""
    if not model:
        return None
    return _MODEL_TIER_MAP.get(model)


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
    """target 协议 = supply["protocol"]。"""
    return supply.get("protocol", "")


def pick_translator(source: str, target: str) -> str:
    """四组合分发决策表。

    (anthropic,anthropic)|(responses,responses) → PASSTHROUGH
    (anthropic,chat) → FORWARD；(responses,anthropic) → REVERSE；其余 → UNSUPPORTED
    """
    if source == "anthropic" and target == "anthropic":
        return PASSTHROUGH
    if source == "responses" and target == "responses":
        return PASSTHROUGH
    if source == "anthropic" and target == "chat":
        return FORWARD
    if source == "responses" and target == "anthropic":
        return REVERSE
    return UNSUPPORTED


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
            self._forward("GET")

    def do_POST(self):
        if self.path.startswith(_CONTROL_PATH_PREFIX):
            self._dispatch_control("POST")
        else:
            self._forward("POST")

    def do_PUT(self):    self._forward("PUT")
    def do_DELETE(self): self._forward("DELETE")
    def do_PATCH(self):  self._forward("PATCH")

    # ------------------------------------------------------------------
    # 转发编排（阶段1：纯透传路由 + cooldown + failover）
    # ------------------------------------------------------------------

    def _forward(self, method: str):
        cs: ConfigStore = self.server.config_store
        cd: CooldownStore = self.server.cooldown_store

        # 1. 热重载 + 读 body
        cs.maybe_reload()
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        # 2. Bearer token
        auth_header = self.headers.get("Authorization", "")
        token = auth_header[len("Bearer "):] if auth_header.startswith("Bearer ") else ""

        # 3. 解析 body 拿 model
        body_json: dict[str, Any] | None = None
        try:
            if raw_body:
                body_json = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            body_json = None
        request_model = body_json.get("model") if isinstance(body_json, dict) else None

        # 4. source 协议识别
        source = detect_source(self.path, body_json)

        # 5. 三阶段匹配：strategy → route → tier → supplies 列表
        strategies = cs.get_strategies()
        routes_map = cs.get_routes_map()
        route = resolve_route(strategies, routes_map, token)
        if route is None:
            log.warning("no strategy/route matched: token_tail4=%s source=%s",
                        token[-4:] if token else "", source)
            self._write_buffered_response(
                401, [], error_body_for_source(source, 401, "no strategy/route matched"))
            return

        tier = resolve_tier(request_model)
        if tier is None:
            log.warning("unknown model tier: model=%s route=%s", request_model, route.get("id"))
            self._write_buffered_response(
                400, [], error_body_for_source(source, 400, f"unknown model tier: {request_model}"))
            return

        supplies_list = select_supply_list(route, tier)
        if not supplies_list:
            log.warning("route missing tier config: route=%s tier=%s", route.get("id"), tier)
            self._write_buffered_response(
                503, [], error_body_for_source(source, 503, f"route {route.get('id')} missing tier {tier}"))
            return

        supply_map = cs.get_supply_map()
        default_cd = cs.get_default_cooldown()
        failover = route.get("failover", "off")

        # 6. failover 循环（tried_set 为请求内局部集合，不改全局状态）
        tried_set: set[str] = set()
        _thinking_retried = False   # thinking 格式重试只做一次，作用域覆盖整个请求周期
        while True:
            supply = select_supply(supplies_list, supply_map, cd, tried_set)
            if supply is None:
                log.warning("all supplies failed or cooling: route=%s tier=%s",
                            route.get("id"), tier)
                self._write_buffered_response(
                    503, [], error_body_for_source(
                        source, 503, "all upstream supplies failed or cooling"))
                return

            supply_id = supply.get("id", "")
            target = detect_target(supply)
            mode = pick_translator(source, target)

            if mode == UNSUPPORTED:
                self._write_buffered_response(
                    501, [], error_body_for_source(
                        source, 501,
                        f"unsupported combination source={source} target={target}"))
                return

            target_model = supply.get("target_model")
            supply_reasoning = bool(supply.get("reasoning", True))
            base_url = supply.get("url", "").rstrip("/")

            # ---- 按 mode 计算 send_body / target_url / 转换上下文 ----
            # fwd_ctx：正向请求转换上下文（tool_name_mapping/request_model），仅 FORWARD 用
            fwd_ctx: dict[str, Any] | None = None
            if mode == PASSTHROUGH:
                # 改写 model → target_model；target_url = supply.url + 清洗后的客户端 path
                send_body = raw_body
                if target_model and isinstance(body_json, dict) and "model" in body_json:
                    body_json["model"] = target_model
                    send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                # thinking 格式预转换：仅 anthropic→anthropic（显式排除组合2 responses→responses，
                # 该组合理论上也无害，但意图要清晰——只在真正涉及 Anthropic thinking 语义时介入）
                if source == "anthropic" and target == "anthropic" and isinstance(body_json, dict):
                    if _maybe_precvt_thinking(body_json, target_model):
                        send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                _parsed = urllib.parse.urlparse(self.path)
                _qs = {k: v for k, v in urllib.parse.parse_qsl(_parsed.query)
                       if k not in {"beta"}}
                _clean_path = _parsed.path + ("?" + urllib.parse.urlencode(_qs) if _qs else "")
                target_url = base_url + _clean_path

            elif mode == FORWARD:
                # 组合3：anthropic 请求 → chat 上游。转成 OpenAI body，打 native chat 端点。
                # 请求转换失败（异常）→ 合法 Anthropic error，400（正向规格 §5.1）
                try:
                    openai_body, fwd_ctx = pt.anthropic_to_openai_request(
                        body_json or {}, model_is_reasoning=supply_reasoning)
                except Exception as e:
                    log.warning("FORWARD request translate failed: %s", e)
                    self._write_buffered_response(
                        400, [], error_body_for_source(
                            source, 400, f"proxy translate failed: {e}"))
                    return
                if target_model:
                    openai_body["model"] = target_model
                    fwd_ctx["request_model"] = target_model  # 响应 model 字段回填 target_model
                send_body = json.dumps(openai_body, ensure_ascii=False).encode("utf-8")
                # chat 端点固定后缀 /chat/completions（客户端 /v1/messages 不透传）
                target_url = base_url + "/chat/completions"

            else:  # REVERSE
                # 组合4：responses 请求 → anthropic 上游。转成 Anthropic body，打 /v1/messages。
                # 请求转换失败（异常）→ 合法 Responses error，400（反向规格 §5.1）
                try:
                    anthropic_body = pt.responses_to_anthropic_request(
                        body_json or {}, max_tokens_default=4096)
                except Exception as e:
                    log.warning("REVERSE request translate failed: %s", e)
                    self._write_buffered_response(
                        400, [], error_body_for_source(
                            source, 400, f"proxy translate failed: {e}"))
                    return
                if target_model:
                    anthropic_body["model"] = target_model
                # reasoning 门控：目标非 reasoning 模型时剔除 thinking/output_config 避免网关 400
                if not supply_reasoning:
                    anthropic_body.pop("thinking", None)
                    anthropic_body.pop("output_config", None)
                # thinking 格式预转换：门控之后执行——若已被剔除，载体里无 thinking 相关字段，
                # _maybe_precvt_thinking 的 has_thinking_related 判断自然跳过
                _maybe_precvt_thinking(anthropic_body, target_model)
                send_body = json.dumps(anthropic_body, ensure_ascii=False).encode("utf-8")
                # anthropic 端点固定后缀 /v1/messages（客户端 /v1/responses 不透传）
                target_url = base_url + "/v1/messages"

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

                # thinking 格式错误自适应重试（仅一次，不 rotate/cooldown supply，
                # 与 failover 重试的关键区别：不加入 tried_set、不调用 cd.cooldown）
                thinking_carrier: dict | None = None
                if mode == PASSTHROUGH and source == "anthropic" and target == "anthropic":
                    thinking_carrier = body_json if isinstance(body_json, dict) else None
                elif mode == REVERSE:
                    thinking_carrier = anthropic_body
                _carrier_has_thinking = thinking_carrier and (
                    thinking_carrier.get("thinking") or thinking_carrier.get("output_config"))
                if (resp_status == 400 and not _thinking_retried
                        and target_model and _carrier_has_thinking):
                    required_fmt = _parse_thinking_error(resp_body)
                    if required_fmt:
                        _set_thinking_fmt(target_model, required_fmt)
                        _thinking_retried = True
                        continue  # 重新走 while 循环：select_supply 会再次选中同一 supply，
                                  # 重新构造 body 时预转换命中刚写入的缓存，自动改对格式后重发

                if failover == "on" and resp_status in _FAILOVER_STATUSES:
                    log.warning("cooldown+failover: supply=%s status=%s key_tail4=%s",
                                supply_id, resp_status, appkey[-4:] if appkey else "")
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
                cd.cooldown(supply_id, cd_seconds)
                tried_set.add(supply_id)
                continue

            is_stream = isinstance(body_json, dict) and body_json.get("stream") is True

            # ---- 按 mode 分派写回 ----
            if mode == PASSTHROUGH:
                # 透传：流式 chunked，非流式 buffered
                if is_stream:
                    self._write_streaming_response(
                        resp_status, list(resp.getheaders()), resp)
                else:
                    resp_body = resp.read()
                    self._write_buffered_response(
                        resp_status, list(resp.getheaders()), resp_body)
                    resp.close()
                return

            if mode == FORWARD:
                # chat 响应 → Anthropic
                if is_stream:
                    adapter = pt.OpenAIToAnthropicStreamAdapter(fwd_ctx, target_model or "")
                    self._write_translated_stream(resp, adapter)
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
                        log.warning("FORWARD response translate failed: %s", e)
                        self._write_buffered_response(
                            500, [], error_body_for_source(
                                source, 500, f"proxy translate failed: {e}"))
                        return
                    self._write_buffered_response(
                        200, [("Content-Type", "application/json")],
                        json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8"))
                return

            # REVERSE：anthropic 响应 → Responses
            _r_effort = ((body_json or {}).get("reasoning") or {}).get("effort")
            _tools_echo = (body_json or {}).get("tools") or []
            if is_stream:
                adapter = pt.AnthropicToResponsesStreamAdapter(
                    model=target_model or "",
                    ctx={"tools": _tools_echo, "reasoning_effort": _r_effort})
                self._write_responses_stream(resp, adapter)
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
                    log.warning("REVERSE response translate failed: %s", e)
                    self._write_buffered_response(
                        500, [], error_body_for_source(
                            source, 500, f"proxy translate failed: {e}"))
                    return
                self._write_buffered_response(
                    200, [("Content-Type", "application/json")],
                    json.dumps(responses_resp, ensure_ascii=False).encode("utf-8"))
            return

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

        # POST /model_proxy/supply/<id>/cooldown/clear
        if method == "POST" and path.startswith("/model_proxy/supply/") and path.endswith("/cooldown/clear"):
            supply_id = path[len("/model_proxy/supply/"):-len("/cooldown/clear")]
            self._handle_cooldown_clear(cd, supply_id)
            return

        self._send_json(404, {"error": "not found"})

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------

    def _handle_status(self, cs: "ConfigStore", cd: "CooldownStore"):
        """回显 supplies/routes + cooldown 剩余秒。

        supplies 屏蔽 appkey（只留尾4位），避免明文暴露。
        """
        supplies = cs.get_supplies()
        safe_supplies: list[dict] = []
        for s in supplies:
            item = {k: v for k, v in s.items() if k != "appkey"}
            appkey = s.get("appkey", "")
            item["appkey_tail4"] = appkey[-4:] if appkey else ""
            safe_supplies.append(item)

        self._send_json(200, {
            "supplies": safe_supplies,
            "routes": cs.get_routes(),
            "strategies": cs.get_strategies(),
            "cooldown": cd.snapshot(),
            "default_cooldown_seconds": cs.get_default_cooldown(),
        })

    def _handle_reload(self, cs: "ConfigStore", cd: "CooldownStore"):
        cs.reload()
        self._send_json(200, {"ok": True})

    def _handle_cooldown_clear(self, cd: "CooldownStore", supply_id: str):
        """手动清除某 supply 的冷却。

        id 不存在也返回 ok:true——clear 语义是"确保不在冷却中"，本身幂等，
        对不存在的 id 调用与对已清除的 id 重复调用效果一致，不视为错误。
        """
        cd.clear(supply_id)
        self._send_json(200, {"ok": True})

    # ------------------------------------------------------------------
    # 写回（拷贝 proxy.py：流式 chunked 透传 / 缓冲响应）
    # ------------------------------------------------------------------

    _SKIP_RESP_HEADERS = {"transfer-encoding", "content-length"}

    def _write_streaming_response(self, status: int, headers: list[tuple[str, str]], resp) -> None:
        """流式回写上游响应，使用 chunked 编码（组合1/2 透传）。"""
        self.send_response(status)
        for hname, hval in headers:
            if hname.lower() in self._SKIP_RESP_HEADERS:
                continue
            self.send_header(hname, hval)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                size_line = f"{len(chunk):X}\r\n".encode("ascii")
                self.wfile.write(size_line)
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()

    def _write_buffered_response(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        """回写已完整读取的 buffer 响应（非流式 / 错误响应用）。"""
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
        """发 200 + text/event-stream 响应头，启用 chunked 逐事件写出。"""
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
            log.warning("FORWARD stream interrupted: %s", e)
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
            log.warning("REVERSE stream interrupted: %s", e)
            try:
                self._write_sse_chunk(pt.responses_sse_bytes(
                    _responses_failed_event(adapter, f"stream interrupted: {e}")))
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            upstream_resp.close()

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

    # 3. 启动 ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", port), ModelProxyHandler)
    server.config_store = config_store    # type: ignore[attr-defined]
    server.cooldown_store = cooldown_store  # type: ignore[attr-defined]

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
