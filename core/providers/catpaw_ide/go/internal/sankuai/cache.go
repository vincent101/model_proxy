package sankuai

import (
	"context"
	"crypto/sha256"
	"fmt"
	"sync"
	"time"
)

type quotaEntry struct {
	ready chan struct{}
	data  Object
	err   error
	at    time.Time
	reset time.Time
}

var quotaCache = struct {
	sync.Mutex
	entries map[string]*quotaEntry
}{entries: map[string]*quotaEntry{}}

// 按控制台身份缓存原始响应，产品额度在读取后分别解析，不能串用。
func (c *Client) sharedQuota(ctx context.Context, id string, a Auth) ([]Window, error) {
	key := fmt.Sprintf("%p/%s/%x", c.HTTP, c.Home, sha256.Sum256([]byte(catpawQuotaKey(a))))
	now := time.Now()
	quotaCache.Lock()
	old := quotaCache.entries[key]
	if old != nil && now.Sub(old.at) < 30*time.Second && now.Before(old.reset) {
		quotaCache.Unlock()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-old.ready:
			if old.err != nil {
				return nil, old.err
			}
			return catpawWindows(old.data, id)
		}
	}
	e := &quotaEntry{ready: make(chan struct{}), at: now, reset: dailyReset(now)}
	if len(quotaCache.entries) >= 32 {
		for k, v := range quotaCache.entries {
			select {
			case <-v.ready:
				delete(quotaCache.entries, k)
			default:
			}
		}
	}
	quotaCache.entries[key] = e
	quotaCache.Unlock()
	data, err := c.catpawQuota(ctx, a)
	quotaCache.Lock()
	e.data, e.err = data, err
	if err != nil && quotaCache.entries[key] == e {
		delete(quotaCache.entries, key)
	}
	close(e.ready)
	quotaCache.Unlock()
	if err != nil {
		return nil, err
	}
	return catpawWindows(data, id)
}
func (c *Client) Quota(ctx context.Context, id string, a Auth) ([]Window, error) {
	if id == "friday-team" {
		return nil, nil
	}
	var err error
	a, err = c.resolveCatpawResource(ctx, id, a, false, catpawAllowance)
	if err != nil {
		return nil, err
	}
	if a.SkipsBrowserCookie() {
		return nil, nil
	}
	if id == "catpaw-cli" || id == "catpaw-ide" {
		ws, err := c.sharedQuota(ctx, id, a)
		if e, ok := err.(*Error); ok && e.Status == 401 {
			a, err = c.resolveCatpawResource(ctx, id, a, true, catpawAllowance)
			if err != nil {
				return nil, err
			}
			return c.sharedQuota(ctx, id, a)
		}
		return ws, err
	}
	ws, err := c.quota(ctx, id, a)
	if e, ok := err.(*Error); ok && e.Status == 401 && a.Metadata.OIDC != nil {
		a, err = c.resolveCatpawResource(ctx, id, a, true, catpawAllowance)
		if err != nil {
			return nil, err
		}
		return c.quota(ctx, id, a)
	}
	return ws, err
}

type modelEntry struct {
	models []Model
	at     time.Time
}

var modelCache = struct {
	sync.Mutex
	entries map[string]modelEntry
}{entries: map[string]modelEntry{}}

func (c *Client) cachedIDEModels(ctx context.Context) ([]Model, error) {
	cred, e := c.IDECredential()
	if e != nil {
		return nil, e
	}
	// 同一登录态在配置/停用个人 Friday AppId 后的目录不同；AppId 只以
	// 摘要进入键，不落到缓存文件或日志。
	sum := sha256.Sum256(append(encoded(cred), []byte("\x00"+c.customFridayAppID)...))
	key := fmt.Sprintf("%p/%s/%x", c.HTTP, c.Home, sum)
	modelCache.Lock()
	old := modelCache.entries[key]
	modelCache.Unlock()
	if len(old.models) > 0 && time.Since(old.at) < time.Minute {
		return old.models, nil
	}
	ms, complete, e := c.ideModels(ctx)
	if e != nil {
		return nil, e
	}
	if !complete {
		// 参数目录失败时仍返回模型用于推理，但不能缓存缺参数的目录：
		// 下一次调用重新请求 model-types。
		return ms, nil
	}
	modelCache.Lock()
	if len(modelCache.entries) >= 32 {
		for k := range modelCache.entries {
			delete(modelCache.entries, k)
			break
		}
	}
	modelCache.entries[key] = modelEntry{ms, time.Now()}
	modelCache.Unlock()
	return ms, nil
}

func catpawQuotaKey(a Auth) string {
	if a.Metadata.OIDC != nil {
		return a.Metadata.OIDC.ID + "/" + a.Metadata.OIDC.ConsoleToken
	}
	return a.Metadata.Cookie
}
