"""多档位多协议实测探针：5 source 档 × 8 组合（4模型×2协议），顺序执行。

每探针：记 log 行数基线 → POST /v1/messages → 解析响应(th_chars/自述/usage) +
事后关联新增 reasoning_debug 行的 wire/target/intent + ACCESS 行的 budget_retried。
结果增量写 JSONL，便于监控与事后分析。
"""
import ast
import json
import re
import time
import urllib.request

BASE = "http://127.0.0.1:18889"
LOG = "/Users/vincentwang/Documents/NoteVault/tools/model_proxy/.claude_model_proxy.log"
OUT = "/tmp/multi_effort_results.jsonl"
PROMPT = "用一句话自我介绍并说明推理强度档位"

COMBOS = [
    ("glm-52-anthropic", "glm-5.2", "anthropic"),
    ("glm-52-responses", "glm-5.2", "responses"),
    ("ds-flash-anthropic", "deepseek-v4-flash", "anthropic"),
    ("ds-flash-responses", "deepseek-v4-flash", "responses"),
    ("ds-pro-anthropic", "deepseek-v4-pro", "anthropic"),
    ("ds-pro-responses", "deepseek-v4-pro", "responses"),
    ("kimi-k3-anthropic", "kimi-k3", "anthropic"),
    ("kimi-k3-responses", "kimi-k3", "responses"),
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

RE_DEBUG = re.compile(
    r"reasoning_debug: supply=(\S+) .*?intent=(\w+)\(\d+\).*? -> target=(\w+)\((\d+)\).*?variant=(\S+) wire=(\{.*\})$")
RE_ACCESS = re.compile(r" ACCESS ")


def log_lines():
    with open(LOG, encoding="utf-8", errors="replace") as f:
        return f.readlines()


def parse_wire_effort(wire_str):
    try:
        d = ast.literal_eval(wire_str)
    except Exception:
        return None
    if isinstance(d, dict):
        if "output_config" in d and isinstance(d["output_config"], dict):
            return d["output_config"].get("effort")
        if "reasoning" in d and isinstance(d["reasoning"], dict):
            return d["reasoning"].get("effort")
    return None


def send(token, effort):
    body = {
        "model": "claude-sonnet",
        "max_tokens": 4000,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "messages": [{"role": "user", "content": PROMPT}],
    }
    req = urllib.request.Request(
        f"{BASE}/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            code = resp.getcode()
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            data = json.loads(e.read().decode("utf-8"))
        except Exception:
            data = {"_raw_error": str(e)}
    except Exception as e:
        return None, f"EXC:{e}", time.time() - t0
    return code, data, time.time() - t0


def main():
    # 重置输出文件
    open(OUT, "w").close()
    total = len(COMBOS) * len(EFFORTS)
    n = 0
    for token, model, proto in COMBOS:
        for effort in EFFORTS:
            n += 1
            baseline = len(log_lines())
            code, data, dt = send(token, effort)
            # 读新增日志
            new = log_lines()[baseline:]
            dbg = []
            access = {}
            for ln in new:
                m = RE_DEBUG.search(ln)
                if m:
                    dbg.append({
                        "supply": m.group(1), "intent": m.group(2),
                        "target": m.group(3), "variant": m.group(5),
                        "wire": parse_wire_effort(m.group(6)),
                    })
                if RE_ACCESS.search(ln):
                    for kv in re.findall(r"(\w+)=([^\s]+)", ln):
                        access[kv[0]] = kv[1]
            # 解析响应
            th_chars, text, usage_rt, usage_tt, stop = None, None, None, None, None
            if isinstance(data, dict) and "content" in data:
                th = "".join(b.get("thinking", "") for b in data.get("content", []) if b.get("type") == "thinking")
                tx = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                th_chars = len(th)
                text = tx.strip()
                stop = data.get("stop_reason")
                u = data.get("usage", {}) or {}
                otd = u.get("output_tokens_details", {}) or {}
                usage_rt = otd.get("reasoning_tokens")
                usage_tt = otd.get("thinking_tokens")
            wire_efforts = sorted({d["wire"] for d in dbg if d["wire"]})
            rec = {
                "n": n, "token": token, "model": model, "protocol": proto,
                "src_effort": effort, "http": code, "time_s": round(dt, 1),
                "attempts": len(dbg),
                "wire_efforts": wire_efforts,
                "wire_effort": wire_efforts[0] if len(wire_efforts) == 1 else wire_efforts,
                "intent": dbg[-1]["intent"] if dbg else None,
                "target": dbg[-1]["target"] if dbg else None,
                "variant": dbg[-1]["variant"] if dbg else None,
                "th_chars": th_chars, "stop_reason": stop,
                "reasoning_tokens": usage_rt, "thinking_tokens": usage_tt,
                "budget_retried": access.get("budget_retried", ""),
                "budget_truncated": access.get("budget_truncated", ""),
                "self_desc": (text[:120] if text else None),
            }
            with open(OUT, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"[{n}/{total}] {token} src={effort} http={code} attempts={len(dbg)} "
                  f"wire={rec['wire_effort']} th={th_chars} t={round(dt,1)}s", flush=True)


if __name__ == "__main__":
    main()
