"""Responses `type: namespace` 工具在 r2a 链路上的摊平与还原。

回归（2026-11-26）：Codex 的子 agent 工具全部包在 `multi_agent_v1` 命名空间里；
`translate_tools` 只认 `type: function`，其余一律跳过，导致上游模型根本看不到
`spawn_agent`，Codex 侧表现为「本会话未提供 spawn_agent 工具」。

实测确认（真实 Codex 0.166 + mock 上游）：
- 工具名只写 `spawn_agent` → Codex 报 `unsupported call: spawn_agent`
- 只写 `multi_agent_v1.spawn_agent` → 同样 `unsupported call`
- 写成 `name=spawn_agent` + `namespace=multi_agent_v1` → 正常派单并返回 agent id
"""
import json
import unittest

from core.translate import (
    AnthropicToResponsesStreamAdapter,
    anthropic_to_responses_response,
    build_namespace_maps,
    namespace_tool_alias,
    responses_to_anthropic_request,
    NAMESPACE_TOOL_PARAM_HINT,
    with_namespace_param_hint,
)

SUB_TOOLS = [
    {"type": "function", "name": "spawn_agent", "description": "spawn",
     "parameters": {"type": "object", "properties": {"agent_type": {"type": "string"}}}},
    {"type": "function", "name": "close_agent", "description": "close",
     "parameters": {"type": "object", "properties": {"target": {"type": "string"}}}},
]
TOOLS = [
    {"type": "function", "name": "exec_command", "description": "run",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "namespace", "name": "multi_agent_v1", "description": "sub-agents",
     "tools": SUB_TOOLS},
]


def _resp(name: str, stop_reason: str = "tool_use") -> dict:
    return {
        "type": "message", "role": "assistant", "stop_reason": stop_reason,
        "content": [{"type": "tool_use", "id": "toolu_1", "name": name,
                     "input": {"task_name": "t", "message": "m"}}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


class NamespaceFlattenRequestTests(unittest.TestCase):
    def test_namespaced_tools_are_flattened_not_dropped(self):
        ab = responses_to_anthropic_request({"model": "m", "input": [], "tools": TOOLS})
        names = [t["name"] for t in ab["tools"]]
        self.assertIn("exec_command", names)
        self.assertIn(namespace_tool_alias("multi_agent_v1", "spawn_agent"), names)
        self.assertIn(namespace_tool_alias("multi_agent_v1", "close_agent"), names)

    def test_flattened_tool_keeps_schema_and_description(self):
        ab = responses_to_anthropic_request({"model": "m", "input": [], "tools": TOOLS})
        by_name = {t["name"]: t for t in ab["tools"]}
        spawn = by_name[namespace_tool_alias("multi_agent_v1", "spawn_agent")]
        # 原描述保留在前，参数填写约定追加在后（2026-11-26 空值重试风暴回归）
        self.assertTrue(spawn["description"].startswith("spawn"))
        self.assertIn(NAMESPACE_TOOL_PARAM_HINT, spawn["description"])
        self.assertIn("agent_type", spawn["input_schema"]["properties"])

    def test_flat_function_tool_gets_no_param_hint(self):
        """提示只加在命名空间工具上，普通 function 工具描述保持原样。"""
        ab = responses_to_anthropic_request({"model": "m", "input": [], "tools": TOOLS})
        flat = {t["name"]: t for t in ab["tools"]}["exec_command"]
        self.assertEqual(flat["description"], "run")

    def test_param_hint_is_idempotent_and_handles_empty_description(self):
        once = with_namespace_param_hint("spawn")
        self.assertEqual(with_namespace_param_hint(once), once)
        self.assertEqual(with_namespace_param_hint(""), NAMESPACE_TOOL_PARAM_HINT)

    def test_hosted_tool_types_are_still_skipped(self):
        tools = TOOLS + [{"type": "web_search", "name": "web_search"}]
        ab = responses_to_anthropic_request({"model": "m", "input": [], "tools": tools})
        names = [t["name"] for t in ab["tools"]]
        self.assertNotIn("web_search", names)
        self.assertEqual(len(names), 3)

    def test_history_function_call_uses_flattened_name(self):
        body = {
            "model": "m",
            "tools": TOOLS,
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call", "name": "spawn_agent", "namespace": "multi_agent_v1",
                 "arguments": "{}", "call_id": "call_1", "status": "completed", "id": "fc_1"},
                {"type": "function_call_output", "call_id": "call_1", "output": "{}"},
            ],
        }
        ab = responses_to_anthropic_request(body)
        tool_uses = [b for m in ab["messages"] if m["role"] == "assistant"
                     for b in m["content"] if b.get("type") == "tool_use"]
        self.assertEqual([b["name"] for b in tool_uses],
                         [namespace_tool_alias("multi_agent_v1", "spawn_agent")])

    def test_tool_choice_namespaced_function_maps_to_alias(self):
        body = {
            "model": "m", "tools": TOOLS, "input": [],
            "tool_choice": {"type": "function", "name": "spawn_agent",
                            "namespace": "multi_agent_v1"},
        }
        ab = responses_to_anthropic_request(body)
        self.assertEqual(ab["tool_choice"],
                         {"type": "tool",
                          "name": namespace_tool_alias("multi_agent_v1", "spawn_agent")})

    def test_history_without_namespace_field_is_unchanged(self):
        body = {
            "model": "m", "tools": TOOLS,
            "input": [{"type": "function_call", "name": "exec_command",
                       "arguments": "{}", "call_id": "c1", "status": "completed"}],
        }
        ab = responses_to_anthropic_request(body)
        tool_uses = [b for m in ab["messages"] if m["role"] == "assistant"
                     for b in m["content"] if b.get("type") == "tool_use"]
        self.assertEqual([b["name"] for b in tool_uses], ["exec_command"])


