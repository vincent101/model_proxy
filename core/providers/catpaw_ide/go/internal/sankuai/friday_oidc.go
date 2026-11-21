package sankuai

import (
	"context"
	"net/http"
)

// AuthorizeCatpaw 保留原有调用入口；Friday 与 CatPaw 共用已登记的 OIDC 回调。
func (c *Client) AuthorizeCatpaw(ctx context.Context, id string, opts ConnectOptions, save func(context.Context, Auth) error, done func(error)) (string, func(), error) {
	return c.AuthorizeOIDC(ctx, id, opts, save, done)
}

func fridayHeaders(token string) http.Header {
	return http.Header{"Content-Type": {"application/json"}, "Access-Token": {token}, "X-Requested-With": {"XMLHttpRequest"}}
}

func applyFridaySession(id string, a Auth) Auth {
	if !FridayID(id) || a.Metadata.OIDC == nil {
		return a
	}
	if id == "friday-personal" {
		a.Key = a.Metadata.OIDC.FridayAppID
		a.Metadata.TenantID = a.Metadata.OIDC.FridayTenant
	}
	return a
}

func (c *Client) prepareFridaySession(ctx context.Context, id string, s *CatpawSession) error {
	if validSecret(s.FridayToken) && (id != "friday-personal" || validAppID(s.FridayAppID) && validSecret(s.FridayTenant)) {
		return nil
	}
	token := s.FridayToken
	if !validSecret(token) {
		var err error
		token, err = c.catpawExchange(ctx, s, fridayClientID)
		if err != nil {
			return err
		}
	}
	v, err := c.json(ctx, "GET", "https://friday.sankuai.com/cockpit/personalTenant/personalInfo", fridayHeaders(token), nil, "Friday 账号")
	if err != nil {
		return err
	}
	d := obj(v["data"])
	if str(d["misId"]) != s.MIS {
		return catpawLoginRequired()
	}
	if id == "friday-personal" {
		appID, tenant := text(d["appId"]), text(d["tenantId"])
		if !validAppID(appID) || !validSecret(tenant) {
			return failure("Friday 未返回个人 APPID 或 tenantId，请确认个人租户已开通")
		}
		s.FridayAppID, s.FridayTenant = appID, tenant
	}
	s.FridayToken = token
	return nil
}

func appIDAuth(appID string) Auth {
	a := Auth{Key: appID}
	a.Metadata.CookieSource = AuthSourceAppID
	return a
}

// PersonalFridayAppIDForAuth 用当前身份换 Friday 业务票，不读取浏览器或重新发起登录。
func (c *Client) PersonalFridayAppIDForAuth(ctx context.Context, id string, a Auth) (Auth, string, error) {
	ready, err := c.resolveCatpawResource(ctx, id, a, false, catpawFriday)
	if err != nil {
		return Auth{}, "", err
	}
	if ready.Metadata.OIDC == nil || !validAppID(ready.Metadata.OIDC.FridayAppID) {
		return Auth{}, "", catpawLoginRequired()
	}
	return ready, ready.Metadata.OIDC.FridayAppID, nil
}
