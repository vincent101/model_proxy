package sankuai

import (
	"context"
	"fmt"
	"math"
	"net/url"
	"strconv"
	"time"
)

type Window struct {
	Amount, Limit, Used float64
	ResetsAt            time.Time
}

func daily(used, limit any) (Window, error) {
	parse := func(v any) (float64, bool) {
		switch v.(type) {
		case float64, string:
		default:
			return 0, false
		}
		if text(v) == "" {
			return 0, false
		}
		n, e := strconv.ParseFloat(text(v), 64)
		if e != nil {
			return 0, false
		}
		return n, n >= 0 && !math.IsNaN(n) && !math.IsInf(n, 0)
	}
	u, ok := parse(used)
	l, ok2 := parse(limit)
	if !ok || !ok2 {
		return Window{}, failure("每日额度数值无效")
	}
	p := 100.0
	if l > 0 {
		p = u / l * 100
	}
	return Window{u, l, p, dailyReset(time.Now())}, nil
}
func dailyReset(now time.Time) time.Time {
	now = now.In(time.FixedZone("Asia/Shanghai", 8*3600))
	return time.Date(now.Year(), now.Month(), now.Day()+1, 0, 0, 0, 0, now.Location())
}

func catpawWindows(data Object, id string) ([]Window, error) {
	if data["byProductMode"] == true {
		products, ok := data["products"].([]any)
		if !ok {
			return nil, failure("CatPaw 产品额度结构无效")
		}
		code := map[string]string{"catpaw-ide": "catpaw_ide", "catpaw-cli": "catclaw_cc"}[id]
		var product Object
		for _, raw := range products {
			if p := obj(raw); p["productCode"] == code {
				product = p
				break
			}
		}
		if product == nil {
			return nil, failure("CatPaw 未返回 " + Name(id) + " 每日额度")
		}
		if product["configured"] != true {
			return nil, failure("CatPaw 未配置 " + Name(id) + " 每日额度")
		}
		data = product
	} else if data["configured"] != true {
		return nil, failure("CatPaw 未配置个人每日额度")
	}
	w, err := daily(data["usedAmount"], data["allocatedQuota"])
	if err != nil {
		return nil, err
	}
	return []Window{w}, nil
}
func (c *Client) quota(ctx context.Context, id string, a Auth) ([]Window, error) {
	if id == "friday-team" {
		return nil, nil
	}
	if id == "friday-personal" {
		if !a.Metadata.OIDC.valid() || !validSecret(a.Metadata.OIDC.FridayToken) {
			return nil, catpawLoginRequired()
		}
		v, e := c.json(ctx, "GET", "https://friday.sankuai.com/cockpit/usage/user/info?tenantId="+url.QueryEscape(a.Metadata.TenantID), fridayHeaders(a.Metadata.OIDC.FridayToken), nil, "Friday 额度")
		if e != nil {
			return nil, e
		}
		rows, ok := v["data"].([]any)
		if !ok {
			return nil, failure("Friday 额度结构无效")
		}
		var out []Window
		for _, r := range rows {
			m := obj(r)
			w, e := daily(m["usedQuota"], m["initialQuota"])
			if e != nil {
				return nil, e
			}
			out = append(out, w)
		}
		return out, nil
	}
	data, err := c.catpawQuota(ctx, a)
	if err != nil {
		return nil, err
	}
	return catpawWindows(data, id)
}

func (c *Client) catpawQuota(ctx context.Context, a Auth) (Object, error) {
	if !a.Metadata.OIDC.valid() || !validSecret(a.Metadata.OIDC.ConsoleToken) {
		return nil, catpawLoginRequired()
	}
	v, err := c.json(ctx, "GET", "https://console.catpaw.sankuai.com/api/console/credit-hub/personal/quota", consoleHeaders(a.Metadata.OIDC.ConsoleToken), nil, "CatPaw 额度")
	if err != nil {
		return nil, err
	}
	d := obj(v["data"])
	if d == nil {
		return nil, failure("CatPaw 额度结构无效")
	}
	return d, nil
}

func (w Window) Display() string { return fmt.Sprintf("%g / %g 额度", w.Amount, w.Limit) }
