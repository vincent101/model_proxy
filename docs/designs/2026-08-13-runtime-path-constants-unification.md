---
type: design-decision
status: confirmed
target: tools/model_proxy
tags: [architect, config, path-management]
---

# 运行时路径常量统一管理方案

## 当前实施状态（2026-08-13，经 architect-max 复核修正）

首次落地时 Bash 侧有变量名 bug（python 用 `k.upper()` 生成 `LOG`/`TOTALS`/`LOCK`，但 cli.sh 后续代码用 `$LOG_FILE`/`$TOTALS_FILE`/`$LOCK_FILE`，名字不匹配导致变量为空、服务启动失败 `No such file or directory`）。已回退。

当前各文件状态：
- `core/server.py`：**已落地且正确**（`resolve_runtime_paths` + `_DEFAULT_PATHS` + `init_logging(log_path)` + main() 调用链），import OK、`resolve_runtime_paths()` 输出正确、661 测试通过。**未回退，保留。**
- `config/runtime_paths.json`：**已创建且正确**，保留。
- `model_proxy_cli.sh`：**已回退到硬编码**，需按本方案 §4.1 重新落地。
- `hooker/ensure_model_proxy.sh`：**已回退到硬编码**，需按本方案 §4.2 重新落地。

**经 architect-max 复核修正的三个硬伤**：
1. **变量名 bug**（首次事故根因）：python 输出改用显式映射表 `{config_key: shell_var_name}`，不用 `k.upper()`。
2. **eval 兜底虚假安全网**：`|| fallback` 只在 python3 退出码非 0 时触发，兜不住 python 逻辑 bug（退出码 0 但变量名错）。改为 eval 后**显式校验关键变量非空**。
3. **cli/server 读取不对称**：server 启动时读一次（不热重载），cli 每次调用都读。运行中改 json 会造成 server 写旧路径、cli 读新路径。已文档化为 §5 约束："修改 runtime_paths.json 后必须重启 server"。

**其他修正**：删除 `MODEL_PROXY_PATHS` 环境变量（无明确需求，避免与 `MODEL_PROXY_CONFIG` 混淆）；cli.sh 映射删除冗余 `LOCK_FILE`（cli 不用进程锁，只 server 用）。

**中间态风险（C12）**：当前 server.py 已读 runtime_paths.json，cli.sh/hooker.sh 仍用硬编码。因 json 值=默认值=硬编码值，目前一致；但若有人改 json 会立即不一致。**需尽快落地 Bash 侧消除此风险。**

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
# paths 文件路径（固定，不提供 env 覆盖——runtime_paths.json 是基础设施路径，
# 无"不同环境用不同路径文件"的需求，避免与 MODEL_PROXY_CONFIG 混淆）
PATHS_FILE="$SCRIPT_DIR/config/runtime_paths.json"

# ---- 从 runtime_paths.json 加载运行时路径（启动时执行一次）----
# 注意：eval 注入的 shell 变量名必须与后续代码使用的变量名完全一致。
# cli.sh 后续代码用 $LOG_FILE / $TOTALS_FILE（$LOCK_FILE 只 server.py 用，cli 不需要）。
# 因此 python 输出用显式映射 {config_key: shell_var_name}，不能用 k.upper()。
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
# config key -> cli.sh 变量名（必须与后续代码一致）
mapping = {
    'log': 'LOG_FILE',
    'totals': 'TOTALS_FILE',
}
defaults = {
    'log': os.path.join(base, '.model_proxy.log'),
    'totals': os.path.join(base, '.model_proxy_totals.json'),
}
for k, var in mapping.items():
    v = paths.get(k, defaults[k])
    if not v.startswith('/'):
        v = os.path.join(base, v)
    print(f'{var}=\"{v}\"')
