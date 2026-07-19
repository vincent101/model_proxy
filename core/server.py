"""
tools/model_proxy/core/server.py — 本地多协议路由代理主体

多协议 AI 模型代理主程序：HTTP server、路由决策、转发编排、协议转换、控制 API。
入口为 tools/model_proxy/model_proxy.py（thin wrapper 调用本模块 main()）。
与线上 proxy.py（18888）完全隔离并行：新端口 18889、新配置
tools/model_proxy/model_proxy_config.json（可用 MODEL_PROXY_CONFIG 环境变量覆盖）、
新进程锁 /tmp/claude_model_proxy.lock、新日志 tools/model_proxy/.claude_model_proxy.log。

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
from .reasoning.capability import ReasoningCapability, align
from .reasoning.registry import apply_fields, get_codec

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

# 默认路径（全部 v2 命名）：锚定包目录 tools/model_proxy/model_proxy_config.json，
# 可用环境变量 MODEL_PROXY_CONFIG 覆盖（与 model_proxy_cli.sh 的 CONFIG_FILE 逻辑一致）。
_DEFAULT_CONFIG_PATH = Path(
    os.environ.get("MODEL_PROXY_CONFIG")
    or (Path(__file__).resolve().parent.parent / "model_proxy_config.json")
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
        pref_store: SyntaxPreferenceStore = self.server.pref_store

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
        _reasoning_retried = False   # reasoning 语法重试只做一次，作用域覆盖整个请求周期

        # reasoning_intent 必须在循环外、基于客户端原始 body_json 只 decode 一次。
        # body_json 在循环体内会被原地改写（model 改写为 target_model、reasoning_fields
        # 通过 apply_fields 写回），若在循环内重新 decode，第二轮起会对"已被上一轮写入
        # 结果污染过"的 body_json 解码，导致客户端原始意图被错误钳位/升档（bug 修复记录，
        # 见 docs/proxy_v2_buildplan.md 或 commit message）。align() 仍需在循环内按每轮
        # supply 的 capability 重新计算，因为不同 supply 的钳位上限不同。
        src_codec = get_codec(source)
        reasoning_intent = src_codec.decode(body_json or {})

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
            base_url = supply.get("url", "").rstrip("/")

            # ---- reasoning 统一链路：decode(source) → align(cap) → select_variant → encode(target) ----
            # 四种协议组合（PASSTHROUGH anthropic→anthropic / PASSTHROUGH responses→responses /
            # ANTHROPIC_TO_CHAT / ANTHROPIC_TO_RESPONSES / RESPONSES_TO_ANTHROPIC）全部走这同一条
            # 链路，差异只在 get_codec 拿到哪个 codec。PASSTHROUGH anthropic→anthropic 现在也用
            # 目标 Claude 模型的 capability 钳位（现状缺失的能力）。
            tgt_codec = get_codec(target)
            reasoning_cap = ReasoningCapability.from_config(supply)
            aligned_effort = align(reasoning_intent, reasoning_cap)
            reasoning_variant = tgt_codec.select_variant(pref_store.snapshot(target_model or ""))
            reasoning_fields = tgt_codec.encode(aligned_effort, reasoning_cap, reasoning_variant)

            # ---- 按 mode 计算 send_body / target_url / 转换上下文 ----
            # fwd_ctx：请求转换上下文（tool_name_mapping/request_model），
            # ANTHROPIC_TO_CHAT 与 ANTHROPIC_TO_RESPONSES 用
            fwd_ctx: dict[str, Any] | None = None
            if mode == PASSTHROUGH:
                # 改写 model → target_model；target_url = supply.url + 清洗后的客户端 path
                send_body = raw_body
                if target_model and isinstance(body_json, dict) and "model" in body_json:
                    body_json["model"] = target_model
                    send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                # reasoning 字段按目标 capability 钳位后原地 merge（含 anthropic→anthropic 与
                # responses→responses：source==target 时 encode 的 variant 就是该协议唯一/学到的
                # 语法，PASSTHROUGH 不代表"不处理 reasoning"，只代表 body 结构本身不用转换）
                if isinstance(body_json, dict) and reasoning_fields:
                    apply_fields(body_json, reasoning_fields)
                    send_body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
                _parsed = urllib.parse.urlparse(self.path)
                _qs = {k: v for k, v in urllib.parse.parse_qsl(_parsed.query)
                       if k not in {"beta"}}
                _clean_path = _parsed.path + ("?" + urllib.parse.urlencode(_qs) if _qs else "")
                target_url = base_url + _clean_path

            elif mode == ANTHROPIC_TO_CHAT:
                # 组合3：anthropic 请求 → chat 上游。转成 OpenAI body，打 native chat 端点。
                # 请求转换失败（异常）→ 合法 Anthropic error，400（正向规格 §5.1）
                try:
                    openai_body, fwd_ctx = pt.anthropic_to_openai_request(
                        body_json or {}, reasoning_fields=reasoning_fields)
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
                # chat 端点固定后缀 /chat/completions（客户端 /v1/messages 不透传）
                target_url = base_url + "/chat/completions"

            elif mode == ANTHROPIC_TO_RESPONSES:
                # 新组合：anthropic 请求 → responses 上游。转成 Responses body，打完整 /v1/responses。
                # 请求转换失败（异常）→ 合法 Anthropic error，400
                try:
                    responses_body, fwd_ctx = pt.anthropic_to_responses_request(
                        body_json or {}, reasoning_fields=reasoning_fields)
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
                # base_url 已配到完整 /v1/responses 层级，不拼子路径
                target_url = base_url
                # Responses reasoning.effort 机制无 Anthropic thinking.type 400 拒绝问题
                # （ResponsesReasoningCodec 单变体，interpret_rejection 恒 None），无需重试。

            else:  # RESPONSES_TO_ANTHROPIC
                # 组合4：responses 请求 → anthropic 上游。转成 Anthropic body，打 /v1/messages。
                # 请求转换失败（异常）→ 合法 Responses error，400（反向规格 §5.1）
                try:
                    anthropic_body = pt.responses_to_anthropic_request(
                        body_json or {}, max_tokens_default=4096,
                        reasoning_fields=reasoning_fields)
                except Exception as e:
                    log.warning("RESPONSES_TO_ANTHROPIC request translate failed: %s", e)
                    self._write_buffered_response(
                        400, [], error_body_for_source(
                            source, 400, f"proxy translate failed: {e}"))
                    return
                if target_model:
                    anthropic_body["model"] = target_model
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

                # reasoning 语法自适应重试（仅一次，不 rotate/cooldown supply，
                # 与 failover 重试的关键区别：不加入 tried_set、不调用 cd.cooldown）。
                # 单变体 codec（Chat/Responses）interpret_rejection 恒 None，天然不触发重试；
                # 只有 AnthropicReasoningCodec（双变体）在 tgt_codec 为它时才可能返回新变体。
                if (resp_status == 400 and not _reasoning_retried and target_model
                        and reasoning_fields):
                    next_variant = tgt_codec.interpret_rejection(resp_body, reasoning_variant)
                    if next_variant:
                        pref_store.learn(target_model, next_variant)
                        _reasoning_retried = True
                        continue  # 重新走 while 循环：select_supply 会再次选中同一 supply，
                                  # 重新算 reasoning_fields 时 select_variant 命中刚学到的偏好，
                                  # 自动改对语法后重发

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

            if mode == ANTHROPIC_TO_CHAT:
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
                        log.warning("ANTHROPIC_TO_CHAT response translate failed: %s", e)
                        self._write_buffered_response(
                            500, [], error_body_for_source(
                                source, 500, f"proxy translate failed: {e}"))
                        return
                    self._write_buffered_response(
                        200, [("Content-Type", "application/json")],
                        json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8"))
                return

            if mode == ANTHROPIC_TO_RESPONSES:
                # responses 响应 → Anthropic
                if is_stream:
                    adapter = pt.ResponsesToAnthropicStreamAdapter(fwd_ctx, target_model or "")
                    self._write_translated_stream_from_responses(resp, adapter)
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

    # 3. 启动 ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", port), ModelProxyHandler)
    server.config_store = config_store    # type: ignore[attr-defined]
    server.cooldown_store = cooldown_store  # type: ignore[attr-defined]
    server.pref_store = pref_store        # type: ignore[attr-defined]

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
