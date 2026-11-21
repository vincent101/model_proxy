package sankuai

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"catboss/internal/filelock"
)

// SharedSSOStore 是应用自己保存的凭据，不读取浏览器或其他客户端。
// 按 MIS 保存唯一来源会话；账号只保存引用，续期在进程间串行。
type SharedSSOStore struct{ Path string }
type sharedSSOEntry struct {
	MIS       string            `json:"mis"`
	Session   *CatpawSession    `json:"session,omitempty"`
	CheckedAt int64             `json:"checkedAt,omitempty"`
	Revoked   bool              `json:"revoked,omitempty"`
	Bindings  map[string]string `json:"bindings,omitempty"`
	Denied    map[string]bool   `json:"denied,omitempty"`
}
type sharedSSOState struct {
	Version    int                       `json:"version"`
	Identities map[string]sharedSSOEntry `json:"identities"`
}

func (st *SharedSSOStore) edit(ctx context.Context, change func(*sharedSSOState) (bool, error)) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	unlock, held := filelock.Lock(st.Path+".lock", filelock.Wait)
	defer unlock()
	if !held {
		return failure("共享登录凭据正在更新，请稍后重试")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	state := sharedSSOState{Version: 1, Identities: map[string]sharedSSOEntry{}}
	b, err := os.ReadFile(st.Path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return failure("无法读取已保存的共享登录")
	}
	if err == nil {
		if json.Unmarshal(b, &state) != nil || state.Version != 1 || state.Identities == nil {
			return failure("共享登录文件无效，未覆盖已保存内容")
		}
		for key, e := range state.Identities {
			if !validSecret(e.MIS) || strings.ToLower(e.MIS) != key || !e.Revoked && !e.Session.valid() {
				return failure("共享登录文件无效，未覆盖已保存内容")
			}
		}
	}
	write, err := change(&state)
	if err != nil || !write {
		return err
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	b, err = json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	dir := filepath.Dir(st.Path)
	if err = os.MkdirAll(dir, 0700); err != nil {
		return failure("无法保存共享登录")
	}
	f, err := os.CreateTemp(dir, ".sso-session-*")
	if err != nil {
		return failure("无法保存共享登录")
	}
	name := f.Name()
	defer os.Remove(name)
	if err = f.Chmod(0600); err == nil {
		_, err = f.Write(append(b, '\n'))
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return failure("无法保存共享登录")
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	if os.Rename(name, st.Path) != nil {
		return failure("无法保存共享登录")
	}
	return nil
}
func (st *SharedSSOStore) Keep(ctx context.Context, s *CatpawSession, channel string) error {
	if !s.valid() {
		return catpawLoginRequired()
	}
	return st.edit(ctx, func(state *sharedSSOState) (bool, error) {
		copy := *s
		if copy.SharedID == "" {
			copy.SharedID = copy.ID
		}
		copy.ID = copy.SharedID
		key := strings.ToLower(copy.MIS)
		e := state.Identities[key]
		if e.Bindings == nil {
			e.Bindings = map[string]string{}
		}
		if e.Denied == nil {
			e.Denied = map[string]bool{}
		}
		e.MIS = copy.MIS
		e.Session = &copy
		e.CheckedAt = time.Now().UnixMilli()
		e.Revoked = false
		e.Bindings[s.ID] = channel
		delete(e.Denied, s.ID)
		state.Identities[key] = e
		return true, nil
	})
}

// Resolve 只在文件缺失该身份时迁移应用原先保存的完整会话；退出标记拒绝旧副本复活。
func (st *SharedSSOStore) Resolve(ctx context.Context, seed *CatpawSession, channel string, force bool, prepare func(*CatpawSession, bool) error) (*CatpawSession, error) {
	if seed == nil || !validSecret(seed.ID) || !validSecret(seed.MIS) {
		return nil, catpawLoginRequired()
	}
	// 网络续期按身份串行，但不占用凭据文件锁，退出与本地推理检查可立即完成。
	digest := sha256.Sum256([]byte(strings.ToLower(seed.MIS)))
	unlock, held := filelock.Lock(fmt.Sprintf("%s.refresh-%x.lock", st.Path, digest[:8]), filelock.Wait)
	defer unlock()
	if !held {
		return nil, failure("共享登录正在续期，请稍后重试")
	}
	var s CatpawSession
	var checkedAt int64
	err := st.edit(ctx, func(state *sharedSSOState) (bool, error) {
		key := strings.ToLower(seed.MIS)
		e, exists := state.Identities[key]
		if e.Denied[seed.ID] {
			return false, catpawLoginRequired()
		}
		if !exists || e.Revoked {
			if !seed.valid() {
				return false, catpawLoginRequired()
			}
			copy := *seed
			if copy.SharedID == "" {
				copy.SharedID = copy.ID
			}
			copy.ID = copy.SharedID
			e.MIS, e.Session, e.Revoked = copy.MIS, &copy, false
		}
		bindingNew := e.Bindings[seed.ID] == ""
		if e.Bindings == nil {
			e.Bindings = map[string]string{}
		}
		e.Bindings[seed.ID] = channel
		s, checkedAt = *e.Session, e.CheckedAt
		s.ID, s.SharedID = seed.ID, e.Session.ID
		state.Identities[key] = e
		return !exists || bindingNew, nil
	})
	if err != nil {
		return nil, err
	}
	refresh := force || time.Since(time.UnixMilli(checkedAt)) >= time.Minute
	before := s
	if err := prepare(&s, refresh); err != nil {
		return nil, err
	}
	if s.MIS != seed.MIS || s.SharedID != before.SharedID {
		return nil, catpawLoginRequired()
	}
	err = st.edit(ctx, func(state *sharedSSOState) (bool, error) {
		key := strings.ToLower(seed.MIS)
		e := state.Identities[key]
		// 退出、删除、换身份的操作优先；迟到的网络响应不能恢复来源。
		if e.Revoked || e.Denied[seed.ID] || e.Bindings[seed.ID] != channel || e.Session == nil || e.Session.ID != before.SharedID {
			return false, catpawLoginRequired()
		}
		stored := s
		stored.ID = s.SharedID
		e.Session = &stored
		if refresh {
			e.CheckedAt = time.Now().UnixMilli()
		}
		state.Identities[key] = e
		return s != before || refresh, nil
	})
	if err != nil {
		return nil, err
	}
	return &s, nil
}
func (st *SharedSSOStore) Pick(ctx context.Context, mis string) (*CatpawSession, error) {
	var out *CatpawSession
	err := st.edit(ctx, func(state *sharedSSOState) (bool, error) {
		for _, e := range state.Identities {
			if e.Revoked || mis != "" && !strings.EqualFold(mis, e.MIS) {
				continue
			}
			if out != nil && mis == "" {
				out = nil
				return false, nil
			}
			s := *e.Session
			s.SharedID = s.ID
			out = &s
		}
		return false, nil
	})
	return out, err
}

// Identities lists the MIS of every active shared sign-in, sorted. It is
// how a caller tells "nobody signed in" from "more than one identity",
// which Pick answers with nil both for.
func (st *SharedSSOStore) Identities(ctx context.Context) ([]string, error) {
	var out []string
	err := st.edit(ctx, func(state *sharedSSOState) (bool, error) {
		for _, e := range state.Identities {
			if !e.Revoked {
				out = append(out, e.MIS)
			}
		}
		return false, nil
	})
	sort.Strings(out)
	return out, err
}

// Forget 只退出指定渠道账号；其他渠道的来源会话与绑定继续有效。
func (st *SharedSSOStore) Forget(ctx context.Context, mis, accountID string) error {
	if !validSecret(mis) || !validSecret(accountID) {
		return catpawLoginRequired()
	}
	return st.edit(ctx, func(state *sharedSSOState) (bool, error) {
		key := strings.ToLower(mis)
		e := state.Identities[key]
		e.MIS = mis
		if e.Denied == nil {
			e.Denied = map[string]bool{}
		}
		e.Denied[accountID] = true
		delete(e.Bindings, accountID)
		if len(e.Bindings) == 0 {
			e.Session = nil
			e.CheckedAt = 0
			e.Revoked = true
		}
		state.Identities[key] = e
		return true, nil
	})
}
func (st *SharedSSOStore) Active(ctx context.Context, seed *CatpawSession, channel string) error {
	if seed == nil || !validSecret(seed.ID) || !validSecret(seed.MIS) {
		return catpawLoginRequired()
	}
	return st.edit(ctx, func(state *sharedSSOState) (bool, error) {
		key := strings.ToLower(seed.MIS)
		e, exists := state.Identities[key]
		if e.Denied[seed.ID] {
			return false, catpawLoginRequired()
		}
		if !exists || e.Revoked {
			if !seed.valid() {
				return false, catpawLoginRequired()
			}
			s := *seed
			if s.SharedID == "" {
				s.SharedID = s.ID
			}
			s.ID = s.SharedID
			e.MIS = s.MIS
			e.Session = &s
			e.Revoked = false
		}
		if e.Bindings[seed.ID] != "" {
			return false, nil
		}
		if e.Bindings == nil {
			e.Bindings = map[string]string{}
		}
		e.Bindings[seed.ID] = channel
		state.Identities[key] = e
		return true, nil
	})
}
func SharedSSOReference(s *CatpawSession) *CatpawSession {
	if s == nil {
		return nil
	}
	return &CatpawSession{ID: s.ID, SharedID: s.SharedID, MIS: s.MIS}
}
