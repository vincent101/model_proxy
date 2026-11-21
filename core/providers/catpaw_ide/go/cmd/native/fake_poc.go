//go:build pocfake

package main

// POC_FAKE=1 时把上游换成确定性假服务，仅用于离线回归；正式构建不带此标签。
import (
	"io"
	"net/http"
	"os"
	"strings"

	"catboss/bridge"
)

func init() {
	if os.Getenv("POC_FAKE") != "1" {
		return
	}
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
