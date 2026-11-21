package sankuai

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"catboss/internal/appdir"
	"github.com/ledongthuc/pdf"
)

const mediaMax = 20 << 20

func sourceURL(s string) (*url.URL, error) {
	u, e := url.Parse(s)
	if e != nil || u.Hostname() == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil {
		return nil, invalid("附件 URL 须为 HTTP 或 HTTPS，不能包含账号密码")
	}
	return u, nil
}
func (c *Client) download(ctx context.Context, target string) ([]byte, string, error) {
	for i := 0; i <= 5; i++ {
		u, e := sourceURL(target)
		if e != nil {
			return nil, "", e
		}
		res, e := c.do(ctx, "GET", u.String(), http.Header{}, nil)
		if e != nil {
			return nil, "", e
		}
		if res.StatusCode >= 300 && res.StatusCode < 400 {
			res.Body.Close()
			loc, e := url.Parse(res.Header.Get("Location"))
			if e != nil || loc.String() == "" {
				return nil, "", failure("附件重定向无效")
			}
			target = u.ResolveReference(loc).String()
			continue
		}
		defer res.Body.Close()
		if res.StatusCode != 200 {
			return nil, "", failure("附件下载失败")
		}
		b, e := limited(res.Body, mediaMax)
		return b, strings.ToLower(strings.Split(res.Header.Get("Content-Type"), ";")[0]), e
	}
	return nil, "", failure("附件重定向过多")
}
func (c *Client) mediaBytes(ctx context.Context, b Object) ([]byte, string, error) {
	s := obj(b["source"])
	switch s["type"] {
	case "text":
		return []byte(str(s["data"])), str(s["media_type"]), nil
	case "base64":
		raw := str(s["data"])
		if len(raw) > mediaMax*4/3+4 {
			return nil, "", invalid("附件超过 20 MiB")
		}
		v, e := base64.StdEncoding.Strict().DecodeString(raw)
		if e != nil || len(v) == 0 || len(v) > mediaMax {
			return nil, "", invalid("附件 Base64 无效")
		}
		return v, str(s["media_type"]), nil
	case "url":
		return c.download(ctx, str(s["url"]))
	}
	return nil, "", invalid("附件须使用文本、Base64 或 HTTP/HTTPS URL")
}
func (c *Client) image(ctx context.Context, b, cred Object) (string, error) {
	return c.uploadImage(ctx, b, ideHeaders(cred))
}

// CatPaw stores an image under a name derived from its bytes. The IDE resends
// the whole conversation every turn, so a fresh object name per upload makes an
// unchanged image a different URL each turn: the session prefix chain in
// ide.go then never matches and the upstream conversation cache misses.
// The map outlives a turn and survives a restart in the user's cache folder:
// the same bytes are uploaded once, not once per process.
const (
	imageUploadTTL  = 7 * 24 * time.Hour
	imageUploadMax  = 4096
	imageUploadFile = "catpaw-image-urls.json"
)

type imageUploadCache struct {
	sync.Mutex
	entries map[string]imageUpload
	loaded  bool
	path    string
}

var imageUploads = imageUploadCache{entries: map[string]imageUpload{}}

type imageUpload struct {
	url string
	at  time.Time
}

type imageUploadDisk struct {
	Version int                          `json:"version"`
	Entries map[string]imageUploadStored `json:"entries"`
}

type imageUploadStored struct {
	URL string `json:"url"`
	At  int64  `json:"at"`
}

func imageUploadTake(key string) string {
	imageUploads.Lock()
	defer imageUploads.Unlock()
	imageUploads.loadLocked()
	now := time.Now()
	for k, v := range imageUploads.entries {
		if now.Sub(v.at) > imageUploadTTL {
			delete(imageUploads.entries, k)
		}
	}
	return imageUploads.entries[key].url
}

