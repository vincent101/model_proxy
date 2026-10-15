#!/usr/bin/env python3
"""新模型可用性测试：opus-5.5 / sonnet-5.5 / gpt-6.1-sol × sankuai/mcli 直打上游。
复用 _config_ops 的鉴权与 header 构造逻辑（appkey / appkey_file / extra_headers / system_inject），
但发正常请求（effort=low）拿真实回复——比 probe（非法 effort 触发错误枚举）更直接判定可用性。
"""
import json, sys, urllib.request, urllib.error, time
sys.path.insert(0, '/Users/vincentwang/Documents/NoteVault/tools/model_proxy')
from _config_ops import resolve_appkey, inject_system_block

SANKUAI = {"url": "https://aigc.sankuai.com/v1/anthropic/v1/messages",
           "appkey": None}  # 从 config 现有 sankuai supply 取 appkey
MCLI = {"url": "https://mcli.sankuai.com/v1/messages",
        "appkey_file": "~/.config/mcopilot-cli/.config.yaml",
        "extra_headers": {"X-Working-Dir": "/"},
        "system_inject": "x-anthropic-billing-header: cc_version=2.1.196; cc_entrypoint=sdk-cli; cch=00000;"}

MODELS = ["claude-opus-5.5", "claude-sonnet-5.5", "gpt-6.1-sol"]

cfg = json.load(open('/Users/vincentwang/Documents/NoteVault/tools/model_proxy/config/model_proxy_config.json'))
SANKUAI["appkey"] = next(s["appkey"] for s in cfg["supplies"] if s["id"] == "claude-opus-sankuai-2023")

def test(upstream: dict, model: str) -> dict:
    supply = dict(upstream); supply["target_model"] = model; supply["protocol"] = "anthropic"
    try:
        appkey = resolve_appkey(supply)
    except Exception as e:
        return {"ok": False, "note": f"appkey 解析失败: {e}"}
    body = {"model": model, "max_tokens": 32,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": "回复一个字：好"}]}
    if supply.get("system_inject"):
        inject_system_block(body, supply["system_inject"])
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {appkey}", "x-api-key": appkey}
    for k, v in (supply.get("extra_headers") or {}).items():
        if k.lower() != "content-length":
            headers[k] = v
    req = urllib.request.Request(supply["url"], data=data, headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            dt = time.time() - t0
            try:
                j = json.loads(raw)
                text = "".join(b.get("text", "") for b in j.get("content", []) if isinstance(b, dict))
                err_in_body = j.get("type") == "error" or "error" in j
                return {"ok": not err_in_body and bool(text), "http": resp.status, "ms": int(dt*1000),
                        "reply": text[:60] or raw[:100], "err": (j.get("error") or {}).get("message", "")[:80] if err_in_body else ""}
            except ValueError:
                return {"ok": False, "http": resp.status, "ms": int(dt*1000), "reply": raw[:100], "err": "非 JSON"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "ms": int((time.time()-t0)*1000),
                "err": e.read().decode("utf-8", "replace")[:120]}
    except Exception as e:
        return {"ok": False, "http": None, "err": f"{type(e).__name__}: {e}"}

results = []
for model in MODELS:
    for name, up in (("sankuai", SANKUAI), ("mcli", MCLI)):
        r = test(up, model)
        results.append((model, name, r))
        mark = "✅" if r["ok"] else "❌"
        print(f"{mark} {model:20} {name:8} http={r.get('http')} {r.get('ms','')}ms "
              f"reply={r.get('reply','')[:40]!r} err={r.get('err','')[:60]!r}")

print("\n=== 汇总 ===")
ok = [(m, u) for m, u, r in results if r["ok"]]
bad = [(m, u, r.get("err") or r.get("reply")) for m, u, r in results if not r["ok"]]
print("可用:", ", ".join(f"{m}@{u}" for m, u in ok) or "无")
for m, u, e in bad:
    print(f"不可用 {m}@{u}: {str(e)[:80]}")
