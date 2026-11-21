package bridge

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

const catalogJSON = `{"data":[{"modelTypeName":"gpt-6.2-sol","modelType":7,"supportAgent":true,"supportThinking":true,"supportImage":false}]}`

func ideStream() string {
	frames := []string{
		`data: {"content":"Hello"}`,
		`data: {"content":"Hello CatPaw","usage":{"prompt_tokens":11,"completion_tokens":3}}`,
		`data: {"content":"[DONE]","lastOne":true,"choices":[{"delta":{},"finishReason":"stop"}]}`,
	}
	return strings.Join(frames, "\n\n") + "\n\n"
}

type fake struct {
	got  map[string]any
	hits int
}

func (f *fake) RoundTrip(r *http.Request) (*http.Response, error) {
	f.hits++
	switch {
	case strings.Contains(r.URL.Path, "/websso/token/get"):
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}},
			Body: io.NopCloser(strings.NewReader(`{"code":0,"data":{"access_token":"at2","refresh_token":"rt2","token_exchange_attestation":"a2"}}`))}, nil
	case strings.Contains(r.URL.Path, "/websso/whoami"):
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}},
			Body: io.NopCloser(strings.NewReader(`{"code":0,"data":{"subject":"mis1","introspectionResultEnum":"SUCCESS"}}`))}, nil
	case strings.Contains(r.URL.Path, "get-user-available-models"):
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}},
			Body: io.NopCloser(strings.NewReader(catalogJSON))}, nil
	case strings.Contains(r.URL.Path, "model-types"):
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}},
			Body: io.NopCloser(strings.NewReader(`{"data":[]}`))}, nil
	case strings.Contains(r.URL.Path, "/stream"):
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &f.got)
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}},
			Body: io.NopCloser(strings.NewReader(ideStream()))}, nil
	}
	return &http.Response{StatusCode: 404, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
}

func authJSON() json.RawMessage {
	return json.RawMessage(`{"key":"","metadata":{"oidc":{"id":"u1","accessToken":"at","refreshToken":"rt","attestation":"a","mis":"mis1"}}}`)
}

func run(t *testing.T, f *fake, stream bool) (string, map[string]any) {
	t.Helper()
	TransportFactory = func() *http.Client { return &http.Client{Transport: f} }
	body, _ := json.Marshal(map[string]any{
		"model": "gpt-6.2-sol", "max_tokens": 64, "stream": stream,
		"messages": []any{map[string]any{"role": "user", "content": "hi"}},
	})
	h, err := Start(Request{Auth: authJSON(), Body: body, Conversation: "conv-1"})
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer Close(h)
	var sb strings.Builder
	buf := make([]byte, 64)
	for {
		n, err := Next(h, buf)
		sb.Write(buf[:n])
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("Next: %v", err)
		}
	}
	return sb.String(), f.got
}

func TestAnthropicStreamShape(t *testing.T) {
	f := &fake{}
	out, sent := run(t, f, true)
	for _, want := range []string{"event: message_start", "event: content_block_delta", "Hello", " CatPaw",
		"event: message_delta", "event: message_stop", `"stop_reason":"end_turn"`, `"input_tokens":11`, `"output_tokens":3`} {
		if !strings.Contains(out, want) {
			t.Fatalf("缺少 %q\n---\n%s", want, out)
		}
	}
	if sent["conversationId"] == "" || sent["userModelTypeCode"] != float64(7) {
		t.Fatalf("IDE 请求体不对: %v", sent)
	}
	if sent["triggerMode"] != "AGENT" {
		t.Fatalf("triggerMode 丢失: %v", sent)
	}
	if f.hits < 3 {
		t.Fatalf("实际调用次数 %d，目录与生成未走真实入口", f.hits)
	}
	if strings.Contains(out, `"input_tokens":0,"output_tokens":0`) == false {
		t.Logf("message_start 使用占位 usage（Catboss 既有行为）")
	}
}

