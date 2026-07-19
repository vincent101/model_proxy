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
import sys
import tempfile
import urllib.error
import urllib.request

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


def probe_effort(supply: dict) -> "tuple[int | None, str, list[str] | None, bool]":
    """向 supply 上游发一个已知非法的 effort 值，尝试解析真实支持的枚举。

    只发一次探测请求。返回 (http_status_or_None, text_fixed, regex_candidates, is_complete)。
    任何异常/超时都被吞掉，返回 (None, str(e), None, False)，调用方按"探测失败"处理。
    """
    url = (supply.get("url") or "").rstrip("/")
    proto = supply.get("protocol")
    appkey = supply.get("appkey", "")
    model = supply.get("target_model", "")

    if proto == "anthropic":
        target = url + "/v1/messages"
        body = {"model": model, "max_tokens": 16,
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": PROBE_VALUE},
                "messages": [{"role": "user", "content": "probe"}]}
    elif proto == "chat":
        target = url + "/chat/completions"
        body = {"model": model, "max_tokens": 16,
                "reasoning_effort": PROBE_VALUE,
                "messages": [{"role": "user", "content": "probe"}]}
    elif proto == "responses":
        target = url  # url 已配到完整 /v1/responses 层级
        body = {"model": model, "max_output_tokens": 16,
                "reasoning": {"effort": PROBE_VALUE},
                "input": "probe"}
    else:
        return None, f"未知 protocol: {proto!r}", None, False

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
        return None, str(e), None, False

    text_fixed = _fix_mojibake(raw_bytes)
    regex_candidates = _extract_enum_candidates(text_fixed)
    is_complete = _is_response_complete(raw_bytes, text_fixed)

    return status, text_fixed, regex_candidates, is_complete


def _llm_probe_official_doc(model_name: str, target_model: str, protocol: str) -> "dict | None":
    """通过具备联网能力的上游查询官方文档，判断模型是否支持 reasoning effort 分档。

    解析失败/请求失败/超时 → 静默降级，返回 None，不清洗白名单，原样返回解析出的dict。

    未实现：当前代码库（_config_ops.py 现有的 urllib.request 直连机制）不具备
    "指向某个可用 supply 发起联网检索并拿到搜索结果"的能力——现有 probe_effort()
    只是直接对上游模型 API 发一个 JSON 请求，不涉及 web_search 工具调用；而
    core/translate.py、core/server.py 对 Anthropic/Responses 托管的 web_search
    类工具是跳过/丢弃处理，不会转发给上游模型执行真实网络检索。没有找到可复用的
    联网上游调用机制，按方案要求在此按"合理降级"返回 None，不臆造新的联网方式。
    """
    return None


