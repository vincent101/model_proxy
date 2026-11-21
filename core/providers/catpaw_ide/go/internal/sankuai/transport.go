package sankuai

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"catboss/internal/lazyre"
)

func ErrorResponse(err error) *http.Response {
	e, ok := err.(*Error)
	if !ok {
		e = &Error{Message: "内部供应商请求失败，请检查登录态和内网连接", Status: 502, Code: "upstream_error"}
	}
	detail := Object{"type": e.Code, "message": e.Message}
	if e.modelError {
		detail["code"] = "catpaw_model_error"
	}
	if e.ResetsIn > 0 {
		detail["resets_in_seconds"] = e.ResetsIn
	}
	return response(e.Status, "application/json", encoded(Object{"type": "error", "error": detail}))
}
func response(status int, typ string, b []byte) *http.Response {
	return &http.Response{StatusCode: status, Header: http.Header{"Content-Type": {typ}}, Body: io.NopCloser(bytes.NewReader(b)), ContentLength: int64(len(b))}
}

func deferredToolBlock(typ string) bool {
	return typ == "tool_addition" || typ == "tool_removal" || typ == "tool_reference"
}

// normalizeDeferredTools removes Claude Code's deferred-tool history before
// CatPaw sees it. A definition carried only by tool_addition joins the
// request's tools once by name; an existing top-level definition wins.
func normalizeDeferredTools(body Object) {
	messages, ok := body["messages"].([]any)
	if !ok {
		return
	}
	tools := append([]any(nil), arr(body["tools"])...)
	names := map[string]bool{}
	for _, raw := range tools {
		if name := str(obj(raw)["name"]); name != "" {
			names[name] = true
		}
	}
	keptMessages := make([]any, 0, len(messages))
	changed := false
	for _, raw := range messages {
		m := obj(raw)
		content, ok := m["content"].([]any)
		if !ok {
			keptMessages = append(keptMessages, raw)
			continue
		}
		kept := make([]any, 0, len(content))
		for _, rawBlock := range content {
			block := obj(rawBlock)
			typ := str(block["type"])
			if !deferredToolBlock(typ) {
				kept = append(kept, rawBlock)
				continue
			}
			changed = true
			if typ != "tool_addition" {
				continue
			}
			tool := obj(block["tool"])
			if tool["type"] != "tool_definition" {
				continue
			}
			definition := obj(tool["definition"])
			name := str(definition["name"])
			if name != "" && !names[name] {
				tools = append(tools, definition)
				names[name] = true
			}
		}
		if len(kept) == 0 {
			continue
		}
		next := m
		if len(kept) != len(content) {
			next = make(Object, len(m))
			for key, value := range m {
				next[key] = value
			}
			next["content"] = kept
		}
		keptMessages = append(keptMessages, next)
	}
	if !changed {
		return
	}
	body["messages"] = keptMessages
	if len(tools) > 0 {
		body["tools"] = tools
	}
}

