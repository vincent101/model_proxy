package sankuai

import "net/http"

// 说明：本文件只补齐被裁剪掉的登录/浏览器模块所导出的常量与辅助函数。
// IDE 推理链路不经过这些入口，行为保持“未登录/无服务”的 fail-closed 语义。

const (
	AuthSourceBrowser = "browser"
	AuthSourceNone    = "none"
	AuthSourceAppID   = "appid"
)

func validAppID(s string) bool {
	if s == "" || len(s) > 128 {
		return false
	}
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
		default:
			return false
		}
	}
	return true
}

// writeAuthResult 在薄桥中没有内嵌结果页；只返回状态，不做 HTML 渲染。
func writeAuthResult(w http.ResponseWriter, status int, id string, success bool) {
	w.WriteHeader(status)
}
