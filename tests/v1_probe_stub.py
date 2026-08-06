#!/usr/bin/env python3
"""V1/V2/V2b 验证用的一次性探针 stub（不属产品代码，仅供沙箱实测）。

用途：验证设计文档 2026-08-04-in-band-route-command-design.md §8 的三项：
  V1  —— `$route nation` 能否在 CLI 与 Claudian 双侧原样到达 API
  V2  —— user 消息是否被客户端追加 `<system-reminder>`（决定「单行」约束是否成立）
  V2b —— `system` 字段的线格式（str / blocks 数组、是否带 cache_control）

行为：监听本地端口，把收到的每个请求体 dump 成 JSON 文件 + 打印关键判定，
然后回一个最小合法的 anthropic 响应（流式/非流式都支持），让客户端不报错。

**不转发任何上游请求**，因此不消耗任何配额、不会打到生产。

用法：
    python3 v1_probe_stub.py [--port 18899] [--outdir /tmp/v1_probe]

再在另一个终端用 CLI 打请求（必须带 --setting-sources，见设计文档 §8）：
    env ANTHROPIC_BASE_URL="http://127.0.0.1:18899/" ANTHROPIC_AUTH_TOKEN="cc" \
      claude --setting-sources project,local -p '$route nation'

Claudian 侧则把插件的 API base 指向同一端口后发一条 `$route nation`。
"""

import argparse
import json
import os
import pathlib
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUTDIR = pathlib.Path("/tmp/v1_probe")
SEQ = {"n": 0}

# 被测指令：与设计文档 §1.4 的匹配规则一致
PROBE_CMD = "$route nation"


