// Package sankuai 实现美团内网供应商协议，不依赖插件宿主或 JavaScript 运行时。
package sankuai

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"time"

	"catboss/internal/appdir"
	"catboss/internal/catalog"
)

type Object = map[string]any

func obj(v any) Object { m, _ := v.(map[string]any); return m }
func arr(v any) []any  { a, _ := v.([]any); return a }
func str(v any) string { s, _ := v.(string); return s }
func num(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case string:
		f, _ := strconv.ParseFloat(n, 64)
		return f
	}
	return 0
}
func encoded(v any) []byte { b, _ := json.Marshal(v); return b }
func first(v ...any) any {
	for _, x := range v {
		if x != nil {
			return x
		}
	}
	return nil
}
func text(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

type Error struct {
	modelError bool
	Message    string
	Status     int
	Code       string
	// ResetsIn is how many seconds the vendor said until a quota or rate
	// limit clears. It is carried in the error body as resets_in_seconds,
	// which the gateway reads when deciding how long to rest the account.
	ResetsIn int64
}

func (e *Error) Error() string { return e.Message }
func invalid(s string) error {
	return &Error{Message: s, Status: 400, Code: "invalid_request_error"}
}
func failure(s string) error {
	return &Error{Message: s, Status: 502, Code: "upstream_error"}
}
func login(s string) error {
	message := s + " 登录态缺失或已过期，请重新登录"
	switch {
	case strings.Contains(s, "个人 Friday AppId"):
		message = "CatPaw IDE 的个人 Friday AppId 无效、未启用或已过期，请检查后重试"
	case strings.Contains(s, "CatPaw"):
		message = "CatPaw 登录已失效，请在猫老大重新打开登录链接"

	case strings.Contains(s, "Friday") || strings.Contains(s, "Cookie") || strings.Contains(s, "网页"):
		message = "账号登录已失效，请重新打开登录链接"
	}
	return &Error{Message: message, Status: 401, Code: "authentication_error"}
}
func Is(id string) bool {
	return id == "friday-personal" || id == "friday-team" || id == "catpaw-cli" || id == "catpaw-ide"
}
func Name(id string) string {
	return map[string]string{"friday-personal": "Friday 个人", "friday-team": "Friday 租户", "catpaw-cli": "CatPaw CLI", "catpaw-ide": "CatPaw IDE"}[id]
}

// MaskAppID shows only the end of a saved AppId in the account row. The full
// value stays in the account store and is never returned to the page.
func MaskAppID(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= 4 {
		return "…"
	}
	return "…" + string(runes[len(runes)-4:])
}

type Auth struct {
	Models   []Model `json:"-"`
	Key      string  `json:"key"`
	Metadata struct {
		OIDC         *CatpawSession `json:"oidc,omitempty"`
		SignedOut    bool           `json:"signedOut,omitempty"`
		Cookie       string         `json:"cookie"`
		CookieSource string         `json:"cookieSource,omitempty"`
		TenantID     string         `json:"tenantId,omitempty"`
		Email        string         `json:"email,omitempty"`
		Profile      string         `json:"profile,omitempty"`
		CLITokenPath string         `json:"cliTokenPath,omitempty"`
		// CustomFridayAppID is the personal Friday AppId a user typed for
		// CatPaw IDE. It is a per-account secret: never logged, never
		// returned in full to the GUI, and only sent to models whose
		// catalogue entry requires it.
		CustomFridayAppID       string `json:"customFridayAppId,omitempty"`
		EnableCustomFridayAppID bool   `json:"enableCustomFridayAppId,omitempty"`
		// CustomFridayAppIDSource says how the saved value was obtained:
		// "browser" from the account's browser Profile, "manual" from the
		// user. Older records without it are manual.
		CustomFridayAppIDSource string `json:"customFridayAppIdSource,omitempty"`
	} `json:"metadata"`
}

// SkipsBrowserCookie is true for a sign-in that explicitly chose not to read
// a browser Cookie. Used only by Friday APPID-only and legacy accounts;
// Friday APPID-only has inference but neither models nor quota.
func (a Auth) SkipsBrowserCookie() bool {
	source := a.Metadata.CookieSource
	return a.Metadata.Cookie == "" && (source == AuthSourceNone || source == AuthSourceAppID)
}

// AppIDOnly is a Friday sign-in that kept only the tenant APPID.
func (a Auth) AppIDOnly() bool { return a.Metadata.CookieSource == AuthSourceAppID }

// CustomFridayAppIDValue is the AppId requests may send. A blank value or a
// disabled switch counts as not configured; the IDE trims the stored value.
func (a Auth) CustomFridayAppIDValue() string {
	if !a.Metadata.EnableCustomFridayAppID {
		return ""
	}
	return strings.TrimSpace(a.Metadata.CustomFridayAppID)
}

const (
	CustomFridayAppIDSourceManual  = "manual"
	CustomFridayAppIDSourceOIDC    = "oidc"
	CustomFridayAppIDSourceBrowser = "browser"
)

// CustomFridayAppIDSourceValue names how the saved AppId was obtained. A
// record without the field predates the choice and is manual.
func (a Auth) CustomFridayAppIDSourceValue() string {
	if a.Metadata.CustomFridayAppIDSource == CustomFridayAppIDSourceOIDC {
		return CustomFridayAppIDSourceOIDC
	}
	if a.Metadata.CustomFridayAppIDSource == CustomFridayAppIDSourceBrowser {
		return CustomFridayAppIDSourceBrowser
	}
	return CustomFridayAppIDSourceManual
}

const BufferGeminiToolsOption = "buffer-gemini-tools"

type Client struct {
	BufferGeminiTools bool
	TraceOn           func(string) bool
	// FridayMinRPM 为目录筛选阈值；0 兼容旧调用，默认 10。
	FridayMinRPM int
	HTTP         *http.Client
	Home         string
	CLITokenPath string
	linux        bool
	// customFridayAppID comes from the account's Auth while a request is
	// in flight; it is never shared across accounts.
	customFridayAppID string
	// AuthorizationStage 只报告固定阶段名，不能包含凭据或上游正文。
	AuthorizationStage func(string)
	Sessions           *SharedSSOStore
	AuthScope          string
	// NoRefresh 表示凭据由外部管理器（如 mtsso_keeper）维护：本客户端只做
	// 齐全性校验，不发起 SSO 刷新。用于 model_proxy 这类只读消费方。
	NoRefresh   bool
	AuthUpdated func(context.Context, Auth, Auth) error
	owned       *CatpawSession
}

func (c *Client) authorizationStage(stage string) {
	if c.AuthorizationStage != nil {
		c.AuthorizationStage(stage)
	}
}

func (c *Client) withAuth(a Auth) *Client {
	copy := *c
	copy.customFridayAppID = a.CustomFridayAppIDValue()
	copy.owned = a.Metadata.OIDC
	return &copy
}

func New(client *http.Client) *Client {
	return &Client{HTTP: client, Home: appdir.ExternalHome(), linux: runtime.GOOS == "linux"}
}
func (c *Client) do(ctx context.Context, method, target string, headers http.Header, body []byte) (*http.Response, error) {
	req, e := http.NewRequestWithContext(ctx, method, target, bytes.NewReader(body))
	if e != nil {
		return nil, e
	}
	req.Header = headers.Clone()
	client := *c.HTTP
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return client.Do(req)
}
func loginRedirect(target string) bool {
	u, e := url.Parse(target)
	if e != nil {
		return false
	}
	host := strings.ToLower(u.Hostname())
	return host == "ssosv.sankuai.com" || host == "passport.sankuai.com"
}
func limited(r io.Reader, n int64) ([]byte, error) {
	b, e := io.ReadAll(io.LimitReader(r, n+1))
	if e == nil && int64(len(b)) > n {
		return nil, failure("上游响应超过大小限制")
	}
	return b, e
}
func (c *Client) json(ctx context.Context, method, target string, h http.Header, body any, stage string) (Object, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	var b []byte
	if body != nil {
		b = encoded(body)
	}
	res, e := c.do(ctx, method, target, h, b)
	if e != nil {
		return nil, failure(stage + " 请求失败，请检查内网连接")
	}
	defer res.Body.Close()
	if res.StatusCode == 401 || res.StatusCode == 403 || res.StatusCode/100 == 3 && loginRedirect(res.Header.Get("Location")) {
		return nil, login(stage)
	}
	if res.StatusCode/100 != 2 {
		return nil, &Error{Message: fmt.Sprintf("%s 请求失败（HTTP %d）", stage, res.StatusCode), Status: res.StatusCode, Code: "upstream_error"}
	}
	b, e = limited(res.Body, 4<<20)
	if e != nil {
		return nil, e
	}
	var out Object
	if json.Unmarshal(b, &out) != nil || out == nil {
		return nil, failure(stage + " 未返回有效 JSON")
	}
	code := num(first(out["code"], out["unifyCode"]))
	nested := num(obj(out["data"])["code"])
	status := num(out["status"])
	if code == 401 || code == 403 || code == 30001 || code == 30002 || code == 50102 || status == 401 || status == 403 || nested == 30001 || nested == 30002 || nested == 50102 {
		return nil, login(stage)
	}
	if (code != 0 && code != 200) || out["success"] == false || out["error"] != nil {
		return nil, failure(fmt.Sprintf("%s 返回业务错误（code=%v）", stage, code))
	}
	return out, nil
}

// PersonalFridayAppID 拒绝已移除的浏览器读取入口。
func (c *Client) PersonalFridayAppID(ctx context.Context, profile string) (string, error) {
	return "", catpawLoginRequired()
}

func validSecret(s string) bool { return s != "" && !strings.ContainsAny(s, "\r\n\x00") }
func (c *Client) IDECredential() (Object, error) {
	if !c.owned.valid() {
		return nil, catpawLoginRequired()
	}
	return Object{"ssoid": c.owned.AccessToken, "misId": c.owned.MIS}, nil
}
func (c *Client) CLIToken() (string, error) {
	if !c.owned.valid() || !validSecret(c.owned.CLIToken) {
		return "", catpawLoginRequired()
	}
	return c.owned.CLIToken, nil
}
func ideHeaders(v Object) http.Header {
	token := str(v["ssoid"])
	h := http.Header{"Content-Type": {"application/json"}}
	for k, v := range map[string]string{"Catpaw-Auth": token, "ide-type": "CatPaw IDE", "client-type": "CatPaw IDE", "ide-version": "2026.9.2", "plugin-version": "2026.9.2", "plugin-id": "mt-idekit.mt-idekit-code", "client-env": "LOCAL_IDE", "platform-info": runtime.GOOS + "-" + runtime.GOARCH, "tenant": "4391f0be98"} {
		h.Set(k, v)
	}
	// MaaS 模型参数目录走 SSO 鉴权，不认 Catpaw-Auth；同一 OIDC AT 以
	// access-token 发送即可，不依赖应用 Cookie。
	if validSecret(token) {
		h.Set("access-token", token)
	}
	if mis := str(v["misId"]); validSecret(mis) {
		for _, k := range []string{"mis-id", "user-mis-id", "user-uid"} {
			h.Set(k, mis)
		}
	}
	return h
}

// ConnectOptions carries sign-in choices that are not credentials of their
// own. FridayAppID selects team billing; CustomFridayAppID is IDE-only.
type ConnectOptions struct {
	ForceLogin              bool
	ReuseMIS                string
	FridayAppID             string
	CustomFridayAppID       string
	EnableCustomFridayAppID bool
	CustomFridayAppIDSource string
}

func (c *Client) Connect(ctx context.Context, id, cookie, appID string) (Auth, error) {
	return c.ConnectWithOptions(ctx, id, cookie, appID, ConnectOptions{})
}

func (c *Client) ConnectWithOptions(ctx context.Context, id, cookie, appID string, opts ConnectOptions) (Auth, error) {
	return Auth{}, catpawLoginRequired()
}

// ConnectWithoutCookie rejects the retired local-client credential flow.
func (c *Client) ConnectWithoutCookie(ctx context.Context, id string) (Auth, error) {
	return c.ConnectWithoutCookieWithOptions(ctx, id, ConnectOptions{})
}

func (c *Client) ConnectWithoutCookieWithOptions(ctx context.Context, id string, opts ConnectOptions) (Auth, error) {
	return Auth{}, catpawLoginRequired()
}

type Model struct {
	catalog.Model
	Type                      int
	Parameters, Constraints   []any
	Thinking                  bool
	RequiresCustomFridayAppID bool
}

func (c *Client) Models(ctx context.Context, id string, a Auth) ([]Model, error) {
	if FridayID(id) && a.AppIDOnly() {
		return nil, nil
	}
	ready, err := c.resolveCatpawResource(ctx, id, a, false, catpawInference)
	if err != nil {
		return nil, err
	}
	ms, err := c.models(ctx, id, ready)
	if e, ok := err.(*Error); ok && e.Status == 401 && ready.Metadata.OIDC != nil {
		ready, err = c.resolveCatpawResource(ctx, id, ready, true, catpawInference)
		if err == nil {
			return c.models(ctx, id, ready)
		}
	}
	return ms, err
}
func (c *Client) models(ctx context.Context, id string, a Auth) ([]Model, error) {
	c = c.withAuth(a)
	var names []string
	rpms := map[string]int{}
	if id == "catpaw-ide" {
		ms, complete, e := c.ideModels(ctx)
		if e != nil {
			return nil, e
		}
		if !complete {
			// 缺参数的目录不能覆盖已有缓存，也不能当作成功目录保存。
			return nil, failure("CatPaw 模型参数目录暂不可用，请稍后重试")
		}
		return ms, nil
	}
	if id == "catpaw-cli" {
		token, e := c.CLIToken()
		if e != nil {
			return nil, e
		}
		v, e := c.json(ctx, "GET", "https://mcli.sankuai.com/v1/models", http.Header{"Authorization": {"Bearer " + token}}, nil, Name(id))
		if e != nil {
			return nil, e
		}
		for _, m := range arr(v["data"]) {
			if s := str(obj(m)["id"]); s != "" {
				names = append(names, s)
			}
		}
	} else if FridayID(id) {
		var e error
		names, rpms, e = c.fridayModels(ctx, id, a)
		if e != nil {
			return nil, e
		}
	}
	proto := "anthropic"
	apis := []string{proto}
	if strings.HasPrefix(id, "friday-") {
		apis = []string{"responses", "chat", "anthropic"}
	}
	var out []Model
	seen := map[string]bool{}
	for _, name := range names {
		if FridayID(id) && !FridayConversationModel(name) {
			continue
		}
		if !seen[name] {
			item := Model{Model: catalog.Model{ID: name, Name: name, Provider: id, APIs: apis, RPM: rpms[name]}}
			// CLI 目录只给 ID；按渠道的 Claude Code / Anthropic 约定声明五档。
			if id == "catpaw-cli" {
				item.Thinking = true
				item.Efforts = []string{"low", "medium", "high", "xhigh", "max"}
			}
			out = append(out, item)
			seen[name] = true
		}
	}
	if len(out) == 0 {
		if FridayID(id) {
			minimum := c.FridayMinRPM
			if minimum < 1 || minimum > 1000000 {
				minimum = 10
			}
			return nil, failure(fmt.Sprintf("%s 没有实际 RPM ≥ %d 的模型，请降低最低 RPM 后重试", Name(id), minimum))
		}
		return nil, failure(Name(id) + " 未返回可用模型")
	}
	return out, nil
}

// ideModels reads the account's IDE models. complete is false when the MaaS
// parameter catalogue failed for a reason other than authentication: the
// model list is still usable, but it carries no parameter definitions and
// must not be cached as a successful catalogue.
func (c *Client) ideModels(ctx context.Context) ([]Model, bool, error) {
	cred, e := c.IDECredential()
	if e != nil {
		return nil, false, e
	}
	h := ideHeaders(cred)
	v, e := c.json(ctx, "GET", "https://catpaw.sankuai.com/api/chat/get-user-available-models?includeCustomAppidModels=true&type=agent", h, nil, "CatPaw IDE 模型")
	if e != nil {
		return nil, false, e
	}
	extra, extraErr := c.json(ctx, "POST", "https://catpaw.sankuai.com/api/agent/maas/model-types", h, Object{"tenant": "CatPaw", "scene": "AGENT"}, "CatPaw 模型参数")
	if extraErr != nil {
		// 认证失败必须交给 Models()/RoundTrip 的 OIDC 刷新重试，不能按
		// 缺省窗口静默继续。
		if e, ok := extraErr.(*Error); ok && e.Status == 401 {
			return nil, false, e
		}
		extra = nil
	}
	extraData, complete := extra["data"].([]any)
	if extraErr != nil {
		complete = false
	}
	if !complete {
		extraData = nil
	}
	var out []Model
	seen := map[string]bool{}
	for _, raw := range arr(v["data"]) {
		m := obj(raw)
		name := str(m["modelTypeName"])
		t := num(m["modelType"])
		requiresAppID := m["requireCustomFridayAppid"] == true
		if m["supportAgent"] != true || requiresAppID || name == "" || t <= 0 || t != float64(int(t)) {
			continue
		}
		if seen[name] {
			return nil, false, failure("CatPaw 模型目录包含重复名称")
		}
		seen[name] = true
		item := Model{Model: catalog.Model{ID: name, Name: name, Provider: "catpaw-ide", APIs: []string{"anthropic"}, Images: m["supportImage"] == true}, Type: int(t), Thinking: m["supportThinking"] == true, RequiresCustomFridayAppID: requiresAppID}
		for _, raw := range extraData {
			x := obj(raw)
			if num(x["modelTypeId"]) == t {
				item.Parameters = arr(x["parameterDefinitions"])
				item.Constraints = arr(x["parameterConstraints"])
				break
			}
		}
		item.Context = maxIDEContext(item.Parameters)
		item.Efforts = effortOptions(item.Parameters)
		out = append(out, item)
	}
	if len(out) == 0 {
		return nil, false, failure("CatPaw 账号没有可用 Agent 模型")
	}
	return out, complete, nil
}

// maxIDEContext 使用可选档位的实际 token 值，不依赖显示名或默认档位。
func maxIDEContext(params []any) int {
	var largest float64
	for _, raw := range params {
		d := obj(raw)
		if d["id"] != "context" {
			continue
		}
		for _, raw := range arr(d["values"]) {
			n := num(obj(raw)["value"])
			if n > largest && n <= 9007199254740991 && math.Trunc(n) == n {
				largest = n
			}
		}
	}
	return int(largest)
}

// effortOptions are the API efforts CatPaw lists for one model. Its thinking
// switch is separate; only levels the model's entry names are offered.
func effortOptions(params []any) []string {
	var raw []string
	for _, p := range params {
		d := obj(p)
		if str(d["id"]) != "effort" {
			continue
		}
		for _, v := range arr(d["values"]) {
			if e := text(obj(v)["value"]); slices.Contains(memberEfforts, e) && !slices.Contains(raw, e) {
				raw = append(raw, e)
			}
		}
	}
	slices.SortStableFunc(raw, func(a, b string) int {
		return slices.Index(memberEfforts, a) - slices.Index(memberEfforts, b)
	})
	return raw
}

func (c *Client) fridayModels(ctx context.Context, id string, a Auth) ([]string, map[string]int, error) {
	minimum := c.FridayMinRPM
	if minimum < 1 || minimum > 1000000 {
		minimum = 10
	}
	if !a.Metadata.OIDC.valid() || !validSecret(a.Metadata.OIDC.FridayToken) {
		return nil, nil, catpawLoginRequired()
	}
	var names []string
	rpms := map[string]int{}

	v, e := c.json(ctx, "GET", "https://friday.sankuai.com/cockpit/rpm/queryModelList?modelName=&appId="+url.QueryEscape(a.Key), fridayHeaders(a.Metadata.OIDC.FridayToken), nil, Name(id))
	if e != nil {
		return nil, nil, e
	}
	all, ok := v["data"].([]any)
	if !ok {
		return nil, nil, failure("Friday APPID 模型目录结构无效")
	}
	for page := 1; page <= 20; page++ {
		v, e = c.json(ctx, "POST", "https://friday.sankuai.com/cockpit/rpm/queryRpmListByAppInfo", fridayHeaders(a.Metadata.OIDC.FridayToken), Object{"appId": a.Key, "modelNameList": append([]any{"全部"}, all...), "type": "RPM", "pageNum": page, "pageSize": 999, "rpmMin": minimum, "sortBy": "rpm", "sortOrder": "asc"}, Name(id))
		if e != nil {
			return nil, nil, e
		}
		rows, ok := obj(v["data"])["dataList"].([]any)
		if !ok {
			return nil, nil, failure("Friday APPID RPM 目录结构无效")
		}
		for _, raw := range rows {
			m := obj(raw)
			if n := num(m["requestLimit"]); n >= float64(minimum) && n <= float64(math.MaxInt32) && n == math.Trunc(n) && str(m["model"]) != "" {
				names = append(names, str(m["model"]))
				rpms[str(m["model"])] = int(num(m["requestLimit"]))
			}
		}
		if len(rows) < 999 {
			break
		}
		if page == 20 {
			return nil, nil, failure("Friday APPID 模型超过分页上限")
		}
	}
	return names, rpms, nil
}
