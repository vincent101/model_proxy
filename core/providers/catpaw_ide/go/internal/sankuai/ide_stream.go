package sankuai

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/google/uuid"
)

type adapter struct {
	model, id                                  string
	reg                                        registry
	content                                    []any
	calls                                      map[string]Object
	order                                      []string
	usage                                      Object
	open                                       int
	text, thinking, phase, responseID, suggest string
	details                                    []any
	segmentCalls                               []string
	native                                     []any
	finished, completed                        bool
	reason                                     string
}

// emptyReasoningMarker is the literal that CatPaw IDE's DeepSeek V4.1
// Flash puts in reasoningContent when it has no visible thought. It is
// not a thought to show as an empty "thinking" block.
const emptyReasoningMarker = "空"

func visibleReasoning(v any) any {
	if s, ok := v.(string); ok && strings.TrimSpace(s) == emptyReasoningMarker {
		return nil
	}
	return v
}

func newAdapter(model string, r registry) *adapter {
	return &adapter{model: model, id: "msg_" + uuid.NewString(), reg: r, calls: map[string]Object{}, usage: Object{"input_tokens": 0, "output_tokens": 0}, open: -1, reason: "end_turn"}
}
func (a *adapter) start() Object {
	return event("message_start", Object{"message": Object{"id": a.id, "type": "message", "role": "assistant", "model": a.model, "content": []any{}, "stop_reason": nil, "stop_sequence": nil, "usage": Object{"input_tokens": 0, "output_tokens": 0}}})
}
func (a *adapter) close(out *[]Object) {
	if a.open < 0 {
		return
	}
	b := obj(a.content[a.open])
	if b["type"] == "thinking" && str(b["signature"]) != "" {
		*out = append(*out, event("content_block_delta", Object{"index": a.open, "delta": Object{"type": "signature_delta", "signature": b["signature"]}}))
	}
	*out = append(*out, event("content_block_stop", Object{"index": a.open}))
	a.open = -1
}
func (a *adapter) capture() {
	if a.text == "" && a.thinking == "" && len(a.segmentCalls) == 0 {
		return
	}
	bs := []any{}
	if a.thinking != "" {
		sig := ""
		if len(a.details) > 0 {
			sig = str(obj(a.details[0])["signature"])
		}
		bs = append(bs, Object{"type": "thinking", "thinking": a.thinking, "signature": sig})
	}
	if a.text != "" {
		bs = append(bs, Object{"type": "text", "text": a.text})
	}
	m := message("assistant", bs)
	for k, v := range map[string]string{"phase": a.phase, "responseId": a.responseID, "suggestUuid": a.suggest} {
		if v != "" {
			m[k] = v
		}
	}
	if len(a.details) > 0 {
		m["reasoningDetails"] = a.details
	}
	if len(a.segmentCalls) > 0 {
		calls := []any{}
		for _, id := range a.segmentCalls {
			calls = append(calls, a.calls[id])
		}
		m["tool_calls"] = calls
	}
	a.native = append(a.native, m)
}
func (a *adapter) append(kind string, value any, out *[]Object) error {
	if value == nil || value == "" {
		return nil
	}
	snapshot, ok := value.(string)
	if !ok {
		return failure("CatPaw 文本快照格式无效")
	}
	before := a.text
	if kind == "thinking" {
		before = a.thinking
		snapshot = before + snapshot
	}
	if !strings.HasPrefix(snapshot, before) {
		return failure("CatPaw 累积文本快照被重写，响应未完成")
	}
	delta := strings.TrimPrefix(snapshot, before)
	if delta == "" {
		return nil
	}
	if a.open < 0 || obj(a.content[a.open])["type"] != kind {
		a.close(out)
		if len(a.content) >= 256 {
			return failure("CatPaw 响应内容块超过 256 个")
		}
		a.open = len(a.content)
		a.content = append(a.content, Object{"type": kind, kind: ""})
		*out = append(*out, event("content_block_start", Object{"index": a.open, "content_block": Object{"type": kind, kind: ""}}))
	}
	b := obj(a.content[a.open])
	b[kind] = str(b[kind]) + delta
	*out = append(*out, event("content_block_delta", Object{"index": a.open, "delta": Object{"type": kind + "_delta", kind: delta}}))
	if kind == "thinking" {
		a.thinking = snapshot
	} else {
		a.text = snapshot
	}
	return nil
}
func checkFrame(v Object) error {
	code := num(first(v["statusCode"], v["code"]))
	switch code {
	case 401, 403, 30002, 50102:
		return login("CatPaw IDE")
	case 429:
		return &Error{Message: "CatPaw 请求频率超过限制", Status: 429, Code: "rate_limit_error"}
	case 510, 528:
		return &Error{Message: "CatPaw 个人账户额度已用尽", Status: 429, Code: "insufficient_quota"}
	case 529:
		return &Error{Message: "CatPaw 模型供应商暂时不可用", Status: 503, Code: "overloaded_error"}
	}
	if code != 0 || v["error"] != nil || v["success"] == false {
		return failure(fmt.Sprintf("CatPaw 模型服务返回错误（code=%v）", code))
	}
	return nil
}
func (a *adapter) consume(v Object) ([]Object, error) {
	if a.finished {
		return nil, nil
	}
	if e := checkFrame(v); e != nil {
		return nil, e
	}
	out := []Object{}
	choice := Object{}
	if xs := arr(v["choices"]); len(xs) > 0 {
		choice = obj(xs[0])
	}
	delta := obj(choice["delta"])
	phase := str(first(delta["phase"], v["phase"]))
	rid := str(v["responseId"])
	if phase != "" && a.phase != "" && phase != a.phase || rid != "" && a.responseID != "" && rid != a.responseID {
		a.close(&out)
		a.capture()
		a.text = ""
		a.thinking = ""
		a.details = nil
		a.segmentCalls = nil
	}
	if phase != "" {
		a.phase = phase
	}
	if rid != "" {
		a.responseID = rid
	}
	if s := str(v["suggestUuid"]); s != "" {
		a.suggest = s
	}
	if e := a.append("thinking", visibleReasoning(delta["reasoningContent"]), &out); e != nil {
		return nil, e
	}
	if d := delta["reasoningDetails"]; d != nil {
		details, ok := d.([]any)
		if !ok {
			return nil, failure("CatPaw 思考详情格式无效")
		}
		a.details = details
		if len(details) > 0 {
			first := obj(details[0])
			if first == nil {
				return nil, failure("CatPaw 思考详情格式无效")
			}
			if a.thinking != "" {
				key := "thinking"
				if first["text"] != nil && first["thinking"] == nil {
					key = "text"
				}
				first[key] = a.thinking
			}
			if sig := str(first["signature"]); sig != "" && a.open >= 0 && obj(a.content[a.open])["type"] == "thinking" {
				obj(a.content[a.open])["signature"] = sig
			}
		}
	}
	if v["content"] != "[DONE]" {
		if e := a.append("text", v["content"], &out); e != nil {
			return nil, e
		}
	}
	if v["toolCalls"] != nil {
		raws, ok := v["toolCalls"].([]any)
		if !ok {
			return nil, failure("CatPaw 工具快照格式无效")
		}
		seen := map[string]bool{}
		for _, raw := range raws {
			m := obj(raw)
			id := str(m["id"])
			f := obj(m["function"])
			if id == "" || f == nil || seen[id] {
				return nil, failure("CatPaw 工具快照格式无效")
			}
			seen[id] = true
			call := a.calls[id]
			if call == nil {
				if len(a.calls) >= 128 {
					return nil, failure("CatPaw 工具调用超过 128 个")
				}
				call = Object{"id": id, "type": "function", "function": Object{"name": "", "arguments": ""}}
				a.calls[id] = call
				a.order = append(a.order, id)
				a.segmentCalls = append(a.segmentCalls, id)
			}
			for _, key := range []string{"name", "arguments"} {
				if v := f[key]; v != nil && v != "" {
					next, ok := v.(string)
					if !ok || !strings.HasPrefix(next, str(obj(call["function"])[key])) || len(next) > 4<<20 {
						return nil, failure("CatPaw 工具名称或参数快照被重写")
					}
					obj(call["function"])[key] = next
				}
			}
		}
	}
	if usage := obj(v["usage"]); usage != nil {
		// IDE 的 prompt_tokens 已排除缓存，与其原生客户端保持同一口径。
		// SSE 后续帧可以只补部分计数；未返回的字段保留上次结果。
		for k, value := range (Object{
			"input_tokens":                usage["prompt_tokens"],
			"output_tokens":               usage["completion_tokens"],
			"cache_read_input_tokens":     first(usage["cacheReadTokens"], obj(usage["prompt_tokens_details"])["cached_tokens"], usage["cache_read_tokens"]),
			"cache_creation_input_tokens": first(usage["cacheWriteTokens"], usage["cache_write_tokens"]),
		}) {
			if value == nil {
				continue
			}
			n := num(value)
			if n < 0 {
				n = 0
			}
			a.usage[k] = n
		}
	}
	reason := str(first(choice["finishReason"], choice["finish_reason"]))
	switch reason {
	case "":
	case "stop", "tool_calls", "function_call", "end_turn":
		a.finished = true
	case "length", "max_tokens":
		a.finished = true
		a.reason = "max_tokens"
	case "content_filter", "audit reject":
		return nil, &Error{Message: "CatPaw 输出被内容审核中止", Status: 400, Code: "content_filter"}
	default:
		return nil, failure("CatPaw 返回未知结束原因")
	}
	a.finished = a.finished || v["lastOne"] == true && v["content"] == "[DONE]"
	return out, nil
}
func (a *adapter) finish() ([]Object, error) {
	if !a.finished {
		return nil, failure("CatPaw 流结束但未收到完成标志")
	}
	if a.reason == "max_tokens" && len(a.calls) > 0 {
		return nil, failure("CatPaw 工具调用因输出长度限制而中止")
	}
	unknown := []string{}
	for _, id := range a.order {
		name := str(obj(a.calls[id]["function"])["name"])
		if a.reg.names[name] == "" {
			unknown = append(unknown, name)
		}
	}
	if len(unknown) > 0 {
		return nil, &unknownTools{names: unknown}
	}
	if a.reg.required && len(a.calls) == 0 || !a.reg.parallel && len(a.calls) > 1 {
		return nil, failure("CatPaw 未满足工具选择要求")
	}
	tools := []Object{}
	for _, id := range a.order {
		call := a.calls[id]
		f := obj(call["function"])
		name := a.reg.names[str(f["name"])]

		s := str(f["arguments"])
		if s == "" {
			s = "{}"
		}
		var input Object
		if json.Unmarshal([]byte(s), &input) != nil || input == nil {
			return nil, failure("CatPaw 工具参数不是完整 JSON 对象")
		}
		f["arguments"] = string(encoded(input))
		tools = append(tools, Object{"type": "tool_use", "id": id, "name": name, "input": input})
	}
	if len(a.content) == 0 && len(tools) == 0 {
		return nil, failure("CatPaw 返回空响应")
	}
	out := []Object{}
	a.close(&out)
	for _, tool := range tools {
		i := len(a.content)
		a.content = append(a.content, tool)
		start := Object{"type": "tool_use", "id": tool["id"], "name": tool["name"], "input": Object{}}
		out = append(out, event("content_block_start", Object{"index": i, "content_block": start}), event("content_block_delta", Object{"index": i, "delta": Object{"type": "input_json_delta", "partial_json": string(encoded(tool["input"]))}}), event("content_block_stop", Object{"index": i}))
	}
	if len(tools) > 0 {
		a.reason = "tool_use"
	}
	a.capture()
	a.completed = true
	out = append(out, event("message_delta", Object{"delta": Object{"stop_reason": a.reason, "stop_sequence": nil}, "usage": a.usage}), event("message_stop", nil))
	return out, nil
}
func (a *adapter) result() Object {
	return Object{"id": a.id, "type": "message", "role": "assistant", "model": a.model, "content": a.content, "stop_reason": a.reason, "stop_sequence": nil, "usage": a.usage}
}
func (a *adapter) history() Object {
	bs := []any{}
	for _, raw := range a.content {
		b := obj(raw)
		if b["type"] == "tool_use" {
			continue
		}
		if b["type"] == "thinking" && b["signature"] == nil {
			b["signature"] = ""
		}
		bs = append(bs, b)
	}
	m := message("assistant", bs)
	if len(a.order) > 0 {
		calls := []any{}
		for _, id := range a.order {
			call := a.calls[id]
			f := obj(call["function"])
			calls = append(calls, Object{"id": id, "type": "function", "function": Object{"name": alias(a.reg.names[str(f["name"])]), "arguments": f["arguments"]}})
		}
		m["tool_calls"] = calls
	}
	return m
}
