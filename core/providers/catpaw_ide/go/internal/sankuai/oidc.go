package sankuai

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
)

const (
	CatpawClientID  = "2f73f89b68"
	catpawConsoleID = "518aba86d4"
	catpawCLIID     = "0fcdafb24e"
	fridayClientID  = "893260a2ae"
	catpawSSO       = "https://ssosv.sankuai.com"
	AuthSourceOIDC  = "oidc"
)

// CatpawSession 保存内网供应商的独立 OIDC 会话；名称保留以兼容已有账号格式。
type CatpawSession struct {
	ID           string `json:"id"`
	SharedID     string `json:"sharedId,omitempty"`
	AccessToken  string `json:"accessToken"`
	RefreshToken string `json:"refreshToken"`
	Attestation  string `json:"attestation"`
	MIS          string `json:"mis"`
	ConsoleToken string `json:"consoleToken,omitempty"`
	CLIToken     string `json:"cliToken,omitempty"`
	FridayToken  string `json:"fridayToken,omitempty"`
	FridayAppID  string `json:"fridayAppId,omitempty"`
	FridayTenant string `json:"fridayTenant,omitempty"`
}

func catpawLoginRequired() error {
	return &Error{Message: "请在猫老大中打开登录链接，重新登录账号", Status: 401, Code: "authentication_error"}
}

func (s *CatpawSession) valid() bool {
	return s != nil && validSecret(s.ID) && validSecret(s.AccessToken) && validSecret(s.RefreshToken) && validSecret(s.Attestation) && validSecret(s.MIS)
}

func oidcParams() url.Values {
	return url.Values{"client_id": {CatpawClientID}, "mode": {"cli"}, "loginProtocol": {"OIDC"}}
}

func randomOIDC() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

// AuthorizeOIDC 复用 IDE 已登记的 localhost 回调；端口被占用时不接管别人的监听。
func (c *Client) AuthorizeOIDC(ctx context.Context, id string, opts ConnectOptions, save func(context.Context, Auth) error, done func(error)) (string, func(), error) {
	if !Is(id) {
		return "", nil, invalid("未知内网供应商")
	}
	if id == "friday-team" && !validAppID(strings.TrimSpace(opts.FridayAppID)) {
		return "", nil, invalid("请先输入团队 APPID")
	}
	if c.Sessions != nil && !opts.ForceLogin {
		reused, err := c.reuseSharedLogin(ctx, id, opts, save)
		if err != nil {
			return "", nil, err
		}
		if reused {
			done(nil)
			return "", func() {}, nil
		}
	}
	var ln net.Listener
	for _, port := range []int{8576, 9576} {
		var err error
		ln, err = net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
		if err == nil {
			break
		}
	}
	if ln == nil {
		return "", nil, failure("公司 SSO 登录回调端口 8576 和 9576 均被占用，请结束其他登录后重试")
	}
	port := ln.Addr().(*net.TCPAddr).Port
	ctx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	state, verifier := randomOIDC(), randomOIDC()
	challenge := sha256.Sum256([]byte(verifier))
	q := oidcParams()
	q.Set("response_type", "code")
	q.Set("scope", "profile")
	q.Set("state", state)
	q.Set("code_challenge_method", "S256")
	q.Set("code_challenge", base64.RawURLEncoding.EncodeToString(challenge[:]))
	q.Set("redirect_uri", fmt.Sprintf("http://localhost:%d/websso/oauth/callback?original-url=%%2F", port))
	var busy, ended atomic.Bool
	finish := func(err error) {
		if ended.CompareAndSwap(false, true) {
			done(err)
			cancel()
		}
	}
	srv := &http.Server{ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 15 * time.Second}
	srv.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if r.Host != "localhost:"+strconv.Itoa(port) && r.Host != ln.Addr().String() {
			http.Error(w, "请求来源无效", 403)
			return
		}
		if r.Method != "GET" {
			w.WriteHeader(405)
			return
		}
		if r.URL.Path == "/wait-for-auth" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(202)
			_ = json.NewEncoder(w).Encode(Object{"code": 1011, "data": Object{"clientId": CatpawClientID}})
			return
		}
		if r.URL.Path != "/websso/oauth/callback" {
			http.NotFound(w, r)
			return
		}
		v := r.URL.Query()
		if len(v["state"]) != 1 || v.Get("state") != state || len(v["code"]) != 1 || !validSecret(v.Get("code")) || len(v.Get("code")) > 16384 {
			writeAuthResult(w, 400, id, false)
			return
		}
		if ended.Load() || !busy.CompareAndSwap(false, true) {
			writeAuthResult(w, 409, id, false)
			return
		}
		a, err := c.completeCatpawLogin(ctx, id, v.Get("code"), state, verifier, opts)
		if err == nil {
			err = ctx.Err()
		}
		if err == nil {
			err = save(ctx, a)
		}
		if err != nil {
			writeAuthResult(w, 400, id, false)
		} else {
			writeAuthResult(w, 200, id, true)
		}
		finish(err)
	})
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) && !errors.Is(err, net.ErrClosed) {
			finish(failure("公司 SSO 登录回调服务已停止"))
		}
	}()
	go func() {
		<-ctx.Done()
		finish(errors.New("公司 SSO 登录已取消或超时，请重新打开登录链接"))
		shutdown, stop := context.WithTimeout(context.Background(), time.Second)
		defer stop()
		_ = srv.Shutdown(shutdown)
		_ = srv.Close()
	}()
	return catpawSSO + "/sson/auth/oidc/v1/authorize?" + q.Encode(), func() { cancel(); _ = ln.Close() }, nil
}