class NamespaceRestoreResponseTests(unittest.TestCase):
    def test_non_stream_restores_name_and_namespace(self):
        alias = namespace_tool_alias("multi_agent_v1", "spawn_agent")
        out = anthropic_to_responses_response(_resp(alias), "m", tools_echo=TOOLS)
        fc = [o for o in out["output"] if o["type"] == "function_call"][0]
        self.assertEqual(fc["name"], "spawn_agent")
        self.assertEqual(fc["namespace"], "multi_agent_v1")

    def test_non_stream_plain_tool_has_no_namespace(self):
        out = anthropic_to_responses_response(_resp("exec_command"), "m", tools_echo=TOOLS)
        fc = [o for o in out["output"] if o["type"] == "function_call"][0]
        self.assertEqual(fc["name"], "exec_command")
        self.assertNotIn("namespace", fc)

    def _stream(self, alias: str) -> list:
        ad = AnthropicToResponsesStreamAdapter(model="m", ctx={"tools": TOOLS})
        events = []
        events += ad.feed("content_block_start", {"index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_2", "name": alias, "input": {}}})
        events += ad.feed("content_block_delta", {"index": 0, "delta": {
            "type": "input_json_delta", "partial_json": "{}"}})
        events += ad.feed("content_block_stop", {"index": 0})
        return events

    def test_stream_added_and_done_carry_namespace(self):
        alias = namespace_tool_alias("multi_agent_v1", "close_agent")
        events = self._stream(alias)
        added = [e for e in events if e["type"] == "response.output_item.added"][0]["item"]
        done = [e for e in events if e["type"] == "response.output_item.done"][0]["item"]
        for item in (added, done):
            self.assertEqual(item["name"], "close_agent")
            self.assertEqual(item["namespace"], "multi_agent_v1")

    def test_stream_plain_tool_has_no_namespace(self):
        item = [e for e in self._stream("exec_command")
                if e["type"] == "response.output_item.done"][0]["item"]
        self.assertEqual(item["name"], "exec_command")
        self.assertNotIn("namespace", item)

    def test_stream_namespace_does_not_leak_to_next_item(self):
        ad = AnthropicToResponsesStreamAdapter(model="m", ctx={"tools": TOOLS})
        ad.feed("content_block_start", {"index": 0, "content_block": {
            "type": "tool_use", "id": "t1",
            "name": namespace_tool_alias("multi_agent_v1", "spawn_agent"), "input": {}}})
        ad.feed("content_block_stop", {"index": 0})
        events = ad.feed("content_block_start", {"index": 1, "content_block": {
            "type": "tool_use", "id": "t2", "name": "exec_command", "input": {}}})
        item = [e for e in events if e["type"] == "response.output_item.added"][0]["item"]
        self.assertEqual(item["name"], "exec_command")
        self.assertNotIn("namespace", item)


class NamespaceMapTests(unittest.TestCase):
    def test_maps_are_bijective_for_distinct_names(self):
        by_pair, by_alias = build_namespace_maps(TOOLS)
        self.assertEqual(len(by_pair), len(by_alias))
        for pair, alias in by_pair.items():
            self.assertEqual(by_alias[alias], pair)

    def test_alias_fits_openai_name_limit(self):
        long_ns = "n" * 40
        long_name = "t" * 40
        alias = namespace_tool_alias(long_ns, long_name)
        self.assertLessEqual(len(alias), 64)

    def test_alias_collision_with_plain_tool_is_resolved(self):
        clash = namespace_tool_alias("multi_agent_v1", "spawn_agent")
        tools = [
            {"type": "function", "name": clash, "parameters": {"type": "object"}},
            {"type": "namespace", "name": "multi_agent_v1", "tools": [SUB_TOOLS[0]]},
        ]
        by_pair, by_alias = build_namespace_maps(tools)
        alias = by_pair[("multi_agent_v1", "spawn_agent")]
        self.assertNotEqual(alias, clash)
        self.assertIn(alias, by_alias)

    def test_alias_is_deterministic(self):
        a = namespace_tool_alias("multi_agent_v1", "spawn_agent")
        b = namespace_tool_alias("multi_agent_v1", "spawn_agent")
        self.assertEqual(a, b)
        self.assertEqual(a, "multi_agent_v1__spawn_agent")

    def test_duplicate_names_across_namespaces_do_not_collide(self):
        tools = [
            {"type": "namespace", "name": "ns_a",
             "tools": [{"type": "function", "name": "run", "parameters": {"type": "object"}}]},
            {"type": "namespace", "name": "ns_b",
             "tools": [{"type": "function", "name": "run", "parameters": {"type": "object"}}]},
        ]
        _, by_alias = build_namespace_maps(tools)
        self.assertEqual(len(by_alias), 2)
        self.assertEqual(sorted(by_alias.values()), [("ns_a", "run"), ("ns_b", "run")])


if __name__ == "__main__":
    unittest.main()