func imageUploadSet(key, target string) {
	imageUploads.Lock()
	defer imageUploads.Unlock()
	imageUploads.loadLocked()
	now := time.Now()
	for k, v := range imageUploads.entries {
		if now.Sub(v.at) > imageUploadTTL {
			delete(imageUploads.entries, k)
		}
	}
	if len(imageUploads.entries) >= imageUploadMax {
		var oldestKey string
		var oldest time.Time
		for k, v := range imageUploads.entries {
			if oldestKey == "" || v.at.Before(oldest) {
				oldestKey, oldest = k, v.at
			}
		}
		delete(imageUploads.entries, oldestKey)
	}
	imageUploads.entries[key] = imageUpload{url: target, at: time.Now()}
	imageUploads.saveLocked()
}

func (s *imageUploadCache) loadLocked() {
	if s.loaded {
		return
	}
	s.loaded = true
	path := s.path
	if path == "" {
		if dir := appdir.Cache(); dir != "" {
			path = filepath.Join(dir, imageUploadFile)
		}
		s.path = path
	}
	if path == "" {
		return
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var disk imageUploadDisk
	if json.Unmarshal(raw, &disk) != nil || disk.Version != 1 {
		return
	}
	for key, entry := range disk.Entries {
		if entry.URL != "" {
			s.entries[key] = imageUpload{url: entry.URL, at: time.Unix(entry.At, 0)}
		}
	}
}

func (s *imageUploadCache) saveLocked() {
	if s.path == "" {
		return
	}
	disk := imageUploadDisk{Version: 1, Entries: map[string]imageUploadStored{}}
	for key, entry := range s.entries {
		disk.Entries[key] = imageUploadStored{URL: entry.url, At: entry.at.Unix()}
	}
	raw, err := json.Marshal(disk)
	if err != nil {
		return
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0700); err != nil {
		return
	}
	temp := s.path + ".tmp"
	if err := os.WriteFile(temp, raw, 0600); err != nil {
		return
	}
	_ = os.Rename(temp, s.path)
}

func (c *Client) uploadImage(ctx context.Context, b Object, h http.Header) (string, error) {
	data, mime, e := c.mediaBytes(ctx, b)
	if e != nil {
		return "", e
	}
	types := map[string]string{"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
	if types[mime] == "" {
		mime = http.DetectContentType(data)
	}
	if types[mime] == "" {
		return "", invalid("图片须为 PNG、JPEG、GIF 或 WebP")
	}
	sum := sha256.Sum256(data)
	digest := hex.EncodeToString(sum[:])
	// Content only: a routing group may send the same conversation through
	// several CatPaw accounts, and all of them name this object the same way.
	key := digest + "." + types[mime]
	if target := imageUploadTake(key); target != "" {
		return target, nil
	}
	config, e := c.json(ctx, "GET", "https://catpaw.sankuai.com/api/s3/url", h, nil, "image-upload")
	if e != nil {
		return "", e
	}
	base := str(config["data"])
	if !strings.Contains(base, "://") {
		base = "http://" + base
	}
	public, e := sourceURL(base)
	if e != nil || public.RawQuery != "" || public.Fragment != "" {
		return "", failure("CatPaw 图片存储地址无效")
	}
	name := "copilot/chat/" + digest + "." + types[mime]
	q := url.Values{"objectName": {name}, "contentType": {mime}, "isUpload": {"true"}}
	signed, e := c.json(ctx, "GET", "https://catpaw.sankuai.com/api/s3/presign/generate?"+q.Encode(), h, nil, "image-upload")
	if e != nil {
		return "", e
	}
	target, e := sourceURL(str(signed["data"]))
	if e != nil {
		return "", e
	}
	if target.Scheme == "http" && target.Host == "s3plus.sankuai.com" {
		target.Scheme = "https"
	}
	res, e := c.do(ctx, "PUT", target.String(), http.Header{"Content-Type": {mime}}, data)
	if e != nil {
		return "", e
	}
	res.Body.Close()
	if res.StatusCode != 200 {
		return "", failure("CatPaw 图片上传失败")
	}
	object := strings.TrimRight(public.String(), "/") + "/" + name
	imageUploadSet(key, object)
	return object, nil
}
func (c *Client) document(ctx context.Context, b Object) (result string, err error) {
	defer func() {
		if recover() != nil {
			result, err = "", invalid("文件结构损坏，无法提取文字")
		}
	}()
	data, mime, e := c.mediaBytes(ctx, b)
	if e != nil {
		return "", e
	}
	if len(data) == 0 || len(data) > mediaMax {
		return "", invalid("文件为空或超过 20 MiB")
	}
	title := str(b["title"])
	ext := strings.ToLower(path.Ext(title))
	var content string
	if bytes.HasPrefix(data, []byte("%PDF-")) || mime == "application/pdf" || ext == ".pdf" {
		r, e := pdf.NewReader(bytes.NewReader(data), int64(len(data)))
		if e != nil {
			return "", invalid("PDF 无效或加密")
		}
		if r.NumPage() > 512 {
			return "", invalid("PDF 超过 512 页")
		}
		var out strings.Builder
		for i := 1; i <= r.NumPage(); i++ {
			if e := ctx.Err(); e != nil {
				return "", e
			}
			s, e := r.Page(i).GetPlainText(nil)
			if e != nil {
				return "", invalid("PDF 文字提取失败")
			}
			out.WriteString(s)
			out.WriteByte('\n')
			if out.Len() > 400000 {
				return "", invalid("PDF 提取文字超过限制")
			}
		}
		content = out.String()
	} else if ext == ".docx" || mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" {
		z, e := zip.NewReader(bytes.NewReader(data), int64(len(data)))
		if e != nil || len(z.File) > 2048 {
			return "", invalid("DOCX 无效")
		}
		var total uint64
		var doc *zip.File
		ct := false
		for _, f := range z.File {
			total += f.UncompressedSize64
			if total > 40<<20 || f.Flags&1 != 0 {
				return "", invalid("DOCX 解压超过 40 MiB 或已加密")
			}
			if f.Name == "word/document.xml" {
				doc = f
			}
			ct = ct || f.Name == "[Content_Types].xml"
		}
		if doc == nil || !ct {
			return "", invalid("DOCX 缺少文档")
		}
		reader, e := doc.Open()
		if e != nil {
			return "", e
		}
		defer reader.Close()
		d := xml.NewDecoder(io.LimitReader(reader, 40<<20))
		var out strings.Builder
		inText := false
		for {
			t, e := d.Token()
			if e == io.EOF {
				break
			}
			if e != nil {
				return "", invalid("DOCX XML 无效")
			}
			switch t := t.(type) {
			case xml.StartElement:
				if t.Name.Local == "t" {
					inText = true
				}
			case xml.EndElement:
				if t.Name.Local == "t" {
					inText = false
				}
				if t.Name.Local == "p" {
					out.WriteByte('\n')
				}
			case xml.CharData:
				if inText {
					out.Write(t)
				}
			}
		}
		content = out.String()
	} else if ext == ".ipynb" {
		var notebook Object
		if json.Unmarshal(data, &notebook) != nil {
			return "", invalid("Notebook JSON 无效")
		}
		var out strings.Builder
		for _, r := range arr(notebook["cells"]) {
			s := obj(r)["source"]
			if v, ok := s.(string); ok {
				out.WriteString(v)
			} else {
				for _, v := range arr(s) {
					out.WriteString(str(v))
				}
			}
			out.WriteByte('\n')
		}
		content = out.String()
	} else {
		if !utf8.Valid(data) {
			return "", invalid("文件不是有效 UTF-8")
		}
		content = strings.TrimPrefix(string(data), "\ufeff")
		for _, r := range content {
			if r < 32 && r != '\n' && r != '\r' && r != '\t' || r == 127 {
				return "", invalid("文件含二进制内容")
			}
		}
	}
	if strings.TrimSpace(content) == "" {
		return "", invalid("文件没有可提取文字，不提供扫描件 OCR")
	}
	if len([]rune(content)) > 100000 {
		return "", invalid("文件提取文字超过 100000 字符")
	}
	return content, nil
}
