---
type: design-decision
status: confirmed
target: tools/model_proxy
tags: [architect, config, path-management]
---

# 运行时路径常量统一管理方案

## 背景与问题

model_proxy 是 Python + Bash 混合项目，运行时文件路径（LOG_FILE / TOTALS_FILE / LOCK_FILE / PID_FILE 等）在 6 处各自硬编码，没有单一真相源。改名要改 6+ 处，漏一处就出 bug（真实事故：implementer 只改了 server.py + .gitignore，漏改 cli.sh 和 hooker.sh，导致 `model_proxy_cli.sh status` 报"日志文件缺失"）。

目标：将所有运行时路径常量集中到独立配置文件 `config/runtime_paths.json`，Python 和 Bash 都从该文件读，实现单一真相源。路径常量与业务配置（supplies/routes/strategies/cooldown_rules）分离，职责清晰。

## 方案设计

### 1. 配置文件：config/runtime_paths.json

新建独立文件 `config/runtime_paths.json`，集中所有运行时文件路径：

```json
{
  "log": ".model_proxy.log",
  "totals": ".model_proxy_totals.json",
  "lock": "/tmp/model_proxy.lock",
  "pid": "/tmp/model_proxy.pid",
  "ensure_log": "/tmp/model_proxy_ensure.log",
  "start_lock": "/tmp/model_proxy_start.lock"
}
```

**路径常量清单（6 个，覆盖现有所有硬编码点）：**

| key | 用途 | Python 消费方 | Bash 消费方 | 默认值（文件缺失时回退） |
|-----|------|--------------|-------------|------------------------|
| `log` | 服务运行日志 | server.py LOG_FILE | cli.sh LOG_FILE | `tools/model_proxy/.model_proxy.log` |
| `totals` | 用量账本 | server.py TOTALS_FILE | cli.sh TOTALS_FILE | `tools/model_proxy/.model_proxy_totals.json` |
| `lock` | 进程互斥锁 | server.py _LOCK_FILE | cli.sh LOCK_FILE | `/tmp/model_proxy.lock` |
| `pid` | 进程 PID 文件 | — | hooker.sh PID_FILE | `/tmp/model_proxy.pid` |
| `ensure_log` | hooker 启动日志 | — | hooker.sh ENSURE_LOG | `/tmp/model_proxy_ensure.log` |
| `start_lock` | hooker 并发启动锁 | — | hooker.sh START_LOCK | `/tmp/model_proxy_start.lock` |

**为什么独立文件而非放进 model_proxy_config.json：**
- 职责分离：`model_proxy_config.json` 是业务配置（渠道/路由/策略/冷却），`runtime_paths.json` 是运行时基础设施路径。两者变更频率和关注者不同。
- 安全隔离：`model_proxy_config.json` 含 appkey 被 .gitignore，`runtime_paths.json` 无敏感信息可入库。
- 加载时机不同：路径在 bootstrap 阶段（ConfigStore 之前）就要用，独立文件让 bootstrap 读取更轻量（不必解析整个业务配置）。

### 2. 路径相对/绝对规则

**相对路径以 model_proxy 目录为基准（即 `tools/model_proxy/`，也就是 runtime_paths.json 的 `parent.parent`）。**

理由：
- Python 侧 `server.py` 的 `__file__.resolve().parent.parent` = `model_proxy/`，与 `runtime_paths.json` 的 `parent.parent` = `model_proxy/`，天然同锚点。
- Bash 侧 `cli.sh` 的 `SCRIPT_DIR` = `model_proxy/`，同锚点。
- hooker.sh 的 `SCRIPT_DIR` = `model_proxy/hooker/`，但 `SCRIPT_DIR/..` = `model_proxy/`，仍同锚点。

**解析规则：**
- 绝对路径（以 `/` 开头）：直接使用，不做转换。
- 相对路径：Python 侧 `Path(base_dir) / relative_path`，Bash 侧 `"$BASE_DIR/$relative_path"`。
- `base_dir` 的计算：Python = `runtime_paths.json` 的 `parent.parent`（即 `model_proxy/`），Bash = `SCRIPT_DIR`（即 `model_proxy/`）。

