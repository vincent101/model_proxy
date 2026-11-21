// Package filelock 是 Catboss filelock 的最小本地实现：进程内互斥 + 锁文件占位。
package filelock

import (
	"os"
	"sync"
)

type Mode int

const Wait Mode = 0

var locks sync.Map

func Lock(path string, _ Mode) (func(), bool) {
	m, _ := locks.LoadOrStore(path, &sync.Mutex{})
	mu := m.(*sync.Mutex)
	mu.Lock()
	f, _ := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	return func() {
		if f != nil {
			_ = f.Close()
		}
		mu.Unlock()
	}, true
}