" "$base" "$PATHS_FILE" 2>/dev/null)"
  # eval 后校验关键变量非空——|| 兜底兜不住 python 逻辑 bug（退出码 0 但变量名错），
  # 必须显式检查变量是否注入成功
  if [[ -z "$LOG_FILE" || -z "$TOTALS_FILE" ]]; then
    LOG_FILE="$SCRIPT_DIR/.model_proxy.log"
    TOTALS_FILE="$SCRIPT_DIR/.model_proxy_totals.json"
  fi
}
load_runtime_paths
```

替换现有第 11-13 行的硬编码（删掉 LOCK_FILE，cli.sh 不用进程锁）。后续代码中 `$LOG_FILE` / `$TOTALS_FILE` 变量名不变，只是来源从硬编码变为文件读取。

**关键设计点**：
- python 输出用显式映射表 `{log:LOG_FILE, totals:TOTALS_FILE}`，不能用 `k.upper()`（会生成 `LOG`/`TOTALS`，与后续代码的 `$LOG_FILE` 等不匹配，导致变量为空、服务启动失败——这是首次落地的真实事故根因）。
- `eval` 后**显式校验变量非空**，不依赖 `||` 兜底（`||` 只在 python3 退出码非 0 时触发，兜不住 python 逻辑正确性 bug）。
- 不引入 `MODEL_PROXY_PATHS` 环境变量——runtime_paths.json 是基础设施路径，无多环境需求，避免与 `MODEL_PROXY_CONFIG` 混淆。

**性能**：每次 CLI 调用只起一次 python3 进程（约 50-80ms），cli.sh 本身就是交互工具，无性能问题。

#### 4.2 hooker/ensure_model_proxy.sh 改造

hooker 需要的路径与 cli.sh 部分重叠（lock）+ 独有（pid/ensure_log/start_lock）。同样用 python3 读 `runtime_paths.json`：

```bash
PATHS_FILE="$SCRIPT_DIR/../config/runtime_paths.json"

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
# config key -> hooker.sh 变量名
mapping = {
    'pid': 'PID_FILE',
    'ensure_log': 'ENSURE_LOG',
    'start_lock': 'START_LOCK',
}
defaults = {
    'pid': '/tmp/model_proxy.pid',
    'ensure_log': '/tmp/model_proxy_ensure.log',
    'start_lock': '/tmp/model_proxy_start.lock',
}
for k, var in mapping.items():
    v = paths.get(k, defaults[k])
    if not v.startswith('/'):
        v = os.path.join(base, v)
    print(f'{var}=\"{v}\"')