**推荐默认配置**：`log` 和 `totals` 用相对路径（落在 model_proxy 目录内，便于 cli.sh 直接 grep/tail），`lock`/`pid`/`ensure_log`/`start_lock` 用绝对路径（`/tmp/`，与现有行为一致，系统重启自动清理）。

### 3. Python 侧改造

#### 3.1 路径解析函数（新增，独立于 ConfigStore）

路径是启动时常量，**不参与热重载**。在 ConfigStore 实例化之前由独立函数解析：

```python
# server.py 新增

_PACKAGE_DIR = Path(__file__).resolve().parent.parent  # model_proxy/
_RUNTIME_PATHS_FILE = _PACKAGE_DIR / "config" / "runtime_paths.json"

_DEFAULT_PATHS = {
    "log": str(_PACKAGE_DIR / ".model_proxy.log"),
    "totals": str(_PACKAGE_DIR / ".model_proxy_totals.json"),
    "lock": "/tmp/model_proxy.lock",
    "pid": "/tmp/model_proxy.pid",
    "ensure_log": "/tmp/model_proxy_ensure.log",
    "start_lock": "/tmp/model_proxy_start.lock",
}

def resolve_runtime_paths(paths_file: Path = _RUNTIME_PATHS_FILE) -> dict[str, Path]:
    """启动时一次性解析所有运行时路径。不参与热重载。

    runtime_paths.json 缺失/corrupt → 全部回退默认值。
    相对路径以 paths_file.parent.parent（即 model_proxy/）为基准。
    """
    paths = {}
    base = paths_file.parent.parent  # model_proxy/
    try:
        with open(paths_file, "r", encoding="utf-8") as f:
            raw_paths = json.load(f)
    except (json.JSONDecodeError, OSError):
        raw_paths = {}
    for key, default in _DEFAULT_PATHS.items():
        val = raw_paths.get(key, default)
        p = Path(val)
        if not p.is_absolute():
            p = base / p
        paths[key] = p
    return paths
```

#### 3.2 启动顺序改造（main() 函数）

当前 main() 的顺序：
```
init_logging()          → 用 LOG_FILE
UsageTotalsStore()      → 用 TOTALS_FILE
flock(_LOCK_FILE)       → 用 _LOCK_FILE
ConfigStore(config_path) → 加载 config
```

改造后：
```
# Phase 0: 解析路径（bootstrap，不依赖 ConfigStore）
runtime_paths = resolve_runtime_paths()

# Phase 1: 用解析后的路径装配日志/锁/账本
init_logging(runtime_paths["log"])
usage_totals = UsageTotalsStore(runtime_paths["totals"])
flock(runtime_paths["lock"])

# Phase 2: 完整加载 ConfigStore（业务配置，与路径无关）
config_store = ConfigStore(config_path, ...)
```

#### 3.3 鸡生蛋问题解法

**问题**：日志路径要从 config 读，但 config 加载失败时要记日志——日志路径从哪来？

**解法：bootstrap 默认值兜底。**

`resolve_runtime_paths()` 在 `runtime_paths.json` 缺失/corrupt 时回退到 `_DEFAULT_PATHS`。这些默认值是硬编码常量（与当前行为完全一致），不依赖 config。因此：
- `runtime_paths.json` 正常 → 从文件读路径，用配置的路径记日志。
- `runtime_paths.json` 缺失/corrupt → 用默认路径记日志，`init_logging()` 后第一件事就是 `log.warning("runtime_paths.json load failed, using default paths")`。

**不存在的死循环**：默认路径是编译期常量，不需要 config 就能知道。bootstrap 阶段永远有路径可用。

#### 3.4 模块级常量改造

现有模块级 `LOG_FILE` / `TOTALS_FILE` / `_LOCK_FILE` 改为：