def _extract_last_user_text(body):
    """复刻设计文档 §2.2 的定位规则：只取 messages 里最后一个 role=user 的文本。

    content 可能是 str，也可能是 content blocks 数组（只取 type=text 的块）。
    返回 (文本, 该消息的原始 content 结构)。
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return None, None
    for m in reversed(msgs):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c, c
        if isinstance(c, list):
            parts = [b.get("text", "") for b in c
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "".join(parts), c
        return None, c
    return None, None


def _match_verdict(text):
    """按 §1.4 的五条规则逐条判定，返回 (是否匹配, 逐条说明)。"""
    if text is None:
        return False, ["没有取到 user 文本"]
    notes = []
    stripped = text.strip()
    notes.append(f"strip 后长度={len(stripped)}")

    single_line = "\n" not in stripped
    notes.append(f"规则2 单行: {'✓' if single_line else '✗ 含换行'}")

    tokens = stripped.split()
    tok0 = tokens[0] if tokens else ""
    r3 = tok0 == "$route"
    notes.append(f"规则3 首token=='$route': {'✓' if r3 else f'✗ 实际={tok0!r}'}")

    r4 = len(tokens) <= 2
    notes.append(f"规则4 token数<=2: {'✓' if r4 else f'✗ 实际={len(tokens)}'}")

    ok = single_line and r3 and r4
    return ok, notes


def _describe_system(sys_field):
    """V2b：描述 system 字段的线格式。"""
    if sys_field is None:
        return "缺失（无 system 字段）"
    if isinstance(sys_field, str):
        return f"str，长度={len(sys_field)}"
    if isinstance(sys_field, list):
        kinds = []
        cache = 0
        for b in sys_field:
            if isinstance(b, dict):
                kinds.append(b.get("type"))
                if "cache_control" in b:
                    cache += 1
        return (f"list（{len(sys_field)} 个 block），types={kinds}，"
                f"带 cache_control 的 block 数={cache}")
    return f"未预期类型 {type(sys_field).__name__}"


class Probe(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # 静音默认访问日志，只打我们自己的结构化输出

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw)
        except Exception:
            body = None

        SEQ["n"] += 1
        idx = SEQ["n"]
        ts = time.strftime("%H:%M:%S")

        # 落盘完整 body 供事后细看
        OUTDIR.mkdir(parents=True, exist_ok=True)
        dump = OUTDIR / f"req-{idx:03d}.json"
        dump.write_text(json.dumps(
            {"path": self.path,
             "headers": dict(self.headers),
             "body": body},
            ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\n{'='*78}")
        print(f"[{ts}] #{idx}  {self.command} {self.path}")
        if not isinstance(body, dict):
            print("  body 非 JSON dict，已 dump:", dump)
            self._respond(body)
            return

        ua = self.headers.get("user-agent", "")
        print(f"  user-agent: {ua[:90]}")
        print(f"  model={body.get('model')!r}  stream={body.get('stream')!r}")

        # ---- V1：指令是否原样到达 ----
        text, raw_content = _extract_last_user_text(body)
        print(f"\n  --- V1 指令可达性 ---")
        print(f"  最后一条 user 文本 = {text!r}")
        if text is not None:
            exact = text.strip() == PROBE_CMD
            print(f"  是否精确等于 {PROBE_CMD!r}: {'✓ 是' if exact else '✗ 否'}")
            ok, notes = _match_verdict(text)
            for x in notes:
                print(f"    {x}")
            print(f"  §1.4 匹配规则总判定: {'✓ 会被识别为指令' if ok else '✗ 不会被识别'}")

        # ---- V2：content 形态 / 是否被追加 system-reminder ----
        print(f"\n  --- V2 content 形态 ---")
        if isinstance(raw_content, str):
            print(f"  content 是 str")
        elif isinstance(raw_content, list):
            types = [b.get("type") for b in raw_content if isinstance(b, dict)]
            print(f"  content 是 list，{len(raw_content)} 个 block，types={types}")
        has_reminder = "<system-reminder>" in (text or "")
        print(f"  文本内含 <system-reminder>: {'⚠ 是（单行约束会被破坏）' if has_reminder else '✓ 否'}")

        # ---- V2b：system 字段线格式 ----
        print(f"\n  --- V2b system 字段 ---")
        print(f"  {_describe_system(body.get('system'))}")
        sys_raw = json.dumps(body.get("system"), ensure_ascii=False)
        for probe in ("ZZPROBE", "Custom Instructions"):
            if probe in sys_raw:
                print(f"  含 {probe!r}: ✓")

        print(f"\n  完整 body 已 dump: {dump}")
        self._respond(body)

    # GET 也兜一下，避免客户端探活失败
    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        payload = b'{"ok":true}'
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _respond(self, body):
        """回一个最小合法的 anthropic 响应，让客户端正常收尾。"""
        is_stream = isinstance(body, dict) and body.get("stream") is True
        model = (body or {}).get("model") or "claude-sonnet-4-20250514"
        text = "[v1_probe_stub] 已记录本次请求，未转发上游。"

        if not is_stream:
            payload = json.dumps({
                "id": "msg_probe",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # 流式：按 core/translate.py AnthropicStreamAdapter 的事件顺序产出
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        def sse(etype, data):
            payload = (f"event: {etype}\n"
                       f"data: {json.dumps(data, ensure_ascii=False)}\n\n").encode()
            self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()

        sse("message_start", {"type": "message_start", "message": {
            "id": "msg_probe", "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}}})
        sse("ping", {"type": "ping"})
        sse("content_block_start", {"type": "content_block_start", "index": 0,
                                    "content_block": {"type": "text", "text": ""}})
        sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": text}})
        sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        sse("message_delta", {"type": "message_delta",
                              "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                              "usage": {"output_tokens": 0}})
        sse("message_stop", {"type": "message_stop"})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18899)
    ap.add_argument("--outdir", default="/tmp/v1_probe")
    args = ap.parse_args()

    global OUTDIR
    OUTDIR = pathlib.Path(args.outdir)
    OUTDIR.mkdir(parents=True, exist_ok=True)

    print(f"v1_probe_stub 监听 127.0.0.1:{args.port}，dump 目录 {OUTDIR}")
    print(f"被测指令: {PROBE_CMD!r}")
    print("不转发上游、不消耗配额。Ctrl-C 退出。\n")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Probe)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