func NormalizeSystem(body Object) error {
	normalizeDeferredTools(body)
	blocks := func(v any) ([]any, error) {
		if v == nil {
			return nil, nil
		}
		if s, ok := v.(string); ok {
			return []any{Object{"type": "text", "text": s}}, nil
		}
		if a, ok := v.([]any); ok {
			for _, b := range a {
				block := obj(b)
				if block["type"] != "text" {
					return nil, invalid("系统指令仅支持文本")
				}
				if _, ok := block["text"].(string); !ok {
					return nil, invalid("系统指令仅支持文本")
				}
			}
			return a, nil
		}
		return nil, invalid("系统指令格式无效")
	}
	system, e := blocks(body["system"])
	if e != nil {
		return e
	}
	messages, ok := body["messages"].([]any)
	if !ok {
		return invalid("缺少 messages")
	}
	kept := []any{}
	changed := false
	for _, raw := range messages {
		m := obj(raw)
		if m["role"] == "system" {
			b, e := blocks(m["content"])
			if e != nil {
				return e
			}
			system = append(system, b...)
			changed = true
		} else {
			kept = append(kept, m)
		}
	}
	if changed {
		body["system"] = system
		body["messages"] = kept
	}
	return nil
}
func (c *Client) RoundTrip(req *http.Request, id string, a Auth) (*http.Response, error) {
	ready, err := c.resolveCatpaw(req.Context(), id, a, false)
	if err != nil {
		return ErrorResponse(err), nil
	}
	raw, err := limited(req.Body, 64<<20)
	if req.Body != nil {
		_ = req.Body.Close()
	}
	if err != nil {
		return ErrorResponse(err), nil
	}
	send := func(auth Auth) (*http.Response, error) {
		copy := req.Clone(req.Context())
		copy.Body = io.NopCloser(bytes.NewReader(raw))
		return c.withAuth(auth).forward(copy, id, auth)
	}
	res, err := send(ready)
	if e, ok := err.(*Error); ok && e.Status == 401 && ready.Metadata.OIDC != nil && !strings.Contains(e.Message, "个人 Friday AppId") {
		ready, err = c.resolveCatpaw(req.Context(), id, ready, true)
		if err == nil {
			res, err = send(ready)
		}
	}
	if err != nil {
		if req.Context().Err() != nil {
			return nil, req.Context().Err()
		}
		return ErrorResponse(err), nil
	}
	return res, nil
}
func (c *Client) forward(req *http.Request, id string, a Auth) (*http.Response, error) {
	raw, e := limited(req.Body, 64<<20)
	if e != nil {
		return nil, e
	}
	_ = req.Body.Close()
	ctx := req.Context()
	if strings.HasPrefix(id, "friday-") {
		path := req.URL.Path
		target := ""
		switch {
		case strings.HasSuffix(path, "/chat/completions"):
			target = "https://aigc.sankuai.com/v1/openai/native/chat/completions"
		case strings.HasSuffix(path, "/responses"):
			target = "https://aigc.sankuai.com/v1/responses"
		case strings.HasSuffix(path, "/messages/count_tokens"):
			target = "https://aigc.sankuai.com/v1/anthropic/v1/messages/count_tokens"
		case strings.HasSuffix(path, "/messages"):
			target = "https://aigc.sankuai.com/v1/anthropic/v1/messages"
		}
		if target == "" {
			return nil, invalid("Friday 不支持此接口")
		}
		if !validSecret(a.Key) {
			return nil, login(Name(id))
		}
		h := http.Header{"Authorization": {"Bearer " + a.Key}, "Content-Type": {"application/json"}, "Anthropic-Version": {"2023-06-01"}}
		var b Object
		if json.Unmarshal(raw, &b) != nil {
			return nil, invalid("请求不是有效 JSON")
		}
		model := str(b["model"])
		limit := 0
		for _, m := range a.Models {
			if m.ID == model {
				limit = m.RPM
				break
			}
		}
		if e := ctx.Err(); e != nil {
			return nil, e
		}
		state := fridayRPMs.state(a.Key, model, limit, time.Now(), true)
		if state.Until != nil {
			res := ErrorResponse(&Error{Message: state.Message(), Status: 429, Code: "rate_limit_error"})
			res.Header.Set("Retry-After", state.RetryAfter())
			return res, nil
		}
		res, e := c.do(ctx, "POST", target, h, raw)
		if e == nil && res.StatusCode == 429 {
			fridayRPMs.cool(a.Key, model, time.Now(), res.Header.Get("Retry-After"))
			// 保留既有错误解释（例如每日额度耗尽），冷却是附加约束。
			_, reason := nativeResponse(res, nil, Name(id))
			state := FridayState(a.Key, model, limit)
			reply := ErrorResponse(reason)
			reply.Header.Set("Retry-After", state.RetryAfter())
			return reply, nil
		}
		return fridayResponse(ctx, res, e, a.Key, model, Name(id))
	}
	var body Object
	if json.Unmarshal(raw, &body) != nil || body == nil {
		return nil, invalid("请求不是有效 JSON")
	}
	if e = NormalizeSystem(body); e != nil {
		return nil, e
	}
	// CatPaw 没有原生计数接口；未知端点不能落到生成接口。
	if (id == "catpaw-ide" || id == "catpaw-cli") && !strings.HasSuffix(req.URL.Path, "/messages") {
		return nil, &Error{Message: Name(id) + " 不支持此接口", Status: http.StatusNotFound, Code: "not_found_error"}
	}
	if id == "catpaw-ide" {
		return c.ide(ctx, body, req.Header.Get(conversationHeader))
	}
	if id != "catpaw-cli" || !strings.HasSuffix(req.URL.Path, "/messages") {
		return nil, invalid("不支持此接口")
	}
	token, e := c.CLIToken()
	if e != nil {
		return nil, e
	}
	dir := c.Home
	if s := req.Header.Get("X-Catboss-Work-Dir"); s != "" {
		dir, e = url.PathUnescape(s)
		if e != nil || !filepath.IsAbs(dir) {
			return nil, invalid("CatPaw CLI 工作目录无效")
		}
	}
	h, e := cliHeaders(token, dir, time.Now(), cliVersions.get(ctx))
	if e != nil {
		return nil, e
	}
	if e = prepareCLI(body); e != nil {
		return nil, e
	}
	session, e := prepareCLISession(body, req.Header, a.Metadata.OIDC.ID)
	if e != nil {
		return nil, e
	}
	h.Set("X-Claude-Code-Session-Id", session)
	if e = c.cliImages(ctx, body, token); e != nil {
		return nil, e
	}
	streaming := body["stream"] == true
	body["stream"] = true
	res, e := c.modelRequest(ctx, id, str(body["model"]), session, "https://mcli.sankuai.com/v1/messages?beta=true", h, encoded(body))
	res, e = nativeResponse(res, e, "CatPaw CLI")
	if e != nil {
		return nil, e
	}
	bufferTools := streaming && c.BufferGeminiTools && len(arr(body["tools"])) > 0 && isGeminiModel(str(body["model"]))
	res, e = validateCLI(ctx, res, body, bufferTools)
	if e != nil || streaming || !strings.Contains(res.Header.Get("Content-Type"), "text/event-stream") {
		return res, e
	}
	return collectCLI(res)
}
func nativeResponse(res *http.Response, err error, name string) (*http.Response, error) {
	if err != nil {
		return nil, err
	}
	if res.StatusCode/100 == 2 {
		return res, nil
	}
	defer res.Body.Close()
	if res.StatusCode == 401 || res.StatusCode == 403 {
		return nil, login(name)
	}
	raw, _ := limited(res.Body, 65536)
	var v Object
	_ = json.Unmarshal(raw, &v)
	if num(v["code"]) == 400305 {
		return nil, &Error{Message: name + " 额度已用尽", Status: res.StatusCode, Code: "insufficient_quota"}
	}
	if res.StatusCode == 429 && strings.HasPrefix(name, "Friday") {
		return nil, &Error{Message: name + " " + fridayRateReason(v) + "，请稍后重试", Status: 429, Code: "rate_limit_error"}
	}
	if isContextOverflow(res.StatusCode, raw) {
		return nil, &Error{Message: "上下文过长，请缩短历史或开启新对话", Status: res.StatusCode, Code: "context_length_exceeded"}
	}
	return nil, &Error{Message: fmt.Sprintf("%s 请求失败（HTTP %d）", name, res.StatusCode), Status: res.StatusCode, Code: "upstream_error"}
}