func TestNonStreamResult(t *testing.T) {
	out, _ := run(t, &fake{}, false)
	var msg map[string]any
	if err := json.Unmarshal([]byte(out), &msg); err != nil {
		t.Fatalf("非流式应返回单个 JSON: %v\n%s", err, out)
	}
	content, _ := msg["content"].([]any)
	if len(content) == 0 || content[0].(map[string]any)["text"] != "Hello CatPaw" {
		t.Fatalf("正文被拆分或丢失: %v", msg)
	}
	if msg["stop_reason"] != "end_turn" {
		t.Fatalf("stop_reason 不对: %v", msg["stop_reason"])
	}
}

func TestUpstreamErrorIsNotSuccess(t *testing.T) {
	TransportFactory = func() *http.Client {
		return &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if strings.Contains(r.URL.Path, "get-user-available-models") {
				return &http.Response{StatusCode: 401, Header: http.Header{},
					Body: io.NopCloser(strings.NewReader(`{"type":"error"}`))}, nil
			}
			return &http.Response{StatusCode: 500, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
		})}
	}
	body, _ := json.Marshal(map[string]any{"model": "gpt-6.2-sol", "stream": true,
		"messages": []any{map[string]any{"role": "user", "content": "hi"}}})
	if _, err := Start(Request{Auth: authJSON(), Body: body}); err == nil {
		t.Fatal("鉴权失败必须返回错误，不能静默成功")
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestCancelReleases(t *testing.T) {
	pr, pw := io.Pipe()
	TransportFactory = func() *http.Client {
		return &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			switch {
			case strings.Contains(r.URL.Path, "/websso/token/get"):
				return &http.Response{StatusCode: 200, Header: http.Header{},
					Body: io.NopCloser(strings.NewReader(`{"code":0,"data":{"access_token":"at2","refresh_token":"rt2","token_exchange_attestation":"a2"}}`))}, nil
			case strings.Contains(r.URL.Path, "/websso/whoami"):
				return &http.Response{StatusCode: 200, Header: http.Header{},
					Body: io.NopCloser(strings.NewReader(`{"code":0,"data":{"subject":"mis1","introspectionResultEnum":"SUCCESS"}}`))}, nil
			case strings.Contains(r.URL.Path, "get-user-available-models"):
				return &http.Response{StatusCode: 200, Header: http.Header{},
					Body: io.NopCloser(strings.NewReader(catalogJSON))}, nil
			case strings.Contains(r.URL.Path, "model-types"):
				return &http.Response{StatusCode: 200, Header: http.Header{},
					Body: io.NopCloser(strings.NewReader(`{"data":[]}`))}, nil
			}
			return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}}, Body: pr}, nil
		})}
	}
	body, _ := json.Marshal(map[string]any{"model": "gpt-6.2-sol", "stream": true,
		"messages": []any{map[string]any{"role": "user", "content": "hi"}}})
	h, err := Start(Request{Auth: authJSON(), Body: body})
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	// 单一消费者按字节读取；io.Pipe 无缓冲，必须与写出并发。
	saw := make(chan struct{}, 4)
	done := make(chan error, 1)
	go func() {
		buf := make([]byte, 256)
		first := true
		for {
			n, err := Next(h, buf)
			if n > 0 && first {
				first = false
				saw <- struct{}{}
			}
			if err != nil {
				done <- err
				return
			}
		}
	}()
	go func() { _, _ = pw.Write([]byte("data: {\"content\":\"a\"}\n\n")) }()
	select {
	case <-saw:
	case <-time.After(5 * time.Second):
		t.Fatal("首帧未在 5s 内产出")
	}
	Cancel(h)
	select {
	case err := <-done:
		if err == nil || err == io.EOF {
			t.Fatalf("取消后应返回传输错误而不是正常结束: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("取消未唤醒阻塞中的读取")
	}
	regMu.Lock()
	_, still := reg[h]
	regMu.Unlock()
	if !still {
		t.Fatal("Cancel 不应回收句柄")
	}
	Close(h)
	Close(h)
	if _, err := Next(h, make([]byte, 8)); err == nil {
		t.Fatal("关闭后句柄不可再用")
	}
	_ = pw.Close()
}
