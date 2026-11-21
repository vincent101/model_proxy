package sankuai

import (
	"context"
	"strings"
	"sync"
	"time"

	"catboss/internal/lazyre"
	"catboss/internal/proc"
	"catboss/internal/update"
)

const cliVersionFallback = "0.2.5"
const cliVersionRefresh = 5 * time.Minute

var cliVersionPattern = lazyre.New(`^CatPaw CLI version ((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$`)

var cliVersions = &cliVersionCache{read: loadCLIVersion, now: time.Now}

// 只在 CLI 请求需要时探测；短期缓存和并发合并避免每次推理都启动 mc。
type cliVersionCache struct {
	mu      sync.Mutex
	value   string
	checked time.Time
	pending chan struct{}
	read    func(context.Context) string
	now     func() time.Time
}

func (c *cliVersionCache) get(ctx context.Context) string {
	c.mu.Lock()
	value := c.value
	if value == "" {
		value = cliVersionFallback
	}
	age := c.now().Sub(c.checked)
	if ctx.Err() != nil || (!c.checked.IsZero() && age >= 0 && age < cliVersionRefresh) {
		c.mu.Unlock()
		return value
	}
	if pending := c.pending; pending != nil {
		c.mu.Unlock()
		select {
		case <-pending:
			return c.get(ctx)
		case <-ctx.Done():
			return value
		}
	}
	c.pending = make(chan struct{})
	c.mu.Unlock()

	version := c.read(ctx)
	c.mu.Lock()
	// 已取消的探测不占用新的缓存周期，其他请求仍可重新读取。
	if ctx.Err() == nil {
		c.value, c.checked = version, c.now()
		value = version
	}
	close(c.pending)
	c.pending = nil
	c.mu.Unlock()
	return value
}

func loadCLIVersion(ctx context.Context) string {
	bin, err := proc.Look("mc")
	if err != nil {
		return cliVersionFallback
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	return readCLIVersion(ctx, bin)
}

func readCLIVersion(ctx context.Context, bin string) string {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	cmd := proc.ProbeContext(ctx, bin, "--version")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return cliVersionFallback
	}
	if err = cmd.Start(); err != nil {
		return cliVersionFallback
	}
	raw, readErr := limited(stdout, 4096)
	if readErr != nil {
		cancel()
	}
	err = cmd.Wait()
	if err != nil || readErr != nil || ctx.Err() != nil {
		return cliVersionFallback
	}
	match := cliVersionPattern.FindStringSubmatch(strings.TrimSpace(string(raw)))
	if match == nil || !update.Newer(match[1], cliVersionFallback) {
		return cliVersionFallback
	}
	return match[1]
}
