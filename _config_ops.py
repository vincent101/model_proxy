#!/usr/bin/env python3
"""model_proxy 配置 CRUD + effort 探测 helper。

由 model_proxy_cli.sh 调用，不直接面向用户。所有交互式提示（input()）、字段校验、
原子写盘（tempfile.mkstemp + os.replace）都收敛在本文件，避免 CLI 脚本里重复
heredoc 样板。

调用约定：
    python3 _config_ops.py <subcommand> <config_file> [args...]

每次操作结束打印一行 `__RELOAD__=yes` 或 `__RELOAD__=no`（供 model_proxy_cli.sh
解析决定是否要调用 reload_proxy），退出码 0=成功 / 1=失败或用户取消。
"""

import json
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.reasoning.registry import resolve_protocol

PROBE_VALUE = "__probe_invalid__"
VALID_PROTOCOLS = ("anthropic", "chat", "responses")
VALID_FAILOVER = ("on", "off")
TIER_NAMES = ("opus", "sonnet", "haiku")


# ---------------------------------------------------------------------------
# 基础：读/写
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path: str, cfg: dict) -> None:
    _dir = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def err(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


def done(reload_needed: bool) -> None:
    """写 reload 标记。

    优先写到 CONFIG_OPS_RELOAD_MARKER 指定的文件（model_proxy_cli.sh 用临时文件传递，
    避免用 stdout 传递导致 bash 端 command substitution 缓冲、破坏 input() 交互实时性）；
    未设置该环境变量时（如单独调试本脚本）回退打印到 stdout。
    """
    marker = os.environ.get("CONFIG_OPS_RELOAD_MARKER")
    text = "yes" if reload_needed else "no"
    if marker:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(f"__RELOAD__={text}")


def confirm(prompt: str) -> bool:
    ans = input(f"{prompt} [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def prompt_source_capability(existing: "dict | None" = None) -> "dict | None":
    """交互式录入 source 侧 tiers_source_capability。
    对 opus/sonnet/haiku 逐 tier 询问；返回 {tier: {"effort_enum":[...]}} 或 None（全跳过）。
    - 输入逗号分隔列表 -> 对应 effort_enum
    - 输入 '-'        -> 空列表 []（该 tier 完全无思考能力）
    - 留空           -> add 场景：不写该 tier 键（运行时走 _DEFAULT_ENUM）；
                         edit 场景：保留 existing 里该 tier 原值（原本没有就仍不写）
    绝不把默认值物化写入。不做白名单清洗（人工把关，跟现有 run_probe_and_maybe_accept 原则一致）。
    """
    existing = existing or {}
    result = {}
    for tier in TIER_NAMES:
        cur = existing.get(tier)
        cur_desc = cur.get("effort_enum") if cur else "(未声明，走默认5档)"
        raw = input(f"  [{tier}] 支持的思考档位（逗号分隔；- 表示空列表[]；留空=保留/跳过）当前={cur_desc}: ").strip()
        if raw == "-":
            result[tier] = {"effort_enum": []}
        elif raw:
            result[tier] = {"effort_enum": [w.strip() for w in raw.split(",") if w.strip()]}
        elif cur is not None:
            result[tier] = cur
        # else: 不写该tier键
    return result or None


# ---------------------------------------------------------------------------
# effort 探测（迁自原 cmd_probe_effort，逻辑不变，只是从"独立命令只打印"
# 改成供 add/edit 内部调用+精确解析才回写）
# ---------------------------------------------------------------------------

def _fix_mojibake(raw: bytes) -> str:
    """尝试修复网关返回的UTF-8被当Latin-1重编码的双重编码乱码。
    仅在检测到mojibake特征时生效，否则原样按utf-8解码返回。"""
    plain = raw.decode("utf-8", errors="replace")
    try:
        fixed = raw.decode("utf-8", errors="ignore").encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return plain
    # 简单的mojibake特征检测：修复后中文字符明显增多则采用修复版
    def _cjk_count(s):
        return sum(1 for ch in s if '一' <= ch <= '鿿')
    if _cjk_count(fixed) > _cjk_count(plain):
        return fixed
    return plain


# 多措辞正则并联匹配，命中即止（对 _fix_mojibake 后的文本跑，无mojibake时退化为plain文本）。
_ENUM_PATTERNS = [
    r"[Ss]upported values (?:are)?\s*[:：]?\s*(.+)",
    r"expected one of\s+(.+?)(?:\s+at line|$)",
    r"(?:可选值为|可选值|valid values?)\s*[：:]\s*(.+)",
]


def _extract_enum_candidates(text: str) -> "list[str] | None":
    """从命中的措辞捕获组文本里抽取候选档名。

    兼容反引号包裹、单/双引号包裹，以及顿号/逗号/，/空格分隔的裸词，
    合并去重保留原始顺序。不做 name_to_canonical 白名单清洗——按方案，
    保留候选原始形态（可能含噪音），人工审查是唯一把关环节。
    """
    tail = None
    for pat in _ENUM_PATTERNS:
        m = re.search(pat, text)
        if m:
            tail = m.group(1)
            break
    if tail is None:
        return None

    backticked = re.findall(r"`([^`]+)`", tail)
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", tail)
    # 裸词兜底：去掉已被反引号/引号包裹的片段后，剩余按顿号/逗号/空格分隔
    bare_source = re.sub(r"`[^`]+`", " ", tail)
    bare_source = re.sub(r"['\"][^'\"]+['\"]", " ", bare_source)
    bare = [w for w in re.split(r"[、,，\s]+", re.split(r"[.。\n]", bare_source)[0]) if w.strip()]

    seen = set()
    cands = []
    for w in backticked + quoted + bare:
        w = w.strip().strip("、,，`'\"")
        if w and w not in seen:
            seen.add(w)
            cands.append(w)

    return cands or None


def _is_response_complete(raw: bytes, text_fixed: str) -> bool:
    """判断探测响应是否完整（未被截断）。主判据JSON完整性，其余作为参考。"""
    try:
        json.loads(text_fixed)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def probe_effort(supply: dict) -> "tuple[int | None, str, list[str] | None, bool, Exception | None]":
    """向 supply 上游发一个已知非法的 effort 值，尝试解析真实支持的枚举。

    只发一次探测请求。返回
    (http_status_or_None, text_fixed, regex_candidates, is_complete, exc)。
    正常完成/HTTP 错误响应路径下 `exc` 为 None；网络层异常（DNS 失败/连接超时/连接拒绝等）
    时 `status` 为 None、`text_fixed` 为 `str(exc)`、`exc` 为捕获到的原始异常对象本身
    （供 classify_supply_reachability 按异常类型细分原因，而不是只能用文本猜）。
    """
    url = (supply.get("url") or "").rstrip("/")
    appkey = supply.get("appkey", "")
    model = supply.get("target_model", "")

    try:
        proto = resolve_protocol(supply)
    except ValueError as e:
        return None, str(e), None, False, e

    target = url  # url 现在语义是完整终态端点，零拼接

    if proto == "anthropic":
        body = {"model": model, "max_tokens": 16,
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": PROBE_VALUE},
                "messages": [{"role": "user", "content": "probe"}]}
    elif proto == "chat":
        body = {"model": model, "max_tokens": 16,
                "reasoning_effort": PROBE_VALUE,
                "messages": [{"role": "user", "content": "probe"}]}
    else:  # responses
        body = {"model": model, "max_output_tokens": 16,
                "reasoning": {"effort": PROBE_VALUE},
                "input": "probe"}

    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {appkey}", "x-api-key": appkey}
    req = urllib.request.Request(target, data=data, headers=headers, method="POST")

    status = None
    raw_bytes = b""
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw_bytes = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw_bytes = e.read()
    except Exception as e:
        return None, str(e), None, False, e

    text_fixed = _fix_mojibake(raw_bytes)
    regex_candidates = _extract_enum_candidates(text_fixed)
    is_complete = _is_response_complete(raw_bytes, text_fixed)

    return status, text_fixed, regex_candidates, is_complete, None


class ReachabilityCategory:
    """supply 连通性分类常量（保持与项目现有 CanonicalEffort 等风格一致的简单命名空间）。"""
    DNS_ERROR = "dns_error"
    TIMEOUT = "timeout"
    CONN_REFUSED = "conn_refused"
    NETWORK_OTHER = "network_other"
    AUTH_ERROR = "auth_error"
    MODEL_ERROR = "model_error"
    REACHABLE = "reachable"
    UNKNOWN = "unknown"


_MODEL_ERROR_KEYWORDS = ("model", "not found", "不存在", "无效模型", "invalid model")


def classify_supply_reachability(status: "int | None", text_fixed: str,
                                  exc: "Exception | None") -> "tuple[str, str]":
    """纯函数：不发请求，只依据 probe_effort 的返回结果判定连通性失败原因。

    返回 (category, description)，category 取值见 ReachabilityCategory，
    description 是给用户看的中文人类可读描述。
    """
    if status is None:
        # urllib.request.urlopen 真实抛出的网络层异常几乎总是 urllib.error.URLError，
        # 底层原因（DNS/超时/拒绝连接）包在 exc.reason 里，不是裸的 socket.gaierror/
        # ConnectionRefusedError 本身——所以 gaierror/ConnectionRefusedError 的 isinstance
        # 判断必须同时覆盖「exc 本身就是该类型」和「exc 是 URLError 且 .reason 是该类型」
        # 两种形态，否则 DNS_ERROR 分支在生产环境永远走不到（reviewer 发现的真实 bug）。
        reason = getattr(exc, "reason", None) if isinstance(exc, urllib.error.URLError) else None
        if isinstance(exc, socket.gaierror) or isinstance(reason, socket.gaierror):
            return ReachabilityCategory.DNS_ERROR, "DNS解析失败，请检查url域名是否正确"
        if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
            return ReachabilityCategory.TIMEOUT, "连接超时，请检查网络/代理设置或目标服务是否可达"
        if isinstance(exc, ConnectionRefusedError) or isinstance(reason, ConnectionRefusedError):
            return ReachabilityCategory.CONN_REFUSED, "连接被拒绝，请检查目标端口/服务是否已启动"
        if isinstance(exc, urllib.error.URLError):
            reason_str = str(reason if reason is not None else exc)
            if "timed out" in reason_str.lower():
                return ReachabilityCategory.TIMEOUT, "连接超时，请检查网络/代理设置或目标服务是否可达"
            if "connection refused" in reason_str.lower():
                return ReachabilityCategory.CONN_REFUSED, "连接被拒绝，请检查目标端口/服务是否已启动"
        return ReachabilityCategory.NETWORK_OTHER, f"网络层异常，无法归入已知类别: {exc}"

    if status in (401, 403):
        return ReachabilityCategory.AUTH_ERROR, f"HTTP {status}，appkey可能失效或无权限访问该模型"

    if status == 404:
        return ReachabilityCategory.MODEL_ERROR, "HTTP 404，target_model 或路径可能配置错误"

    if status == 400:
        low = text_fixed.lower()
        if any(kw.lower() in low for kw in _MODEL_ERROR_KEYWORDS):
            return ReachabilityCategory.MODEL_ERROR, "HTTP 400 且响应含模型相关关键词，target_model 可能配置错误"
        return ReachabilityCategory.REACHABLE, "连通鉴权正常（400 是探测用的非法 effort 参数被拒，符合预期）"

    if status is not None and 200 <= status <= 299:
        return ReachabilityCategory.REACHABLE, "连通鉴权正常"

    return ReachabilityCategory.UNKNOWN, f"HTTP {status}，未归入已知类别，请人工核查响应内容"


def _print_connectivity_status(status: "int | None", text_fixed: str, category: str, desc: str) -> None:
    """打印结构化输出的第①项：当前接口联通状态（成功/失败及原因 + HTTP status，
    失败时附上游原始响应/异常文本）。供 run_connectivity_test 和
    run_probe_and_maybe_accept（独立跑分支）共用，保证展示格式统一。"""
    if category == ReachabilityCategory.REACHABLE:
        print(f"- 当前接口联通状态：成功（{desc}）")
    else:
        print(f"- 当前接口联通状态：失败，{desc}")
    print(f"    HTTP status={status}")
    if category != ReachabilityCategory.REACHABLE:
        print(f"    {text_fixed}")


def run_connectivity_test(supply: dict) -> "tuple[int | None, str, list[str] | None, bool, Exception | None]":
    """独立的联通性测试：调用 probe_effort 并按分类打印结构化的"联通状态"一项。

    返回 probe_effort 的原始五元组，供调用方在 REACHABLE 场景下直接复用
    （通过 run_probe_and_maybe_accept 的 prefetched 参数），避免重复发请求。
    """
    print(f"正在测试 supply={supply.get('id')} protocol={supply.get('protocol')} "
          f"model={supply.get('target_model')} 连通性 ...")
    result = probe_effort(supply)
    status, text_fixed, regex_candidates, is_complete, exc = result
    category, desc = classify_supply_reachability(status, text_fixed, exc)
    _print_connectivity_status(status, text_fixed, category, desc)
    return result


def run_probe_and_maybe_accept(supply: dict, interactive_prompt: bool = True,
                                 prefetched: "tuple | None" = None) -> dict | None:
    """执行探测，展示结果，询问用户是否接受；返回要写入的
    reasoning_capability dict 或 None（跳过/失败/不写入）。

    interactive_prompt=False 时（非交互批量场景预留），探测成功也不写入，只打印，
    避免在没有人工确认的路径上悄悄写库——当前 CLI 所有调用点都是交互式，此参数保留扩展位。

    prefetched 非 None 时（须是 probe_effort 格式的五元组），跳过 probe_effort 调用，
    直接复用传入的结果（并跳过"正在探测..."打印，因为调用方已在 run_connectivity_test
    里打印过测试过程），避免对同一 supply 重复发起真实上游请求。
    """
    if prefetched is not None:
        # 被 connectivity_test_then_probe 串联调用：run_connectivity_test 已经打印过
        # 结构化的「① 联通状态」一项，这里不再重复打印。
        status, text_fixed, regex_candidates, is_complete, exc = prefetched
    else:
        print(f"正在探测 supply={supply.get('id')} protocol={supply.get('protocol')} "
              f"model={supply.get('target_model')} ...")
        status, text_fixed, regex_candidates, is_complete, exc = probe_effort(supply)

    category, desc = classify_supply_reachability(status, text_fixed, exc)
    if prefetched is None:
        # 独立跑（无前置 run_connectivity_test）：补上「① 联通状态」一项，
        # 保证独立调用和串联调用看到同样结构化的输出。
        _print_connectivity_status(status, text_fixed, category, desc)

    a_success = bool(regex_candidates) and is_complete

    candidates = None
    source_desc = None

    if a_success:
        print(f"- 从接口返回探测到的思考程度档位：成功，候选档位={regex_candidates}（请核对，可能含噪音）")
        print(f"    {text_fixed}")
        candidates = regex_candidates
        source_desc = "网关探测"
    else:
        if category != ReachabilityCategory.REACHABLE:
            reason = f"连通性异常（{category}：{desc}），未能进行探测"
        elif regex_candidates and not is_complete:
            reason = f"响应疑似被截断，候选可能不全（不采纳，参考: {regex_candidates}）"
        else:
            reason = "探测正则不保证准确，供应商报错格式差异大，未提取到可信档位"
        print(f"- 从接口返回探测到的思考程度档位：失败，{reason}")
        print(f"    {text_fixed}")
        candidates = None
        source_desc = "探测无结论，请查官方文档后人工判断"

    if not interactive_prompt:
        print(f"（非交互模式，不自动写入，仅供参考，来源={source_desc}）")
        return None

    # candidates 为 None（探测无结论）时，仍进入人工输入环节——不能因为"探测不出"
    # 就直接放弃，人工可能依据外部信息（官方文档/已知架构结论）判断出这个 supply
    # 真实支持的档位（包括显式空集，表示确认不支持任何档位）。留空表示"跳过，不写入"，
    # 与 candidates 非 None 时"留空=沿用候选"的语义不同，此处沿用候选无意义（候选为空）。
    if candidates is not None:
        edited = input(
            f"- 请核对/编辑要写入的档位列表（来源=网关探测，逗号分隔；"
            f"输入 - 表示空列表/确认不支持任何档位；留空=沿用上面候选 {candidates}）: "
        ).strip()
    else:
        print("- 探测结果可信度有限，建议查看模型官方文档，确认支持的 reasoning effort 分档")
        edited = input(
            "    编辑要写入的档位列表（无自动候选，逗号分隔；"
            "输入 - 表示确认不支持任何档位；留空=跳过，不写入）: "
        ).strip()
    if edited == "-":
        final_enum = []
    elif edited:
        final_enum = [w.strip() for w in edited.split(",") if w.strip()]
    elif candidates is not None:
        final_enum = candidates
    else:
        print("已跳过，不写入 reasoning_capability。")
        return None

    if confirm(f"- 接受并写入 reasoning_capability.effort_enum={final_enum}?"):
        return {"effort_enum": final_enum}
    print("已跳过，不写入 reasoning_capability。")
    return None


def connectivity_test_then_probe(supply: dict) -> "tuple[str, str, dict | None]":
    """连通性测试 + REACHABLE 时复用响应做 effort 探测确认。

    只发一次上游请求（run_connectivity_test 内的 probe_effort 是唯一请求点）。
    返回 (category, desc, rcap)：
      - category/desc 来自 classify_supply_reachability，供调用方决定后续分支；
      - rcap：REACHABLE 且用户接受写入时为 {"effort_enum":[...]}，否则 None。
    非 REACHABLE 时不进入探测/写入环节，rcap 恒为 None。
    """
    result = run_connectivity_test(supply)
    status, text_fixed, regex_candidates, is_complete, exc = result
    category, desc = classify_supply_reachability(status, text_fixed, exc)
    if category == ReachabilityCategory.REACHABLE:
        rcap = run_probe_and_maybe_accept(supply, prefetched=result)
        return category, desc, rcap
    return category, desc, None


# ---------------------------------------------------------------------------
# supply
# ---------------------------------------------------------------------------

def _mask_appkey(appkey: str) -> str:
    return f"...{appkey[-4:]}" if appkey else "(空)"


def supply_list(path: str) -> None:
    cfg = load_config(path)
    for s in cfg.get("supplies", []):
        sid = s.get("id", "?")
        proto = s.get("protocol", "?")
        model = s.get("target_model", "?")
        tail4 = _mask_appkey(s.get("appkey", ""))
        rcap = "Y" if s.get("reasoning_capability") else "-"
        cd = f"{s['cooldown_seconds']}s" if "cooldown_seconds" in s else "(默认)"
        print(f"  {sid:24} protocol={proto:10} model={model:26} appkey={tail4:8}"
              f"  reasoning_capability={rcap}  cooldown={cd}")
    done(False)


def supply_add(path: str) -> None:
    cfg = load_config(path)
    supplies = cfg.setdefault("supplies", [])

    sid = input("Supply ID: ").strip()
    if not sid:
        err("Supply ID 不能为空")
        done(False); sys.exit(1)
    if any(s.get("id") == sid for s in supplies):
        err(f"supply id 已存在: {sid}")
        done(False); sys.exit(1)

    surl = input("完整终态端点 URL（如 https://aigc.sankuai.com/v1/anthropic/v1/messages，不是 base）: ").strip()
    sproto = input("协议 [anthropic/chat/responses]（可选，留空则从 url 尾缀自动推断）: ").strip()
    if sproto and sproto not in VALID_PROTOCOLS:
        err(f"协议非法: {sproto!r}（须为 {'/'.join(VALID_PROTOCOLS)}）")
        done(False); sys.exit(1)
    sappkey = input("Appkey: ").strip()
    smodel = input("目标模型 target_model: ").strip()
    scooldown = input("冷却时长 cooldown_seconds (回车用全局默认): ").strip()

    entry: dict = {
        "id": sid, "url": surl,
        "appkey": sappkey, "target_model": smodel,
    }
    if sproto:
        entry["protocol"] = sproto
    if scooldown:
        if not scooldown.isdigit() or int(scooldown) <= 0:
            err(f"cooldown_seconds 须为正整数: {scooldown!r}")
            done(False); sys.exit(1)
        entry["cooldown_seconds"] = int(scooldown)

    # 保存前用唯一权威解析校验：protocol 留空时须能从 url 尾缀推断出来，否则拒绝保存。
    try:
        resolve_protocol(entry)
    except ValueError as e:
        err(str(e))
        done(False); sys.exit(1)

    # 连通性测试 + 同步探测（add 时无条件跑，用户在场即时决定接受/跳过）。
    # REACHABLE 时复用同一次响应做档位解析，不再发第二次请求。
    category, desc, rcap = connectivity_test_then_probe(entry)
    if category != ReachabilityCategory.REACHABLE:
        if not confirm(f"连通性测试未通过（{desc}），是否仍要保存该 supply？"):
            print("已取消新增。")
            done(False); sys.exit(1)
    if rcap:
        entry["reasoning_capability"] = rcap

    supplies.append(entry)
    atomic_write(path, cfg)
    print(f"Added supply: {sid}")
    done(True)


def _find_supply(cfg: dict, sid: str) -> dict | None:
    for s in cfg.get("supplies", []):
        if s.get("id") == sid:
            return s
    return None


def supply_edit(path: str, sid: str) -> None:
    cfg = load_config(path)
    target = _find_supply(cfg, sid)
    if target is None:
        err(f"supply id 不存在: {sid}")
        done(False); sys.exit(1)

    def ask(field: str, label: str, current) -> str:
        shown = current if current not in (None, "") else "(空)"
        raw = input(f"{label} [{shown}]: ").strip()
        return raw if raw else (current or "")

    new_url = ask("url", "完整终态端点 URL（如 https://aigc.sankuai.com/v1/anthropic/v1/messages，不是 base）",
                  target.get("url", ""))
    new_proto = ask("protocol", "协议 [anthropic/chat/responses]（可选，留空则从 url 尾缀自动推断）",
                     target.get("protocol", ""))
    if new_proto and new_proto not in VALID_PROTOCOLS:
        err(f"协议非法: {new_proto!r}（须为 {'/'.join(VALID_PROTOCOLS)}）")
        done(False); sys.exit(1)

    cur_appkey = target.get("appkey", "")
    raw_appkey = input(f"Appkey [{_mask_appkey(cur_appkey)}]（回车保留，输入新值替换): ").strip()
    new_appkey = raw_appkey if raw_appkey else cur_appkey

    new_model = ask("target_model", "目标模型 target_model", target.get("target_model", ""))

    cur_cd = str(target.get("cooldown_seconds", "")) if "cooldown_seconds" in target else ""
    raw_cd = input(f"冷却时长 cooldown_seconds [{cur_cd or '(默认)'}]: ").strip()
    if raw_cd:
        if not raw_cd.isdigit() or int(raw_cd) <= 0:
            err(f"cooldown_seconds 须为正整数: {raw_cd!r}")
            done(False); sys.exit(1)

    target["url"] = new_url
    if new_proto:
        target["protocol"] = new_proto
    else:
        target.pop("protocol", None)  # 留空则不写入 protocol 键，运行时从 url 尾缀推断
    target["appkey"] = new_appkey
    target["target_model"] = new_model
    if raw_cd:
        target["cooldown_seconds"] = int(raw_cd)

    # 保存前用唯一权威解析校验：protocol 留空时须能从 url 尾缀推断出来，否则拒绝保存。
    try:
        resolve_protocol(target)
    except ValueError as e:
        err(str(e))
        done(False); sys.exit(1)

    # reasoning_capability 重新探测（可选）：先跑连通性测试，REACHABLE 则复用响应做探测，
    # 否则提示归因后跳过 rcap 写入（edit 自身有保存流程，不阻断整体 edit）。
    if confirm("reasoning_capability 重新探测?"):
        category, desc, rcap = connectivity_test_then_probe(target)
        if category != ReachabilityCategory.REACHABLE:
            print(f"连通性测试未通过（{desc}），跳过 reasoning_capability 探测。")
        if rcap:
            target["reasoning_capability"] = rcap

    atomic_write(path, cfg)
    print(f"Edited supply: {sid}")
    done(True)


def supply_check(path: str, sid: str) -> None:
    """连通性测试 + REACHABLE 则继续 effort 探测确认，接受则写入 reasoning_capability。
    整合原 supply-test（只读连通性）与 supply-probe（探测写入）为单一交互入口，
    全流程只发一次上游请求。
    """
    cfg = load_config(path)
    target = _find_supply(cfg, sid)
    if target is None:
        err(f"supply id 不存在: {sid}")
        done(False); sys.exit(1)
    category, desc, rcap = connectivity_test_then_probe(target)
    if category != ReachabilityCategory.REACHABLE:
        print("连通性测试未通过，跳过 effort 探测。")
        done(False)
        return
    if rcap:
        target["reasoning_capability"] = rcap
        atomic_write(path, cfg)
        print(f"Updated reasoning_capability for supply: {sid}")
        done(True)
    else:
        print("未写入变更。")
        done(False)


def supply_del(path: str, sid: str) -> None:
    cfg = load_config(path)
    target = _find_supply(cfg, sid)
    if target is None:
        err(f"supply id 不存在: {sid}")
        done(False); sys.exit(1)

    refs = []
    for r in cfg.get("routes", []):
        for tier_name, sids in (r.get("tiers") or {}).items():
            if sid in (sids or []):
                refs.append(f"route={r.get('id')} tier={tier_name}")
    if refs:
        err(f"supply {sid} 仍被以下 route/tier 引用，拒绝删除: {refs}")
        done(False); sys.exit(1)

    if not confirm(f"确认删除 supply {sid}?"):
        print("已取消。")
        done(False); sys.exit(1)

    cfg["supplies"] = [s for s in cfg.get("supplies", []) if s.get("id") != sid]
    atomic_write(path, cfg)
    print(f"Deleted supply: {sid}")
    done(True)


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------

def route_list(path: str) -> None:
    cfg = load_config(path)
    for r in cfg.get("routes", []):
        rid = r.get("id", "?")
        tiers = r.get("tiers", {})
        opus = ",".join(tiers.get("opus", []))
        sonnet = ",".join(tiers.get("sonnet", []))
        haiku = ",".join(tiers.get("haiku", []))
        failover = r.get("failover", "?")
        print(f"  {rid:12} opus=[{opus}] sonnet=[{sonnet}] haiku=[{haiku}] failover={failover}")
    done(False)


def _find_route(cfg: dict, rid: str) -> dict | None:
    for r in cfg.get("routes", []):
        if r.get("id") == rid:
            return r
    return None


def _split_supply_ids(raw: str) -> list[str]:
    """逗号分隔解析，与 route_list 的展示格式（`",".join(...)`）保持一致。"""
    return [w.strip() for w in raw.split(",") if w.strip()]


def route_add(path: str) -> None:
    cfg = load_config(path)
    routes = cfg.setdefault("routes", [])
    known_supplies = {s.get("id") for s in cfg.get("supplies", [])}

    rid = input("Route ID: ").strip()
    if not rid:
        err("Route ID 不能为空")
        done(False); sys.exit(1)
    if any(r.get("id") == rid for r in routes):
        err(f"route id 已存在: {rid}")
        done(False); sys.exit(1)

    ropus = _split_supply_ids(input("Opus 档 supplies (逗号分隔, 按优先级排序): "))
    rsonnet = _split_supply_ids(input("Sonnet 档 supplies (逗号分隔, 按优先级排序): "))
    rhaiku = _split_supply_ids(input("Haiku 档 supplies (逗号分隔, 按优先级排序): "))
    rfailover = input("Failover [on/off]: ").strip()

    if rfailover not in VALID_FAILOVER:
        err(f"failover 非法: {rfailover!r}（须为 {'/'.join(VALID_FAILOVER)}）")
        done(False); sys.exit(1)
    bad = [x for x in (ropus + rsonnet + rhaiku) if x not in known_supplies]
    if bad:
        err(f"以下 supply id 不存在: {sorted(set(bad))}")
        done(False); sys.exit(1)

    routes.append({
        "id": rid,
        "tiers": {"opus": ropus, "sonnet": rsonnet, "haiku": rhaiku},
        "failover": rfailover,
    })
    atomic_write(path, cfg)
    print(f"Added route: id={rid} opus={ropus} sonnet={rsonnet} haiku={rhaiku} failover={rfailover}")
    done(True)


def route_edit(path: str, rid: str) -> None:
    cfg = load_config(path)
    target = _find_route(cfg, rid)
    if target is None:
        err(f"route id 不存在: {rid}")
        done(False); sys.exit(1)
    known_supplies = {s.get("id") for s in cfg.get("supplies", [])}

    tiers = target.get("tiers") or {}
    new_tiers = {}
    for tier_name in TIER_NAMES:
        cur = tiers.get(tier_name, [])
        raw = input(
            f"{tier_name.capitalize()} 档 supplies (逗号分隔) [{','.join(cur)}]: "
        ).strip()
        new_tiers[tier_name] = _split_supply_ids(raw) if raw else cur

    bad = [x for x in sum(new_tiers.values(), []) if x not in known_supplies]
    if bad:
        err(f"以下 supply id 不存在: {sorted(set(bad))}")
        done(False); sys.exit(1)

    cur_failover = target.get("failover", "off")
    raw_fo = input(f"Failover [on/off] [{cur_failover}]: ").strip()
    new_failover = raw_fo if raw_fo else cur_failover
    if new_failover not in VALID_FAILOVER:
        err(f"failover 非法: {new_failover!r}（须为 {'/'.join(VALID_FAILOVER)}）")
        done(False); sys.exit(1)

    target["tiers"] = new_tiers
    target["failover"] = new_failover
    atomic_write(path, cfg)
    print(f"Edited route: {rid}")
    done(True)


def route_del(path: str, rid: str) -> None:
    cfg = load_config(path)
    target = _find_route(cfg, rid)
    if target is None:
        err(f"route id 不存在: {rid}")
        done(False); sys.exit(1)

    refs = [s.get("client_token") for s in cfg.get("strategies", []) if s.get("route_id") == rid]
    if refs:
        err(f"route {rid} 仍被以下 strategy 引用，拒绝删除: {refs}")
        done(False); sys.exit(1)

    if not confirm(f"确认删除 route {rid}?"):
        print("已取消。")
        done(False); sys.exit(1)

    cfg["routes"] = [r for r in cfg.get("routes", []) if r.get("id") != rid]
    atomic_write(path, cfg)
    print(f"Deleted route: {rid}")
    done(True)


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------

def _strategy_route_desc(st: dict) -> str:
    """打印用的 route 归属描述：兼容旧单值 route_id 与新 route_pool 写法（见
    docs/solutionDesigns/2026-07-28-session-route-dispatch-design.md §4/§5）。
    """
    route_pool = st.get("route_pool")
    if route_pool:
        parts = []
        for item in route_pool:
            if not isinstance(item, dict):
                continue
            rid = item.get("route_id", "?")
            weight = item.get("weight", 1)
            parts.append(f"{rid}:{weight}")
        return "pool[" + ",".join(parts) + "]"
    return st.get("route_id", "?")


def strategy_list(path: str) -> None:
    cfg = load_config(path)
    for st in cfg.get("strategies", []):
        tok = st.get("client_token", "?")
        rid = _strategy_route_desc(st)
        note = st.get("note", "") or ""
        print(f"  {tok:16} -> {rid:12} ({note})")
    done(False)


def _find_strategy(cfg: dict, token: str) -> dict | None:
    for s in cfg.get("strategies", []):
        if s.get("client_token") == token:
            return s
    return None


def _validate_strategy_route_fields(entry: dict) -> None:
    """校验 route_id 与 route_pool 互斥（见设计文档 §4 校验规则）。

    一条 strategy 若同时含 route_id 与 route_pool 两个字段，属非法/歧义配置
    （core/server.py 的 extract_route_candidates 遇到这种态会忽略 route_id、
    静默走 route_pool 分支，运行时可容错，但写盘时必须主动拒绝，避免脏配置
    落地）。校验失败即报错并以非零退出码终止，不写盘。
    """
    if entry.get("route_id") and entry.get("route_pool"):
        err(f"strategy client_token={entry.get('client_token', '?')} 同时配置了 "
            f"route_id 与 route_pool，两者互斥，请只保留一个后再写入")
        done(False); sys.exit(1)


def strategy_add(path: str) -> None:
    cfg = load_config(path)
    strategies = cfg.setdefault("strategies", [])
    known_routes = {r.get("id") for r in cfg.get("routes", [])}

    stoken = input("Client token: ").strip()
    if not stoken:
        err("client_token 不能为空")
        done(False); sys.exit(1)
    if any(s.get("client_token") == stoken for s in strategies):
        err(f"client_token 已存在 strategy 绑定: {stoken}")
        done(False); sys.exit(1)

    srid = input("Route ID (需为已存在的 route id): ").strip()
    if srid not in known_routes:
        err(f"route id 不存在: {srid}")
        done(False); sys.exit(1)

    print("录入 tiers_source_capability（source 侧能力，逐 tier 询问）:")
    stiers_cap = prompt_source_capability()

    snote = input("Note (可选备注): ").strip()

    entry: dict = {"client_token": stoken, "route_id": srid}
    if stiers_cap is not None:
        entry["tiers_source_capability"] = stiers_cap
    entry["note"] = snote

    _validate_strategy_route_fields(entry)
    strategies.append(entry)
    atomic_write(path, cfg)
    print(f"Added strategy: {stoken} -> {srid}")
    done(True)


def strategy_edit(path: str, token: str) -> None:
    cfg = load_config(path)
    target = _find_strategy(cfg, token)
    if target is None:
        err(f"未找到该 token 的 strategy 绑定: {token}")
        done(False); sys.exit(1)
    known_routes = {r.get("id") for r in cfg.get("routes", [])}

    # route_pool 写法（新）不走本 CLI 的单值 Route ID 编辑（见
    # docs/solutionDesigns/2026-07-28-session-route-dispatch-design.md §5：
    # CLI 菜单本次不支持录入 route_pool/dispatch，但编辑其他字段时不能丢/不能因
    # 强制要求单值 route_id 而报错阻塞）。target 引用是 cfg 内原字典，
    # route_pool/dispatch 字段全程保留不动。
    if target.get("route_pool"):
        print(f"该 strategy 使用 route_pool 多路由写法（{_strategy_route_desc(target)}），"
              f"本 CLI 暂不支持编辑 route_pool/dispatch，如需修改请直接编辑配置文件。")
        new_rid = None
    else:
        cur_rid = target.get("route_id", "")
        raw_rid = input(f"Route ID [{cur_rid}]: ").strip()
        new_rid = raw_rid if raw_rid else cur_rid
        if new_rid not in known_routes:
            err(f"route id 不存在: {new_rid}")
            done(False); sys.exit(1)

    if confirm("重新录入 tiers_source_capability?"):
        new_tiers_cap = prompt_source_capability(target.get("tiers_source_capability"))
        if new_tiers_cap is not None:
            target["tiers_source_capability"] = new_tiers_cap
        else:
            target.pop("tiers_source_capability", None)

    cur_note = target.get("note", "") or ""
    raw_note = input(f"Note [{cur_note}]: ").strip()
    new_note = raw_note if raw_note else cur_note

    if new_rid is not None:
        target["route_id"] = new_rid
    target["note"] = new_note
    _validate_strategy_route_fields(target)
    atomic_write(path, cfg)
    print(f"Edited strategy: {token} -> {_strategy_route_desc(target)}")
    done(True)


def strategy_del(path: str, token: str) -> None:
    cfg = load_config(path)
    target = _find_strategy(cfg, token)
    if target is None:
        err(f"未找到该 token 的 strategy 绑定: {token}")
        done(False); sys.exit(1)

    if not confirm(f"确认删除 strategy {token}?"):
        print("已取消。")
        done(False); sys.exit(1)

    cfg["strategies"] = [s for s in cfg.get("strategies", []) if s.get("client_token") != token]
    atomic_write(path, cfg)
    print(f"Deleted strategy: {token}")
    done(True)


# ---------------------------------------------------------------------------
# switch（顶层快捷方式，非交互，参数式一行）
# ---------------------------------------------------------------------------

def switch(path: str, token: str, route_id: str) -> None:
    cfg = load_config(path)
    target = _find_strategy(cfg, token)
    if target is None:
        err(f"未找到该 token 的 strategy 绑定: {token}，请先用 strategy add 新增")
        done(False); sys.exit(1)
    if target.get("route_pool"):
        # route_id 与 route_pool 互斥（见设计文档 §4 校验规则），不能对
        # route_pool 写法的 strategy 写单值 route_id，避免产生非法/歧义配置。
        err(f"该 strategy 使用 route_pool 多路由写法（{_strategy_route_desc(target)}），"
            f"switch 只支持单值 route_id 的旧写法，如需调整请直接编辑配置文件。")
        done(False); sys.exit(1)
    if not any(r.get("id") == route_id for r in cfg.get("routes", [])):
        err(f"route id 不存在: {route_id}")
        done(False); sys.exit(1)

    old = target.get("route_id")
    target["route_id"] = route_id
    atomic_write(path, cfg)
    print(f"已切换: {token} -> route_id={route_id}（原 route_id={old}）")
    done(True)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_DISPATCH = {
    "supply-list": (supply_list, 0),
    "supply-add": (supply_add, 0),
    "supply-edit": (supply_edit, 1),
    "supply-del": (supply_del, 1),
    "supply-check": (supply_check, 1),
    "route-list": (route_list, 0),
    "route-add": (route_add, 0),
    "route-edit": (route_edit, 1),
    "route-del": (route_del, 1),
    "strategy-list": (strategy_list, 0),
    "strategy-add": (strategy_add, 0),
    "strategy-edit": (strategy_edit, 1),
    "strategy-del": (strategy_del, 1),
    "switch": (switch, 2),
}


def main() -> None:
    if len(sys.argv) < 3:
        err("用法: _config_ops.py <subcommand> <config_file> [args...]")
        sys.exit(1)
    subcmd = sys.argv[1]
    config_path = sys.argv[2]
    extra = sys.argv[3:]

    entry = _DISPATCH.get(subcmd)
    if entry is None:
        err(f"未知子命令: {subcmd}")
        sys.exit(1)
    func, nargs = entry
    if len(extra) < nargs:
        err(f"{subcmd} 需要 {nargs} 个参数，收到 {len(extra)} 个")
        sys.exit(1)

    try:
        func(config_path, *extra[:nargs])
    except FileNotFoundError:
        err(f"config not found: {config_path}")
        sys.exit(1)
    except (json.JSONDecodeError, ValueError) as e:
        err(f"config 解析失败: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # 交互过程中 Ctrl-C：静默退出，不吐 traceback；不写配置，等价用户主动取消。
        # 退出码用 130（== 128+SIGINT，shell 生态里"进程被中断"的惯例值），
        # 供 model_proxy_cli.sh 的菜单循环识别并整体退出，而不是继续问下一轮操作。
        print("\n已取消。")
        done(False)
        sys.exit(130)


if __name__ == "__main__":
    main()