var (
	contextOverflowError = lazyre.New(`(?i)context_length_exceeded|context[_ ]window_exceeded|model_context_window_exceeded|maximum context length|prompt (?:is )?too long|input (?:is )?too long|too many (?:input |prompt )?tokens|exceeded model token limit|context (?:length|limit|window) (?:exceeded|is exceeded)|maximum prompt length is \d|reduce the length of the messages|longer than the model'?s context length|available context size|greater than the context length|prompt token count of [\d,]+ exceeds the limit|range of input length should be|token limit exceeded|input tokens? (?:count )?(?:exceeds?|exceeded) (?:the )?(?:max(?:imum)? )?limit|上下文(?:长度)?(?:超|过长)|超(?:过|出)(?:了)?(?:模型)?(?:的)?(?:最大)?上下文`)
	maxOutputTokenError  = lazyre.New(`(?i)max_(?:output_|completion_)?tokens`)
	rateLimitError       = lazyre.New(`(?i)rate.?limit|per (minute|hour|day)|\bTP[MD]\b|please wait|try again later|throttl`)
)

// isContextOverflow recognizes a native provider's explicit context refusal
// without passing its body, which may echo user content, to the client.
func isContextOverflow(status int, raw []byte) bool {
	if status != http.StatusBadRequest && status != http.StatusRequestEntityTooLarge && status != http.StatusUnprocessableEntity {
		return false
	}
	text := string(raw)
	var v any
	if json.Unmarshal(raw, &v) == nil {
		var parts []string
		collectStrings(v, 0, &parts)
		text += "\n" + strings.Join(parts, "\n")
	}
	// A reply-limit or rate-limit refusal is not a conversation overflow.
	return !maxOutputTokenError.MatchString(text) && !rateLimitError.MatchString(text) && contextOverflowError.MatchString(text)
}

