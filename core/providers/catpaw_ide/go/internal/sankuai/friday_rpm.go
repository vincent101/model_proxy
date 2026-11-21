package sankuai

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"sync"
	"time"
)

// FridayRPM 是本进程对 APPID/模型的观察，不代表其他客户端消耗后的全局余额。
type FridayRPM struct {
	Limit  int        `json:"limit,omitempty"`
	Used   int        `json:"used"`
	Until  *time.Time `json:"until,omitempty"`
	Reason string     `json:"reason,omitempty"` // rpm 或 upstream429
}

type fridayMinute struct {
	sent []time.Time
	cool time.Time
}
type fridayLimiter struct {
	mu      sync.Mutex
	minutes map[string]*fridayMinute
}

var fridayRPMs fridayLimiter

func FridayID(id string) bool { return id == "friday-personal" || id == "friday-team" }
func fridayKey(app, model string) string {
	h := sha256.Sum256([]byte(app))
	return hex.EncodeToString(h[:]) + "/" + model
}

func (l *fridayLimiter) minute(key string, now time.Time) *fridayMinute {
	if l.minutes == nil {
		l.minutes = map[string]*fridayMinute{}
	}
	// 只清理已经完全失效的窗口，不驱逐仍在限流的身份。
	for k, v := range l.minutes {
		if !v.cool.After(now) && (len(v.sent) == 0 || !v.sent[len(v.sent)-1].After(now.Add(-time.Minute))) {
			delete(l.minutes, k)
		}
	}
	m := l.minutes[key]
	if m == nil {
		m = &fridayMinute{}
		l.minutes[key] = m
	}
	i := 0
	for i < len(m.sent) && !m.sent[i].After(now.Add(-time.Minute)) {
		i++
	}
	m.sent = m.sent[i:]
	return m
}
func minuteState(m *fridayMinute, limit int, now time.Time) FridayRPM {
	s := FridayRPM{Limit: limit, Used: len(m.sent)}
	if limit > 0 && len(m.sent) >= limit {
		until := m.sent[len(m.sent)-limit].Add(time.Minute)
		s.Until = &until
		s.Reason = "rpm"
	}
	if m.cool.After(now) && (s.Until == nil || m.cool.After(*s.Until)) {
		until := m.cool
		s.Until = &until
		s.Reason = "upstream429"
	}
	return s
}
func (l *fridayLimiter) state(app, model string, limit int, now time.Time, take bool) FridayRPM {
	l.mu.Lock()
	defer l.mu.Unlock()
	m := l.minute(fridayKey(app, model), now)
	s := minuteState(m, limit, now)
	if take && s.Until == nil {
		// 并发调用可能以相反顺序取得锁，计数时间不能倒退。
		if n := len(m.sent); n > 0 && now.Before(m.sent[n-1]) {
			now = m.sent[n-1]
		}
		m.sent = append(m.sent, now)
	}
	return s
}
func FridayState(app, model string, limit int) FridayRPM {
	return fridayRPMs.state(app, model, limit, time.Now(), false)
}
func (l *fridayLimiter) cool(app, model string, now time.Time, retry string) {
	until := now.Add(time.Minute)
	if seconds, e := strconv.ParseFloat(retry, 64); e == nil && seconds > 0 && !math.IsInf(seconds, 0) {
		until = now.Add(max(time.Minute, time.Duration(min(math.Ceil(seconds), float64((1<<63-1)/int64(time.Second))))*time.Second))
	} else if at, e := http.ParseTime(retry); e == nil && at.After(until) {
		until = at
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	m := l.minute(fridayKey(app, model), now)
	if until.After(m.cool) {
		m.cool = until
	}
}
func (s FridayRPM) RetryAfter() string {
	if s.Until == nil {
		return "1"
	}
	return strconv.Itoa(max(1, int(math.Ceil(time.Until(*s.Until).Seconds()))))
}
func (s FridayRPM) Message() string {
	limit := "未知"
	if s.Limit > 0 {
		limit = strconv.Itoa(s.Limit)
	}
	return fmt.Sprintf("Friday %s: 本机滚动窗口已发送 %d 次，RPM %s，%s 秒后重试", s.Reason, s.Used, limit, s.RetryAfter())
}
