package sankuai

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"strings"

	"github.com/google/uuid"
)

var cliConversationNamespace = uuid.MustParse("d4d410d2-6d7e-50aa-9b6c-d9166a289edf")

func validCLISession(s string) bool {
	if s == "" || len(s) > 128 {
		return false
	}
	for _, c := range s {
		if c < 0x21 || c > 0x7e {
			return false
		}
	}
	return true
}

// prepareCLISession keeps Claude Code's structured user_id verbatim. Other
// callers get the same session envelope; an opaque user_id remains inside it.
// The session, rather than a per-request id or a rotating inference token,
// lets mcli reuse Gemini's cached context (see catpaw-cli-protocol.md).
func prepareCLISession(body Object, headers http.Header, account string) (string, error) {
	metadata := obj(body["metadata"])
	if body["metadata"] != nil && metadata == nil {
		return "", invalid("CatPaw CLI metadata 必须是对象")
	}
	if v := metadata["user_id"]; v != nil {
		if _, ok := v.(string); !ok {
			return "", invalid("CatPaw CLI metadata.user_id 必须是字符串")
		}
	}
	raw := str(metadata["user_id"])
	var identity Object
	if json.Unmarshal([]byte(raw), &identity) == nil && identity != nil {
		if session := str(identity["session_id"]); validCLISession(session) {
			return session, nil
		}
	}
	conversation := ""
	for _, h := range []string{"X-Claude-Code-Session-Id", "X-Catboss-Session", conversationHeader} {
		if s := strings.TrimSpace(headers.Get(h)); validCLISession(s) {
			// The gateway's anonymous id hashes the raw opening, including
			// cache marks. Derive it below instead; explicit client ids win.
			if h == conversationHeader && cliDerivedConversation(s) {
				continue
			}
			conversation = s
			break
		}
	}
	if conversation == "" {
		for _, raw := range arr(body["messages"]) {
			m := obj(raw)
			if m["role"] == "user" {
				conversation = string(encoded(cliSessionOpening(m)))
				break
			}
		}
	}
	session := conversation
	if parsed, err := uuid.Parse(session); err == nil && len(session) == 36 {
		session = parsed.String()
	} else {
		session = uuid.NewSHA1(cliConversationNamespace, []byte(conversation)).String()
	}
	if identity == nil {
		identity = Object{}
		if raw != "" {
			identity["user_id"] = raw
		}
	}
	if _, exists := identity["device_id"]; !exists {
		sum := sha256.Sum256([]byte("catboss-cli\x00" + account))
		identity["device_id"] = hex.EncodeToString(sum[:])
	}
	if _, exists := identity["account_uuid"]; !exists {
		identity["account_uuid"] = ""
	}
	identity["session_id"] = session
	if metadata == nil {
		metadata = Object{}
		body["metadata"] = metadata
	}
	metadata["user_id"] = string(encoded(identity))
	return session, nil
}

func cliDerivedConversation(s string) bool {
	raw, ok := strings.CutPrefix(s, "catboss-")
	if !ok || len(raw) != 24 {
		return false
	}
	_, err := hex.DecodeString(raw)
	return err == nil
}

// Cache breakpoints move between turns; they are not part of an opening's id.
func cliSessionOpening(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := Object{}
		for k, value := range x {
			if k != "cache_control" {
				out[k] = cliSessionOpening(value)
			}
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, value := range x {
			out[i] = cliSessionOpening(value)
		}
		return out
	}
	return v
}
