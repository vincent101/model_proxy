package sankuai

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
)

// 别名保留可读名称；清理或截短时附加摘要，避免不同原名合并。
func alias(name string) string {
	clean := strings.Map(func(r rune) rune {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-' {
			return r
		}
		return '_'
	}, name)
	if clean != name || len(clean) > 47 {
		sum := sha256.Sum256([]byte(name))
		if len(clean) > 30 {
			clean = clean[:30]
		}
		clean += "_" + hex.EncodeToString(sum[:8])
	}
	return "mcp_tool_catboss_" + clean
}
func legacyAlias(name string) string {
	sum := sha256.Sum256([]byte(name))
	return "mcp_tool_magpie_" + hex.EncodeToString(sum[:12])
}
func blocks(v any) ([]any, error) {
	if s, ok := v.(string); ok {
		return []any{Object{"type": "text", "text": s}}, nil
	}
	if a, ok := v.([]any); ok {
		return a, nil
	}
	if v == nil {
		return nil, nil
	}
	return nil, invalid("消息内容必须是字符串或数组")
}

// memberEfforts are all levels an upstream model may list; "ultra" is
// Codex's mode, not a generic fallback.
var memberEfforts = []string{"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}

// CatPaw IDE uses this fallback when a tool result has no text block.
const noTextToolOutput = "(tool returned non-text content)"

// conversationHeader carries the client conversation the gateway derived
// (internal/gateway's conversationID): the agent's own session id, else a
// digest of the conversation's first user message.
const conversationHeader = "X-Catboss-Conversation"

// ideConversationNamespace keeps derived CatPaw conversation ids stable across
// processes and accounts. The model type joins the name so one conversation
// switching models does not continue the other model's session.
var ideConversationNamespace = uuid.MustParse("8f2a6c1d-3b4e-5a7c-9d0e-1f2a3b4c5d6e")

func ideConversationID(conversation string, model int) string {
	if conversation == "" {
		return ""
	}
	return uuid.NewSHA1(ideConversationNamespace, []byte(conversation+"\x00"+strconv.Itoa(model))).String()
}

func message(role string, parts []any) Object {
	texts := []string{}
	for _, p := range parts {
		m := obj(p)
		if m["type"] == "text" {
			texts = append(texts, str(m["text"]))
		}
	}
	if len(parts) == 0 {
		parts = []any{Object{"type": "text", "text": ""}}
	}
	content := strings.Join(texts, "\n")
	if role == "tool" && content == "" {
		content = noTextToolOutput
		filtered := []any{Object{"type": "text", "text": noTextToolOutput}}
		for _, part := range parts {
			m := obj(part)
			if m["type"] != "text" || str(m["text"]) != "" {
				filtered = append(filtered, part)
			}
		}
		parts = filtered
	}
	return Object{"role": role, "content": content, "multiModalContent": parts, "triggerMode": "AGENT"}
}

type registry struct {
	names              map[string]string
	definitions        []any
	required, parallel bool
}

func toolRegistry(body Object) (registry, error) {
	r := registry{names: map[string]string{}, parallel: true}
	choice := obj(body["tool_choice"])
	kind := str(choice["type"])
	r.required = kind == "any" || kind == "tool"
	r.parallel = choice["disable_parallel_tool_use"] != true
	for _, raw := range arr(body["tools"]) {
		t := obj(raw)
		name := str(t["name"])
		if name == "" || obj(t["input_schema"]) == nil {
			return r, invalid("CatPaw IDE 需要函数工具名称与 input_schema")
		}
		if kind == "none" || kind == "tool" && choice["name"] != name {
			continue
		}
		a := alias(name)
		if _, ok := r.names[a]; ok {
			return r, invalid("工具名称重复")
		}
		r.names[a] = name
		for _, spelling := range []string{name, legacyAlias(name)} {
			if old := r.names[spelling]; old != "" && old != name {
				return r, invalid("工具名称映射冲突")
			}
			r.names[spelling] = name
		}
		r.definitions = append(r.definitions, Object{"type": "function", "function": Object{"name": a, "description": str(t["description"]), "parameters": t["input_schema"]}})
	}
	if r.required && len(r.names) == 0 {
		return r, invalid("指定工具不存在")
	}
	return r, nil
}
func (r registry) config(model int) Object {
	tools := []any{}
	for _, name := range ideTools {
		t := Object{"toolUseName": name, "enable": false}
		if name == "use_mcp_tool" {
			t["enable"] = len(r.names) > 0
			t["mcpTools"] = r.definitions
		}
		tools = append(tools, t)
	}
	return Object{"id": "sankuai-providers", "name": "catboss", "type": "CUSTOM_AGENT", "const": Object{"trigger": "AGENT", "applyTrigger": "AGENT_APPLY"}, "model": Object{"default": model, "maxMode": false, "autoMode": false}, "tools": tools, "defaultDocs": []any{}, "subAgents": []any{}}
}
func parameters(model Model, body Object) Object {
	p := Object{}
	allowed := map[string]map[string]bool{}
	for _, raw := range model.Parameters {
		d := obj(raw)
		id := str(d["id"])
		values := map[string]bool{}
		for _, v := range arr(d["values"]) {
			values[text(obj(v)["value"])] = true
		}
		allowed[id] = values
		if d["defaultValue"] != nil && values[text(d["defaultValue"])] {
			p[id] = text(d["defaultValue"])
		}
	}
	delete(p, "context")
	if n := maxIDEContext(model.Parameters); n > 0 {
		p["context"] = strconv.Itoa(n)
	}
	thinking := obj(body["thinking"])
	effort := str(obj(body["output_config"])["effort"])
	off := effort == "none" || thinking["type"] == "disabled"
	if off {
		if allowed["thinking"]["false"] || !allowed["effort"]["none"] {
			p["thinking"] = "false"
			delete(p, "effort")
		} else {
			p["effort"] = "none"
		}
	} else if effort != "" || thinking["type"] == "enabled" || thinking["type"] == "adaptive" {
		if allowed["thinking"]["true"] || thinking != nil {
			p["thinking"] = "true"
		}
		if effort == "" && thinking["budget_tokens"] != nil {
			n := num(thinking["budget_tokens"])
			if n <= 1024 {
				effort = "low"
			} else if n <= 8192 {
				effort = "medium"
			} else {
				effort = "high"
			}
		}
		if effort != "" {
			p["effort"] = effort
		}
	}
	for _, raw := range model.Constraints {
		r := obj(raw)
		if r["effectKind"] != "NOT_APPLICABLE" || obj(r["when"]) == nil {
			continue
		}
		matched := true
		for k, v := range obj(r["when"]) {
			found := false
			for _, s := range arr(v) {
				found = found || text(s) == str(p[k])
			}
			matched = matched && found
		}
		if matched {
			delete(p, str(r["target"]))
		}
	}
	return p
}

// 原生思考详情与阶段字段仅在当前进程短期保存，按登录身份和模型隔离。
var ideHistory = struct {
	sync.Mutex
	entries map[string]historyEntry
}{entries: map[string]historyEntry{}}

type historyEntry struct {
	at       time.Time
	messages []any
}

func historyKey(identity string, model int, m any) string {
	sum := sha256.Sum256(append([]byte(fmt.Sprintf("%s/%d/", identity, model)), encoded(m)...))
	return hex.EncodeToString(sum[:])
}
func historyGet(key string) []any {
	ideHistory.Lock()
	defer ideHistory.Unlock()
	e := ideHistory.entries[key]
	if time.Since(e.at) > 4*time.Hour {
		return nil
	}
	var v []any
	_ = json.Unmarshal(encoded(e.messages), &v)
	return v
}
func historyPut(key string, m []any) {
	ideHistory.Lock()
	defer ideHistory.Unlock()
	for k, e := range ideHistory.entries {
		if time.Since(e.at) > 4*time.Hour {
			delete(ideHistory.entries, k)
		}
	}
	if len(ideHistory.entries) >= 32 {
		var old string
		var at time.Time
		for k, e := range ideHistory.entries {
			if old == "" || e.at.Before(at) {
				old, at = k, e.at
			}
		}
		delete(ideHistory.entries, old)
	}
	ideHistory.entries[key] = historyEntry{time.Now(), m}
}
func (c *Client) ideMessages(ctx context.Context, body, cred Object) ([]any, error) {
	messages := []any{}
	calls := map[string]string{}
	pending := map[string]bool{}
	characters := 0
	for _, raw := range arr(body["messages"]) {
		m := obj(raw)
		role := str(m["role"])
		if role != "user" && role != "assistant" {
			return nil, invalid("消息角色无效")
		}
		bs, e := blocks(m["content"])
		if e != nil {
			return nil, e
		}
		ordinary, tools := []any{}, []any{}
		media := func(b Object) (Object, error) {
			switch b["type"] {
			case "text", "thinking":
				return b, nil
			case "image":
				u, e := c.image(ctx, b, cred)
				return Object{"type": "image_url", "image_url": Object{"url": u, "detail": "low"}}, e
			case "document":
				s, e := c.document(ctx, b)
				characters += len([]rune(s))
				if characters > 500000 {
					return nil, invalid("文件文字总计超过 500000 字符")
				}
				return Object{"type": "text", "text": s}, e
			}
			return nil, invalid("不支持的消息内容类型")
		}
		for _, raw := range bs {
			b := obj(raw)
			switch b["type"] {
			case "tool_use":
				id, name := str(b["id"]), str(b["name"])
				if id == "" || name == "" || role != "assistant" || calls[id] != "" || obj(b["input"]) == nil {
					return nil, invalid("工具调用格式无效")
				}
				calls[id] = name
				pending[id] = true
				tools = append(tools, Object{"id": id, "type": "function", "function": Object{"name": alias(name), "arguments": string(encoded(b["input"]))}})
			case "tool_result":
				id := str(b["tool_use_id"])
				if role != "user" || !pending[id] {
					return nil, invalid("工具结果缺少匹配调用")
				}
				delete(pending, id)
				parts := []any{}
				content, e := blocks(b["content"])
				if e != nil {
					return nil, e
				}
				if b["is_error"] == true {
					parts = append(parts, Object{"type": "text", "text": "Error: the caller reported that this tool execution failed."})
				}
				for _, raw := range content {
					part, e := media(obj(raw))
					if e != nil {
						return nil, e
					}
					parts = append(parts, part)
				}
				tm := message("tool", parts)
				tm["tool_call_id"] = id
				tm["tool_call_name"] = alias(calls[id])
				messages = append(messages, tm)
			default:
				part, e := media(b)
				if e != nil {
					return nil, e
				}
				ordinary = append(ordinary, part)
			}
		}
		if len(ordinary)+len(tools) > 0 {
			m := message(role, ordinary)
			if len(tools) > 0 {
				m["tool_calls"] = tools
			}
			messages = append(messages, m)
		}
	}
	if len(pending) > 0 {
		return nil, invalid("工具调用缺少结果")
	}
	if len(messages) == 0 {
		return nil, invalid("消息不能为空")
	}
	return messages, nil
}
func (c *Client) ide(ctx context.Context, body Object, conversation string) (*http.Response, error) {
	if format := obj(obj(body["output_config"])["format"]); len(format) > 0 && format["type"] != "text" {
		return nil, invalid("CatPaw IDE 没有已验证的结构化输出参数")
	}
	cred, e := c.IDECredential()
	if e != nil {
		return nil, e
	}
	models, e := c.cachedIDEModels(ctx)
	if e != nil {
		return nil, e
	}
	var model Model
	for _, m := range models {
		if m.ID == str(body["model"]) {
			model = m
			break
		}
	}
	if model.ID == "" {
		return nil, invalid("当前账号目录中没有该模型，请刷新模型列表")
	}
	reg, e := toolRegistry(body)
	if e != nil {
		return nil, e
	}
	messages, e := c.ideMessages(ctx, body, cred)
	if e != nil {
		return nil, e
	}
	prefix := make([]string, 0, len(messages)+1)
	for _, m := range messages {
		prefix = append(prefix, historyKey("", 0, m))
	}
	expanded := []any{}
	origins := map[string]string{}
	callNames := map[string]string{}
	for _, m := range messages {
		native := historyGet(historyKey(str(cred["ssoid"]), model.Type, m))
		if len(native) == 0 {
			native = []any{m}
		}
		expanded = append(expanded, native...)
	}
	messages = expanded
	for _, raw := range messages {
		m := obj(raw)
		for _, raw := range arr(m["tool_calls"]) {
			callNames[str(obj(raw)["id"])] = str(obj(obj(raw)["function"])["name"])
			if s := str(m["suggestUuid"]); s != "" {
				origins[str(obj(raw)["id"])] = s
			}
		}
		if name := callNames[str(m["tool_call_id"])]; name != "" {
			m["tool_call_name"] = name
		}
		if s := origins[str(m["tool_call_id"])]; s != "" {
			m["suggestUuid"] = s
		}
	}
	rules, err := blocks(body["system"])
	if err != nil {
		return nil, err
	}
	if reg.required {
		rules = append(rules, Object{"type": "text", "text": "This turn must call one of the declared function tools."})
	}
	if !reg.parallel {
		rules = append(rules, Object{"type": "text", "text": "Return at most one tool call this turn."})
	}
	if len(rules) > 0 {
		messages = append([]any{message("system", rules)}, messages...)
	}
	scope := historyKey(str(cred["ssoid"]), model.Type, Object{"rules": rules, "config": reg.config(model.Type), "params": parameters(model, body)})
	cid := ideConversationID(conversation, model.Type)
	if cid == "" {
		cid = sessionTake(scope, prefix)
	}
	payload := Object{"conversationId": cid, "messages": messages, "userModelTypeCode": model.Type, "triggerMode": "AGENT", "agentModeConfig": reg.config(model.Type), "userModelParams": parameters(model, body)}
	h := ideHeaders(cred)
	h.Set("Accept", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("UI-Version", "0.2.5")
	callerContext := ctx
	ctx, cancel := context.WithCancel(ctx)
	send := func() (*http.Response, error) {
		up, err := c.modelRequest(ctx, "catpaw-ide", model.ID, cid, "https://catpaw.sankuai.com/api/gpt/openai/stream", h, encoded(payload))
		up, err = nativeResponse(up, err, "CatPaw IDE")
		if err != nil {
			return nil, err
		}
		if up.Header.Get("encrypted-key") != "" {
			up.Body.Close()
			return nil, failure("当前内部 SSO 适配器不支持外部租户加密响应")
		}
		return up, nil
	}
	up, e := send()
	if e != nil {
		cancel()
		return nil, e
	}
	var adapter *adapter
	run := func(emit func(Object) error) error {
		defer cancel()
		var err error
		// 先保存续轮所需的原生历史，再公布完成，避免紧接着的工具结果抢先到达。
		terminal := []Object{}
		adapter, err = recoverIDETools(ctx, up, model.ID, reg, payload, send, func(frame Object) error {
			if frame["type"] == "message_delta" || frame["type"] == "message_stop" {
				terminal = append(terminal, frame)
				return nil
			}
			return emit(frame)
		})
		if err != nil {
			return err
		}
		historyPut(historyKey(str(cred["ssoid"]), model.Type, adapter.history()), adapter.native)
		if conversation == "" && len(prefix) < 4096 {
			sessionPut(scope, cid, append(prefix, historyKey("", 0, adapter.history())))
		}
		for _, frame := range terminal {
			if err := emit(frame); err != nil {
				return err
			}
		}
		return nil
	}
	if body["stream"] != true {
		defer up.Body.Close()
		if e := run(func(Object) error { return nil }); e != nil {
			return nil, e
		}
		return response(200, "application/json", encoded(adapter.result())), nil
	}
	res := stream(callerContext, up, run)
	res.Body = &ideCancelBody{ReadCloser: res.Body, cancel: cancel}
	return res, nil
}
