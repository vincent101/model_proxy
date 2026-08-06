#!/usr/bin/env python3
"""$route in-band 指令匹配规则的参考实现与回归测试。

对应设计文档 §2.2（两级提取）+ §1.4（四条判定规则）+ §8 的 V2c / V9。

本文件的 `parse_route_command` 是**参考实现**：实施时 core/server.py 侧应与此保持
同一口径（或直接复用），任何一方改动都必须让本测试仍然通过。

跑法：
    python3 tests/test_command_match_rules.py          # 合成用例
    python3 tests/test_command_match_rules.py --replay # 附带真实 transcript 全量回归
"""

import re
import sys

# ---------------------------------------------------------------------------
# 参考实现
# ---------------------------------------------------------------------------

CMD_PREFIX = "$route"

# 与 Claudian src/utils/context.ts 的 XML_CONTEXT_PATTERN 完全同源。
# 六个标签名必须完整覆盖，缺一个即在该场景失效。
# 锚定 "\n\n<tag" + [\s>] 结尾，用于区分 <current_note> 与 <current_note_foo>。
XML_CONTEXT_PATTERN = re.compile(
    r"\n\n<(?:current_note|editor_selection|editor_cursor"
    r"|context_files|canvas_selection|browser_selection)[\s>]"
)


def last_text_block(content):
    """级1：定位用户这一轮实际打的那句话。

    只取最后一个 type=="text" 块，**不拼接**。
    理由（实测）：CLI 会把 <system-reminder> 作为独立的前置 text block 注入；
    拼接会让首 token 变成 <system-reminder> 且引入换行，判定必然失败。

    注意：与 core/translate.py 内「拼接全部 text 块」的口径**故意不同**——
    那边要的是「这条消息的全部文本」（传给上游），这边要的是「用户这轮打的话」。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return texts[-1] if texts else None
    return None


def strip_trailing_context(text):
    """级2：剥离 Claudian 追加在用户输入之后的 XML 上下文标签。

    只截首个匹配之前的内容，**不做全局替换**——用户正文里若本就含这些标签字样，
    全局替换会改变正文语义、放大误判面。
    """
    m = XML_CONTEXT_PATTERN.search(text)
    return text[: m.start()].strip() if m else text.strip()


def parse_route_command(content):
    """返回 (是否为指令, 参数 or None)。

    参数语义：None=查询当前 route；"reset"=清除 override；其他=目标 route id。

    判定规则（§1.4）：整条消息单行 + 首 token 精确等于 $route + token 数 <= 2。
    任一不满足即 fail-open（照常转发，绝不吞用户消息）。
    """
    text = last_text_block(content)
    if text is None:
        return False, None
    text = strip_trailing_context(text)

    if "\n" in text:                      # 规则2：单行
        return False, None
    tokens = text.split()
    if not tokens:
        return False, None
    if tokens[0] != CMD_PREFIX:           # 规则3：首 token 精确匹配（大小写敏感）
        return False, None
    if len(tokens) > 2:                   # 规则4：token 数 <= 2
        return False, None
    return True, (tokens[1] if len(tokens) == 2 else None)


# ---------------------------------------------------------------------------
# 合成用例
# ---------------------------------------------------------------------------

NOTE = "<current_note>\ntools/model_proxy/docs/designs/x.md\n</current_note>"

CASES = [
    # (用例名, content, 期望是否识别, 期望参数)
    ("纯指令(str)",              "$route nation", True, "nation"),
    ("指令+current_note",        f"$route nation\n\n{NOTE}", True, "nation"),
    ("指令+editor_selection",
     '$route nation\n\n<editor_selection path="a.md" lines="1-2">\nfoo\n</editor_selection>', True, "nation"),
    ("指令+browser_selection",
     '$route nation\n\n<browser_selection source="browser:x">\nbar\n</browser_selection>', True, "nation"),
    ("指令+context_files",       "$route nation\n\n<context_files>\na.md, b.md\n</context_files>", True, "nation"),
    ("指令+editor_cursor",       '$route nation\n\n<editor_cursor path="a.md" line="3">\n</editor_cursor>', True, "nation"),
    ("指令+canvas_selection",    "$route nation\n\n<canvas_selection>\nnode\n</canvas_selection>", True, "nation"),
    ("查询式(无参)",              "$route", True, None),
    ("查询式+上下文",             f"$route\n\n{NOTE}", True, None),
    ("reset",                    "$route reset", True, "reset"),
    ("reset+上下文",             f"$route reset\n\n{NOTE}", True, "reset"),
    ("CLI reminder+指令(list)",
     [{"type": "text", "text": "<system-reminder>\nfoo\n</system-reminder>"},
      {"type": "text", "text": "$route nation"}], True, "nation"),
    ("image+指令(list)",
     [{"type": "image"}, {"type": "text", "text": f"$route nation\n\n{NOTE}"}], True, "nation"),
    ("前后空白",                  "  $route nation  ", True, "nation"),

    # --- 以下必须 fail-open ---
    ("句中提及+上下文",           f"请解释 $route nation 是什么\n\n{NOTE}", False, None),
    ("多行含指令行",              "帮我看下\n$route nation\n这个", False, None),
    ("参数过多",                  "$route nation extra arg", False, None),
    ("非指令普通消息",            f"hello\n\n{NOTE}", False, None),
    ("代码块内",                  "```\n$route nation\n```", False, None),
    ("大小写不同",                "$ROUTE nation", False, None),
    ("前缀相似",                  "$routex nation", False, None),
    ("空消息",                    "", False, None),
    ("仅 tool_result 无 text",    [{"type": "tool_result", "content": "$route nation"}], False, None),
    ("标签名相似不应剥离",         "$route nation\n\n<current_note_foo>\nx\n</current_note_foo>", False, None),
]


def run_synthetic():
    failed = []
    for name, content, want_ok, want_arg in CASES:
        got_ok, got_arg = parse_route_command(content)
        ok = (got_ok == want_ok) and (not want_ok or got_arg == want_arg)
        mark = "✓" if ok else "✗ FAIL"
        print(f"  {mark} {name:26s} 期望={want_ok}/{want_arg!r:9s} 实际={got_ok}/{got_arg!r}")
        if not ok:
            failed.append(name)
    return failed


def run_replay():
    """真实 transcript 全量回归：确认历史消息不会被误命中。"""
    import glob
    import json
    import os

    d = os.path.expanduser("~/.claude/projects/-Users-vincentwang-Documents-NoteVault")
    if not os.path.isdir(d):
        print(f"  (跳过：{d} 不存在)")
        return []

    total, hits = 0, []
    for f in glob.glob(os.path.join(d, "*.jsonl")):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("type") != "user":
                        continue
                    msg = rec.get("message", {})
                    if msg.get("role") != "user":
                        continue
                    total += 1
                    ok, arg = parse_route_command(msg.get("content"))
                    if ok:
                        hits.append((rec.get("timestamp", ""), arg))
        except Exception:
            continue

    print(f"  真实 user 消息总数: {total}")
    print(f"  命中数: {len(hits)}")
    for ts, arg in hits:
        print(f"    - {ts} arg={arg!r}")
    print("  注：2026-08-06 的命中是沙箱实测期间用户亲手发的指令，属正确命中。")
    print("      除此之外若有任何命中，即为误命中，必须收紧规则。")
    unexpected = [h for h in hits if not h[0].startswith("2026-08-06")]
    return unexpected


if __name__ == "__main__":
    print("=== 合成用例 ===")
    failed = run_synthetic()

    unexpected = []
    if "--replay" in sys.argv:
        print("\n=== 真实 transcript 全量回归 ===")
        unexpected = run_replay()

    print()
    if failed:
        print(f"合成用例失败 {len(failed)} 项: {failed}")
    if unexpected:
        print(f"真实回归出现意外命中 {len(unexpected)} 条: {unexpected}")
    if not failed and not unexpected:
        print(f"全部通过（合成 {len(CASES)}/{len(CASES)}）")
    sys.exit(1 if (failed or unexpected) else 0)