func collectStrings(v any, depth int, out *[]string) {
	if depth > 16 || len(*out) >= 64 {
		return
	}
	switch x := v.(type) {
	case string:
		*out = append(*out, x)
	case []any:
		for _, item := range x {
			collectStrings(item, depth+1, out)
		}
	case map[string]any:
		for _, item := range x {
			collectStrings(item, depth+1, out)
		}
	}
}

func cliHeaders(token, dir string, now time.Time, version string) (http.Header, error) {
	if !validSecret(token) || !filepath.IsAbs(dir) || !validSecret(dir) {
		return nil, invalid("CatPaw CLI 凭据或工作目录无效")
	}
	key := sha256.Sum256([]byte(token))
	block, _ := aes.NewCipher(key[:])
	gcm, _ := cipher.NewGCM(block)
	encrypt := func(v string) string {
		nonce := make([]byte, gcm.NonceSize())
		_, _ = rand.Read(nonce)
		return base64.StdEncoding.EncodeToString(gcm.Seal(nonce, nonce, []byte(v), nil))
	}
	stamp := strconv.FormatInt(now.Unix(), 10)
	h := http.Header{}
	for k, v := range map[string]string{"Authorization": "Bearer " + token, "X-Working-Dir": strings.ReplaceAll(url.QueryEscape(dir), "+", "%20"), "X-Client-Version": version, "X-Timestamp": encrypt(stamp), "X-Client-Token": encrypt(stamp + "|" + dir), "Content-Type": "application/json", "Accept": "text/event-stream", "anthropic-version": "2023-06-01", "User-Agent": "claude-cli/2.1.268 (external, sdk-cli)", "x-app": "cli", "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14,effort-2025-11-24"} {
		h.Set(k, v)
	}
	return h, nil
}

var documentTextFile = lazyre.New(`(?i)\.(txt|md|json|csv|log|yaml|yml|xml)$`)
var cliIdentity = lazyre.New(`(?i)^\s*You are\s+opencode\b`)
var cliMarketing = lazyre.New(`(?i)best coding agent on the planet`)

func prepareCLI(body Object) error {
	system := arr(body["system"])
	if s, ok := body["system"].(string); ok {
		system = []any{Object{"type": "text", "text": s}}
	}
	identity := "You are a Claude agent, built on Anthropic's Claude Agent SDK."
	found := false
	for _, b := range system {
		m := obj(b)
		if m["type"] != "text" {
			return invalid("CatPaw CLI system 仅支持文本")
		}
		s := cliMarketing.ReplaceAllString(cliIdentity.ReplaceAllString(str(m["text"]), "You are Claude Code"), "best coding assistant")
		m["text"] = s
		found = found || strings.Contains(s, identity) || strings.Contains(s, "You are Claude Code")
	}
	if !found {
		system = append([]any{Object{"type": "text", "text": identity}}, system...)
	}
	body["system"] = system
	var normalize func(any) error
	normalize = func(v any) error {
		for _, raw := range arr(v) {
			b := obj(raw)
			if b["type"] == "tool_result" {
				if e := normalize(b["content"]); e != nil {
					return e
				}
			}
			if b["type"] != "document" {
				continue
			}
			source := obj(b["source"])
			if source["type"] != "base64" {
				continue
			}
			raw := str(source["data"])
			if len(raw) > mediaMax*4/3+4 {
				return invalid("文件超过 20 MiB")
			}
			data, e := base64.StdEncoding.Strict().DecodeString(raw)
			if e != nil {
				return invalid("文件 Base64 无效")
			}
			if source["media_type"] == "text/plain" || source["media_type"] == "application/octet-stream" && documentTextFile.MatchString(str(b["title"])) {
				if !utf8.Valid(data) {
					return invalid("文本文件不是有效 UTF-8")
				}
				b["source"] = Object{"type": "text", "media_type": "text/plain", "data": string(data)}
			} else if source["media_type"] != "application/pdf" {
				return invalid("CatPaw CLI 文件支持 PDF 或 UTF-8 文本")
			}
		}
		return nil
	}
	for _, m := range arr(body["messages"]) {
		if e := normalize(obj(m)["content"]); e != nil {
			return e
		}
	}
	return nil
}
