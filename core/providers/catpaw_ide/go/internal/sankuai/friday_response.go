package sankuai

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
)

// 只检查错误对象；模型输出中引用限流文字不能触发冷却。
func fridayRateError(v Object) Object {
	for _, e := range []Object{obj(v["error"]), obj(obj(v["response"])["error"])} {
		for _, code := range []string{str(e["code"]), str(e["type"])} {
			if code == "rate_limit_exceeded" || code == "rate_limit_error" || code == "request_rate_limited" {
				return e
			}
		}
	}
	return nil
}

func fridayResponse(ctx context.Context, res *http.Response, err error, app, model, name string) (*http.Response, error) {
	if err != nil || res.StatusCode == 401 || res.StatusCode == 403 {
		return nativeResponse(res, err, name)
	}
	cool := func() { fridayRPMs.cool(app, model, time.Now(), res.Header.Get("Retry-After")) }
	if res.StatusCode/100 == 2 && strings.Contains(res.Header.Get("Content-Type"), "text/event-stream") {
		var done atomic.Bool
		out := stream(ctx, res, func(emit func(Object) error) error {
			e := readFrames(res.Body, func(v Object) error {
				if rate := fridayRateError(v); rate != nil {
					cool()
					// 保留协议、错误码与响应身份；补充类型和说明，让旧 Magpie
					// 宿主也能把中文错误正确识别为限流，而非通用 502。
					rate["type"], rate["message"] = "rate_limit_error", "Friday "+fridayRateReason(rate)+"（rate_limit_exceeded），当前 APPID/模型已冷却，请稍后重试"
					if e := emit(v); e != nil {
						return e
					}
					return endFrames
				}
				return emit(v)
			}, func() error { done.Store(true); return endFrames })
			if errors.Is(e, endFrames) {
				return nil
			}
			return e
		})
		out.Header = res.Header.Clone()
		out.Header.Del("Content-Length")
		out.Body = &fridayDoneBody{ReadCloser: out.Body, done: &done}
		return out, nil
	}
	// 非流式 Responses 也可能用 HTTP 200 携带 status:failed。
	raw, e := limited(res.Body, 64<<20)
	res.Body.Close()
	if e != nil {
		return nil, e
	}
	var v Object
	if json.Unmarshal(raw, &v) == nil && fridayRateError(v) != nil {
		cool()
		out := ErrorResponse(&Error{Message: "Friday " + fridayRateReason(fridayRateError(v)) + "（rate_limit_exceeded），当前 APPID/模型已冷却，请稍后重试", Status: 429, Code: "rate_limit_error"})
		out.Header.Set("Retry-After", FridayState(app, model, 0).RetryAfter())
		return out, nil
	}
	res.Body = io.NopCloser(bytes.NewReader(raw))
	return nativeResponse(res, nil, name)
}

// 仅在上游明确发出结束标志时保留它，截断或错误流不补造完成。
type fridayDoneBody struct {
	io.ReadCloser
	done *atomic.Bool
	tail *strings.Reader
}

func (b *fridayDoneBody) Read(p []byte) (int, error) {
	if b.tail != nil {
		return b.tail.Read(p)
	}
	n, err := b.ReadCloser.Read(p)
	if err == io.EOF && b.done.Load() {
		b.tail = strings.NewReader("data: [DONE]\n\n")
		if n > 0 {
			return n, nil
		}
		return b.tail.Read(p)
	}
	return n, err
}

func fridayRateReason(v Object) string {
	message := strings.ToLower(str(first(v["message"], obj(v["error"])["message"], v["msg"])))
	switch {
	case strings.Contains(message, "tpm"):
		return "每分钟 Token 数（TPM）超过限制"
	case strings.Contains(message, "rpm"), strings.Contains(message, "每分钟请求次数"):
		return "每分钟请求次数（RPM）超过限制"
	case strings.Contains(message, "并发"), strings.Contains(message, "concurren"):
		return "并发请求数超过限制"
	}
	return "请求频率超过限制"
}
