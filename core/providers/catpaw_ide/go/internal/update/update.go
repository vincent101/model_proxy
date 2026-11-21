// Package update 是 Catboss update 的最小本地实现：仅比较点分版本号。
package update

import (
	"strconv"
	"strings"
)

func Newer(candidate, current string) bool {
	a, ok1 := parse(candidate)
	b, ok2 := parse(current)
	if !ok1 {
		return false
	}
	if !ok2 {
		return true
	}
	for i := 0; i < len(a) || i < len(b); i++ {
		var x, y int
		if i < len(a) {
			x = a[i]
		}
		if i < len(b) {
			y = b[i]
		}
		if x != y {
			return x > y
		}
	}
	return false
}

func parse(v string) ([]int, bool) {
	parts := strings.Split(strings.TrimSpace(v), ".")
	out := make([]int, 0, len(parts))
	for _, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil {
			return nil, false
		}
		out = append(out, n)
	}
	return out, true
}
