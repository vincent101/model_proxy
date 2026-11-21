package sankuai

import (
	"strings"

	"catboss/internal/catalog"
	"catboss/internal/lazyre"
)

var fridaySearchModel = lazyre.New(`^(google-search|bing-search(-v[0-9]+)?|tencent-search|bocha-search|zhipu-search|quark-search|baidu-search(-v[0-9]+)?|serper)$`)

// FridayConversationModel 排除 APPID 目录混入的搜索、向量等专用 API。
// 这些模型不能用 Messages/Chat 的 hi 请求判断聊天端点是否可用。
func FridayConversationModel(id string) bool {
	return catalog.ServesOf(id) == "" && !fridaySearchModel.MatchString(strings.ToLower(id))
}
