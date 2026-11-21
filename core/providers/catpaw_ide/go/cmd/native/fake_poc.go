//go:build pocfake

package main

// 带 pocfake 标签的构建一律使用确定性假上游，**永不触网**。
// 构建标签本身就是开关：正式构建不含本文件。去掉环境变量门是刻意的——
// 若依赖 POC_FAKE 环境变量，误漏该变量会让假 token 打到真实上游。
import (
	"io"
	"net/http"
	"strings"

	"catboss/bridge"
)

func init() {
	bridge.TransportFactory = func() *http.Client { return &http.Client{Transport: fakeTransport{}} }
}

type fakeTransport struct{}

func (fakeTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	json200 := func(s string) *http.Response {
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}},
			Body: io.NopCloser(strings.NewReader(s))}
	}
	switch {
	case strings.Contains(r.URL.Path, "/websso/token/get"):
		return json200(`{"code":0,"data":{"access_token":"at2","refresh_token":"rt2","token_exchange_attestation":"a2"}}`), nil
	case strings.Contains(r.URL.Path, "/websso/whoami"):
		return json200(`{"code":0,"data":{"subject":"mis1","introspectionResultEnum":"SUCCESS"}}`), nil
	case strings.Contains(r.URL.Path, "get-user-available-models"):
		return json200(`{"data":[{"modelTypeName":"gpt-6.2-sol","modelType":7,"supportAgent":true,"supportThinking":true}]}`), nil
	case strings.Contains(r.URL.Path, "model-types"):
		return json200(`{"data":[]}`), nil
	case strings.Contains(r.URL.Path, "/stream"):
		sse := "data: {\"content\":\"Hello\"}\n\ndata: {\"content\":\"Hello CatPaw\",\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":3}}\n\ndata: {\"content\":\"[DONE]\",\"lastOne\":true,\"choices\":[{\"delta\":{},\"finishReason\":\"stop\"}]}\n\n"
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}},
			Body: io.NopCloser(strings.NewReader(sse))}, nil
	}
	return &http.Response{StatusCode: 404, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
}
