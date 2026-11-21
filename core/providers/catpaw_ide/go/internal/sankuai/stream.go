package sankuai

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

var endFrames = errors.New("native stream finished")

// readFrames 支持 CR、LF 和 CRLF；空行分帧，不拆分 JSON 中的 Unicode 分隔符。
func newFrameScanner(r io.Reader) *bufio.Scanner {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 4096), 8<<20)
	afterCR := false
	scanner.Split(func(data []byte, eof bool) (int, []byte, error) {
		if afterCR && len(data) > 0 {
			afterCR = false
			if data[0] == '\n' {
				return 1, nil, nil
			}
		}
		for i, b := range data {
			if b == '\n' || b == '\r' {
				afterCR = b == '\r'
				return i + 1, data[:i], nil
			}
		}
		if eof && len(data) > 0 {
			return len(data), data, nil
		}
		return 0, nil, nil
	})
	return scanner
}

// nil 帧代表 [DONE]，io.EOF 代表传输结束。
func nextFrame(scanner *bufio.Scanner) (Object, error) {
	var data []string
	complete := false
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" && len(data) > 0 {
			complete = true
			break
		}
		if strings.HasPrefix(line, "data:") {
			data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	if err := scanner.Err(); !complete && err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return nil, io.EOF
	}
	raw := strings.Join(data, "\n")
	if raw == "[DONE]" {
		return nil, nil
	}
	var frame Object
	if !utf8.ValidString(raw) || json.Unmarshal([]byte(raw), &frame) != nil || frame == nil {
		return nil, failure("上游 SSE data 不是有效 JSON 对象")
	}
	return frame, nil
}

func readFrames(r io.Reader, consume func(Object) error, onDone ...func() error) error {
	scanner := newFrameScanner(r)
	for {
		frame, err := nextFrame(scanner)
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if frame != nil {
			err = consume(frame)
		} else if len(onDone) > 0 {
			err = onDone[0]()
		}
		if err != nil {
			return err
		}
	}
}
func wire(v Object) []byte {
	if str(v["type"]) == "" {
		return append(append([]byte("data: "), encoded(v)...), []byte("\n\n")...)
	}
	return append(append([]byte("event: "+str(v["type"])+"\ndata: "), encoded(v)...), []byte("\n\n")...)
}
func event(typ string, v Object) Object {
	if v == nil {
		v = Object{}
	}
	v["type"] = typ
	return v
}
func stream(ctx context.Context, up *http.Response, run func(func(Object) error) error) *http.Response {
	reader, writer := io.Pipe()
	go func() {
		defer up.Body.Close()
		defer writer.Close()
		emit := func(v Object) error { _, e := writer.Write(wire(v)); return e }
		if e := run(emit); e != nil {
			if ctx.Err() != nil {
				_ = writer.CloseWithError(ctx.Err())
				return
			}
			res := ErrorResponse(e)
			raw, _ := io.ReadAll(res.Body)
			var v Object
			_ = json.Unmarshal(raw, &v)
			_ = emit(v)
		}
	}()
	return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}, "Cache-Control": {"no-cache"}}, Body: &streamBody{ReadCloser: reader, up: up.Body}, ContentLength: -1}
}

type streamBody struct {
	io.ReadCloser
	up io.ReadCloser
}

func (s *streamBody) Close() error { _ = s.up.Close(); return s.ReadCloser.Close() }

const (
	cliRouteRefusalPrefix = "本次请求未命中任何路由策略，且当前场景未配置兜底模型。"
	cliHourLimitPrefix    = "当前小时请求过于频繁"
	cliDayLimitPrefix     = "今日请求过于频繁"
)

var (
	cliTextPrefixes = [...]string{cliRouteRefusalPrefix, cliHourLimitPrefix, cliDayLimitPrefix}
	cliTextHold     = func() int {
		n := 0
		for _, prefix := range cliTextPrefixes {
			if len(prefix) > n {
				n = len(prefix)
			}
		}
		return n
	}()
	// CatPaw CLI's limits are counted on the internal service's Beijing day.
	cliQuotaZone = time.FixedZone("UTC+8", 8*60*60)
)

func cliModelUnavailable() error {
	return &Error{Message: "当前模型在 CatPaw CLI 下不可用，请切换模型或联系管理员检查模型权限和路由配置", Status: 503, Code: "model_unavailable"}
}

type cliLimit int

const (
	cliHourLimit cliLimit = iota
	cliDayLimit
)

