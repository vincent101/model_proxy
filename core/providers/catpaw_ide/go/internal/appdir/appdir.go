// Package appdir 是 Catboss appdir 的最小本地实现；只解析调用方给出的环境，
// 不探测真实用户配置目录。
package appdir

import (
	"os"
	"path/filepath"
)

func Getenv(key string) string { return os.Getenv(key) }

func ExternalHome() string {
	if v := os.Getenv("CATPAW_HOME"); v != "" {
		return v
	}
	return ""
}

func Cache() string {
	if v := os.Getenv("CATPAW_CACHE"); v != "" {
		return v
	}
	if h := ExternalHome(); h != "" {
		return filepath.Join(h, "cache")
	}
	return ""
}
