// Package agenttrace 是 Catboss agenttrace 的最小本地实现：默认不采集、不发送。
package agenttrace

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"time"
)

type Event struct {
	Source        string
	Kind          string
	User          string
	Model         string
	Trace         string
	Span          string
	Session       string
	Provider      string
	Status        int
	StatusMessage string
	Start         time.Time
	End           time.Time
}

// Provider 恒为 false：薄桥不启用 Catboss 的模型追踪上报。
func Provider(string) bool { return false }

func Submit(context.Context, []Event, func() bool) {}

func NewID(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return ""
	}
	return hex.EncodeToString(b)[:n]
}

func ID(s string, n int) string {
	sum := make([]byte, n)
	for i := 0; i < n; i++ {
		sum[i] = s[i%len(s)]
	}
	return hex.EncodeToString(sum)[:n]
}