```python
# 模块级只保留默认值（供 _DEFAULT_PATHS 引用 + 测试 import 时不触碰文件）
_LOG_FILE_DEFAULT = _PACKAGE_DIR / ".model_proxy.log"
_TOTALS_FILE_DEFAULT = _PACKAGE_DIR / ".model_proxy_totals.json"
_LOCK_FILE_DEFAULT = Path("/tmp/model_proxy.lock")

# 运行时实际路径（main() 启动路径赋值，测试 import 时为 None）
_runtime_paths: dict[str, Path] | None = None
```

`init_logging()` / `UsageTotalsStore` / `main()` 中的锁逻辑都改为从 `_runtime_paths` 取路径。模块级函数签名不变，只是路径来源从硬编码常量变为 `_runtime_paths` 字典查找。

**测试兼容**：现有测试 import `core.server` 时不调 `main()`，`_runtime_paths` 为 None。测试中若需要 LOG_FILE，走 `_DEFAULT_PATHS["log"]` 或测试自行 mock。

### 4. Bash 侧改造

#### 4.1 cli.sh 改造

cli.sh 已有 `get_admin_token()` 用 `python3 -c` 读 JSON 的模式。复用此模式读 `runtime_paths.json`，新增 `load_runtime_paths()` 函数：

```bash
# config/paths 文件路径
PATHS_FILE="${MODEL_PROXY_PATHS:-$SCRIPT_DIR/config/runtime_paths.json}"

# ---- 从 runtime_paths.json 加载运行时路径（启动时执行一次）----
load_runtime_paths() {
  local base="$SCRIPT_DIR"
  eval "$(python3 -c "
import json, sys, os
base = sys.argv[1]
paths_file = sys.argv[2]
try:
    with open(paths_file) as f:
        paths = json.load(f)
except Exception:
    paths = {}
defaults = {
    'log': os.path.join(base, '.model_proxy.log'),
    'totals': os.path.join(base, '.model_proxy_totals.json'),
    'lock': '/tmp/model_proxy.lock',
}
for k, d in defaults.items():
    v = paths.get(k, d)
    if not v.startswith('/'):
        v = os.path.join(base, v)
    print(f'{k.upper()}=\"{v}\"')
" "$base" "$PATHS_FILE" 2>/dev/null)" || {
    # python 执行失败 → 回退默认值
    LOG_FILE="$SCRIPT_DIR/.model_proxy.log"
    TOTALS_FILE="$SCRIPT_DIR/.model_proxy_totals.json"
    LOCK_FILE="/tmp/model_proxy.lock"
  }
}
load_runtime_paths
```

替换现有第 11-13 行的硬编码。后续代码中 `$LOG_FILE` / `$TOTALS_FILE` / `$LOCK_FILE` 变量名不变，只是来源从硬编码变为文件读取。

**性能**：每次 CLI 调用只起一次 python3 进程（约 50-80ms），cli.sh 本身就是交互工具，无性能问题。

#### 4.2 hooker/ensure_model_proxy.sh 改造

hooker 需要的路径与 cli.sh 部分重叠（lock）+ 独有（pid/ensure_log/start_lock）。同样用 python3 读 `runtime_paths.json`：

```bash
PATHS_FILE="${MODEL_PROXY_PATHS:-$SCRIPT_DIR/../config/runtime_paths.json}"

load_hooker_paths() {
  local base="$(cd "$SCRIPT_DIR/.." && pwd)"
  eval "$(python3 -c "
import json, sys, os
base = sys.argv[1]
paths_file = sys.argv[2]
try:
    with open(paths_file) as f:
        paths = json.load(f)
except Exception:
    paths = {}
defaults = {
    'pid': '/tmp/model_proxy.pid',
    'ensure_log': '/tmp/model_proxy_ensure.log',
    'start_lock': '/tmp/model_proxy_start.lock',
}
for k, d in defaults.items():
    v = paths.get(k, d)
    if not v.startswith('/'):
        v = os.path.join(base, v)
    print(f'{k.upper()}=\"{v}\"')
" "$base" "$PATHS_FILE" 2>/dev/null)" || {
    PID_FILE="/tmp/model_proxy.pid"
    ENSURE_LOG="/tmp/model_proxy_ensure.log"
    START_LOCK="/tmp/model_proxy_start.lock"
  }
}
load_hooker_paths
```