// oidcJSON 不把认证响应、URL 参数或上游错误正文带入日志与公开错误。
func (c *Client) oidcJSON(ctx context.Context, method, target string, h http.Header, body []byte) (Object, error) {
	r, err := c.do(ctx, method, target, h, body)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, failure("公司 SSO 认证请求失败，请检查内网连接")
	}
	defer r.Body.Close()
	if r.StatusCode == 400 || r.StatusCode == 401 || r.StatusCode == 403 {
		return nil, catpawLoginRequired()
	}
	if r.StatusCode/100 != 2 {
		return nil, failure(fmt.Sprintf("公司 SSO 认证服务请求失败（HTTP %d）", r.StatusCode))
	}
	b, err := limited(r.Body, 256<<10)
	if err != nil {
		return nil, err
	}
	var v Object
	if json.Unmarshal(b, &v) != nil || v == nil {
		return nil, failure("公司 SSO 认证服务未返回有效 JSON")
	}
	if code := num(v["code"]); code != 0 && code != 200 {
		return nil, catpawLoginRequired()
	}
	if v["error"] != nil || v["success"] == false {
		return nil, catpawLoginRequired()
	}
	return obj(v["data"]), nil
}

func (c *Client) completeCatpawLogin(ctx context.Context, id, code, state, verifier string, opts ConnectOptions) (Auth, error) {
	q := oidcParams()
	q.Set("code", code)
	q.Set("state", state)
	q.Set("code_verifier", verifier)
	q.Set("original-url", "/")
	v, err := c.oidcJSON(ctx, "GET", catpawSSO+"/websso/oauth/pkce?"+q.Encode(), nil, nil)
	if err != nil {
		return Auth{}, err
	}
	s := CatpawSession{ID: uuid.NewString()}
	if err = mergeCatpawTokens(&s, v); err != nil {
		return Auth{}, err
	}
	mis, err := c.catpawWho(ctx, s.AccessToken)
	if err != nil {
		return Auth{}, err
	}
	s.MIS = mis
	s.SharedID = s.ID
	a := Auth{Key: "catpaw-oidc"}
	a.Metadata.OIDC = &s
	a.Metadata.Email = mis
	a.Metadata.CookieSource = AuthSourceOIDC
	a.Metadata.CustomFridayAppID = strings.TrimSpace(opts.CustomFridayAppID)
	a.Metadata.EnableCustomFridayAppID = opts.EnableCustomFridayAppID && a.Metadata.CustomFridayAppID != ""
	a.Metadata.CustomFridayAppIDSource = CustomFridayAppIDSourceManual
	if id == "friday-team" {
		a.Key = strings.TrimSpace(opts.FridayAppID)
	}
	if err = c.prepareCatpawSession(ctx, id, &s, false, catpawAll); err != nil {
		return Auth{}, err
	}
	a = applyFridaySession(id, a)
	a.Models, err = c.withAuth(a).models(ctx, id, a)
	return a, err
}

func mergeCatpawTokens(s *CatpawSession, v Object) error {
	if !validSecret(str(v["access_token"])) || !validSecret(str(v["refresh_token"])) {
		return catpawLoginRequired()
	}
	s.AccessToken = str(v["access_token"])
	s.RefreshToken = str(v["refresh_token"])
	if a := str(v["token_exchange_attestation"]); validSecret(a) {
		s.Attestation = a
	}
	if !validSecret(s.Attestation) {
		return catpawLoginRequired()
	}
	return nil
}

func (c *Client) catpawWho(ctx context.Context, token string) (string, error) {
	v, err := c.oidcJSON(ctx, "GET", catpawSSO+"/websso/whoami?"+oidcParams().Encode(), http.Header{"Authorization": {"Bearer " + token}}, nil)
	if err != nil {
		return "", err
	}
	mis := str(v["subject"])
	if v["introspectionResultEnum"] != "SUCCESS" || !validSecret(mis) || strings.HasPrefix(mis, "passport_") {
		return "", catpawLoginRequired()
	}
	return mis, nil
}

func (c *Client) catpawExchange(ctx context.Context, s *CatpawSession, target string) (string, error) {
	v, err := c.oidcJSON(ctx, "POST", catpawSSO+"/websso/auth/token-exchange?"+oidcParams().Encode(), http.Header{"Content-Type": {"application/json"}}, encoded(Object{"client_id": CatpawClientID, "audience": []string{target}, "access_token": s.AccessToken, "token_exchange_attestation": s.Attestation}))
	if err != nil {
		return "", err
	}
	t := str(v["access_token"])
	if !validSecret(t) {
		return "", catpawLoginRequired()
	}
	return t, nil
}

