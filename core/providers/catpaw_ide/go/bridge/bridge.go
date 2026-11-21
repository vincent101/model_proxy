// Package bridge 把 Catboss 的 CatPaw IDE 入口包成进程内可调用的小接口。
// 它只负责：注入凭据、发起一次 /messages 调用、按字节取回 Anthropic 形态的 SSE。
package bridge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"

	"catboss/internal/sankuai"
)

// Version 是桥接契约版本，Python 加载后先握手比对。
const Version = 1

type Request struct {
	Auth         json.RawMessage `json:"auth"`
	Body         json.RawMessage `json:"body"`
	Conversation string          `json:"conversation,omitempty"`
	// NoRefresh 由 Python 侧固定传 true：凭据由 mtsso_keeper 维护，
	// 桥只做齐全性校验，不发起 SSO 刷新。
	NoRefresh bool `json:"noRefresh,omitempty"`
}

type handle struct {
	res    *http.Response
	cancel context.CancelFunc

	mu     sync.Mutex
	closed bool
}

var (
	regMu   sync.Mutex
	regNext int64
	reg     = map[int64]*handle{}
)

func register(res *http.Response, cancel context.CancelFunc) int64 {
	regMu.Lock()
	defer regMu.Unlock()
	regNext++
	reg[regNext] = &handle{res: res, cancel: cancel}
	return regNext
}

func lookup(h int64) (*handle, bool) {
	regMu.Lock()
	defer regMu.Unlock()
	v, ok := reg[h]
	return v, ok
}

// TransportFactory 允许离线测试注入假上游；生产路径不修改它。
var TransportFactory = func() *http.Client { return &http.Client{} }

// Start 发起一次请求，返回句柄。错误在提交前返回，不产生句柄。
func Start(req Request) (int64, error) {
	if len(req.Auth) == 0 || len(req.Body) == 0 {
		return 0, fmt.Errorf("auth 和 body 不能为空")
	}
	var auth sankuai.Auth
	if err := json.Unmarshal(req.Auth, &auth); err != nil {
		return 0, fmt.Errorf("auth 不是有效 JSON: %w", err)
	}
	var body map[string]any
	if err := json.Unmarshal(req.Body, &body); err != nil || body == nil {
		return 0, fmt.Errorf("body 不是有效 JSON 对象")
	}
	ctx, cancel := context.WithCancel(context.Background())
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://catpaw-bridge.invalid/v1/messages", bytes.NewReader(req.Body))
	if err != nil {
		cancel()
		return 0, err
	}
	if req.Conversation != "" {
		httpReq.Header.Set("X-Catboss-Conversation", req.Conversation)
	}
	c := sankuai.New(TransportFactory())
	c.NoRefresh = req.NoRefresh
	res, err := c.RoundTrip(httpReq, "catpaw-ide", auth)
	if err != nil {
		cancel()
		return 0, err
	}
	if res == nil {
		cancel()
		return 0, fmt.Errorf("上游没有返回响应")
	}
	if res.StatusCode/100 != 2 {
		raw, _ := io.ReadAll(io.LimitReader(res.Body, 64<<10))
		_ = res.Body.Close()
		cancel()
		return 0, fmt.Errorf("上游 HTTP %d: %s", res.StatusCode, strings.TrimSpace(string(raw)))
	}
	return register(res, cancel), nil
}

// Next 读取至多 len(buf) 字节；返回 0 表示流结束。
func Next(h int64, buf []byte) (int, error) {
	v, ok := lookup(h)
	if !ok {
		return 0, fmt.Errorf("句柄不存在")
	}
	v.mu.Lock()
	if v.closed {
		v.mu.Unlock()
		return 0, io.EOF
	}
	body := v.res.Body
	v.mu.Unlock()
	n, err := body.Read(buf)
	if err == io.EOF {
		return n, io.EOF
	}
	if err != nil {
		return n, err
	}
	return n, nil
}

// Cancel 尽力停止上游；可与其他调用并发。关闭响应体是为了唤醒阻塞中的 Next。
func Cancel(h int64) {
	if v, ok := lookup(h); ok {
		v.cancel()
		_ = v.res.Body.Close()
	}
}

// Close 幂等释放句柄与响应体。
func Close(h int64) {
	if v, ok := lookup(h); ok {
		v.mu.Lock()
		if !v.closed {
			v.closed = true
			v.cancel()
			_ = v.res.Body.Close()
		}
		v.mu.Unlock()
	}
	regMu.Lock()
	delete(reg, h)
	regMu.Unlock()
}