func (k cliLimit) errorAt(now time.Time) error {
	local := now.In(cliQuotaZone)
	until := time.Date(local.Year(), local.Month(), local.Day()+1, 0, 0, 0, 0, cliQuotaZone)
	message := "CatPaw CLI 今日请求过于频繁，请明日再试"
	if k == cliHourLimit {
		until = time.Date(local.Year(), local.Month(), local.Day(), local.Hour()+1, 0, 0, 0, cliQuotaZone)
		message = "CatPaw CLI 当前小时请求过于频繁，请下个整点重试"
	}
	seconds := int64((until.Sub(now) + time.Second - 1) / time.Second)
	if seconds < 1 {
		seconds = 1
	}
	return &Error{Message: message, Status: http.StatusTooManyRequests, Code: "rate_limit_error", ResetsIn: seconds}
}

func cliTextError(text string) error {
	switch {
	case strings.HasPrefix(text, cliRouteRefusalPrefix):
		return cliModelUnavailable()
	case strings.HasPrefix(text, cliHourLimitPrefix):
		return cliHourLimit.errorAt(time.Now())
	case strings.HasPrefix(text, cliDayLimitPrefix):
		return cliDayLimit.errorAt(time.Now())
	}
	return nil
}

func cliTextPending(text string) bool {
	for _, prefix := range cliTextPrefixes {
		if strings.HasPrefix(prefix, text) {
			return true
		}
	}
	return false
}

// visibleThinking keeps a signed thinking block while replacing CatPaw's
// empty reasoning marker with a blank value. Dropping the block would drop
// the signature a later tool turn may need.
func visibleThinking(v any) any {
	if s, ok := v.(string); ok && strings.TrimSpace(s) == emptyReasoningMarker {
		return " "
	}
	return v
}

// 仅暂存尚可能命中路由拒绝或限额提示的正文，避免错误文本被标题生成等调用方消费。
// 一旦正文偏离全部前缀，后续事件立即透传；跨块拼接不改变判断。
type cliTextGuard struct {
	text    string
	pending []Object
	decided bool
}

func (g *cliTextGuard) flush(emit func(Object) error) error {
	for _, v := range g.pending {
		if e := emit(v); e != nil {
			return e
		}
	}
	g.pending = nil
	return nil
}

func (g *cliTextGuard) emit(v Object, emit func(Object) error) error {
	if g.decided {
		return emit(v)
	}
	text, isText := "", false
	if v["type"] == "content_block_start" && obj(v["content_block"])["type"] == "text" {
		text, isText = str(obj(v["content_block"])["text"]), true
	} else if v["type"] == "content_block_delta" && obj(v["delta"])["type"] == "text_delta" {
		text, isText = str(obj(v["delta"])["text"]), true
	}
	if !isText && len(g.pending) == 0 {
		return emit(v)
	}
	g.pending = append(g.pending, v)
	remaining := cliTextHold - len(g.text)
	if remaining < 0 {
		remaining = 0
	}
	if len(text) > remaining {
		text = text[:remaining]
	}
	g.text += text
	if e := cliTextError(g.text); e != nil {
		return e
	}
	if !cliTextPending(g.text) {
		g.decided = true
		return g.flush(emit)
	}
	return nil
}

