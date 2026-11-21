package sankuai

import (
	"bytes"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

const tCatalog = `{"data":[{"modelTypeName":"gpt-6.2-sol","modelType":7,"supportAgent":true,"supportThinking":true}]}`

const tStream = "data: {\"content\":\"hi\"}\n\ndata: {\"content\":\"[DONE]\",\"lastOne\":true,\"choices\":[{\"delta\":{},\"finishReason\":\"stop\"}]}\n\n"

type countingTransport struct{ sso, ide int32 }

func (c *countingTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	j := func(s string) *http.Response {
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}},
			Body: io.NopCloser(strings.NewReader(s))}
	}
	switch {
	case strings.Contains(r.URL.Host, "ssosv.sankuai.com"):
		atomic.AddInt32(&c.sso, 1)
		switch {
		case strings.Contains(r.URL.Path, "/websso/token/get"):
			return j(`{"code":0,"data":{"access_token":"at2","refresh_token":"rt2","token_exchange_attestation":"a2"}}`), nil
		case strings.Contains(r.URL.Path, "/websso/whoami"):
			return j(`{"code":0,"data":{"subject":"mis1","introspectionResultEnum":"SUCCESS"}}`), nil
		}
		return j(`{"code":0,"data":{}}`), nil
	case strings.Contains(r.URL.Path, "get-user-available-models"):
		atomic.AddInt32(&c.ide, 1)
		return j(tCatalog), nil
	case strings.Contains(r.URL.Path, "model-types"):
		atomic.AddInt32(&c.ide, 1)
		return j(`{"data":[]}`), nil
	case strings.Contains(r.URL.Path, "/stream"):
		atomic.AddInt32(&c.ide, 1)
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"text/event-stream"}},
			Body: io.NopCloser(strings.NewReader(tStream))}, nil
	}
	return &http.Response{StatusCode: 404, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
}

func tAuth() Auth {
	a := Auth{Key: "catpaw-oidc"}
	a.Metadata.OIDC = &CatpawSession{ID: "u1", SharedID: "u1", AccessToken: "at", RefreshToken: "rt", Attestation: "a", MIS: "mis1"}
	return a
}

func callIDE(t *testing.T, noRefresh bool, tr *countingTransport) {
	t.Helper()
	c := New(&http.Client{Transport: tr})
	c.NoRefresh = noRefresh
	body := []byte(`{"model":"gpt-6.2-sol","max_tokens":8,"stream":false,"messages":[{"role":"user","content":"hi"}]}`)
	req, _ := http.NewRequest(http.MethodPost, "https://bridge.invalid/v1/messages", bytes.NewReader(body))
	res, err := c.RoundTrip(req, "catpaw-ide", tAuth())
	if err != nil {
		t.Fatalf("RoundTrip: %v", err)
	}
	if res.StatusCode/100 != 2 {
		raw, _ := io.ReadAll(res.Body)
		_ = res.Body.Close()
		t.Fatalf("HTTP %d: %s", res.StatusCode, raw)
	}
	_, _ = io.ReadAll(res.Body)
	_ = res.Body.Close()
}

// NoRefresh 下不得产生任何 SSO 请求（刷新归 mtsso_keeper）。
func TestNoRefreshSkipsSSO(t *testing.T) {
	tr := &countingTransport{}
	callIDE(t, true, tr)
	if tr.sso != 0 {
		t.Fatalf("NoRefresh=true 仍发起 %d 次 SSO 请求", tr.sso)
	}
	if tr.ide == 0 {
		t.Fatal("IDE 请求未发出，测试无效")
	}
}

// 对照：关闭该开关时首次调用会刷新，证明开关确实生效而非路径本就无 SSO。
func TestRefreshOnWhenEnabled(t *testing.T) {
	tr := &countingTransport{}
	callIDE(t, false, tr)
	if tr.sso == 0 {
		t.Fatal("NoRefresh=false 未刷新，对照组失效")
	}
}

// 凭据残缺时仍然 fail-closed，不因 NoRefresh 放宽。
func TestNoRefreshStillRequiresCompleteCredential(t *testing.T) {
	tr := &countingTransport{}
	c := New(&http.Client{Transport: tr})
	c.NoRefresh = true
	a := tAuth()
	a.Metadata.OIDC.Attestation = "" // 缺一项
	req, _ := http.NewRequest(http.MethodPost, "https://bridge.invalid/v1/messages",
		strings.NewReader(`{"model":"gpt-6.2-sol","messages":[]}`))
	res, err := c.RoundTrip(req, "catpaw-ide", a)
	if err != nil {
		t.Fatalf("RoundTrip 不应返回 Go error: %v", err)
	}
	defer res.Body.Close()
	if res.StatusCode != 401 {
		t.Fatalf("凭据残缺应 401，实际 %d", res.StatusCode)
	}
	if tr.sso != 0 {
		t.Fatalf("残缺凭据不应触发刷新，实际 %d", tr.sso)
	}
}
