// Package proc 是 Catboss proc 的最小本地实现：只做可执行文件查找与探测命令构造。
package proc

import (
	"context"
	"os/exec"
)

func Look(name string) (string, error) { return exec.LookPath(name) }

func ProbeContext(ctx context.Context, bin string, args ...string) *exec.Cmd {
	return exec.CommandContext(ctx, bin, args...)
}