func validateCLI(ctx context.Context, res *http.Response, body Object, buffered bool) (*http.Response, error) {
	names := map[string]bool{}
	for _, t := range arr(body["tools"]) {
		names[str(obj(t)["name"])] = true
	}
	calls := map[string]bool{}
	checkReason := func(reason string) error {
		if !slices.Contains([]string{"end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal"}, reason) || (len(calls) > 0) != (reason == "tool_use") {
			return failure("CatPaw CLI 结束原因与工具调用不匹配")
		}
		return nil
	}
	check := func(v Object) error {
		if v["type"] == "tool_use" && (!names[str(v["name"])] || str(v["id"]) == "" || calls[str(v["id"])]) {
			return failure("CatPaw CLI 返回了调用方未声明或重复的工具")
		}
		if v["type"] == "tool_use" {
			calls[str(v["id"])] = true
		}
		return nil
	}
	if !strings.Contains(res.Header.Get("Content-Type"), "text/event-stream") {
		defer res.Body.Close()
		raw, e := limited(res.Body, 32<<20)
		if e != nil {
			return nil, e
		}
		var v Object
		if json.Unmarshal(raw, &v) != nil {
			return nil, failure("CatPaw CLI 响应未完成")
		}
		if v["model"] == "error" {
			var detail cliErrorText
			detail.content(v)
			return nil, detail.failure()
		}
		if v["type"] == "error" {
			return nil, cliStreamError(v)
		}
		if v["type"] != "message" || v["stop_reason"] == nil || len(arr(v["content"])) == 0 {
			return nil, failure("CatPaw CLI 响应未完成")
		}
		var text strings.Builder
		changed := false
		for _, b := range arr(v["content"]) {
			if obj(b)["type"] == "thinking" {
				if s, ok := obj(b)["thinking"].(string); ok && strings.TrimSpace(s) == emptyReasoningMarker {
					obj(b)["thinking"] = " "
					changed = true
				}
			}
			if obj(b)["type"] == "text" {
				text.WriteString(str(obj(b)["text"]))
			}
			if e := check(obj(b)); e != nil {
				return nil, e
			}
		}
		if e := cliTextError(text.String()); e != nil {
			return nil, e
		}
		if e := checkReason(str(v["stop_reason"])); e != nil {
			return nil, e
		}
		if changed {
			raw = encoded(v)
		}
		return response(200, "application/json", raw), nil
	}
	const bufferLimit = 32 << 20
	if buffered {
		res.Body = http.MaxBytesReader(nil, res.Body, bufferLimit)
	}
	scanner := newFrameScanner(res.Body)
	next := func() (Object, error) {
		for {
			frame, err := nextFrame(scanner)
			if err != nil {
				return nil, err
			}
			if frame == nil {
				continue
			}
			if frame["type"] == "message_start" && obj(frame["message"])["model"] == "error" {
				return nil, readCLIError(ctx, res.Body, scanner, obj(frame["message"]))
			}
			return frame, nil
		}
	}
	// 交给宿主之前先检查首条消息。期间保活由网关发送。
	first, err := next()
	for err == nil && first["type"] == "ping" {
		first, err = next()
	}
	if err != nil {
		res.Body.Close()
		if err == io.EOF {
			err = failure("CatPaw CLI 流结束但未收到完成标志")
		}
		return nil, err
	}
	validate := func(emit func(Object) error) error {
		guard := &cliTextGuard{}
		started, stopped := false, false
		reason := ""
		open := map[float64]Object{}
		terminal := []Object{}
		blocks := 0
		consume := func(v Object) error {
			typ := str(v["type"])
			if typ == "error" {
				return cliStreamError(v)
			}
			if stopped {
				return failure("CatPaw CLI 响应流中止")
			}
			switch typ {
			case "ping":
				return emit(v)
			case "message_start":
				m := obj(v["message"])
				if started || m["type"] != "message" || m["role"] != "assistant" || len(arr(m["content"])) > 0 {
					return failure("无效或重复 message_start")
				}
				started = true
			case "content_block_start":
				if !started || len(open) > 0 || reason != "" || num(v["index"]) != float64(blocks) {
					return failure("CatPaw CLI 内容块顺序无效")
				}
				blocks++
				b := obj(v["content_block"])
				if e := check(b); e != nil {
					return e
				}
				open[num(v["index"])] = Object{"block": b, "partial": ""}
			case "content_block_delta":
				b := open[num(v["index"])]
				if b == nil {
					return failure("CatPaw CLI 内容增量缺少起始块")
				}
				d := obj(v["delta"])
				field := map[string]string{"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "signature", "input_json_delta": "partial_json"}[str(d["type"])]
				_, valid := d[field].(string)
				kind := obj(b["block"])["type"]
				if !valid || field == "" || field == "text" && kind != "text" || (field == "thinking" || field == "signature") && kind != "thinking" || field == "partial_json" && kind != "tool_use" {
					return failure("CatPaw CLI 内容增量格式无效")
				}
				if d["type"] == "input_json_delta" {
					s := str(b["partial"]) + str(d["partial_json"])
					if len(s) > 4<<20 {
						return failure("工具参数超过 4 MiB")
					}
					b["partial"] = s
				}
				if d["type"] == "thinking_delta" {
					d["thinking"] = visibleThinking(d["thinking"])
				}
			case "content_block_stop":
				b := open[num(v["index"])]
				if b == nil {
					return failure("内容块未开始")
				}
				if obj(b["block"])["type"] == "tool_use" && str(b["partial"]) != "" {
					var input Object
					if json.Unmarshal([]byte(str(b["partial"])), &input) != nil || input == nil {
						return failure("工具参数不是完整 JSON 对象")
					}
				}
				delete(open, num(v["index"]))
			case "message_delta":
				if !started || reason != "" {
					return failure("CatPaw CLI 重复或缺失起始状态")
				}
				reason = str(obj(v["delta"])["stop_reason"])
				if e := checkReason(reason); e != nil {
					return e
				}
				if len(open) > 0 || reason == "" {
					return failure("CatPaw CLI 结束状态无效")
				}
				terminal = append(terminal, v)
				return nil
			case "message_stop":
				if reason == "" || blocks == 0 {
					return failure("CatPaw CLI 响应未完成")
				}
				stopped = true
				terminal = append(terminal, v)
				return nil
			default:
				return failure("CatPaw CLI 返回未知流事件")
			}
			return guard.emit(v, emit)
		}
		frame := first
		for {
			if err := consume(frame); err != nil {
				return err
			}
			var err error
			frame, err = next()
			if err == io.EOF {
				break
			}
			if err != nil {
				return err
			}
		}
		if !stopped {
			return failure("CatPaw CLI 流结束但未收到完成标志")
		}
		if e := guard.flush(emit); e != nil {
			return e
		}
		for _, v := range terminal {
			if e := emit(v); e != nil {
				return e
			}
		}
		return nil
	}
	if !buffered {
		return stream(ctx, res, validate), nil
	}
	// 只在整轮通过同一个流校验器后交付 SSE。失败直接返回给路由，保留原始状态和退避策略。
	defer res.Body.Close()
	var held bytes.Buffer
	err = validate(func(frame Object) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if frame["type"] == "ping" {
			return nil
		}
		data := wire(frame)
		if len(data) > bufferLimit-held.Len() {
			return failure("CatPaw CLI 缓冲回复超过 32 MiB")
		}
		_, err := held.Write(data)
		return err
	})
	var tooLarge *http.MaxBytesError
	if errors.As(err, &tooLarge) {
		return nil, failure("CatPaw CLI 缓冲回复超过 32 MiB")
	}
	if err != nil {
		return nil, err
	}
	return response(200, "text/event-stream", held.Bytes()), nil
}

