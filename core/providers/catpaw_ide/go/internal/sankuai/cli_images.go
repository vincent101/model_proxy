package sankuai

import (
	"context"
	"crypto/sha256"
	"net/http"
)

// CLI 支持 URL 图片；上传复用 IDE 的 S3 接口，以 CLI 自身凭据认证。
// 只遍历协议内容块，不能改写工具参数中恰好同名的字段。
func (c *Client) cliImages(ctx context.Context, body Object, token string) error {
	uploaded := map[[32]byte]string{}
	var blocks func(any) error
	blocks = func(content any) error {
		for _, raw := range arr(content) {
			b := obj(raw)
			if b["type"] == "tool_result" {
				if err := blocks(b["content"]); err != nil {
					return err
				}
			}
			if b["type"] != "image" {
				continue
			}
			source := obj(b["source"])
			if source["type"] == "url" {
				if _, err := sourceURL(str(source["url"])); err != nil {
					return err
				}
				continue
			}
			if source["type"] != "base64" {
				return invalid("图片仅支持 Base64 或 HTTP/HTTPS URL")
			}
			key := sha256.Sum256(encoded(source))
			target, ok := uploaded[key]
			if !ok {
				var err error
				target, err = c.uploadImage(ctx, b, http.Header{"Authorization": {"Bearer " + token}})
				if err != nil {
					return err
				}
				uploaded[key] = target
			}
			b["source"] = Object{"type": "url", "url": target}
		}
		return nil
	}
	for _, raw := range arr(body["messages"]) {
		if err := blocks(obj(raw)["content"]); err != nil {
			return err
		}
	}
	return nil
}
