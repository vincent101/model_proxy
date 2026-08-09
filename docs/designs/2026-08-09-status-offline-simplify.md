---
type: design-decision
status: confirmed
target: tools/model_proxy
tags: [model_proxy, cli, status]
---

# status 离线分支精简（仅保留进程态 + config mtime）

## 背景

`model_proxy_cli.sh status`（cmd_status）在线分支保留不变；离线分支当前调 `_format_ops.py status-offline` 输出完整诊断（health/degraded/active sessions/config 计数）。用户离线时只需知道"进程在不在 + config 还在不在"，完整诊断口径过度。

## 改动清单

### 1. 主改动（model_proxy_cli.sh cmd_status 离线分支）

离线分支（pid 为空时）将 python 调用替换为一行 config mtime echo：

```bash
# 现：
echo "service NOT running on port $MODEL_PROXY_PORT"
python3 "$SCRIPT_DIR/_format_ops.py" status-offline "$CONFIG_FILE" "$TOTALS_FILE" "$LOG_FILE"
return 1

# 改为：
echo "service NOT running on port $MODEL_PROXY_PORT"
echo "  config mtime $(stat -f "%Sm" -t "%m-%d %H:%M" "$CONFIG_FILE")"
return 1
```

在线分支（L134-167）原封不动。

### 2. 死代码清理（_format_ops.py）

删离线专属代码（在线 `status-format` 仍用 `_format_status_from_json`，不动）：

- `_format_status_offline` 函数（~15 行）
- `main()` 里 `status-offline` 分发分支（~10 行）
- **L27 `from core.commands import SessionOverridesSidecar`**（删 `_format_status_offline` 后该 import 无消费方；`SessionOverridesSidecar` 类本身仍被 `core/server.py` 多处使用，不受影响）
- docstring 中 status-offline 提及（L8）及 L12-14 关于"status-offline 取覆盖数 / commands.py 重依赖拖慢 CLI status"的约束注释（前提已不成立）

### 3. 测试清理（tests/test_format_ops.py）

- 删 `_format_status_offline` 的 import
- 删整个 `TestStatusOffline` 类（L387-452）——含 3 个离线 test 方法 + `_make_config`/`_make_totals` 两个辅助方法（仅离线测试用）

**不删**：`_format_status_from_json` 及其 ~15 个测试用例（在线路径仍用，在 `TestStatusFormatFromJson` / `TestStatusFormatWithSessions` 两个类里）。

### 4. README 联动

`tools/model_proxy/README.md`：
- L616 status 命令注释末尾离线统一展示的描述改为"离线只报进程态 + config mtime"
- L54 若提及"配置概览"措辞校正

## 不变项

- 在线 status 输出形态（health/degraded/active sessions/cooldown/config 计数）完全不变
- `_format_status_from_json` / `load_supply_health` / `_compute_degraded` / `_format_active_sessions` / `_supply_refs` 全保留（在线仍用）
- server 侧 `/model_proxy/status` endpoint 保留（通用 admin API）
- 退出码语义：config 缺失 return 1、离线 return 1、在线隐式 0

## 验证

1. 跑 `tests/test_format_ops.py` 确认删测试后全绿
2. 离线实测：停代理后 `model_proxy_cli.sh status` 输出恰 2 行
3. 在线实测：起代理后 `model_proxy_cli.sh status` 输出与现状一致（回归）
4. `bash -n model_proxy_cli.sh` 确认 cmd_status 改动无语法错

## 工作量

约 50 行 diff（主改动 1 行 + 死代码 ~25 行 + 测试 3 函数 + README 散点）。