" "$base" "$PATHS_FILE" 2>/dev/null)"
  # eval 后校验关键变量非空（同 cli.sh，不依赖 || 兜底）
  if [[ -z "$PID_FILE" || -z "$ENSURE_LOG" || -z "$START_LOCK" ]]; then
    PID_FILE="/tmp/model_proxy.pid"
    ENSURE_LOG="/tmp/model_proxy_ensure.log"
    START_LOCK="/tmp/model_proxy_start.lock"
  fi
}
load_hooker_paths
```

替换现有第 9-14 行硬编码。变量名从 `LOG`/`LOCKDIR` 改为 `ENSURE_LOG`/`START_LOCK`（语义更清晰，与 config key 对齐）。已核实无外部引用这些变量名，安全。同样用显式映射表生成变量名，不能用 `k.upper()`。eval 后显式校验变量非空。

**hooker.sh 变量改名涉及 6 处引用**（implementer 落地时需逐行核对）：
- `$LOG` 3 处：定义行 10、使用行 38（nohup 重定向）、行 51（tail）
- `$LOCKDIR` 3 处：定义行 14、使用行 15（mkdir）、行 20（trap rmdir）

#### 4.3 为何不用 jq / grep/sed

- **jq**：macOS 默认未安装，引入外部依赖。项目原则是"仅使用标准库，不引入第三方依赖"，Bash 侧同样应保持。
- **grep/sed 提取 JSON**：脆弱，路径含特殊字符（空格/引号）时会断。
- **python3 -c**：项目已依赖 python3（server.py / _config_ops.py / _format_ops.py / _install_ops.py 全是 Python），cli.sh 已有此模式（`get_admin_token()`）。零新增依赖，最可靠。

### 5. cli/server 读取不对称约束

**关键约束：修改 `runtime_paths.json` 后必须重启 server，不能只 reload。**

读取时机的差异：
- **server.py**：`main()` 启动时调一次 `resolve_runtime_paths()`，路径存入 `_runtime_paths`，此后不变（**不参与热重载**——这是有意决策，运行中改日志路径会丢失连续性，改锁路径会破坏进程锁）。
- **cli.sh / hooker.sh**：每次调用都执行 `load_runtime_paths`，重新读 `runtime_paths.json`。

**不对称后果**：运行中改 `runtime_paths.json` 的 `log` 路径 → server 继续写旧路径（内存里是旧值），cli.sh 读新路径 → **server 写 A 文件、cli 读 B 文件**，`cli.sh logs`/`status` 看不到数据。

**为什么这样设计仍合理**：
1. `runtime_paths.json` 是基础设施路径，改动频率极低（几乎只在初次配置时改一次）。
2. server 不热重载路径是必要的——运行中切换日志文件会丢日志、切换锁文件会破坏进程互斥。
3. cli 每次读是为了在 server 未运行时（`cli.sh on`/`off`、故障排查）也能拿到正确路径——这些场景恰恰是最需要路径常量的，不能依赖 server 在线。

**文档化此约束**：在 `runtime_paths.json` 文件内无法加注释（JSON），但：
- 本设计文档显式记录（本节）。
- `.gitignore` 顶部注释提醒（§6）。
- `runtime_paths.json` 的路径值默认与硬编码一致，不主动改就不会触发不对称。

### 6. .gitignore 处理与第二真相源说明

**.gitignore 无法从 config 读，这是纯文本文件的固有限制。接受".gitignore 仍需手动同步"这个例外。**

但例外范围已缩到最小：
- `/tmp/` 下的文件（lock/pid/ensure_log/start_lock）不需要 .gitignore 条目——它们在仓库外。
- 只有落在 model_proxy 目录内的文件需要 .gitignore 条目——当前只有 `log` 和 `totals`（及其 corrupt 备份）。

**缓解措施**：在 `.gitignore` 顶部加注释提醒"修改 log/totals 路径时需同步更新 .gitignore + runtime_paths.json"。

**第二真相源（fallback 默认值）说明**：

`runtime_paths.json` 是主真相源（json 存在时三方都从它读）。但 Python `_DEFAULT_PATHS`（server.py）和 Bash 各脚本的 defaults 字典（cli.sh/hooker.sh 的 `if [[ -z ]]` fallback）各自硬编码了与 json 相同的路径值，构成**第二真相源**。

- **json 存在时**：fallback 是 dead code，不触发。改名只需改 json + .gitignore，主路径达成"单一真相源"。
- **json 缺失时**（如故障排查删了 json）：fallback 生效，用硬编码默认值。若改名时只改 json 不改 fallback，此时会写旧路径。

**改名时的完整清单**（以 log 改名为例）：

| # | 文件 | 性质 | 必须改？ |
|---|------|------|---------|
| 1 | `config/runtime_paths.json` | 主真相源 | 是 |
| 2 | `core/server.py` `_DEFAULT_PATHS` | fallback 默认值 | 是（保持兜底一致） |
| 3 | `model_proxy_cli.sh` defaults + fallback | fallback 默认值 | 是（保持兜底一致） |
| 4 | `hooker/ensure_model_proxy.sh` defaults + fallback | fallback 默认值 | 是（保持兜底一致） |
| 5 | `.gitignore` | git 忽略规则 | 是 |

严格说"改名只改一处"在主路径成立，但 fallback 默认值需同步——这是为了让兜底行为与主路径一致。若接受"json 缺失时用旧名"的妥协，可只改 json + .gitignore，但不推荐（会导致故障排查时行为不一致）。

### 7. 默认值表

| key | 默认值（runtime_paths.json 缺失时） | 与当前硬编码一致？ |
|-----|------|---|
| `log` | `<model_proxy>/.model_proxy.log` | 是（server.py:51, cli.sh:11）|
| `totals` | `<model_proxy>/.model_proxy_totals.json` | 是（server.py:134, cli.sh:12）|
| `lock` | `/tmp/model_proxy.lock` | 是（server.py:327, cli.sh:13）|
| `pid` | `/tmp/model_proxy.pid` | 是（hooker.sh:9）|
| `ensure_log` | `/tmp/model_proxy_ensure.log` | 是（hooker.sh:10）|
| `start_lock` | `/tmp/model_proxy_start.lock` | 是（hooker.sh:14）|

**向后兼容**：`runtime_paths.json` 不存在 → 全部回退默认值 → 行为与当前完全一致。

### 8. 启动顺序与循环依赖（总结）

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

### 9. 影响面

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

### 10. 与 repo-split 的关系

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
