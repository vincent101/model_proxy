package sankuai

import (
	"context"
	"fmt"
	"sync"
	"time"
)

type catpawSessionEntry struct {
	gate    chan struct{}
	auth    Auth
	checked time.Time
}

var catpawSessions = struct {
	sync.Mutex
	entries map[string]*catpawSessionEntry
}{entries: map[string]*catpawSessionEntry{}}

// resolveCatpaw 只复用同一登录会话的更新；重新登录的随机 ID 会切断旧缓存。
func (c *Client) resolveCatpaw(ctx context.Context, id string, a Auth, force bool) (Auth, error) {
	if FridayID(id) {
		if a.Metadata.SignedOut {
			return Auth{}, catpawLoginRequired()
		}
		if c.Sessions != nil && a.Metadata.OIDC != nil {
			if err := c.Sessions.Active(ctx, a.Metadata.OIDC, id); err != nil {
				return Auth{}, err
			}
		}
		return a, nil // Friday 推理只使用已保存 APPID，不依赖网页认证。
	}
	return c.resolveCatpawResource(ctx, id, a, force, catpawInference)
}

// 额度和推理各自更新所需的票据，共享同一 SSO 会话和保存锁。
func (c *Client) resolveCatpawResource(ctx context.Context, id string, a Auth, force bool, resource catpawResource) (Auth, error) {
	if a.Metadata.SignedOut {
		return Auth{}, catpawLoginRequired()
	}
	if !Is(id) || FridayID(id) && a.AppIDOnly() {
		return a, nil
	}
	// [model_proxy 本地改动] 凭据由 mtsso_keeper 维护时不刷新；仍要求四项齐全，残缺即拒绝。
	// 位置必须早于 Sessions 分支与 force 重试，否则 401 后仍会自动刷新。
	if c.NoRefresh {
		if !a.Metadata.OIDC.valid() {
			return Auth{}, catpawLoginRequired()
		}
		return a, nil
	}
	if c.Sessions != nil {
		original := a
		s, err := c.Sessions.Resolve(ctx, a.Metadata.OIDC, id, force, func(s *CatpawSession, refresh bool) error {
			if force {
				if FridayID(id) || resource == catpawFriday {
					s.FridayToken = ""
				}
				if resource == catpawAllowance {
					s.ConsoleToken = ""
				}
				if resource != catpawAllowance && resource != catpawFriday && id == "catpaw-cli" {
					s.CLIToken = ""
				}
			}
			return c.prepareCatpawSession(ctx, id, s, refresh, resource)
		})
		if err != nil {
			return Auth{}, err
		}
		a.Metadata.OIDC = s
		a = applyFridaySession(id, a)
		if c.AuthUpdated != nil {
			if err := c.AuthUpdated(ctx, original, a); err != nil {
				return Auth{}, err
			}
		}
		return a, nil
	}
	if !a.Metadata.OIDC.valid() {
		return Auth{}, catpawLoginRequired()
	}
	scope := c.AuthScope
	if scope == "" {
		scope = fmt.Sprintf("%p", c.HTTP)
	}
	key := scope + "/" + a.Metadata.OIDC.ID
	catpawSessions.Lock()
	e := catpawSessions.entries[key]
	if e == nil {
		if len(catpawSessions.entries) >= 128 {
			for k, v := range catpawSessions.entries {
				if len(v.gate) == 1 {
					delete(catpawSessions.entries, k)
					break
				}
			}
		}
		e = &catpawSessionEntry{gate: make(chan struct{}, 1), auth: a}
		e.gate <- struct{}{}
		catpawSessions.entries[key] = e
	}
	catpawSessions.Unlock()
	select {
	case <-ctx.Done():
		return Auth{}, ctx.Err()
	case <-e.gate:
	}
	defer func() { e.gate <- struct{}{} }()
	current := a
	session := *e.auth.Metadata.OIDC
	current.Metadata.OIDC = &session
	refresh := force || time.Since(e.checked) >= time.Minute
	missing := resource != catpawInference && session.ConsoleToken == "" || resource != catpawAllowance && id == "catpaw-cli" && session.CLIToken == ""
	if FridayID(id) || resource == catpawFriday {
		missing = session.FridayToken == ""
	}
	current = applyFridaySession(id, current)
	if refresh || missing {
		old := current
		copySession := session
		current.Metadata.OIDC = &copySession
		if force {
			if FridayID(id) || resource == catpawFriday {
				copySession.FridayToken = ""
			}
			if resource == catpawAllowance {
				copySession.ConsoleToken = ""
			}
			if resource != catpawAllowance && resource != catpawFriday && id == "catpaw-cli" {
				copySession.CLIToken = ""
			}
		}
		if err := c.prepareCatpawSession(ctx, id, &copySession, refresh, resource); err != nil {
			return Auth{}, err
		}
		current = applyFridaySession(id, current)
		if c.AuthUpdated != nil && session != copySession {
			if err := c.AuthUpdated(ctx, old, current); err != nil {
				return Auth{}, err
			}
		}
		e.auth = current
		if refresh {
			e.checked = time.Now()
		}
	}
	return current, nil
}

// ReadyCatpaw 可用于导入和显式验证；旧的 Cookie / 本机凭据不会被自动导入。
func (c *Client) ReadyCatpaw(ctx context.Context, id string, a Auth) (Auth, error) {
	return c.resolveCatpaw(ctx, id, a, false)
}