替换现有第 9-14 行硬编码。变量名从 `LOG`/`LOCKDIR` 改为 `ENSURE_LOG`/`START_LOCK`（语义更清晰，与 config key 对齐）。已核实无外部引用这些变量名，安全。

#### 4.3 为何不用 jq / grep/sed

- **jq**：macOS 默认未安装，引入外部依赖。项目原则是"仅使用标准库，不引入第三方依赖"，Bash 侧同样应保持。
- **grep/sed 提取 JSON**：脆弱，路径含特殊字符（空格/引号）时会断。
- **python3 -c**：项目已依赖 python3（server.py / _config_ops.py / _format_ops.py / _install_ops.py 全是 Python），cli.sh 已有此模式（`get_admin_token()`）。零新增依赖，最可靠。

### 5. .gitignore 处理

**.gitignore 无法从 config 读，这是纯文本文件的固有限制。接受".gitignore 仍需手动同步"这个例外。**

但例外范围已缩到最小：
- `/tmp/` 下的文件（lock/pid/ensure_log/start_lock）不需要 .gitignore 条目——它们在仓库外。
- 只有落在 model_proxy 目录内的文件需要 .gitignore 条目——当前只有 `log` 和 `totals`（及其 corrupt 备份）。

**缓解措施**：在 `runtime_paths.json` 旁无注释能力（JSON），但在 `.gitignore` 顶部加注释提醒"修改 log/totals 路径时需同步更新 .gitignore"。当前 .gitignore 无需改动（路径名不变）。

### 6. 默认值表

| key | 默认值（runtime_paths.json 缺失时） | 与当前硬编码一致？ |
|-----|------|---|
| `log` | `<model_proxy>/.model_proxy.log` | 是（server.py:51, cli.sh:11）|
| `totals` | `<model_proxy>/.model_proxy_totals.json` | 是（server.py:134, cli.sh:12）|
| `lock` | `/tmp/model_proxy.lock` | 是（server.py:327, cli.sh:13）|
| `pid` | `/tmp/model_proxy.pid` | 是（hooker.sh:9）|
| `ensure_log` | `/tmp/model_proxy_ensure.log` | 是（hooker.sh:10）|
| `start_lock` | `/tmp/model_proxy_start.lock` | 是（hooker.sh:14）|

**向后兼容**：`runtime_paths.json` 不存在 → 全部回退默认值 → 行为与当前完全一致。

### 7. 启动顺序与循环依赖（总结）

完整启动序列（Python 侧 main()）：

```
1. resolve_runtime_paths() → 解析路径（文件缺失/corrupt 回退默认）
2. init_logging(paths["log"]) → 装配日志 handler
3. flock(paths["lock"]) → 获取进程锁
4. UsageTotalsStore(paths["totals"]) → 实例化账本
5. ConfigStore(config_path) → 完整加载业务 config（含热重载能力）
6. ThreadingHTTPServer → 启动服务
```

循环依赖不存在：步骤 1 读取 `runtime_paths.json` 只做 `json.load`（标准库，不依赖日志），失败回退默认值（编译期常量）。步骤 2 的日志路径永远可用（要么来自文件，要么来自默认值）。

### 8. 影响面