func consoleHeaders(token string) http.Header {
	return http.Header{"Access-Token": {token}, "X-Harness-Surface": {"console-internal"}, "Referer": {"https://console.catpaw.sankuai.com/"}}
}

type catpawResource uint8

const (
	catpawAll catpawResource = iota
	catpawInference
	catpawAllowance
	catpawFriday
)

func (c *Client) prepareCatpawSession(ctx context.Context, id string, s *CatpawSession, refresh bool, resource catpawResource) error {
	if refresh {
		h := http.Header{"Content-Type": {"application/json"}, "Authorization": {"Bearer " + s.AccessToken}, "Authorization-Refresh": {"Bearer " + s.RefreshToken}, "Authorization-Attestation": {"Bearer " + s.Attestation}}
		v, err := c.oidcJSON(ctx, "POST", catpawSSO+"/websso/token/get?"+oidcParams().Encode(), h, nil)
		if err != nil {
			return err
		}
		old := s.AccessToken
		if err = mergeCatpawTokens(s, v); err != nil {
			return err
		}
		mis, err := c.catpawWho(ctx, s.AccessToken)
		if err != nil {
			return err
		}
		if mis != s.MIS {
			return catpawLoginRequired()
		}
		if old != s.AccessToken {
			s.ConsoleToken = ""
			s.CLIToken = ""
			s.FridayToken = ""
		}
	}
	if resource == catpawFriday {
		return c.prepareFridaySession(ctx, "friday-personal", s)
	}
	if FridayID(id) {
		return c.prepareFridaySession(ctx, id, s)
	}
	if resource != catpawInference && s.ConsoleToken == "" {
		token, err := c.catpawExchange(ctx, s, catpawConsoleID)
		if err != nil {
			return err
		}
		v, err := c.json(ctx, "GET", "https://console.catpaw.sankuai.com/api/organization/user/ssoUser", consoleHeaders(token), nil, "CatPaw 账号")
		if err != nil {
			return err
		}
		if str(v["mis_id"]) != s.MIS {
			return catpawLoginRequired()
		}
		s.ConsoleToken = token
	}
	if resource != catpawAllowance && id == "catpaw-cli" && s.CLIToken == "" {
		token, err := c.catpawExchange(ctx, s, catpawCLIID)
		if err != nil {
			return err
		}
		candidate := strings.ReplaceAll(uuid.NewString(), "-", "")
		h := http.Header{"Cookie": {catpawCLIID + "_ssoid=" + token}}
		r, err := c.do(ctx, "GET", "https://mcli.sankuai.com/api/v1.0/user/login?uuid="+candidate, h, nil)
		if err != nil {
			return failure("CatPaw CLI 登录绑定失败，请检查内网连接")
		}
		_ = r.Body.Close()
		if r.StatusCode/100 != 2 && r.StatusCode/100 != 3 {
			return catpawLoginRequired()
		}
		v, err := c.json(ctx, "POST", "https://mcli.sankuai.com/api/v1.0/user/info", http.Header{"Content-Type": {"application/json"}}, Object{"uuid": candidate}, "CatPaw CLI 登录确认")
		if err != nil {
			return err
		}
		data := obj(v["data"])
		if str(data["misId"]) != s.MIS || str(data["uuid"]) != candidate {
			return catpawLoginRequired()
		}
		s.CLIToken = candidate
	}
	return nil
}

func (c *Client) reuseSharedLogin(ctx context.Context, id string, opts ConnectOptions, save func(context.Context, Auth) error) (bool, error) {
	s, err := c.Sessions.Pick(ctx, opts.ReuseMIS)
	if err != nil || s == nil {
		return false, err
	}
	s.SharedID = s.ID
	s.ID = uuid.NewString()
	kept := false
	defer func() {
		if !kept {
			cleanup, stop := context.WithTimeout(context.WithoutCancel(ctx), time.Second)
			defer stop()
			_ = c.Sessions.Forget(cleanup, s.MIS, s.ID)
		}
	}()
	a := Auth{Key: "catpaw-oidc"}
	a.Metadata.OIDC = s
	a.Metadata.Email = s.MIS
	a.Metadata.CookieSource = AuthSourceOIDC
	a.Metadata.CustomFridayAppID = strings.TrimSpace(opts.CustomFridayAppID)
	a.Metadata.EnableCustomFridayAppID = opts.EnableCustomFridayAppID && a.Metadata.CustomFridayAppID != ""
	a.Metadata.CustomFridayAppIDSource = CustomFridayAppIDSourceManual
	if id == "friday-team" {
		a.Key = strings.TrimSpace(opts.FridayAppID)
	}
	ready, err := c.resolveCatpawResource(ctx, id, a, false, catpawAll)
	if e, ok := err.(*Error); ok && e.Status == 401 {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	ready.Models, err = c.withAuth(ready).models(ctx, id, ready)
	if err != nil {
		return false, err
	}
	if err = save(ctx, ready); err != nil {
		return false, err
	}
	kept = true
	return true, nil
}