// CLI only accepts streaming upstream requests. Non-streaming callers receive a
// message only after the validator has observed the complete terminal sequence.
func collectCLI(res *http.Response) (*http.Response, error) {
	defer res.Body.Close()
	result := Object{}
	content := []any{}
	partial := map[int]string{}
	stopped := false
	e := readFrames(io.LimitReader(res.Body, 32<<20), func(v Object) error {
		i := int(num(v["index"]))
		switch v["type"] {
		case "error":
			// 此处消费的是本适配器已校验并编码的错误。
			if detail := obj(v["error"]); detail["code"] == "catpaw_model_error" {
				var diagnostic cliErrorText
				diagnostic.add(str(detail["message"]))
				return diagnostic.failure()
			}
			return cliStreamError(v)
		case "message_start":
			result = obj(v["message"])
		case "content_block_start":
			content = append(content, obj(v["content_block"]))
		case "content_block_delta":
			if i < 0 || i >= len(content) {
				return failure("内容块索引无效")
			}
			b, d := obj(content[i]), obj(v["delta"])
			switch d["type"] {
			case "text_delta":
				b["text"] = str(b["text"]) + str(d["text"])
			case "thinking_delta":
				b["thinking"] = str(b["thinking"]) + str(visibleThinking(d["thinking"]))
			case "signature_delta":
				b["signature"] = str(b["signature"]) + str(d["signature"])
			case "input_json_delta":
				partial[i] += str(d["partial_json"])
			}
		case "content_block_stop":
			if partial[i] != "" {
				var input Object
				if json.Unmarshal([]byte(partial[i]), &input) != nil {
					return failure("工具参数无效")
				}
				obj(content[i])["input"] = input
			}
		case "message_delta":
			for k, value := range obj(v["delta"]) {
				result[k] = value
			}
			if u := obj(v["usage"]); u != nil {
				old := obj(result["usage"])
				if old == nil {
					old = Object{}
				}
				for k, value := range u {
					old[k] = value
				}
				result["usage"] = old
			}
		case "message_stop":
			stopped = true
		}
		return nil
	})
	if e != nil {
		return nil, e
	}
	if !stopped || result == nil {
		return nil, failure("CatPaw CLI 响应未完成")
	}
	result["content"] = content
	return response(200, "application/json", encoded(result)), nil
}

func cliStreamError(v Object) error {
	switch str(obj(v["error"])["type"]) {
	case "model_unavailable":
		return cliModelUnavailable()
	case "authentication_error", "permission_error":
		return login("CatPaw CLI")
	case "rate_limit_error":
		return &Error{Message: "CatPaw CLI 请求频率超过限制", Status: 429, Code: "rate_limit_error"}
	case "overloaded_error":
		return &Error{Message: "CatPaw CLI 模型暂时不可用", Status: 503, Code: "overloaded_error"}
	}
	return failure("CatPaw CLI 响应流中止")
}
