package sankuai

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

// FridayMarketAudience is the OIDC client of Friday's Skills marketplace.
const FridayMarketAudience = "0968168675"

const fridayMarketChannel = "friday-skills"

var fridayMarketTokens struct {
	sync.Mutex
	mis   string
	token string
	until time.Time
}

// FridayMarketToken exchanges the one shared app sign-in for a ticket to
// Friday's Skills marketplace. It never reads a browser, another client's
// token file, or an mtskills cache. A cached ticket is dropped and the
// source session refreshed when Friday refuses it.
func (c *Client) FridayMarketToken(ctx context.Context, force bool) (string, error) {
	if c.Sessions == nil {
		return "", failure("请先在 Providers 中登录内网账号，再打开 Friday 技能广场")
	}
	ids, err := c.Sessions.Identities(ctx)
	if err != nil {
		return "", err
	}
	switch len(ids) {
	case 0:
		return "", failure("请先在 Providers 中登录 Friday 个人或 CatPaw 账号，再打开 Friday 技能广场")
	case 1:
	default:
		return "", failure(fmt.Sprintf("检测到 %d 个共享内网身份，请先保留一个可用身份，再打开 Friday 技能广场", len(ids)))
	}
	mis := ids[0]
	if !force {
		fridayMarketTokens.Lock()
		if strings.EqualFold(fridayMarketTokens.mis, mis) && fridayMarketTokens.token != "" && time.Now().Before(fridayMarketTokens.until) {
			token := fridayMarketTokens.token
			fridayMarketTokens.Unlock()
			return token, nil
		}
		fridayMarketTokens.Unlock()
	}
	s, err := c.Sessions.Pick(ctx, mis)
	if err != nil {
		return "", err
	}
	if s == nil {
		return "", failure("共享内网登录已失效，请重新登录后再打开 Friday 技能广场")
	}
	token, err := c.catpawExchange(ctx, s, FridayMarketAudience)
	if err != nil && loginError(err) {
		refreshed, refreshErr := c.refreshFridayMarketSession(ctx, s)
		if refreshErr != nil {
			return "", refreshErr
		}
		token, err = c.catpawExchange(ctx, refreshed, FridayMarketAudience)
	}
	if err != nil {
		return "", err
	}
	fridayMarketTokens.Lock()
	fridayMarketTokens.mis, fridayMarketTokens.token = mis, token
	fridayMarketTokens.until = time.Now().Add(5 * time.Minute)
	fridayMarketTokens.Unlock()
	return token, nil
}

func (c *Client) refreshFridayMarketSession(ctx context.Context, s *CatpawSession) (*CatpawSession, error) {
	seed := SharedSSOReference(s)
	if seed == nil {
		return nil, catpawLoginRequired()
	}
	return c.Sessions.Resolve(ctx, seed, fridayMarketChannel, true, func(live *CatpawSession, refresh bool) error {
		if !refresh {
			return nil
		}
		return c.prepareCatpawSession(ctx, "catpaw-ide", live, true, catpawInference)
	})
}

func loginError(err error) bool {
	var e *Error
	return errors.As(err, &e) && (e.Status == 401 || e.Status == 403)
}
