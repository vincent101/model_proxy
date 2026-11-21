package sankuai

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"
)

const cliGenerationFailed = "CatPaw CLI 上游生成失败，本次回复未完成"
const cliErrorTextLimit = 64 << 10

func isGeminiModel(model string) bool {
	model = model[strings.LastIndexByte(model, '/')+1:]
	return strings.HasPrefix(strings.ToLower(model), "gemini-")
}

// model=error 已确定这是失败。只有完整的结构化 422 会改变原有的重试分类。
func cliErrorStatus(text string) int {
	head, payload, ok := strings.Cut(strings.TrimSpace(text), ", response:")
	reason, match := strings.CutPrefix(head, "422 ")
	if !ok || !match || strings.TrimSpace(reason) == "" || strings.ContainsAny(reason, ",\r\n") {
		return http.StatusBadGateway
	}
	var record Object
	payload = strings.TrimSpace(payload)
	if strings.HasPrefix(payload, "[") {
		var items []json.RawMessage
		if json.Unmarshal([]byte(payload), &items) != nil || len(items) != 1 {
			return http.StatusBadGateway
		}
		payload = string(items[0])
	}
	if json.Unmarshal([]byte(payload), &record) == nil {
		d := obj(record["error"])
		if d["code"] == float64(422) && strings.TrimSpace(str(d["message"])) != "" {
			return http.StatusUnprocessableEntity
		}
	}
	return http.StatusBadGateway
}

type cliErrorText struct {
	text strings.Builder
	full bool
}

func (d *cliErrorText) add(s string) {
	if d.full {
		return
	}
	n := cliErrorTextLimit - d.text.Len()
	if len(s) >= n {
		if len(s) > n {
			for n > 0 && !utf8.RuneStart(s[n]) {
				n--
			}
			s = s[:n]
		}
		d.full = true
	}
	d.text.WriteString(s)
}
func (d *cliErrorText) content(message Object) {
	for _, v := range arr(message["content"]) {
		if b := obj(v); b["type"] == "text" {
			d.add(str(b["text"]))
		}
	}
}
func (d *cliErrorText) failure() error {
	text := d.text.String()
	if strings.TrimSpace(text) == "" {
		text = cliGenerationFailed
	}
	return &Error{Status: cliErrorStatus(text), Code: "upstream_error", Message: text, modelError: true}
}

// 错误标记后的正文单独收集。即使上游中断或不再发送帧，也交付已经读到的报错。
func readCLIError(ctx context.Context, body io.Closer, scanner *bufio.Scanner, message Object) error {
	var detail cliErrorText
	detail.content(message)
	timer := time.AfterFunc(2*time.Second, func() { _ = body.Close() })
	defer timer.Stop()
	for !detail.full {
		frame, err := nextFrame(scanner)
		if err != nil || frame == nil || frame["type"] == "message_stop" || frame["type"] == "message_start" {
			break
		}
		switch frame["type"] {
		case "content_block_start":
			if b := obj(frame["content_block"]); b["type"] == "text" {
				detail.add(str(b["text"]))
			}
		case "content_block_delta":
			if d := obj(frame["delta"]); d["type"] == "text_delta" {
				detail.add(str(d["text"]))
			}
		}
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return detail.failure()
}
