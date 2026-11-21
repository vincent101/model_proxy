package sankuai

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
)

const maxToolCorrections = 2

type unknownTools struct{ names []string }

func (e *unknownTools) Error() string { return "CatPaw 返回未声明或本轮不允许的工具" }

// 不执行未知调用，也不向客户端泄露它。整批返回错误后让模型重新选择，
// 避免同批合法调用在重试前产生副作用；保留文本流，仅将最终有效工具调用输出给客户端。
func recoverIDETools(ctx context.Context, initial *http.Response, model string, reg registry, payload Object, send func() (*http.Response, error), emit func(Object) error) (*adapter, error) {
	up := initial
	usage := Object{}
	recovered := []any{}
	carried := []any{}
	responseID := ""
	for round := 0; ; round++ {
		if err := ctx.Err(); err != nil {
			up.Body.Close()
			return nil, err
		}
		a := newAdapter(model, reg)
		if responseID == "" {
			responseID = a.id
			if err := emit(a.start()); err != nil {
				up.Body.Close()
				return nil, err
			}
		} else {
			a.id = responseID
		}
		emitFrame := func(f Object) error {
			if index, ok := f["index"].(int); ok {
				f["index"] = index + len(carried)
			}
			return emit(f)
		}
		size := 0
		err := readFrames(up.Body, func(v Object) error {
			frames, err := a.consume(v)
			if err != nil {
				return err
			}
			for _, f := range frames {
				size += len(encoded(f))
			}
			toolBytes := 0
			for _, call := range a.calls {
				f := obj(call["function"])
				toolBytes += len(str(f["name"])) + len(str(f["arguments"]))
			}
			if size+toolBytes > 16<<20 {
				return failure("CatPaw 单轮响应超过 16 MiB")
			}
			for _, f := range frames {
				if err := emitFrame(f); err != nil {
					return err
				}
			}
			if a.finished {
				return endFrames
			}
			return nil
		})
		up.Body.Close()
		if err != nil && !errors.Is(err, endFrames) {
			return nil, err
		}
		final, err := a.finish()
		for k, v := range a.usage {
			usage[k] = num(usage[k]) + num(v)
		}
		var unknown *unknownTools
		if errors.As(err, &unknown) {
			for _, name := range unknown.names {
				// 仅记录名称和关联 ID，不记录参数、正文、凭据。异常名称有界。
				safe := strings.Map(func(r rune) rune {
					if r < 32 || r == 127 {
						return '_'
					}
					return r
				}, name)
				if len(safe) > 256 {
					safe = safe[:256]
				}
				slog.WarnContext(ctx, "CatPaw 工具映射失败，反馈模型纠正", "module", "sankuai", "provider", "catpaw-ide", "model", model, "conversation_id", payload["conversationId"], "response_id", a.responseID, "suggest_uuid", a.suggest, "tool_name", safe, "reason", "not_allowed", "attempt", round+1, "retry", round < maxToolCorrections)
			}
			if round >= maxToolCorrections {
				return nil, &Error{Message: "CatPaw 工具纠正达到上限（2 次），模型仍返回未声明或本轮不允许的工具", Status: 502, Code: "tool_recovery_exhausted"}
			}
			closed := []Object{}
			a.close(&closed)
			for _, f := range closed {
				if err := emitFrame(f); err != nil {
					return nil, err
				}
			}
			carried = append(carried, a.content...)
			a.capture()
			feedback := append([]any{}, a.native...)
			for _, id := range a.order {
				f := obj(a.calls[id]["function"])
				msg := "Error calling tool: tool not found or not allowed this turn. Please check the declared tools and use the exact function.name."
				if reg.names[str(f["name"])] != "" {
					msg = "Tool was not executed because another call in this batch was invalid. Retry this call if still needed."
				}
				result := message("tool", []any{Object{"type": "text", "text": msg}})
				result["tool_call_id"], result["tool_call_name"] = id, f["name"]
				for _, raw := range a.native {
					m := obj(raw)
					for _, rawCall := range arr(m["tool_calls"]) {
						if obj(rawCall)["id"] == id {
							if m["suggestUuid"] != nil {
								result["suggestUuid"] = m["suggestUuid"]
							}
						}
					}
				}
				feedback = append(feedback, result)
			}
			recovered = append(recovered, feedback...)
			payload["messages"] = append(arr(payload["messages"]), feedback...)
			up, err = send()
			if err != nil {
				return nil, err
			}
			continue
		}
		if err != nil {
			return nil, err
		}
		a.usage = usage
		a.native = append(recovered, a.native...)
		// finish 的 message_delta 引用了本轮 usage；替换成包含纠正轮的总量。
		for _, f := range final {
			if f["type"] == "message_delta" {
				f["usage"] = usage
			}
		}
		for _, f := range final {
			if err := emitFrame(f); err != nil {
				return nil, fmt.Errorf("发送 CatPaw 响应: %w", err)
			}
		}
		a.content = append(carried, a.content...)
		return a, nil
	}
}

type ideCancelBody struct {
	io.ReadCloser
	cancel context.CancelFunc
}

func (b *ideCancelBody) Close() error { b.cancel(); return b.ReadCloser.Close() }