| 文件 | 改动范围 | 说明 |
|------|---------|------|
| `config/runtime_paths.json` | **新建** | 6 个 key，值与当前默认一致 |
| `core/server.py` | 行 50-51：删除 `LOG_FILE` 硬编码，改为 `_LOG_FILE_DEFAULT`<br>行 134：删除 `TOTALS_FILE` 硬编码，改为 `_TOTALS_FILE_DEFAULT`<br>行 327：删除 `_LOCK_FILE` 硬编码，改为 `_LOCK_FILE_DEFAULT`<br>新增 `_DEFAULT_PATHS` + `_RUNTIME_PATHS_FILE` + `resolve_runtime_paths()` 函数<br>`init_logging()`：签名改为 `init_logging(log_path: Path)`<br>`main()`：调用 `resolve_runtime_paths()`，传路径给 `init_logging()`/`UsageTotalsStore`/`flock` | 核心改动 |
| `model_proxy_cli.sh` | 行 11-13：删除硬编码，改为 `load_runtime_paths` 函数调用<br>新增 `PATHS_FILE` + `load_runtime_paths()` 函数 | Bash 侧 |
| `hooker/ensure_model_proxy.sh` | 行 9-14：删除硬编码，改为 `load_hooker_paths` 函数调用<br>新增 `PATHS_FILE` + `load_hooker_paths()` | Bash 侧 |
| `.gitignore` | 无需改动（路径名不变） | 仅加注释提醒 |
| `config/model_proxy_config.json` | **不动** | 业务配置与路径分离 |
| `config/model_proxy_config.example.json` | **不动** | — |

**风险评估：**
- **server.py LOG_FILE 初始化时机**：`init_logging()` 签名从无参改为 `init_logging(log_path)`，现有调用点只有 `main()` 里一处，改动安全。模块级 `LOG_FILE` 变量改为 `_LOG_FILE_DEFAULT`（仅供默认值引用），不影响 `init_logging` 行为（init_logging 只在 main() 调用）。
- **cli.sh 性能**：每次调用起一次 python3（~50ms），cli 是交互工具，无感知。
- **hooker.sh 性能**：SessionStart hook 每次起一次 python3（~50ms），hooker 本就有 `sleep 1` + `lsof` 等耗时操作，50ms 可忽略。
- **热重载安全**：paths 不参与热重载（`resolve_runtime_paths` 只在启动时调一次）。ConfigStore 的 `maybe_reload` 不影响已解析的运行时路径。
- **hooker 变量改名**：`LOG`→`ENSURE_LOG`、`LOCKDIR`→`START_LOCK`。已核实无外部引用（_install_ops.py 引用的是脚本路径，非内部变量），安全。

### 9. 与 repo-split 的关系

**建议此改动在 repo-split 之前做。**

- `runtime_paths.json` 随 model_proxy 目录迁移，相对路径以 model_proxy 目录为基准，迁移后基准不变，路径自动正确。
- `/tmp/` 绝对路径与 repo 位置无关，不受影响。
- 路径常量统一后，拆分时内层 repo 自包含，无路径调整负担。

## 验证方式

1. **向后兼容**：`runtime_paths.json` 不存在 → `model_proxy_cli.sh status` / `logs` / `stats` 行为不变，server 正常启动（回退默认路径）。
2. **路径生效**：`runtime_paths.json` 含 `"log": "/tmp/custom.log"` → server 日志写到 `/tmp/custom.log`，`cli.sh logs` 读 `/tmp/custom.log`。
3. **Bash 读取**：`cli.sh status` 能正确显示 `runtime_paths.json` 中配置的路径对应的文件信息。
4. **hooker 读取**：修改 `runtime_paths.json` 中 `pid` → hooker 使用新 PID 文件路径。
5. **文件缺失**：删除 `runtime_paths.json` → server 启动用默认路径，cli.sh 回退默认路径。
6. **热重载安全**：运行中修改 `runtime_paths.json` 的 `log` → `reload` 后日志路径不变（paths 不参与热重载）。
7. **改名验证**：修改 `runtime_paths.json` 中 `log` 的值 → 重启 server + `cli.sh logs` 能读到新路径的日志（单一真相源验证）。
8. **测试回归**：现有 633 测试全部通过（测试 mock 路径不受影响，`_runtime_paths` 为 None 时走默认值）。

## 关联

- [[2026-07-22-access-log-and-latency]] — LOG_FILE 设计背景
- [[2026-07-23-usage-totals-ledger]] — TOTALS_FILE 设计背景
- [[2026-08-09-obsidian-git只同步手写文档-嵌套repo隔离理想架构]] — repo-split 治理规则