def run_probe_and_maybe_accept(supply: dict, interactive_prompt: bool = True) -> dict | None:
    """执行探测（a/b/c 三步），展示结果，询问用户是否接受；返回要写入的
    reasoning_capability dict 或 None（跳过/失败/不写入）。

    interactive_prompt=False 时（非交互批量场景预留），探测成功也不写入，只打印，
    避免在没有人工确认的路径上悄悄写库——当前 CLI 所有调用点都是交互式，此参数保留扩展位。
    """
    print(f"正在探测 supply={supply.get('id')} protocol={supply.get('protocol')} "
          f"model={supply.get('target_model')} ...")
    status, text_fixed, regex_candidates, is_complete = probe_effort(supply)
    print(f"HTTP status={status}")

    a_success = bool(regex_candidates) and is_complete

    candidates = None
    source_desc = None

    if a_success:
        print(f"探测命中，候选档位（请核对，可能含噪音): {regex_candidates}")
        print(text_fixed[:500] + ("...(truncated)" if len(text_fixed) > 500 else ""))
        candidates = regex_candidates
        source_desc = "网关探测"
    else:
        if regex_candidates and not is_complete:
            print(f"探测响应疑似被截断/不完整，候选可能不全: {regex_candidates}（不采纳，转查官方文档）")
        doc_result = _llm_probe_official_doc(
            supply.get("id", ""), supply.get("target_model", ""), supply.get("protocol", ""))
        if doc_result is not None:
            print(f"官方文档查询结果: supports_reasoning={doc_result.get('supports_reasoning')} "
                  f"effort_enum={doc_result.get('effort_enum')} "
                  f"confidence={doc_result.get('confidence')} "
                  f"source_urls={doc_result.get('source_urls')}")
            if regex_candidates:
                print(f"（疑似截断，仅供交叉参考的探测候选: {regex_candidates}）")
            candidates = doc_result.get("effort_enum")
            source_desc = "官方文档查询，confidence=" + str(doc_result.get("confidence"))
        else:
            print("探测与文档查询均未得到结论，请人工核对官方文档后手动确认")
            print(text_fixed[:500] + ("...(truncated)" if len(text_fixed) > 500 else ""))
            candidates = None
            source_desc = "无自动结论，人工判断"

    if not interactive_prompt:
        print(f"（非交互模式，不自动写入，仅供参考，来源={source_desc}）")
        return None

    # candidates 为 None（a/b 均无结论）时，仍进入人工输入环节——不能因为"探测不出"
    # 就直接放弃，人工可能依据外部信息（官方文档/已知架构结论）判断出这个 supply
    # 真实支持的档位（包括显式空集，表示确认不支持任何档位）。留空表示"跳过，不写入"，
    # 与 candidates 非 None 时"留空=沿用候选"的语义不同，此处沿用候选无意义（候选为空）。
    prompt_candidates_desc = f"候选档位={candidates}" if candidates is not None else "（无自动候选）"
    edited = input(
        f"来源={source_desc}，{prompt_candidates_desc}\n"
        f"请核对/编辑要写入的档位列表（逗号分隔；输入 - 表示空列表/确认不支持任何档位；"
        f"留空={'沿用上面候选' if candidates is not None else '跳过，不写入'}）: "
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

    if confirm(f"接受并写入 reasoning_capability.effort_enum={final_enum}?"):
        return {"effort_enum": final_enum}
    print("已跳过，不写入 reasoning_capability。")
    return None


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

    surl = input("上游 URL: ").strip()
    sproto = input("协议 [anthropic/chat/responses]: ").strip()
    if sproto not in VALID_PROTOCOLS:
        err(f"协议非法: {sproto!r}（须为 {'/'.join(VALID_PROTOCOLS)}）")
        done(False); sys.exit(1)
    sappkey = input("Appkey: ").strip()
    smodel = input("目标模型 target_model: ").strip()
    scooldown = input("冷却时长 cooldown_seconds (回车用全局默认): ").strip()

    entry: dict = {
        "id": sid, "url": surl, "protocol": sproto,
        "appkey": sappkey, "target_model": smodel,
    }
    if scooldown:
        if not scooldown.isdigit() or int(scooldown) <= 0:
            err(f"cooldown_seconds 须为正整数: {scooldown!r}")
            done(False); sys.exit(1)
        entry["cooldown_seconds"] = int(scooldown)

    # 同步探测（add 时无条件跑，用户在场即时决定接受/跳过）
    rcap = run_probe_and_maybe_accept(entry)
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

    new_url = ask("url", "上游 URL", target.get("url", ""))
    new_proto = ask("protocol", "协议 [anthropic/chat/responses]", target.get("protocol", ""))
    if new_proto not in VALID_PROTOCOLS:
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
    target["protocol"] = new_proto
    target["appkey"] = new_appkey
    target["target_model"] = new_model
    if raw_cd:
        target["cooldown_seconds"] = int(raw_cd)

    # reasoning_capability 重新探测（可选）
    if confirm("reasoning_capability 重新探测?"):
        rcap = run_probe_and_maybe_accept(target)
        if rcap:
            target["reasoning_capability"] = rcap

    atomic_write(path, cfg)
    print(f"Edited supply: {sid}")
    done(True)


def supply_probe(path: str, sid: str) -> None:
    """轻量子命令：只跑探测步骤，按 add/edit 同样规则回写（供批量补探）。"""
    cfg = load_config(path)
    target = _find_supply(cfg, sid)
    if target is None:
        err(f"supply id 不存在: {sid}")
        done(False); sys.exit(1)
    rcap = run_probe_and_maybe_accept(target)
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

    ropus = input("Opus 档 supplies (空格分隔, 按优先级排序): ").split()
    rsonnet = input("Sonnet 档 supplies (空格分隔, 按优先级排序): ").split()
    rhaiku = input("Haiku 档 supplies (空格分隔, 按优先级排序): ").split()
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
        raw = input(f"{tier_name.capitalize()} 档 supplies [{' '.join(cur)}]: ").strip()
        new_tiers[tier_name] = raw.split() if raw else cur

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

def strategy_list(path: str) -> None:
    cfg = load_config(path)
    for st in cfg.get("strategies", []):
        tok = st.get("client_token", "?")
        rid = st.get("route_id", "?")
        note = st.get("note", "") or ""
        print(f"  {tok:16} -> {rid:12} ({note})")
    done(False)


def _find_strategy(cfg: dict, token: str) -> dict | None:
    for s in cfg.get("strategies", []):
        if s.get("client_token") == token:
            return s
    return None


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
    snote = input("Note (可选备注): ").strip()

    strategies.append({"client_token": stoken, "route_id": srid, "note": snote})
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

    cur_rid = target.get("route_id", "")
    raw_rid = input(f"Route ID [{cur_rid}]: ").strip()
    new_rid = raw_rid if raw_rid else cur_rid
    if new_rid not in known_routes:
        err(f"route id 不存在: {new_rid}")
        done(False); sys.exit(1)

    cur_note = target.get("note", "") or ""
    raw_note = input(f"Note [{cur_note}]: ").strip()
    new_note = raw_note if raw_note else cur_note

    target["route_id"] = new_rid
    target["note"] = new_note
    atomic_write(path, cfg)
    print(f"Edited strategy: {token} -> {new_rid}")
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
    "supply-probe": (supply_probe, 1),
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


if __name__ == "__main__":
    main()
