---
type: design-decision
status: draft
target: "tools/model_proxy"
tags:
  - architect
  - model-proxy
  - windows
created: 2026-11-12
---

# model_proxy 统一 Python 内核与原生 Windows CLI：务实分阶段设计草案

## 背景与问题

让同一份 Python 业务代码在 macOS 和**原生 Windows**运行，以 `.sh` / `.bat` 为入口，同时将根目录四个 Python 文件下沉；不使用 WSL，不擅改模型协议、转发和路由逻辑。用户已确定务实分阶段和根目录 A，并已指令后续在新 worktree 落地既有方案；本轮只订正设计，不创建 worktree、不实施或测试。本文件保持 **draft**：实施指令不等于未决项获确认，也不是平台通过或生产操作授权。测试隔离按 §11 务实执行，不以 OS 级全域沙箱为前置；生产行为安全和既有功能的约束不因此削弱。

调查基线：独立仓库 `tools/model_proxy` 的 HEAD 为 `7547517`；工作区已有未跟踪的 [[tools/model_proxy/docs/designs/2026-11-12-model-proxy-Windows兼容性审查与改造方案]]，本次只读，不覆盖、不标 superseded。未启动服务、跑测试、发请求、读取真实业务配置/凭据或日志内容，也未联网；只读源码、测试、路径元数据及历史文档。以下路径和行号除特别说明外相对 `tools/model_proxy/`；行号仅对该基线有效。

2026-11-13 本次订正采纳用户认可的 Mac 控制边界：保留两把 flock 的防重复启动作用，但不再要求证明同一 PID 持有两把锁；进程身份和发凭据前实际连接对端仍须核验。根据此前本机测试结论，`lsof` 和 `F_GETLKPID` 无法可靠证明 flock 持锁者；该实证**仅针对 macOS 本机**，本次未重跑，也不外推至其他系统。

### 静态事实

1. `model_proxy.py:10-14` 是薄入口；`core/server.py:3752-3763` 启动时才 `import fcntl`、`open(..., "w")` 然后非阻塞 flock，原生 Windows Python 无此模块。竞争者先截断后拿锁，可清掉持有者写的 PID；不能用文件内容判互斥。`init_logging` 在锁前执行（`server.py:3746-3756`），还会裁剪日志，竞争启动可能先触碰正在使用的日志。
2. `model_proxy_cli.sh:373-453` 依赖 `nohup/lsof/pgrep/ps/kill`；`on` 和就绪仅看端口，无法证明监听者是本代理；`off/restart` 依赖根目录入口的绝对命令行，且存在按文件名匹配监听 PID 的兜底。`hooker/ensure_model_proxy.sh:10-37` 自读 start_lock 并用 mkdir；异常中断可能留下目录。`status` 也先看监听 PID 再请求控制 API。
3. `config/runtime_paths.json` 是已跟踪的基础路径文件：log/totals 相对项目根；lock、pid、ensure_log、start_lock 当前是 `/tmp/model_proxy...`。`core/server.py:79-124`、shell CLI、hook 分别实现解析和 fallback；服务启动时解析一次，CLI 每次启动解析，因此运行时改路径可能分叉。根入口迁移**不应**改变项目根基准。
4. `_install_ops.py:30-41` 用自身 `__file__` 定项目根、推导 vault 根和 hook、catalog 模板；其 `choose_token:145-165` 会打印 client token，`preview_confirm_write:172-218` 展示原始 diff、备份后直接写文件；Codex catalog 会联网（`466-504`）。`_config_ops.py`、`core/commands.py` 的 chmod 不能证明 Windows ACL 私密。`appkey_file` 在 `server.py:1500-1531` 每请求用 `expanduser` 读取。
5. 当前 CLI 有 `on/off/restart/status/reload/logs/stats/supply/route/strategy/switch/install` 和帮助；**没有独立的 `config` 子命令，本设计明确不新增**。现有测试的 Python 类型写法提示 3.10 为候选下限，实际支持版本需在对应真实解释器验证。

## 方案设计

### 1. 已定边界、非目标与共同契约

1. **已定**：务实分阶段，而非缩减最终功能；根目录 A，即只将现有四个根 Python 脚本下沉，根目录仍可保留 `README.md`、`VERSION`、`CHANGELOG.md`、`.gitignore`、现有目录和运行文件。`.sh`/`.bat` 是根目录面向用户的 CLI 入口；不因整理根目录而迁移/删除配置、日志、账本或历史文档。
2. **共同 CLI**：最终 macOS `.sh` 与 Windows 原生 `.bat` 只做解释器发现、定位入口、参数转交和返回退出码；命令分发、输入、输出、校验、菜单、reload 判断、日志和统计均在一套 Python 中。系统差异仅限锁、进程、路径、权限等底层适配。POSIX 实现应兼容 macOS/Linux 的差异，但 Linux 未实测，是否作为正式支持平台是待决项。
3. **同一份 Python，不等于代码不改**：允许改 import、路径、锁、生命周期、安全写、命令编排、测试与平台分支；`core/server.py` 的路由、模型协议、转发、配置字段及对外控制 API 不因平台移植改语义。新增内部进程控制通道属于另一个取舍，未经用户确认不加入。
4. 既有 CLI 命令完整保留；**不新增 `config` 子命令**。此前 draft 中 `config` 候选撤销；`config` 仍按未知命令处理。原有 `supply/route/strategy/switch/install` 是配置管理入口。最终目标是 Windows 功能覆盖，不意味着可在未验证时承诺所有第三方客户端 hook 已可用。
5. **用户已确认的方向**：路径配置文件缺失、损坏或不可读取时拒绝启动；有效 object **只缺一个字段**时沿用该字段原默认并执行实例安全检查；手动 reload 即使加载失败也继续尝试清 cooldown；**仅安装输出**脱敏；短期保持 Mac 现有手动 `off/restart` 和非 TTY 使用习惯，生产重启由用户手动决定，助手不自动操作线上；发送管理凭据前先核验本机连接对象，无法核验则不发送并报告未知；`ConfigStore.reload` 上报实际加载成败，管理接口成功 HTTP 200、失败非 2xx 且原因脱敏。用户另说明当前线上是 Mac、没有 Windows 进程，这不批准未来 Windows 强制停止。**仍未确认**：Windows 停止机制；全量 ACCESS 日志/stats 账本脱敏、跨进程持久冻结、每次交互确认和非 TTY 行为改变均不在已授权范围。可实施部分已有后续实施指令，但 `status: draft` 不变；未决项和真实环境/生产操作仍须分别获确认或授权，门禁未过不得部署。

### 2. 模块、入口及依赖方向

**目标入口与 import**（路径均相对项目根）：

```text
model_proxy_cli.sh ──┐
                     ├─> core/cli/main.py ─> core.cli.dispatch ─> lifecycle / observe / menu
model_proxy_cli.bat ─┘                                        └> _config_ops / _format_ops / _install_ops
hooker/ensure_model_proxy.sh|.bat ─> 同一 Python CLI 的 on
core/cli/model_proxy.py ─> core.server.main
                                  └> core.runtime_paths / platform_lock / secure_files
```

- `core/cli/main.py` 是唯一 CLI bootstrap：由 `__file__.resolve().parents[2]` 取得项目根，将其插入 import 路径后调用 `core.cli.dispatch.main(argv)`；其他模块使用绝对包名，不以调用者 cwd 或移动后的 helper 位置猜项目根。`core/cli/model_proxy.py` 同样只自举根、调用 `core.server.main()`，服务 subprocess 使用该文件**绝对路径**与当前 `sys.executable`，不改变调用者 cwd。这样 `MODEL_PROXY_CONFIG` 的现有相对路径含义不因入口 `cd` 改变。
- `_config_ops.py`、`_format_ops.py`、`_install_ops.py` 移至 `core/cli/`，分别以 `core.cli._config_ops`、`core.cli._format_ops`、`core.cli._install_ops` 导入；`core.reasoning.registry` 等已有包名不变。删除旧的按 `__file__` 将 helper 目录当项目根的注入。`_install_ops` 的模板、vault、hook 均接收/解析真正项目根。测试和 probe 的 import、字符串 patch 目标同步迁至包路径，避免同一模块以新旧两名字加载。
- `core/cli/dispatch.py` 负责参数和命令路由；`lifecycle.py` 负责 on/off/restart 状态机；`observe.py` 迁移 shell 的 logs、stats 编排并复用 `_format_ops`；`menus.py` 管菜单和 `_config_ops` 调用；`core/cli/control.py` 是手动和隐式 reload/status 唯一可信连接与结果解析入口；`core/discovery.py` 提供与可变运行配置无关的安装锚点和发现；`core/platform_process.py`、`platform_lock.py`、`runtime_paths.py`、`secure_files.py` 提供底层适配。各模块接收显式上下文和可注入依赖，不自行探测第二套路径。
- 复用 `_config_ops` 的现有 CRUD、验证和 `done()`/reload marker；共同 CLI 可由 `core/cli/helper_entry.py` 以同一 Python 和绝对路径启动已移动 helper，保留 stdin 实时交互、marker 及 helper 130 语义。该入口只接受内部 helper 标识和参数，自举后以规范包名导入，将 helper 所需参数恢复为原 `sys.argv`，传播 `SystemExit` 和中断，不扩展公开子命令。若将调用改为直接函数，应先有返回结果契约和等价测试，不能在不同平台复制 CRUD。install/格式化同样复用既有实现；这一单一 helper 子进程路径可长期保留，并不是两套业务实现。
- **阶段 1 即消除 marker 安全旁路**：暂留的 macOS shell 菜单必须经 `helper_entry.py` 以项目根自举后调用 `core.cli._config_ops`，传入私有 marker；helper 正常返回后 marker 为 `yes` 才调用 `helper_entry.py` 的内部 reload 分支，再进入同一 `core.cli.control`。手动 `reload` 也进入该路径；删除旧 `reload_proxy → curl`，绝不可在 shell 读取 admin token 或把它放入 curl/其他进程 argv。marker 缺失或 `no` 不发送，单次 `yes` 最多一次发送；helper 失败/130 不凭残留 marker 重载。该内部 helper 分支不是新增公开 CLI 命令；结果和退出码按 §3.1 汇总。
- **阶段 1 路径和安装输出门禁**：暂留 shell 不得执行旧 `load_runtime_paths`/`eval`/整文件默认回退，也不能以一次预检后继续按旧路径读写；需要路径、状态或写入的操作均通过同一 Python resolver/anchor。`install` 的候选、diff、成功消息和手工片段须经共同 helper 脱敏；管理 token 不进 argv。`status/strategy/logs/stats` 保持原有 ACCESS/stats 内容和 `[cc]/[codex]` 显示，不将全量脱敏冒充本批要求；既有敏感数据风险明确保留。marker、路径和安装输出门禁必须对实际操作生效；做不到或无法保住原 macOS 命令，阶段 1 不得上线。
- `.sh` 最终用自身目录构造 Python bootstrap 绝对路径，原样传 `"$@"`、`exec` 传播返回码；不保留菜单、`eval` 路径、`pgrep` 流程或 logs/stats heredoc。`.bat` 用 `%~dp0`、`setlocal DisableDelayedExpansion` 和 `%*`，结束 `exit /b` 原样传播；只在**解释器发现阶段**尝试 Windows `py -3` 或 `python`，不能因业务命令失败而换解释器重跑。版本下限先实测后声明；不依赖 WSL 或 Git Bash。调用者 cmd 已展开的 `%`/元字符不能由 BAT 还原。
- stdout、stderr、换行、退出码和交互时序由 Python 调度一次决定；shell/bat 不解析输出或二次判断成功。平台差异只通过底层接口注入。子进程用参数列表、`shell=False`，不将 admin token 传给进程命令行；当前 `switch <client_token> <route_id>` 的既有参数暴露风险见 §6。

### 3. 既有命令、参数、输出与退出码对照

旧行为以 `model_proxy_cli.sh:67-127,145-716` 和 helper 源码为静态基线；先在隔离副本采集 golden（stdout/stderr、rc、文件变化、交互与 API 次数），不将帮助文档当作唯一真相。下表“保持”是实施目标，不宣称已实现；安全异常的兼容变化见表后。

| 用户入口及既有参数 | 当前事实 / 拟复用实现 | 必测契约与特殊点 |
|---|---|---|
| 无参数、`-h`、`--help` | shell 显示帮助；在 dispatch 前先解析 listen | 保持命令和先期 listen 验证顺序；不启动服务，不因路径文件缺损阻止帮助；`.sh`/`.bat` 入口名、换行和编码允许差异，见 §11 |
| `on` | `lsof` 看端口、`nohup` 启动，循环等待；现有端口占用被当成功 | 迁 Python 生命周期；无实例启动、已是本实例幂等、无关占端口、锁冲突、超时对拍；无关占端口由旧 0 改为已获批准的非零 |
| `off` | 旧绝对路径 `pgrep` + 监听文件名兜底；无匹配 rc 0 | 两平台进入共同 Python 生命周期；Mac 仅对可核验所属进程发送 SIGTERM。Windows 停止机制未确认时直接安全失败、非零且不结束进程；强制停止只是候选，见 §5 |
| `restart` | `off` 后等进程与端口，最多约 5 秒再 `on`；会中断请求和丢内存态 | Mac 等目标退出+锁+端口，失败非零、不盲目双启；非 TTY 用法不新增确认，生产重启由用户手动决定。Windows 停止机制未确认时直接安全失败、非零，不停止也不启动，见 §5、§9 |
| `status` | 检查监听，调 `/model_proxy/status`，再走 `_format_ops.status-format` | 离线原 rc 1；保持字段和格式化单源；身份未知不带 token 请求；JSON 错误输出和原 rc 对拍 |
| `reload` | 调 `/model_proxy/reload`；旧服务即使内部加载失败也可能返回 200，且清空 cooldown；旧 shell 的 `echo` 可能掩盖失败 | 已确认 §3.1：进入已认证 handler 后加载失败才适用“尝试清 cooldown”；CLI 无法获得凭据/listen 或核验连接时不发送、非零，不能声称已清。自动 mtime 重载不清 cooldown |
| `logs [N] [req=x\|level=x\|event=x]` | 默认末 30 条 ACCESS；支持过滤和数量任意次序；shell 循环保留最后出现的数量/过滤器 | 包含 0、多个参数、未知字段、无匹配、日志缺失、大文件；原 `grep` 的基本正则及 `grep | tail` rc 与 Python 实现对拍，不暗改成字面包含或 Python 正则 |
| `stats [total\|today\|month\|YYYY-MM\|YYYY-MM-DD]` | shell 内嵌 Python 从账本投影；UTC+8、累计/日/月 | 空账本原返回提示；首参数非法原 rc 1；当前实现仅处理首个选择器，额外参数不能悄悄改变行为；日/月归档、空数据和大数精确对拍 |
| `supply` | list 后 TTY 菜单 `a/e/d/t/q`，t 会做连通性/effort 探测 | 复用 `_config_ops`；非 TTY 当前 list 后退出 0；`supply add ...` 并非公开直达，现有多余参数被忽略；交互/取消/130/reload marker 对拍，网络全 mock |
| `route` | list 后 TTY 菜单 `a/e/d/q` | 复用 helper；非 TTY 当前 list 后 0；输入、取消、无效项、额外参数和 marker 对拍 |
| `strategy` | list 后 TTY 菜单 `a/e/d/q` | 同上；本批保持现有显示和持久内容，不扩大全量脱敏；仅安装输出按本批脱敏 |
| `switch <client_token> <route_id>` | 修改绑定，成功按 marker reload；缺任一参数 rc 1；多余参数未进入 helper | 原命令继续可用、不新增 CLI 选项；输入含空格/特殊字符、取消、配置失败、成功仅一次 reload；命令行/历史泄漏无法消除 |
| `install` | 调 `_install_ops`，交互选 Claude/Codex/Hermes/OpenClaw；shell 向 `cmd_install` 传第二参数但函数不使用 | 保持菜单和确认；不能新增直达参数。**本批仅安装输出脱敏**，不改 ACCESS 日志、stats 账本、`[cc]/[codex]` 显示或持久内容。README 称非 TTY 只 list，但源码 `_interactive_select()` 直接 `input()`，EOF/管道实际结果须隔离实测，不能擅自改变 |
| 未知命令，包括 `config` | shell 报 Unknown command 并 rc 1 | 保持失败；`config` 不作为别名、菜单或隐藏新功能 |

**返回码与输出门禁**：已确认异常非零；身份不明、无关端口占用、权限/锁错误和可识别的控制请求失败均返回非零。reload 按用户确认的实际加载结果及 §3.1 返回，现有 API 的失败状态将改变，不能承诺旧客户端能自动正确处理。安装输出按本批脱敏；ACCESS 日志、stats 账本、`[cc]/[codex]` 显示保持现状。正常场景保留原命令，允许平台入口名、路径、换行、终端编码差异；旧 `reload` 的 echo、`off` 的管道、`logs` 的 `tail` 曾可能掩盖异常的兼容差异须逐项记录。`_config_ops` 的 130 是内部信号，顶层最终返回码按真机基线确认。

### 3.1 reload：真实加载结果上报（用户已确认方向）

**现有源码基线（本轮未测试线上）**：手动 CLI 通过 `POST /model_proxy/reload` 触发 `ConfigStore.reload()` 并清空 cooldown；配置编辑 helper 的 `yes` marker 也触发该请求，但 shell 返回 helper 自己的状态。当前 `reload()` 不向 handler 返回 `_reload_locked()` 的 bool，handler 即使加载失败也可能回应 HTTP 200/`{"ok": true}`；旧 CLI 用 `echo` 打印 Reloaded，可能掩盖请求失败。旧 handler 只有在 `cs.reload()` 返回后才执行清理，未捕获异常或进程中断可能使清理未发生，不能把历史所有异常说成已清。转发请求还通过 `maybe_reload()` 比较 mtime 自动重载，该路径不主动清 cooldown。

**用户已确认的变更**：`ConfigStore.reload()` 返回本次实际加载结果；管理接口只有实际加载成功且必要的本次后续动作成功才返回 HTTP 200，失败返回适当非 2xx 和脱敏原因；CLI 同时检查实际 HTTP 状态与响应，不再把“收到 200 但内部失败”当成功。**这是加载结果上报，不是严格 schema 校验、额外的模型协议功能或全量日志脱敏。**

1. **成功的精确定义**：一次尝试完整读取配置文件、成功解码/JSON 解析为 getter 所需的顶层 object，并在 `ConfigStore` 锁内完成内存配置引用及其 mtime 状态切换。顶层 object 是既有 getter 使用 `.get()` 的最低运行前提，不是对字段、业务值或全量 schema 的严格校验。保留现有 `_validate_config` 只告警、异常被记录而不阻止成功的语义；不可对外宣称业务配置已严格校验或所有请求可用。
2. **提交边界与真实失败状态**：在更改内存状态前完成读取、解析和必要准备；在同一锁内提交新配置/mtime。读取、解码、解析或提交前准备失败时，**这次尝试未替换**先前内存快照/mtime，返回 `loaded=false`；它只保证该次尝试的边界，其他并发重载可能在前后替换配置，不能断言此后所有请求一直使用旧配置。当前 `_reload_locked()` 会先赋值再执行日志/校验，因此实施时须调整提交顺序，不能直接把当前 bool 当作完整事务证明。
3. **提交后的部分失败**：若内存已经切换，但 `_on_reload` 回调或本次必要后续动作（例如 cooldown 操作）失败，返回非 2xx 并明确 `loaded=true`、后续步骤失败；不得谎称旧配置仍在用或静默回滚。若进程中断、异常发生点或并发使提交状态无法确定，则 `loaded=null`/未知，不伪造 true/false；本地异常内容不放响应。锁保护切换不意味着并发请求会瞬间统一使用新配置：在途请求、已拿到的引用、分别取不同 getter 的并发调用可能继续使用原快照或跨切换读取，不能声称瞬时全局切换。

**管理接口契约（字段名为设计，用户已确认成功/失败方向）**：保留路径及认证，成功保持旧形状 `HTTP 200 {"ok": true}`；解析/顶层类型错误建议 422，读取/权限及其他内部故障建议 500，均为非 2xx。若本次读取或解析失败且清 cooldown 成功，错误体示例为 `{"ok": false, "error": "config_parse_error", "loaded": false, "cooldown_cleared": true}`：`loaded=false` 仅表示本次未切换，旧快照在该提交边界保留。若已切换但回调等后续步骤失败，返回 `loaded=true` 和对应 cooldown 状态；无法可靠判定时用 `null`/未知，不能谎称仍在使用旧配置。错误码使用固定脱敏枚举，不输出配置、token、原始异常/路径或堆栈；既有认证失败 401、admin token 未配置 503 行为保持。状态码/字段仍待最终审阅和实测，不能把单次结果推广为对并发请求的永久保证。

**cooldown 策略已决：手动失败仍清，自动重载不清**。经过认证并进入手动 `POST /model_proxy/reload` handler 后，无论读取失败、文件不存在/无权限、解码/JSON 解析失败、顶层类型错误，或加载成功，都尝试清 cooldown；前述加载失败时保持该次旧内存快照并返回非 2xx，**只有清理实际完成才在错误体写 `cooldown_cleared=true`**。为覆盖加载函数异常，handler 应在可执行到的错误处理路径中确保尝试清理；进程崩溃、强杀或清理本身异常不能保证完成。清理失败或无法判断时如实报告 `cooldown_cleared=null`/未知和后续动作错误，不能假称已清，也不能误称已切换的配置仍旧。

认证失败、admin token 未配置、错误路径/方法等**未进入 reload handler**的请求不执行本次 cooldown 清理；发送后超时或断线无法判断是否进入 handler，不假报已清或未清。其他并发请求仍可能新增冷却或执行重载，因此“已清”仅指本次清理动作完成，不保证之后持续为空。mtime 驱动的 `maybe_reload()` 无论成功或失败均不主动清 cooldown。此策略保留既有手动清理意图，但新增真实失败状态上报；未做端到端验证。

**CLI 前置失败与 handler 加载失败不是同一事件**：CLI/marker 若从其业务配置来源无法读取、解析或取得可用 admin token，或无法按既有 listen 规则确定可用端点，必须在发送前非零退出，说明“未发送 reload，本次未触发 cooldown 清理”；不得从 last-good 路径快照猜凭据、猜端口、输出秘密或绕过认证。配置文件已由 helper 写入时同时报告“文件已写入，reload 未发送”，不回滚。只有凭据和端点可用、实际同一连接身份已核验并经服务端认证进入 handler，服务端才对**自身那次读取**失败仍尝试清 cooldown；例如 CLI 先取得凭据后文件被并发损坏。认证拒绝不清；已发送但超时/断线则结果未知，不能宣称未进入或已清。

**读取快照与自动探测绑定**：初始化、手动和 mtime 路径共用稳定读取步骤，不先读取文件 A 的字节、再把路径上替代文件 B 的 mtime 记为 A 的版本。打开文件后从同一描述符取得读取前/后的文件身份及可用的高精度 mtime、ctime、大小；读取后核验两次元数据一致、当前路径仍指向同一文件且元数据一致，再解析顶层 object；不一致或不可可靠核验时本次不提交，保留旧快照及旧探测标记，报告失败或供后续请求重试。提交前再核验一次路径，随后在 `ConfigStore` 锁内一起提交配置和**读取描述符对应的**私有探测标记；初始加载也遵守同一规则。避免路径替换落在最后核验与提交之间造成永久漏检：即使此间发生变化，也只保存已读文件的标记，下一次自动探测比较当前路径标记。

`maybe_reload()` 以文件身份、可用的高精度时间和大小组成的标记做**不相等**比较，不再仅用 `mtime > _mtime`：原子替换导致身份变化、mtime 相同或回退也应触发；双重检查在锁内进行。Windows 不得假设 Python `st_ctime` 一定表示内容变更时间，候选从文件句柄取得可验证的文件 ID 和 `FILE_BASIC_INFO.ChangeTime`；具体 API、精度和可用性须真机验证，不能证明时对应能力阻断。读取不稳定、文件暂缺或加载失败不得推进标记为未读取的新文件；恢复后后续请求应再次尝试。自动路径仍不清 cooldown，不新增对外版本字段或配置/凭据哈希。此机制依赖受支持本地文件系统提供可靠身份和变更元数据，以及写入者不刻意恢复元数据；不承诺识别保留同一身份、时间和大小的恶意修改、粗粒度元数据碰撞，或完全消除不遵循约定的原地并发写入。不能证明稳定时失败关闭；需要严格字节版本或更强并发保证须另行设计。§11 对 A 读后替换 B 的竞态做隔离验证。

**CLI、marker 与旧客户端兼容**：经 §5.1 验证同一连接对象后，手动/marker 路径复用同一个客户端：可靠 200/`ok=true` 且该次加载完成 → 0，提示“本次读取并切换成功、cooldown 已清”；收到 `loaded=false,cooldown_cleared=true` → 非零并提示“reload 失败（本次未切换），cooldown 已清”。若 helper 的配置文件已写入，还须先报告“文件已写入”，不静默回滚；若 `loaded=true` 但后续失败，明确“本次已加载、后续步骤失败”及已知 cooldown 状态。身份不可核验时不发送，认证失败按未进入 handler 处理；发送后超时、断连、响应损坏时，加载和 cooldown 结果均报未知、非零且不自动重发。marker=`yes` 每次操作最多发送一次，`no`/helper 失败不发送。

成功的 200 仍兼容读取 `{"ok": true}` 的旧调用者，但**失败由 200 变为非 2xx 是有意的可见兼容变化**：旧 shell 的 `curl -s`/`echo` 和旧 marker 路径可能仍打印成功或返回 helper 状态，不能假设旧客户端自动识别 HTTP 错误。升级必须让本工具的服务端和新版 CLI 一起交付，并提醒未知外部调用者检查 HTTP 状态；旧客户端的结果以兼容性测试为准，不承诺失败被正确传播。

**并发编辑与版本边界**：本方案不增加加载版本字段、文件内容摘要、配置/token 的哈希或凭据派生指纹；成功只表示这次实际读取的 JSON 快照已完成切换，不证明该快照就是 CLI 刚写的目标文件，也不证明后续没有并发编辑/重载。mtime 仅保留原有自动重载触发作用，不是目标内容一致性的证明。若未来需要对拍“确切目标文件”，须另行设计与实际读取字节可信绑定的版本机制及并发约束，经用户批准后再考虑新增字段；不能暴露明文或可被字典攻击的密钥派生信息。

### 4. 单一路径解析、固定发现锚点与锁

**单一解析**：`core/runtime_paths.py` 是现有 `config/runtime_paths.json` 及平台映射的唯一解析器；server、CLI、hook 不各自重做 fallback。项目根从服务入口自身规范化得到，`VERSION`、assets 和业务配置仍按原根；`MODEL_PROXY_CONFIG` 和相对 `appkey_file` 保留原调用 cwd。有效的旧 macOS 配置及 legacy flock 路径不迁移；Windows 仅将明确列出的四项旧 `/tmp/model_proxy...` 默认值按同一解析器映射到本机私有目录，不猜其他自定义 POSIX 路径。盘符、UNC、空格、中文、路径别名及权限需真机验证。

**已决 fail-closed 范围**：运行路径文件本身不存在、JSON 损坏、根非 object、无读权限或读取 IO 错误时，不再使用旧版整文件默认回退启动，也不自动创建/修复；`on`、`restart` 和直接服务入口在裁剪日志、写账本/sidecar 前失败，锁冲突和权限错误不得切换备用锁。涉及配置写入或 reload 的 CLI 操作先检查该门禁；`logs/stats` 不猜路径。帮助不启动服务，不因路径文件缺损被拒；`status/off` 的恢复路径见下。此决定仅针对**运行路径文件**，不擅改业务配置或 listen 的既有缺省规则。

**有效 object 中只缺单个字段：已决沿用该字段原默认**。旧 `resolve_runtime_paths()` 使用 `raw_paths.get(key, default)`；用户已同意保留 `log/totals/lock/pid/ensure_log/start_lock` 的单键缺失默认，由 server、CLI 和 hook 共用唯一解析器，不把单键缺失错误地当作整文件损坏。**缺键不等于显式空值**：字段存在但为 `null`、非字符串、空串/全空白或 OS 不可识别的路径时先验报错，不偷偷代入默认。未知额外键按旧规则忽略；多个字段同时缺失的扩展超出本次明确拍板范围，须另核对兼容性，不在本稿假称已批准。

| 有效文件示例（仅示意） | 单键默认与安全检查 |
|---|---|
| 缺 `log` | 默认项目根 `.model_proxy.log`；相对项目根，不随当前 cwd 变化 |
| 缺 `totals` | 默认项目根 `.model_proxy_totals.json`；必须与活动实例的账本快照一致，不能切换或重写另一账本 |
| 缺 `lock` 或 `pid` | Mac 使用既有 `/tmp/model_proxy.lock` 或 `/tmp/model_proxy.pid` 默认；Windows 仅将这两个**旧默认值**映射到当前用户私有 `%LOCALAPPDATA%/model_proxy/runtime/` 下对应名称 |
| 缺 `ensure_log` 或 `start_lock` | Mac 分别沿用 `/tmp/model_proxy_ensure.log`、`/tmp/model_proxy_start.lock`；Windows 按同一规则映射这两个旧默认值，未知自定义 POSIX 绝对路径不猜测。新短锁仍按本节固定 anchor 规则，不删除旧 mkdir 锁 |

单键默认经同一解析器计算后仍须依次通过固定 anchor、活动实例 `active/last-good` 快照、实际 lock/账本身份及端口检查；默认值若与已运行实例不一致、路径/进程无法识别或安全性无法证明，停止并提示，不切锁、启动第二实例或写账本。可信旧实例仍按快照定位；未知状态不能根据新默认猜测。Windows 环境变量、驱动器/UNC/ACL 和这四项映射均**待真实 Windows 验证**，不宣称已通过。

**固定发现与同安装互斥**：新增 `core/discovery.py`，安装根 `project_root.resolve()` 下固定 `.model_proxy_runtime/`（例如 `anchor.lock`、`control.lock`、`install.json`、`active.json`、`last_good_paths.json`）；其位置和锁规则仅由代码及规范化安装根决定，**不依赖 runtime_paths、业务配置、port、pid、HOME 或环境变量**。目录须同用户受控、ACL/权限可验证，锁文件不删、不截断、不替换，安装根不可写、锁/ACL 不可靠或状态文件不可读即 UNKNOWN/非零，不能转到临时目录。允许根目录保留目录和运行产物的 A 范围；未来 `.gitignore` 明确忽略这些私有状态，不纳入交付数据。

**运行文件身份门禁**：将六项 `log/totals/lock/pid/ensure_log/start_lock`、固定目录及其 `anchor.lock/control.lock/install.json/active.json/last_good_paths.json` 一起按用途检查：不同角色不得指向同一文件或目录，不得把运行目标放入固定状态目录或让状态目录落在运行文件/旧锁目录内。先规范化绝对路径并解析已有父目录的符号链接/重解析点；对存在的文件额外比对 POSIX 设备号+inode 或 Windows 卷标识+文件 ID，因而不同名称的符号链接和硬链接也要识别。对尚不存在的目标比较经已存在祖先解析后的目标位置；类型、父链、身份或权限无法可靠判定时 UNKNOWN/非零，不猜、不碰账本/日志。已有的固定锁、记录不得是符号链接/重解析点；固定锁的硬链接数异常或不能确认时拒绝，不对可能被别名共享的文件加锁或写入。

为避免创建锚点本身就碰到被配置为日志/账本的同一路径，服务和短锁入口先做**只读**路径/状态预检，再创建或打开固定锁；不得用会截断的模式。仅只读预检不授予实例所有权：持锁后重新解析并核对配置、父链、文件身份和打开的句柄，获取 legacy 锁及执行日志/账本、sidecar、记录写入前再次核验；发现冲突、变化或无法证明即退出，不做数据写入。路径配置损坏时，`status/off` 只可使用可信快照及已存在且可安全核验的固定锁进行恢复；若无法证明锁和运行数据不冲突，不创建锁、不写记录，返回 UNKNOWN。此门禁也适用于 helper、hook 和直接服务入口；依赖 OS 句柄和受控目录约束，非协作进程在最后核验后替换路径仍存在竞态，不声称防御同用户恶意写入。

- server 的每一入口（CLI 子进程及用户直接执行的 `core/cli/model_proxy.py`）均先完成上述只读预检，再取得并全程持有同一个 `anchor.lock`，持锁复核运行路径文件和身份后获取其配置的 legacy 实例锁，最后才 bind、初始化日志/账本/sidecar 并进入服务。固定获取顺序为 **anchor → legacy lock**；错误释放已获句柄，不退化为无锁。绑定失败不能写账本；实际绑定句柄须一直保有到退出。锁的 POSIX `flock` 与 Windows `LockFileEx` 含义、固定范围、不可继承及生命周期见本节末。CLI 生命周期短期 `control.lock` 也在固定目录，CLI 不持 anchor 等待子进程：探测后释放，再启动由子进程独立获取；直接服务入口可绕过短锁，但绝不能绕过 anchor。
- 因此**同一个规范化安装根**即使有效配置的 lock/pid/port 全部改成另一组，第二个新实例也会在 anchor 冲突处停止，不会因为新 legacy 锁空闲而重写原账本。运行实例记录实际已用的路径快照；CLI 发现当前有效配置与活动快照不同时标记漂移、非零，禁止以新端口/新锁认领旧实例或启动第二个实例。**恢复例外**：若 `active` 和 `last-good` 可信且可核验 PID/创建身份，`off` 可在用户选择的日常停止策略和授权范围内使用快照中的旧路径/端口定位现有实例，不依赖漂移后的配置；`status` 可提供脱敏漂移诊断。`restart` 先非零停止自动流程，不一边猜旧端口一边启动新配置；经确认关闭原实例、审查新路径后再显式启动。
- 该保证不覆盖多个不同安装根指向同一账本/sidecar、同一账本硬链接、未知外部写入者、网络共享或云同步、多用户以不同权限绕过、正在运行时移动/替换安装根等场景。符号链接指向同一规范化安装根时要求解析到同一 anchor；hardlink 和跨安装共享即使路径字符串不同也可能是同一物理文件，**不宣称能全面检测或互斥**。实施时拒绝已能识别的危险共享；未知情况纳入不支持和用户迁移检查，不能宣称被解决。旧版服务完全不持 anchor，因此不能用新锁保护旧代码锁前裁剪日志，切换必须先静默并停旧服务。

**Windows 端口独占是待验证假设，不是现有保证**：获得 anchor 和 legacy 锁并不证明系统不会让另一 socket 绑定重叠地址；不能仅凭一次 bind 成功断言独占。§11 须在获授权 Windows 真机以同端口的相同地址、通配地址、IPv4/IPv6 重叠及占用顺序实测现有 socket 选项和归属；必要时在 `bind()` **之前**配置 Windows `SO_EXCLUSIVEADDRUSE`，避免与 `SO_REUSEADDR` 冲突，并复测占用、失败后不写账本、正常重启/TIME_WAIT 与兼容性。若无法实现和验证所需独占，Windows 启动/上线门禁不通过，不借 anchor 宣称端口互斥；Mac 绑定行为不因此改变。此项不授权 Windows 强制停止，也不承诺阻止更早抢占端口或所有网络/权限场景。

**发现、首次启动和 UNKNOWN**：`install.json` 是经本机受保护的安装初始化和生命周期记录，`active.json` 记录服务进程的 PID、OS 创建时间、映像、安装根、启动 nonce、端口、路径指纹和 `STARTING/READY`；`last_good_paths.json` 是不含 token 值的最后可信路径/端口快照，仍属敏感元数据，不能公开打印。文件均原子提交且受保护；ACL 错误、损坏或锁状态不可判定是 UNKNOWN。`install.json` 完全缺失且 anchor 空闲时特判为下述 `UNINITIALIZED_OR_RECORD_LOST`，而不是 STOPPED；已初始化但从未启动时允许缺少 active/last-good，之后异常缺失必须先恢复。启动持 anchor 和 legacy 锁并成功 bind 后，先安全持久化 last-good 和 `active=STARTING`，再触碰日志、账本与 sidecar，持久化失败即释放锁并退出。锁持有期间 active 缺失为 UNKNOWN；获取 anchor 成功但记录指向仍活且身份一致的进程、进程可见性不足或 legacy 状态无法验证，也属 UNKNOWN；记录 PID 已被可靠证明退出或创建时间已变、且无旧版风险时才可视为 stale 并重新启动。状态是辅助证据，不是锁，也不证明记录 PID 持有两把锁，不防同用户恶意篡改。

- **首次安装可用且不能靠丢记录猜测**：`install.json` 缺失且 anchor 可取，只能标记 `UNINITIALIZED_OR_RECORD_LOST`，无法仅凭缺失区分真正新安装和记录被删除。相同实现的交互式 `on` 或直接前台服务入口运行首次/恢复预检：在保持 anchor 期间，对已知 legacy 入口/锁与目标端口做有权限的本机检查，并要求用户提供新安装或恢复的来源、确认该安装无旧启动者、无共享账本且自动启动已静默；只有检查无冲突、无未知且获得明确确认，才写受保护的初始化记录。无法确认来源、已有记录损坏、旧版不明或证据不足均走人工恢复，不能冒充首次。非 TTY 或未确认则退出非零并提示运行交互式 `on` 完成预检；无新公开子命令。CLI 在预检完成后释放 anchor，子进程自行获取；直接服务入口调用同一 preflight/anchor 代码。锁被持有且记录尚未出现时等待有限时间，仍不可判定则 UNKNOWN，不能另起。
- `on` 的启动尝试使用非秘密 nonce，核对已创建子进程身份、安装路径、服务入口与记录，并确认 anchor 处于持有状态后才认定 READY；对应关系依赖受保护记录和“只有持锁服务能写记录”的实现不变量，属辅助判断而非证明该 PID 持有两把锁。等待超时但子进程仍活/anchor 仍持有时返回非零及“启动结果未知，勿重试”，不杀、不自动再起；若迟到 READY，后续查询可经同一记录验证，不为超时补发另一个实例。短锁仅串行本 CLI；无论直接启动或 CLI 启动都服从固定 anchor。
- **损坏或漂移后的安全恢复与旧版迁移必须分开**：新版记录损坏/缺失、锚点持有但身份不可核实或权限不足时均为 UNKNOWN；获授权操作者依据受保护备份及系统进程/锁/端口信息人工核实，不运行旧 CLI 盲控新版进程、删 anchor 或盲杀。只有经证据确认目标确是**旧版服务**，且仍有匹配其安装布局的旧 CLI/路径和可验证身份时，才可在批准的停机流程中使用旧版工具。配置文件损坏时不读取凭据或发送 token，可信新版快照可按 §5 的停止选项定位原实例；确认排空或获得中断授权并停服、释放固定和原 legacy 锁后，用户显式恢复已确认配置并重新预检，不自动生成默认文件。

**锁和写入细节**：两把锁均经 `core/platform_lock.py`：macOS/Linux `fcntl.flock(LOCK_EX|LOCK_NB)`，Windows 候选 `CreateFileW`+`LockFileEx` 固定 offset 0、长度 1；不继承句柄，退出释放，不删/replace 锁，区分冲突、ACL、不可用。仅当旧/新 macOS legacy 路径指向同一 inode 时它们才共享 flock；旧版可先截断 PID 且拿锁前初始化/裁剪日志，即使最终竞争失败也可能先触碰日志。`start_lock` 旧字段保留于 schema/快照以兼容配置，旧 hook 的 mkdir 目录不由新代码删除；新 hook 委托 CLI 的固定 `control.lock`，两者不互锁。原子替换及 ACL 的各提交状态见 §6.1；强杀只能依赖 OS 回收句柄，不能假装 Python `finally` 执行。

**Mac 锁与控制证据边界**：服务仍须按 anchor → legacy 顺序获取并持有两把 flock，以防协作入口重复启动；锁冲突、锁路径/状态错误仍按原启动门禁处理。CLI 不以“OS 能证明某 PID 同时持有这两把 flock”为 `on/status/reload/off/restart` 的控制前提，也不因无法取得这种证明而单独失败。`lsof` 可提供打开文件等线索，不能证明该进程持有 flock；此前 macOS 本机测试中 `F_GETLKPID` 也不能可靠证明 flock 持锁者，均不作为持锁者证明。受保护记录、启动 nonce、锁状态和进程信息仅为相互印证的辅助证据，不等价于证明该 PID 持有两锁；同用户恶意伪造或替换仍不受防护。

### 5. 生命周期、真实连接归属与停止方式

`core/cli/lifecycle.py` 管 `UNINITIALIZED/STOPPED/STARTING/READY/UNKNOWN/STOPPING`；`on` 与直接服务入口均执行 §4 的固定 anchor、初始化和有效配置门禁。旧服务无法用新记录安全认领；路径配置损坏时仅可通过可信快照和授权的身份/停止方法恢复。`restart` 必须先安全停止并核验进程、固定 anchor、原 legacy lock 与端口释放，任一未知/超时非零，不再次启动；等待或杀进程不能替代 §9 的在途请求处理。

**用户如何操作**：Mac 用 `model_proxy_cli.sh off/restart`，原生 Windows 的同名 `.bat` 入口进入同一 Python `lifecycle.py`，Windows 不依赖 WSL。命令入口一致不代表停止机制一致或均已获批准；Windows 停止方案未确认时，`off/restart` 均直接非零安全失败，不结束进程、不发送停止信号，也不启动替代进程。确认之前只可实现并验收此失败分支，不能把候选强制停止接为实际行为。

- **Mac 现状与边界**：现有 `off` 发送 SIGTERM，可能立即中断在途请求、日志或账本收尾；`restart` 等退出后再启动，不等于优雅排空。保留手动使用习惯和非 TTY 兼容，由用户决定生产重启。CLI 根据受保护记录核对规范化安装路径、PID、OS 进程启动时间和服务入口，信号前再次核验目标；任一核验失败、证据不一致或权限不足时返回 UNKNOWN/非零，不发信号、不盲目重启。不要求证明该 PID 同时持有两把 flock。POSIX 核验与 `kill(pid)` 之间仍有小窗口，PID 退出或重用可能导致误杀，不能宣称消除 PID 重用、同用户恶意仿冒或安全无损。
- **Windows 推荐候选（待用户明确确认）**：用当前 Python 与绝对服务入口启动；可用 `OpenProcess` 取得同一进程句柄，结合 PID、OS 创建时间、映像、安装记录和可核验的实例归属确认目标，再在**同一已验证句柄**上候选调用 `TerminateProcess`。此段仅描述待裁决方案，未确认时不接入实际停止分支。若未来获确认，不能按 `python.exe`、单独 PID 或监听端口猜测/批量杀进程；无法核验或权限不足即 UNKNOWN、非零、不结束，也不重启。结束后验证该句柄进程已退出、原锁释放且端口无冲突，`restart` 才可启动；超时不强杀其他进程。后台启动候选 `DETACHED_PROCESS` 的句柄继承、关 cmd 和注销行为均待验证。
- **明确风险和授权**：`TerminateProcess` 是受控**强制**停止，不是优雅排空，可能中断在途请求、丢最后账本收尾，Python `finally` 可能不执行；系统回收锁也须实测。用户仅询问“类似 Mac 用 CLI 或进程控制”，尚未批准该候选；当前没有 Windows 进程只是背景。真实 Windows 进程、PID 复用、并发、结束后账本和失败路径尚未验证，确认前不能宣称 Windows 自动 `off/restart` 已可用。若需要优雅排空，可另行设计并获批准，不能作为已确认的新控制通道；现有 `server.shutdown()` 退出行为也须实测。

### 5.1 status/reload：对已建立连接验证，再发送 token

原草案“先查 PID/端口再发 token”不能防止正常端口释放后被另一进程抢占。**本批已采纳的设计方向**是：发送管理凭据前核验实际连接对象；具体 OS 机制仍是未验证候选，不能声称已通过。共同 `core/cli/control.py` 的门禁设计如下：先按 §4 受保护的记录核对规范化安装路径、PID、进程启动时间及服务入口，识别目标实例；从本机直接连接指定 loopback（禁用代理、禁止重定向），但在任何 HTTP header/body 或 token 写入前，用 OS API 对**已建立的同一 socket**取本端/对端四元组，定位服务端对应已建立连接的真实拥有 PID；将 PID 与可信记录及可核验进程身份读取的创建时间、映像和服务入口核对。映射必须明确指向服务端连接而非客户端自身；TCP 端口名或监听表预检不够。锁状态和记录只是辅助证据，不要求或声称证明该 PID 同时持有两把 flock。若身份、安装路径或服务入口核验失败、证据不一致，或所选 OS、权限、IPv4/IPv6 或 API 不能可靠提供端点/进程绑定，则关闭该 socket、UNKNOWN/非零，**不发送 admin token**。

**可行性候选，尚未证明**：
- macOS：以可信记录给出的目标 PID 为起点，候选使用 `proc_pidinfo` 枚举该进程 FD，`proc_pidfdinfo` 查询 socket 信息，比对目标进程已接受连接的反向四元组和进程创建身份；struct/权限/竞争和不同 macOS 版本行为必须先在离线隔离样例及经授权的真机上验证，不能仅根据函数名假设可用。
- Windows：候选 `GetExtendedTcpTable` 的 owner-PID 连接表分别查询 IPv4/IPv6，匹配服务端**已建立连接**的反向四元组，再用同一 `OpenProcess` 句柄核对 PID 创建时间/映像；表项、权限、PID 重用与连接竞争必须验证，不能把监听 PID 当作已接受连接 PID。
- Linux（如纳入支持）：候选按已建立连接的反向四元组和 socket inode 关联 `/proc` 内目标进程 FD，并验证 PID 创建身份；访问控制、namespace 和可见性受环境影响。它不是 macOS 或 Windows 的兜底。

客户端 `connect()` 成功但服务端尚未 `accept()` 时，目标 socket/FD 的归属暂不可见；只对**同一个已建立 socket**按单调时钟做有界轮询，候选总等待 2 秒、间隔 50 毫秒，最终超时即 UNKNOWN/非零并关闭，不发送凭据、不重新 connect。参数只是待实测的工程候选；目标进程死亡、连接关闭、四元组冲突、权限不足或 PID 创建身份不匹配时立即 fail closed。禁止先发 HTTP 请求推动 accept、禁止发送 token 后再验证，也不静默退回端口预检。

验证后仅在**该已验证 socket**上构造并发送认证请求；不自动重连、无重定向、无代理和无凭据 argv/environment，连接关闭/端点不可证实时不得换一个 socket 重试。已建立连接即使监听端口随后重用，也不会被当作新连接而自动发送给抢占者；服务崩溃则该请求失败/结果未知。端点归属获取后的句柄转交、同用户恶意进程、OS API 语义及身份 TOCTOU 仍需验证；此方法不是对同用户攻击或所有平台的零风险承诺。

如果目标系统无法实现可靠的已连接端点归属，默认 fail closed：status/reload/marker 不发 token，报告未知并返回非零，不得声称功能已交付。**隔离先验证 macOS；Windows 未来必须在真实 Windows 环境验证，候选对端校验/ACL 语义不能预先宣称可用。** 备选是另行设计具备对端认证的本机私有控制通道，或明确接受剩余泄露风险；当前没有对后者的授权。端口抢占、连接后目标退出、假监听、访问权限拒绝和不自动重连均须真机/隔离 mock 测试。

### 5.2 日常 off/restart：本轮决定与未来候选

**本轮决定（短期）**：保持 Mac 现有手动 `off/restart` 使用习惯；用户手动决定生产重启，助手不自动操作线上服务。停止动作只能作用于核验后的目标实例，并须明确提示会中断在途流/账本收尾，不能宣称已排空。保留现有非 TTY 自动化脚本兼容：不新增每次必交互提示、不改变现有非 TTY 脚本行为；自动化执行即可能中断，风险未消除。

长期优雅排空是备选，不在本批实施；如未来采用，须另设计准入冻结、在途计数、超时和授权。Windows 运行时如何结束进程、是否强制仍待后续确认；当前只有 Mac 使用背景，不能据此声称已选强杀。

### 6. 凭据、ACL、客户端与 hook

本批**只做安装输出脱敏**：四客户端候选 token、预览 diff、Claude 写入成功消息、Claude/Codex/Hermes/OpenClaw 手工片段、hook 提示和安装异常不得输出 token、key 或完整原始配置；实际写入仍使用内存中的真实 token。手工片段用 `<CLIENT_TOKEN>` 占位并提示从原有安全渠道获取，不自动打印或复制秘密。

**保持现状，不在本批扩大**：ACCESS 日志、stats 账本、`[cc]/[codex]` 显示及其持久内容不变；不迁移、不改写旧日志或账本。全面日志/账本脱敏是独立后续候选，仍有敏感数据风险；未经另行授权不实施，也不作为本批交付门禁。`switch <client_token> <route_id>` 保留旧参数，命令行/历史暴露不可消除。

Windows 验证目录继承、原目标/临时/replace 后的真实 DACL；POSIX 检查 owner/权限；`chmod(0600)` 不证明 Windows ACL。未知 ACL、不能保护临时或备份时，不开始敏感写入；不未经用户允许改变已有文件共享权限或重解析点。管理员、SYSTEM、同用户进程、既有历史、旧日志和备份介质不在本方案完全控制范围内。保留 `expanduser` 和相对 key 路径相对原 cwd 的含义；不记录凭据内容。install 只在用户授权的目标客户端/目录下操作；hook 仅识别受管旧/新条目并保留其他条目，实际客户端 shell 未验证时不自动写 Windows hook。Codex catalog 默认测试 mock，真实访问须另授权。

### 6.1 原子替换、后验 ACL 及多文件部分完成

**先保护既有链接和权限，再决定能否提交**：对每个 install/hook 目标，读取并记录目标路径及父链的链接/重解析点、最终对象身份及原有保护属性；POSIX 至少核验 owner、group、mode 和适用 ACL，Windows 核验 owner、DACL、继承/保护标志及实际有效保护。不能读取、复制并验证原有保护时拒绝写入，不能默认 `0600` 或目录继承就认为保持原 ACL。临时文件和备份先以不弱于原目标且符合敏感数据保护要求的权限创建；替换前在临时文件上设置并验证最终保护，替换后再核验等价性。若原保护本身不符合安全门槛、又未获授权更改，则拒绝而非静默变更权限。无原文件时使用经过验证的安全创建权限，不改变已有目录权限。

现有目标是符号链接/重解析点时，**不得用 `os.replace(temp, link_path)` 把链接本身替换成普通文件**。仅当实际目标及父目录在本次授权范围内、可安全解析和重验链与身份，且能原样保留链接及目标权限时，才可对已验证的实际目标执行同卷原子替换；断链、越界、链变化、无法安全保持则拒绝。现有目标有多个硬链接时，普通替换会使别名分叉，若不能保持既有链接关系就拒绝；不以原地写入绕过安全替换。目录重解析点、Windows ACL 能力和 POSIX ACL 可用性均须实测；保护不能证明即在提交前失败，而不是先替换再碰运气。

**从读取到前向提交的并发检查**：预览所依据的是对目标的一次稳定读取快照：存在性、父链/链接链及其身份、最终对象身份及硬链接数、相关保护属性及实际原始字节均在内存中保存，不打印秘密；备份必须对应该快照。用户确认后，在创建备份前和实际 `replace` 前分别重新稳定读取并比较上述内容，即使只有字节变化、身份替换或“不存在→已创建”也视为变化；不匹配或无法证明稳定则停止、不前向替换，提示重新读取、重新预览并再次确认，不自动合并或重试。多文件安装逐个执行；同一文件本次已成功提交后，再次修改须以新快照重新预览。若备份已产生，按实际状态和安全位置报告；已提交的其他文件不假装回滚。

此比较不能把 `os.replace` 变成 compare-and-swap：不遵守同一锁的编辑者仍可在**最后检查之后、替换之前**修改目标，仍有覆盖竞态。目标级协作锁仅在另获批准并实现后才能用于串行化协作者，本稿不假定该锁已存在；对第三方或不协作写入者，明确要求用户在安装期间暂停编辑，不能承诺完全防丢更新。路径不存在时使用经平台验证的原子“不覆盖已有目标”创建；若不可用则拒绝，不能用普通 replace 宣称不存在目标时绝不覆盖并发创建。

逐文件结果必须区分，不能把“未完成”统称“未写入”：

1. **PREPARE / 未提交**：先完成链接、原有保护、预览快照及目录/备份能力验证；在同卷临时文件完整写入、关闭并确认与目标应有 ACL 等价。提交前再次检查快照及链接/权限，任一不符停止并重新预览。此阶段失败，只能在确认尚未 replace 时报告“目标未替换”，保留可用旧文件和安全备份。
2. **REPLACED / 已提交待验**：`os.replace` 成功即目标内容已改变；不能因之后 ACL 检查失败而报告原文件没动。记录无秘密的目标、阶段和备份位置。
3. **VERIFIED**：替换后的文件与父目录 ACL、内容完整性验证通过后才报告该文件成功。
4. **POSTCHECK_FAILED / 已替换、保护未知或异常**：返回非零并明确警告“目标已改变，权限/校验失败”；停止后续自动写入，保护备份，提示用户核对后决定手工恢复。不得静默回滚覆盖并发更新，不能承诺恢复成功。

**本批新增校验的失败边界**：仅针对本批确实新增权限校验的受控写入目标，例如经授权的 install 目标、备份或 hook 文件；不因此改写账本、sidecar、ACCESS 日志或其写入策略。后验失败后，**当前写入操作**立即停止对该目标及其后续 install 步骤的自动写入，报告“已提交、权限未知”和备份位置，不静默恢复。对当前进程内随后针对同一目标的新增写操作，须保持未知状态并拒绝自动重试，直到经用户授权核查恢复；已提交/已发出响应不能补第二个 HTTP 状态。

上述边界不自动覆盖进程重启、独立 CLI、服务或第三方客户端：进程内状态消失或另一写入者不遵守时，不能称已冻结。若为实现**本批同一受控目标**的可靠后续写入边界，确需跨进程状态/锁，可另评估限定该目标的共享锁、持久未知标记和恢复流程；它们尚未获批准，也不等于冻结其他账本或日志。若无法证明后续写入受控，状态只可报告 UNKNOWN，**相关新增权限校验能力不得视为闭合或进入部署**，先由用户裁决扩展范围；不得悄悄增加全局 journal/freeze。

`install` 跨多个文件（尤其 Codex 可先写 config、再写 catalog、随后再次写 config），不具整体事务原子性。**Codex 可选 catalog**：下载/模板在 catalog 自身写入前不可用，保留原有降级并提示；已有 `model_catalog_json` 行不变，主配置若成功可返回 0 加警告，不能把可选网络不可用升级为整个安装失败。**安全/ACL 或已开始提交后的写失败**：停止后续写入，整体非零，报告哪些已提交、哪个未提交/后验失败、哪些未执行及受保护备份；不得伪称全未写、整体原子或自动回滚成功。用户取消按原语义。真实客户端 ACL/路径须单独授权验证。

服务日志及账本保持现有写入和保留策略；本批不为新增权限校验而改变其格式、引入全局冻结或额外替换。若未来另授权对这些文件新增权限/替换校验，其 handler 和跨进程边界须重新设计，不能借本批直接实施。

### 6.2 全量日志/账本脱敏：后续独立候选，不入本批

源码现状：ACCESS 日志使用入站 client token，stats 账本组合键可含匹配的 strategy token，active sessions 以 token 分组并显示 `[cc]/[codex]`。本批保持这些显示、现有日志和账本格式与内容，不为脱敏迁移或改写旧日志/账本；原有正常追加、累计及原保留策略照旧。既有敏感数据仍有暴露风险，不能声称被消除。

若未来另获授权推进全量脱敏，须独立设计安全标识、密钥/映射、旧新数据聚合、统计不重计数及回滚；低熵 token 的无密钥 hash 不构成匿名化。该候选不作为本批交付、阶段门禁或实现范围，不因本次 reload 结果上报而附带实施。

### 7. 四个根 Python 文件及引用盘点

以下是对本项目及 vault 内**可安全扫描文本**的静态结果，不是未知外部消费者的证明。项目内覆盖隐藏文件、已跟踪及未跟踪的 `.py/.sh/.md`；跳过 `.git`、缓存、`config/`、日志和机密/凭据目录。vault 扫描限 `.py/.sh/.bat/.cmd/.md/.ts/.js`，排除本项目、缓存、依赖目录、配置/机密、worktrees、个人私密目录和二进制；未读取真实配置/凭据，因此 JSON 配置、实际用户注册的 hook、计划任务、shell 别名、系统服务、外部工作区均属**未知，不得宣称全 vault 无引用**。

| 根文件 → 设计下沉路径 | 直接消费者及需改动 |
|---|---|
| `model_proxy.py` → `core/cli/model_proxy.py` | CLI `on/off/restart`、入口自举的 `sys.path`、`core/server.py` 文件头、README、`docs/ARCHITECTURE.md:144,160-161`；改为可靠地添加项目根再导入 `core.server`。旧绝对路径和相对路径启动都会失效；新的进程识别不可只把 pgrep 字符串改个路径。 |
| `_config_ops.py` → `core/cli/_config_ops.py` | shell CLI、`_install_ops.py`、`_format_ops.py` 内说明；`tests/test_config_ops.py`、`test_config_compact_format.py`、`test_format_ops.py`、`test_route.py`，以及 `tools/model_proxy/.context/test_new_models_probe.py`；须同步 `patch("_config_ops....")`、mock target 和 probe 的 import/path。`core/server.py`、`core/commands.py`、`docs/CONFIG.md` 含说明引用。 |
| `_format_ops.py` → `core/cli/_format_ops.py` | shell CLI status、`_config_ops.py`、`_install_ops.py`、`tests/test_format_ops.py`；`core/session_identity.py` 有说明；改为包内导入，避免和旧顶层模块重复加载。 |
| `_install_ops.py` → `core/cli/_install_ops.py` | shell CLI、`tests/test_install_ops.py`（大量字符串 patch 路径）、README；更新其导入及项目根/vault 根、catalog、hook 的全部相对计算。 |

- `core/reasoning/registry.py` 是协议 codec 注册表，`_config_ops` / `_install_ops` 引用它；它不是文件入口注册表，保留 `core.reasoning.registry` 身份和注册表语义。`core/session_identity.py:18` 另有外部 Claude 会话注册表 `~/.claude/sessions`，由 server 和格式化共享，保留只读、不可测试时读到真实目录。
- vault 中安全扫描命中但**不是四个 py 的直接运行入口**：`project/credit_customer_fact_kernel/tools/runner/run_test.py:53`、`project/credit_loan_decide_prompt/tools/runner/run_loan_chain_test.py:47`、`run_v31_infra_recovery.py:42-43`、`project/credit_pattern_recgnise_prompt/tools/runner/run_chain_test.py:31` 指向配置，infra recovery 还指向根日志；`project/credit_loan_decide_prompt/output/v31_rc6_noiseS4_20261104/prep.py:63` 记录配置路径；`tools/video-explainer/lib/core/vision_adapter.py` 有代理/配置相关引用。已定 A 保留这些路径；不能只验证四个 Python 入口而忽略上述消费者。
- vault 外部历史设计中，`docs/designs/2026-07-24-model-proxy-reasoning统计移除安全上线.md`、`2026-08-26-本地三服务开放家庭局域网.md` 提及旧入口；`2026-09-20-model_proxy接入mcli的supply层扩展.md`、`2026-09-20-model_proxy-CLI探测感知supply扩展字段.md` 提及配置 helper；`2026-08-24-session标识统一识别方案.md` 提及格式 helper；`2026-07-23-子agent进度追踪机制清理撤销方案.md`、`2026-09-16-subagent模型路由场景化设计-v3.md` 提及 install helper。它们是历史证据，不批量改写；可在当前 README/架构文档标注旧路径历史含义。
- 本项目 `docs/designs/` 与 `docs/archive/` 也有大量历史提及，列于文末附录；它们不构成现行调用，尤其不得改写本任务指定的既有未跟踪草案。`CHANGELOG.md` 是历史记录，新增版本条目而非回写旧条目。

**兼容取舍**：根目录不允许 `.py` 与原样保留 `python tools/model_proxy/model_proxy.py` / `import _config_ops` 互相矛盾，不可同时成立。已定不在根放 Python shim：旧直接入口/顶层 helper import 明确 breaking，提供迁移指引至 `.sh/.bat` 或新包路径；已知测试和 probe 同步修改，外部未知用户需公告与人工检查。若要求原路径继续可用，须放宽根目录限制或由用户确认提供**根外**兼容包装及调用方式变更；根外包装也不能使原绝对路径自动生效。未搜索的 shell 别名/计划任务/注册 hook 先由用户在无凭据暴露前提下盘点。切换前必须用**旧版 CLI**停旧进程；切换后的新 CLI 不承诺认领/终止缺身份记录的旧进程。

### 8. 阶段、中间态及零双栈验收

| 阶段 | 交付/中间态 | 必须通过的门禁与功能影响 |
|---|---|---|
| 0 基线与隔离 | worktree 或临时副本建立旧 CLI 对拍、假配置/凭据和客户端、多进程及端点核验设施 | 按 §11 启动前核实六路径、配置、假 HOME、端口与进程归属；默认绝对路径只用纯逻辑替身验证。不访问生产数据、锁或进程，不连接真实上游；不强制 OS 级全域沙箱，真实 Windows/客户端验证另获授权 |
| 1 锚点、服务与生命周期 | 四个 `.py` 下沉；固定 anchor 覆盖直接服务入口及 CLI；路径文件缺损拒启、单键缺失用原默认；共同 Python 生命周期、安装输出脱敏及 reload 真实结果上报 | 先落实路径同身份门禁，再执行固定锁与数据写入；配置读取快照/探测标记绑定；安装先保护链接/ACL 并检查预览后并发变更。暂留 shell 菜单走共同门禁、`helper_entry` 和 marker 安全路径；服务与 CLI 同步交付 reload 成败协议，只有进入认证 handler 的手动失败仍尝试清 cooldown。端点/凭据不可用不发送；Windows `off/restart` 未确认时安全失败，不接入强制停止候选。**只要求安装输出脱敏** |
| 2 共同 CLI | 迁 logs/stats、菜单、格式化和全部分发；`.sh/.bat` 退化成薄入口 | 删除旧 shell 路径解析、菜单和统计 heredoc；保持 ACCESS/stats 显示不变；仅安装输出脱敏；Mac 原功能及已批准异常非零全量对拍 |
| 3 客户端与完整覆盖 | 四客户端安装/备份/安装输出脱敏、各平台 hook 分别验收 | 真实客户端未验证前标未测；Windows 全命令目标不降级，但停止方式待决且端口独占须真机验证；未确认不能完成 Windows `off/restart` 功能验收。跨文件 install 部分完成必须准确报告 |

**执行顺序与授权边界**：后续在用户指示的新 worktree 中，先建并证明 §11 隔离门禁，再实施可判定部分：只读身份预检→固定锁及持锁复核→legacy 锁→端口绑定→记录/日志/账本；reload 稳定读取及认证边界与服务端/CLI 一起交付；install 按读取快照→脱敏预览→确认→复核/备份→再次复核→替换→权限后验执行。测试先走纯逻辑替身，再走六键隔离的真实 OS 测试；真实 Windows 的端口/ACL 验证另获授权。Windows `off/restart` 只实现安全失败，待用户对停止方案明确拍板后才可实现对应动作和验收完整功能。

**worktree 交接门禁**：原调查时只读 `git status` 显示本设计在 `tools/model_proxy` 仓库中尚未跟踪；新 Git worktree 不会自动带上未跟踪文件。后续实施前须重新核实状态并确认 worktree 能读到本次订正版本，经授权安全携带或纳入版本，不能按默认分支旧内容开工；本轮不创建 worktree、不暂存或提交。

各阶段先完成隔离证据，不自动上线。阶段 1 的 shell 菜单是限期过渡，**路径/marker 安全和安装输出脱敏不可绕过**；全量日志/账本脱敏不是本批门禁。阶段 2 删除其余编排。最终验收：根目录所有 `.py` 名称（含额外文件、大小写变体和符号链接/断链）为零；两入口同一 dispatcher；直接入口持 anchor；未测平台/客户端不得标通过。实施指令不解除单项阻断或生产手动操作边界。

**开发、回归与部署阶段验收**：用户要求“先不做部署，都开发验证完成后，并在worktree上启用新端口进行回归测试后，再和我对齐部署节奏”。因此先在 worktree 完成全部开发与验证，再在该 worktree 上按 §11 配置新 loopback 端口及完整隔离，进行真实回归并记录通过、失败和未测项；仅换端口不算通过。完成后向用户汇报并对齐部署节奏；在此之前不得合并到主线、部署或重启任何现有服务，对齐本身也不自动构成操作授权，**不得自动合并或上线**。测试中的 `restart` 仅针对经核验归属的隔离测试进程，不是重启现有或生产服务。Windows 停止未确认等未决项如未完成，须明确标未测/阻断，不能宣称全部开发验证完成或完整功能验收通过；可先向用户汇报阻碍并请求裁决，但不以部分完成代替部署节奏对齐的前提。

**生产实现复杂性：候选评估，不自动简化**。测试环境前置的放宽不取消进程与连接核验；生产契约按 §4、§5 的 Mac 锁与控制证据边界订正，其他既定约束不变。实施时应先评估可行性、工作量与替代方案的实际保障，并上报用户裁决，不能为了加速默默降低安全或功能：
- §4 的固定 anchor、路径身份/硬链接检查、active/last-good 快照及 legacy 锁迁移，涉及多入口并发和恢复状态。
- §5.1 的同一已连接 socket 对端进程归属核验，依赖 macOS/Windows 平台 API、权限及竞争语义；无法可靠核验时现有设计仍不发送管理凭据。
- §3.1 的稳定读取、文件身份及变更标记与 reload 原子提交，尤其 Windows 文件句柄元数据适配，涉及并发替换和自动重载。
- §6.1 的安装目标链接/硬链接、原有 ACL/DACL 保全、并发复核及部分提交报告，涉及跨平台文件系统行为；跨进程持久冻结仍是待裁决项。
- §4 的 Windows 锁和端口独占、§5 的 Windows 停止，以及客户端 hook 的真实平台适配，均需实际环境证据；强制停止仍待用户确认。

以上是静态识别的高复杂度候选，尚无实现和工期数据，不能断言哪项已无法在合理时间落实。若实施评估发现确实超出合理时间，报告具体阻碍、影响和候选简化及其安全/兼容代价，等用户明确决定；未经批准保持现有设计，不把未完成项标为通过。

### 9. 受控上线、请求排空与回滚（仅未来另获授权）

**排空/中断硬门禁**：维护窗口、active sessions 快照、端口检查、空闲连接快照都**不能证明没有在途模型请求或账本待收尾**。短期沿用 Mac 现有手动 `off/restart` 习惯；生产重启由用户手动决定，助手不自动操作线上。自动化脚本执行即可能中断在途流，未获真实验证前不宣称排空。上线或回滚只能在下列之一成立时继续：① 有经验证的真实排空证据；② 向用户明示在途流可能断开、账本末笔/内存学习态可能丢失，取得针对本次停服的明确中断接受。没有任一项则停止；不得自动断流、强杀或以维护窗口推定同意。

若未来用户选择私有优雅停止，额外方案应先在同一同步门禁下冻结模型请求准入、对新请求明确拒绝；原子维护所有已准入请求（含流式、异常、断连及相关后台账本收尾）的在途计数，并确保最后响应/账本 flush 结束才减至零。管理/健康操作须明确定义豁免范围；达到零且账本收尾通过才停服。等待有上限，超时保持不自动强杀，提示用户决定继续等待、恢复准入或另行授权中断。任何该能力、状态接口及拒绝新请求行为均须单独授权与实测，不是现有 API 已提供。

**Mac 旧版切换**（当前仅用户说明线上是 Mac、无 Windows 进程；以下动作均未执行）：
1. 用户批准目标实例、维护窗口、预检和必要的真实健康验证范围；先在隔离副本验证 anchor、缺损拒启、旧/新 legacy flock 和全部 CLI/marker。安全备份原代码布局、路径文件、用户允许备份的配置及 ACL，不打印内容；核对旧入口、配置的实际 lock/port/账本与外部调用。若无法获得可信原路径、备份、锁或身份证据，停止。
2. 静默会触发旧启动的客户端、SessionStart 和计划任务，记录恢复方式；旧 hook 的 mkdir 锁与新 anchor/短锁不互通，若不能证实不再有旧启动者，停止。完成上方排空证明或取得本次中断授权；不通过自动检查替代用户裁决。
3. 文件尚未替换时，由用户手动决定并通过**旧版 CLI**停旧服务；助手不自动操作线上。核验精确旧进程身份已退出、原 legacy 锁可独占、原端口无该服务。旧 CLI 的“没匹配”和端口空都不足单独证明。权限不足、PID 复用、持锁但身份未知或没有可靠退出证据即停止，不自动 kill -9。当前旧代码不持新 anchor，其日志可能在竞争锁前被修改，所以先静默是必要条件。
4. 停服后备份账本/sidecar 等所需状态及 ACL；部署完整新版文件集合，不启动混合版本。若首次/迁移安装缺 `install.json`，由获授权操作者按 §4 在固定 anchor 下完成预检、确认并创建初始化记录；不能把缺记录自动当首次。验证根目录所有 `.py` 名称/链接为零，且旧 .sh 用户路径仍存在。任何部分文件部署或初始化失败不得启动。
5. 由用户手动决定以新 CLI 启动，检查 anchor 与 legacy 锁的预期状态，并核对规范化安装路径、PID、进程启动时间、服务入口、端口和记录 nonce；需要发 token 时另按 §5.1 核验实际连接对端。不以这些检查声称证明同一 PID 持有两把锁；检查失败或证据不一致不发 token/信号。助手不自动重启线上。不得触发真实上游，除非另获授权。配置缺损必须拒启；服务状态不明或迟到 READY 不自动重启，先按 §4 安全恢复。

**配置损坏、有效漂移与回滚**：
- 固定 anchor/受保护的 active 和 last-good 快照是定位入口；缺损或有效漂移都不能按当前配置的新路径猜测旧实例。快照和新版身份可信时，在用户选择的日常停止政策及维护授权范围内用原快照定位，并按 §5 核对安装路径、PID、启动时间和服务入口后停止；不以证明两把 flock 的同一 PID 持有者为前提。快照/记录损坏、核验失败或证据不一致时暂停，不发 token/信号，不用旧 CLI 盲控新版。仅目标已确认为旧版、原 CLI 与身份可验证时才按旧版迁移流程操作。
- 回滚前重新执行排空证明或取得本次中断接受；由用户手动决定停止，并用新方案核对进程、固定 anchor、原 legacy 锁与端口全部释放，助手不自动操作线上，未知不杀。先安全保存新版运行期间产生的数据及 ACL，再恢复旧代码整体布局、原根 Python 入口及用户明确允许恢复的 hook/配置；不得 `git reset --hard` 或用旧数据备份静默覆盖新增账本。
- 旧版本不认新 anchor，因此恢复旧版本前仍须保证只有旧启动链可执行；旧版运行时不能把空闲的新 anchor 当作“没有服务”，下一次升级必须重新停旧版并做迁移预检。数据格式相容时优先保留当前状态；回填/合并旧快照需用户决定。最后由用户手动决定用旧 CLI 启动并核验原身份/锁/端口，再恢复自动触发；助手不自动重启线上，任何验证不明保持离线或原状。

### 10. 交付物规范（后续实施；本轮只订正本设计）

本轮无新产物，只就地订正本文件；以下目录树是后续实施目标，非本轮已创建的文件或 worktree。

**①目标目录树**：绝对根 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/`；以下为实施完成后的结构，`[新]`、`[移]` 明示变更，未列出的历史文档/测试按原位保留，不是删除清单。

```text
<model_proxy>/
├── .git/ .gitignore README.md VERSION CHANGELOG.md   # 保留原位
├── model_proxy_cli.sh                                # 保留路径，最终薄入口
├── model_proxy_cli.bat                               # [新] 原生 Windows 薄入口
├── .model_proxy_runtime/                             # [运行时新建、忽略] 固定锚点/记录
│   ├── anchor.lock control.lock                      # 持久锁文件，不删除
│   └── install.json active.json last_good_paths.json # 初始化、发现、可信快照
├── config/                                           # 保留；真实数据不进入交付物
│   ├── runtime_paths.json                            # 已有，单一配置来源
│   └── model_proxy_config.example.json               # 已有示例
├── assets/codex_catalog_template.json                # 保留
├── core/
│   ├── __init__.py server.py commands.py listen_config.py session_identity.py
│   ├── translate.py protocol_hints.py thinking_guard.py claudian_titles.py item_id_normalize.py
│   ├── reasoning/{__init__,capability,codecs,ladder,registry}.py  # 保留
│   ├── discovery.py runtime_paths.py platform_lock.py platform_process.py secure_files.py  # [新]
│   └── cli/                                           # [新] 同一 CLI 包
│       ├── __init__.py main.py dispatch.py            # bootstrap + 公共分发
│       ├── lifecycle.py control.py observe.py menus.py helper_entry.py  # [新] 共同编排与控制
│       ├── model_proxy.py                             # [移] 原服务薄入口
│       ├── _config_ops.py _format_ops.py _install_ops.py    # [移] 原 helper
├── hooker/ensure_model_proxy.sh                       # 保留路径，委托 CLI
├── hooker/ensure_model_proxy.bat                      # [新] 经客户端验证后使用
├── tests/
│   ├── isolated_runner.py                             # [新] 隔离测试门禁与入口
│   ├── test_cli_contract.py test_cli_parity.py        # [新]
│   ├── test_cli_lifecycle.py test_platform_process.py # [新]
│   ├── test_platform_lock.py test_runtime_paths_cross_platform.py  # [新]
│   ├── test_discovery_anchor.py test_control_binding.py test_reload_contract.py # [新]
│   ├── test_secure_files.py test_install_windows.py  # [新]
│   └── 原 tests/test_*.py、samples/                   # 保留并调整必要 import/mock
├── tools/model_proxy/.context/test_new_models_probe.py  # 原位，仅迁 import；禁止真实运行
└── docs/{ARCHITECTURE.md,CONFIG.md,designs/,archive/,...}  # 原位，历史不改写
```

**②命名规则**：`model_proxy_cli.*` 为唯一公开 CLI 包装，`core/cli/` 为共同编排，`core/platform_*` 为 OS 适配，`test_*.py` 为测试，`hooker/ensure_*` 为受管 hook；固定锚点从规范安装根推导，受保护记录为自动运行状态，既不是第二份人工路径配置，也不以易变 runtime key 派生。后续将本树及产物索引登记项目 README；本轮不改 README。

**③产物清单及消费者**：`main.py/dispatch.py`←两平台包装；`lifecycle.py`←dispatch；`control.py`←手动和 marker 状态/重载；`observe.py`←logs/stats/status；`menus.py`←supply/route/strategy；`helper_entry.py`←共同 CLI 的隔离 helper 子进程；四个移动文件←服务入口/CLI/测试；`discovery.py`←server、CLI 和恢复流程；`runtime_paths.py/platform_lock.py`←server 与 CLI；`secure_files.py`←本批实际新增权限校验的受控写入，**不扩至日志/账本冻结**；`platform_process.py`←lifecycle/control；`.bat` hook←已验证的 Windows 客户端；各新测试和隔离 runner←实施者/验收者。持久 gate、密钥或映射不是本批已批准产物。新增运行记录、临时文件及备份不纳入 Git，不作为交付的真实数据。

**④归属及偏离**：沿用项目已有 `core/`、`tests/`、`hooker/`、`docs/designs/`，`core/cli/` 是新增分层；既有 [[tools/model_proxy/docs/designs/2026-08-13-runtime-path-constants-unification]] 的路径文件约定保留，但由三个解析器收敛为一个；用户已确认文件缺失/损坏拒启、有效 object 单键缺失沿用原默认并经身份门禁。固定 `.model_proxy_runtime/` 是与业务路径无关的新增运行状态，不自动迁移原配置或账本；日志不原地改写，账本脱敏属于 §6.2 的独立后续候选。原项目根 README/VERSION/CHANGELOG 及配置目录保留。

**需同步的现有文件**：`core/server.py`（含 reload 成败上报）、`model_proxy_cli.sh`、`hooker/ensure_model_proxy.sh`；`tests/test_config_compact_format.py`、`test_config_ops.py`、`test_format_ops.py`、`test_install_ops.py`、`test_route.py`、`test_listen_config.py`、`test_supply_ext.py`、`test_session_identity.py`、`test_usage_totals.py`、`test_log_retention.py` 的必要 import/mock/兼容测试；`tools/model_proxy/.context/test_new_models_probe.py` 只改包引用不运行。`core/commands.py` 的 sidecar 写入不因本批脱敏/权限方案扩改。`README.md`、`docs/ARCHITECTURE.md`、`docs/CONFIG.md` 更新当前入口及限制；`CHANGELOG.md` 只新增记录，`VERSION` 是否升级遵循项目流程/用户确认；`.gitignore` 只按新增运行文件需要修改。除非用户另批，不改真实 `config/`、历史设计或其他 vault 项目。

### 11. 可执行验收矩阵与推演

**本轮禁止执行，以下仅供后续实施**：在新 worktree 或其临时项目副本测试，以该 worktree/副本及单次测试专用临时目录作为隔离根。使用假业务配置、假凭据、假客户端、假 HOME 和独立 loopback 端口；六项运行路径、固定 anchor 和所有测试状态均须归属该隔离根。禁止访问或修改生产数据、配置、凭据、日志、账本、锁和已有运行进程，包括对真实 `/tmp/model_proxy*` 做 `stat/open/mkdir/flock` 等；禁止连接真实外部上游或已有服务。OS 级全域文件/网络沙箱不是必需前置，可作为额外手段；不把仅换端口、仅设环境变量或仅靠 monkeypatch 当成完整隔离。真实 Windows、真实上游、真实客户端及线上实例的使用须用户另行明确授权；未授权/未执行标“未测”，绝不当作通过。

**拆分默认值语义与真实 OS 操作**：① 默认路径/单键缺失仅在纯逻辑单测中注入假的路径解析及文件系统接口；可断言解析结果含既有 `/tmp/model_proxy...` 字符串，但绝不能据此对真实绝对路径执行 `stat/open/mkdir/flock` 等。项目根相对默认值也只映射到隔离副本。② 真实多进程、服务、锁、端口、ACL 和 CLI 对拍测试必须显式提供六个均指向隔离根的路径；需要测缺键与锁联动时，在隔离 runner 内注入**隔离的默认值提供者**，不得运行未隔离的生产默认分支。旧版 golden 同样只允许隔离适配后的副本，绝不能因旧版硬编码 `/tmp` 而直接运行真实默认分支。

**启动前检查及运行边界**：每次测试和并发实例分别建立可识别的独立隔离目录；启动任何服务、CLI 或被测子进程前：
1. 明确实际项目根、工作目录、`HOME`/`USERPROFILE`/`LOCALAPPDATA`/`TMP` 等平台相关目录及固定 anchor，使用假 HOME 和假客户端目录；检查实际加载的 `runtime_paths.json`、业务配置、`MODEL_PROXY_CONFIG`、`appkey_file`、相对路径基准及其他会读写的配置/凭据均指向隔离根内的假文件，不复制或引用生产数据。对六键 `log/totals/lock/pid/ensure_log/start_lock` 使用显式独立路径，逐项解析并检查真实归属；检查已有父链、符号链接/重解析点、硬链接及目标身份，防止别名指向生产或逃出隔离根。六路径与固定状态及彼此之间不得意外重叠；并行测试各用独立的一组，只有专门的竞争测试才在该测试自身共享指定假资源。默认分支按上段隔离替身处理，不让旧版硬编码 `/tmp` 落到真实位置。
2. 为代理和所有 mock 上游分配不与现有服务共用的独立 loopback 地址/端口，启动前确认端口可用；实际 bind 失败或归属不明则停止，不连接或操作占用者。测试配置只指向本次自建的 loopback mock，禁用代理环境与真实上游/真实客户端访问；检查测试代码、helper 和子进程的网络入口，替换或阻断外连路径，不能证明只会连接本次 mock 时该测试阻断/未测。OS 网络限制可选，单靠 Python 级 mock 不足以证明子进程不会外连。
3. 记录测试启动的进程及其可核验身份、监听端口和所属隔离实例；状态、停止、重启及清理只针对能够确认由本次测试创建的进程，身份不明不发送控制请求、不结束进程，不能按端口或名称猜测归属。测试结束仅清理确认归属本次测试的资源。

以上检查覆盖直接入口、旧版 golden、CLI、hook、helper 与其子进程；用非生产哨兵或假夹具验证配置和路径解析，不为验证拦截访问生产。负向测试可有意构造缺损配置、隔离根内路径冲突、别名或配置漂移，以验证被测程序拒绝或报告错误；须事先核验测试涉及的所有候选目标均在隔离根内，并隔离默认值或注入替身，保证即使被测门禁失效也不会触及生产。运行中发现**非预期**配置漂移或路径别名、外连或进程归属无法确认时，立即停止该项并报告未测/阻断，不靠事后清理补救。上述是务实隔离而非 OS 级全域拦截保证；测试期间不触碰生产的要求不变。

实施完成并满足隔离门禁后，待实现的测试入口约定为：

```text
macOS/Linux: python3 -m tests.isolated_runner --suite all
Windows cmd:  py -3 -m tests.isolated_runner --suite all
```

`tests.isolated_runner` 是未来交付物，不是当前可执行工具。每项记录 OS/Python、commit、命令、通过/失败/未测；单测与真实 OS 多进程、终端和 ACL 测试分别记录。

| 范围 | 可复现步骤与红绿判据 |
|---|---|
| 既有命令 / 两入口 | 隔离副本逐条运行 §3 全命令；对 stdout/stderr、rc、输入时序、文件副作用、reload 次数对拍。`config` 必须未知且非零；排除已批准的异常非零/脱敏变化，兼容正常行为 |
| help / cmd 交互与重定向 | 同一 dispatcher 的语义及命令列表相同；允许 `.sh/.bat` 入口名、真实绝对路径、CRLF/LF 与终端编码差异。Windows `cmd.exe` 分别在交互终端、stdin 管道/EOF、stdout/stderr 重定向与批处理调用中测试中文、空格、`&`、括号、`!`、已由 caller 展开的 `%`、rc 和解释器发现；不得把 BAT 可见性限制误报为参数完全等价 |
| 首次与发现记录 | 新安装无记录 + 获授权交互预检 → 初始化一次并启动；无记录非 TTY、用户拒绝、来源不明/可能丢记录、无权限、记录损坏或 legacy 状态未知 → 非零且不写账本；假定已初始化但记录被删时不能自动当新安装。锁持有但记录尚缺 → 有界等待后 UNKNOWN |
| 可变路径 / 同一账本 | 让 A 持规范安装根 anchor 使用锁 L1、PID1、端口 P1、账本 D；将有效配置改成 L2/PID2/P2 但保留 D，分别并发 `on` 和直接执行新服务：均不得再获得 anchor/写 D；CLI 报漂移不向新端口发 token。可信 active/last-good 下，Mac `off` 只定位 L1/P1 的 A；Windows 未确认停止方案时 `off` 安全失败。两平台 `restart` 在漂移下非零且不启动 B；快照损坏或身份不明拒绝自动停止。模拟旧版迁移与新版记录损坏，确认不会用旧 CLI 盲控新版 |
| 运行文件同身份 | 在隔离根依次令 log=totals、lock=totals、pid=log、ensure_log=log，及日志/账本等于新 anchor、control 或状态文件；再用不同路径的符号链接、父目录链接、硬链接制造同一身份，覆盖存在及尚不存在目标、断链/无法取身份。server/CLI/hook/直接入口均须在任何数据写入或危险锁创建前拒绝，原内容不变；正常互异路径可继续。记录已存在锁的别名、权限不可判定和预检后路径变化时均 UNKNOWN |
| 并发、崩溃、迟到 READY | N 个 CLI + 直接入口同时启动仅一实例持 anchor；新版本失败者不裁剪日志；A 持 anchor 未 bind/记录未完整时 B 返回 UNKNOWN 而不是第二次启动；CLI 超时后 A 延迟 READY 不自动启动 B、不误杀 A，后续可验证其 nonce；杀死假进程后 OS 回收锁、有效 stale 记录经核验处理。旧版锁前可能碰日志，混跑只在假副本验证且真实切换必须先静默 |
| Mac 锁证据与控制身份 | 隔离假进程实际竞争 anchor 和 legacy flock，验证防重复启动与释放；正常控制不得依赖 `lsof`/`F_GETLKPID` 能证明同一 PID 持有两锁，也不得把记录、nonce、打开文件或锁冲突称为等价证明。分别注入安装路径、PID、启动时间、服务入口不符或不可读，验证 UNKNOWN/非零、零 token/信号且不重启；身份一致的正常路径仍可控制。测试仅验证可观察条件，不宣称排除同用户仿冒 |
| Mac 误杀、端口占用与 PID 变化 | 在隔离环境用已登记的假目标和无关假进程，覆盖同名/不同安装入口、无关进程占用新端口、目标退出及信号前可检测的 PID 或启动时间变化；错误目标不收信号，冲突端口不被认领、不发 token。用可控替身注入 PID 重用和核验间变化，另做真实隔离进程退出/端口竞争回归；核验后至发信号间的不可消除窗口须记录为剩余风险，不以测试通过宣称绝不误杀 |
| 缺损及恢复 | 文件不存在、无权限、OSError、损坏、非 object 均在写数据前非零；有效 object 逐一缺少 `log/totals/lock/pid/ensure_log/start_lock` 时，按本节隔离拆分：纯逻辑替身验证原默认，真实入口注入隔离默认或使用六键显式路径，所有入口规则一致；空串/null/错误类型/不可识别路径不能当缺键 fallback。模拟默认 lock/totals 与运行快照相同或冲突：相同不双启，冲突/未知不切锁、不写账本；Windows 四个 POSIX 默认映射须真实验证。运行中损坏以 anchor 和 last-good 输出脱敏诊断，身份不明不盲杀 |
| 不支持边界 | 在假环境演示两个不同安装根指向同一账本、hardlink、共享网络盘或多用户分别锚定时不自动得到全局互斥；共享网络盘使用模拟，不连接真实网络盘。文档和输出不得声称被保护。对能检测到的共享报错；未检测到不等于安全 |
| 阶段 1 shell / helper 门禁 | 暂留 shell 菜单的 install、marker 及路径操作：破坏路径文件必须在实际动作前非零，旧 fallback/eval 不能执行；**安装输出**注入假 token、原始 diff 时不泄漏，ACCESS/stats/`[cc]/[codex]` 保持原样。`helper_entry.py` 正确自举，测试 sys.argv、TTY、取消/130、marker yes/no/缺失/残留及失败；yes 仅调用一次共同 Python reload，身份未知为 0 次认证发送，argv/env 无假 admin token；做不到即阶段 1 不上线 |
| reload 实际结果 | 假配置读取/解析失败进入手动 handler：旧快照在本次提交边界保留、清 cooldown 后返回非 2xx、`loaded=false`、`cooldown_cleared=true`；CLI 非零并同时报告“reload 失败、cooldown 已清”。成功 200/CLI 0；回调在切换后失败应报告 `loaded=true` 和真实清理状态；清理异常/断线则标未知，不伪称已清或旧配置仍在用 |
| reload 前置失败与自动路径 | CLI 的业务配置损坏、admin token 不可得、listen 不可解析或端点身份不可验证时，断言发送次数为零、非零且不称 cooldown 已清；marker 已写文件则同时报告文件已写。另以假凭据先通过 CLI 门禁后再损坏服务端读取文件，证明已认证 handler 加载失败仍尝试清 cooldown；认证失败、未进入 handler 不清，发送后断线报未知。mtime 自动重载成功/失败都不主动清；旧 `curl -s`/旧 shell 可能掩盖非 2xx，新 CLI 必须检查实际状态 |
| reload 快照/mtime 竞态 | 使用隔离假配置和同步屏障：读完 A 后原子替换为 B（包括相同或更早 mtime）；最后一次路径核验前替换时必须拒绝不一致快照，核验后至提交前替换时允许先提交 A 及其标记，但下一次请求必须检测/加载 B，不能把 B 标记配给 A 导致持续漏载。覆盖读取中原地变化、路径暂缺/恢复、失败不推进标记、读取后替换与并发两次重载；自动路径不清 cooldown。相同身份和全部元数据均不变的非协作修改列为限制，不宣称检测到 |
| status/reload 端点绑定 | 验证 macOS `proc_pidinfo/proc_pidfdinfo`、Windows owner-PID 连接表及目标版本/权限的可行性；先核对安装路径、PID、启动时间、服务入口，再在发送前核验同一实际连接对端。客户端先 connect、服务端延迟 accept，在同一 socket 有界轮询后可匹配才发送；超时、FD/表权限拒绝、身份或 PID/四元组不符均非零且无 token、无重连。再让 A 退出、B 抢占端口，验证 B 收不到 token；覆盖 IPv4/IPv6 与死连接，未获真机授权不能标通过。此前 `lsof`/`F_GETLKPID` 的 macOS 本机实证不等于本项已通过 |
| Windows 端口独占 | 获授权 Windows 真机且仍受隔离约束时，用两个假进程按不同顺序绑定相同/重叠的独立 loopback 地址与 IPv4/IPv6，观察实际 socket 选项和连接归属；若默认行为不满足独占，验证 bind 前 `SO_EXCLUSIVEADDRUSE`、避免 `SO_REUSEADDR` 冲突、第二绑定失败、无账本副作用及重启/TIME_WAIT。通配地址绑定会超出 loopback 限制，本阶段不运行；§4 要求的通配地址证据须另获授权并在适当隔离环境取得，不能用 loopback 测试冒充。无法证明所需独占则 Windows 启动/上线阻断；不得外推到 Mac 或所有网络场景 |
| 进程及 Windows stop | 未确认停止机制时，隔离环境验证 Windows `.bat off/restart` 均非零、不调用停止 API、不启动替代进程；Mac `.sh`/非 TTY 用法对拍。只有用户先明确确认候选并另授权真实 Windows 进程试验后，才测同句柄身份、PID 重用、权限、退出/锁释放及强制停止后的账本；Python `finally` 和账本收尾不保证。未确认不能标完整功能通过 |
| ACL / 写入失败边界 | 注入 PREPARE/REPLACED/POSTCHECK_FAILED、备份/临时 ACL/replace/后验失败及目标占用；断言报告真实“已替换/未替换”、保留安全备份、不静默覆盖、已提交响应不补第二个状态。跨请求/跨进程持续冻结是待评估扩展，不能作为本批已完成项；若可靠边界必须依赖它，阶段 1 阻断 |
| install 链接和原权限 | 隔离夹具覆盖有效/断开的符号链接、父目录重解析点、目标硬链接、现有非默认 mode/ACL/DACL/继承及不可复制权限：能安全保持时链接、授权目标及保护等价；否则 replace 次数为零且原目标未变。临时/备份不弱化保护，替换后校验失败按 POSTCHECK_FAILED 如实报告；Windows ACL 条件须获授权真机验证 |
| install 预览后并发 | 用屏障在读取/预览、用户确认、备份及 replace 前分别修改字节、替换身份/链接或创建原本不存在的目标；可检测变化必须不前向覆盖，提示重新预览，准确报告已完成的前序文件/备份。验证不存在目标的原子不覆盖创建；在纯假夹具演示最终检查后不协作写入仍可能竞争，不把比较当作 CAS 或声称彻底解决 |
| install 脱敏 / Codex 部分完成 | 四客户端候选、成功提示、手工片段、diff、异常和输出均不得泄漏假 token；假客户端文件中应有真实假 token 而非占位符。分别模拟 catalog 下载/模板在自身写入前不可用：已有行保留、允许主配置成功 + 警告；以及 catalog/config ACL、replace 或后验失败：停止后续、整体非零、逐文件真实状态与备份，不伪称全未写 |
| 现有日志/账本保持 | 本批不迁移/改写：用按旧 ACCESS 日志和 v3 账本格式构造的假数据验证 `[cc]/[codex]`、stats 日/月/total 输出和持久内容均与基线一致，不读取或复制生产日志/账本；只测安装输出不含假 token。全量 HMAC/随机 ID、密钥、账本迁移属于后续独立候选，不能作为本批通过条件 |
| 日常 off/restart | 保持 Mac 现有手动习惯：验证只停目标、提示可能中断在途、错误非零；非 TTY 自动化现有行为不被新增交互改变。活跃流时不得声称排空；不把 Windows 强杀或优雅排空当已选。生产重启由用户手动决定，助手不自动执行；隔离演练 §9 回滚及账本保全 |
| 业务与无双栈 | 在隔离环境运行既有协议/路由/流式/用量测试；AST/静态检查 `.sh/.bat` 不再分发业务，status 和两类 reload 同一 control，server 与 CLI 共用 anchor，其他模块无裸 `fcntl` 启动；不假称真实平台已经通过 |

**根目录零 `.py` 验收**：在隔离副本项目根直接枚举 `Path('.').iterdir()` 的每个名字（包括隐藏、大小写变体、符号链接和断链），只要 `name.casefold().endswith('.py')` 即失败；不能只检查四个旧名字或只用会漏掉断链的 `is_file()`。例如在项目根使用已选 Python 运行：

```text
python -c "from pathlib import Path; import sys; a=[p.name for p in Path('.').iterdir() if p.name.casefold().endswith('.py')]; print(*a, sep=chr(10)) if a else print('OK'); sys.exit(bool(a))"
```

该命令仅展示枚举逻辑；Windows 用可用的 `py -3 -c`，macOS 用 `python3 -c`，只在隔离副本获准执行。测试先创建额外 `.py`、`.PY`、隐藏 `.py`、正常/断链符号链接，验证均被发现；只报告未知文件，不擅自删除。

**〔纯示意〕有效配置漂移推演**：实例 A 持有固定 anchor，使用 L1/P1 写 D；配置切为 L2/P2 但 D 不变。直接启动 B 首先竞争同一 anchor 失败，既不能获得 L2 后写 D，也不能因为 P2 空闲报 READY。若是不同安装根共享 D，anchor 不同，无法据此证明安全，明确不支持。

**〔纯示意〕迟到 READY / 初始化推演**：无记录且 anchor 空闲只能证明该锚点没有持有者，不能证明首次或没有 legacy；必须完成可核实预检。若 A 已持 anchor 但启动超时，CLI 不再发 B；A 之后 READY，后续查询核对 nonce/进程身份才认领。

**〔纯示意〕端口抢占推演**：CLI 已连 A，随后 A 关闭而 B 在原端口监听；CLI 只允许在原已建立且已校验的 socket 上发送，不重新 connect，B 不能因监听同端口而收到这一请求。若 OS 不能证明该 socket 的服务端 PID 或进程身份，则根本不发送 token。结论依赖 OS API 可用、锁记录可信和无同用户恶意转交等前提，需真机验证，不等于绝对安全。

**〔纯示意〕ACL 后验故障**：某安装目标已替换但后验权限检查失败；当前操作报告“文件已改、权限未知”，不静默回滚或继续该次写入。若不能证明其它进程后续不会写同一目标，则跨进程状态仍为 UNKNOWN；是否需要扩大到共享锁或持久标记待评估并阻断，不能声称全局冻结。

**〔纯示意〕同身份与入口覆盖**：假账本 D 与固定 `anchor.lock` 是不同名字、同一 inode 的硬链接；纯路径字符串比较会漏掉，身份预检须在创建或写入锚点、触碰 D 前拒绝。CLI、hook、直接服务入口均经过同一门禁；另一安装根里的未知硬链接是反例，不因此宣称全局互斥。以上不是实测。

**〔纯示意〕reload 替换时序**：读取 A 后路径换为 B，若将 B 的 mtime 记到 A 上，下一次可能误判未变化；新设计若在最终核验前发现 B 则本次不提交，若 B 在核验后替换，则提交 A 的身份标记，下一次比较发现 B。反例是同身份且全部可观察元数据未变化的非协作原地改写，不能保证发现。以上不是实测。

**〔纯示意〕安装并发**：用户预览 X 并确认，另一写入者改为 Y；若发生在最终复核前，停止并要求重新预览 Y，不覆盖。若发生在最终复核后、替换前，普通 `replace` 仍可能覆盖 Y；原路径是符号链接时还须保留该链接，无法证明则不提交。以上不是实测。

**结构不变量**：`.sh/.bat → 相同 dispatcher/控制模块 → OS adapter`；旧菜单 marker 与手动 reload 同一控制路径；hook/直接服务入口都不得绕过固定 anchor；`server` 不反向依赖 CLI。实例互斥、token 端点绑定、文件提交状态和请求排空分别验证，不能以其中任一项代替其他。

### 12. 独立复核逐条对照（均为设计，尚未实施或验证）

| 项 | 处理和验证位置 | 仍需选择或保留限制 |
|---|---|---|
| ① reload 200 与 rc | §3.1、§11：加载成功 200/CLI 0；失败非 2xx/非零、原因脱敏；进入手动 handler 的加载失败仍尝试清 cooldown | 认证失败未进 handler 不清；清理失败/掉线如实报未知；无版本对拍，旧客户端可能掩盖错误 |
| ② 阶段 1 旧 shell 旁路 | §2、§8、§11：marker/路径走共同门禁；本批仅安装输出脱敏 | 全日志/账本/`[cc]/[codex]` 不在本批门禁；门禁未通过则不得上线 |
| ③ 有效配置漂移和缺键 | §4、§9、§11：文件缺失/损坏拒启，object 只缺单键沿用原默认并经过 anchor/锁/账本快照核验 | 空/null/错误类型不当作缺键；多键同时缺失及 Windows 映射未实测，风险不明时不启动 |
| ④ 同 socket 对端验证 | §5.1、§11：Mac FD、Windows owner 表、未 accept 有界等待，同连接不重连 | OS 能力/权限须实际证明；不可证明不发 token |
| ⑤ ACL 后验故障 | §6.1、§11：报告真实部分完成、不静默回滚、已提交响应不补状态 | 跨请求/跨进程持续冻结待评估；若可靠边界必须依赖它则阻断，第三方进程不保证 |
| ⑥ Codex catalog | §6.1、§11：可选下载失败保留降级，ACL/提交失败非零并报告部分完成 | 各客户端真实 ACL/路径仍未验证 |
| ⑦ 全量日志/账本脱敏 | §6.2、§11：独立后续候选；本批保持现有 ACCESS/stats/`[cc]/[codex]` 和持久内容不变 | HMAC/随机 ID、密钥及迁移未授权；历史敏感数据风险仍在 |
| ⑧ 日常停止 | §5.2、§9、§11：短期维持 Mac 手动习惯、只停目标并提示中断；非 TTY 兼容保留 | 不要求每次交互；Windows 强杀/优雅排空未选；真实上线/回滚另授权 |

### 12.1 本次七项独立复核订正

| 项 | 设计与执行位置 | 验收和剩余边界 |
|---|---|---|
| 1 运行路径同身份 | §4 只读预检、锁内复核及数据写前检查；§8 先于数据写入 | §11 同路径、符号链接、硬链接与新 anchor 冲突；不同安装根/非协作写入不保证全局互斥 |
| 2 reload 前置失败 | §3.1 区分 CLI 凭据/listen 不可得、认证 handler 内加载失败与发送后未知 | §11 验证零发送及 handler 内仍尝试清 cooldown；mtime 自动路径不清 |
| 3 安装链接与权限 | §6.1 保存/核验目标身份、链接和原有 ACL；不能安全保持则拒绝 | §11 断链、硬链接、不可复制 ACL 及后验故障；真实 Windows 待授权验证 |
| 4 前向提交并发 | §6.1 确认后和 replace 前比对身份、权限、链接及原始字节，变化则重新预览 | §11 覆盖外部改写/创建；最后检查与 replace 间的非协作竞态仍存在 |
| 5 默认绝对路径与隔离 | §8、§11 将默认值纯逻辑测试与显式隔离路径的真实 OS 测试分开 | 禁止触碰真实 `/tmp/model_proxy*`；六路径、配置、外连边界或测试进程归属无法确认则阻断，不要求 OS 级全域沙箱 |
| 6 mtime/读取快照 | §3.1 将实际读取描述符的稳定元数据与配置一起提交；标记不等触发 | §11 A→B 替换、同/较早 mtime 与读取中变化；元数据相同的非协作修改不保证检测 |
| 7 Windows 端口独占 | §4 保留为待真机证据的假设；需要时 bind 前使用独占选项 | §11 测地址重叠、端口竞争及重启；不能证明则 Windows 启动/上线阻断 |

## 风险与权衡

1. **已决（按用户明确原话）**：路径文件缺失/损坏/不可读取拒启；仅安装输出脱敏；短期保持 Mac 现有手动 `off/restart` 习惯，生产重启由用户决定；保留 flock 防重复启动，CLI 核对安装路径、PID、进程启动时间和服务入口，发送凭据前核验实际连接对象；核验失败或证据不一致不发 token/信号。无需证明同一 PID 持有两把 flock；reload 如实上报本次加载成败，成功 HTTP 200，失败非 2xx 且原因脱敏。ACCESS/stats/`[cc]/[codex]` 保持现状。用户说明当前仅 Mac、无 Windows 进程，不批准未来 Windows 强杀。status 仍 draft。
2. **仍待用户决定**：① Windows 是否采用经核验同一进程句柄的 `TerminateProcess` 推荐候选，或以后另设计优雅排空；确认前 `off/restart` 安全失败、不停止也不启动；② 如需未来另做全量日志/账本脱敏须独立授权。手动 reload 进入认证 handler 后失败仍尝试清 cooldown、有效 object 单键缺失用原默认均已定；多键同时缺失的扩展未拍板。已有实施指令不代替这些未决项的确认。
3. **另须裁决/评估**：ACL 后验失败是否引入跨进程持久冻结；若可靠失败边界无法不扩范围实现，阶段 1 阻断。目标系统无法可靠核验连接对象时保留 fail-closed 或另设计可信通道；真实停服是否中断在途、Windows 停止方式、正式版本/Linux/客户端 hook 范围亦待定。跨安装共享账本/多用户/网络盘需另设计。
4. 旧根 Python 直接路径与无根 `.py` 不可兼得；已知用户和未知外部调用需在迁移前查明并公告。首次安装/遗失记录需预检；同用户恶意进程、OS 权限不可见及跨安装共享并未被宣称解决。PID 身份最后核验与发信号之间存在小窗口，不能保证避免 PID 重用或误杀；install 的最终复核与使用之间也有非协作竞态，mtime 标记不能识别所有元数据不变的修改；不声称事务隔离或绝不漏改。
5. 关于本次锁持有者问题，此前针对 `lsof`/`F_GETLKPID` 无法可靠证明 flock 持锁者的实证仅限 macOS 本机，不代表端点归属或其他平台能力已验证。端点归属、WinAPI 锁/ACL/端口独占、配置读取快照、安装输出脱敏、跨进程写入边界、Windows 停止和客户端 shell 均未实测；全量日志/账本脱敏未授权。可实施部分已有后续 worktree 实施指令，本轮仍只订正；开发完成并在 worktree 新端口真实回归后须与用户对齐部署节奏，不自动合并或上线。真实 Windows/客户端、生产停服或真实上游使用须另行授权，生产 Mac 的停服/重启始终由用户手动决定。§11 务实隔离前置、所需进程/连接身份、权限或端口独占无法核验时相应阶段/平台阻断；不把无法证明两把 flock 的同一 PID 持有者单独作为 Mac 控制阻断。OS 级全域沙箱不是测试前置。

## 验证方式

具体命令、隔离前提、真机授权门禁、逐项通过标准和回滚演练见 §11；本次订正仅静态检查本设计记录，**未运行任何测试、未启动服务、未访问网络，也未读取生产配置或日志**。

## 关联

- [[tools/model_proxy/docs/designs/2026-11-12-model-proxy-Windows兼容性审查与改造方案]]：已有静态审查；本草案另建，不覆盖。
- [[tools/model_proxy/docs/designs/2026-08-13-runtime-path-constants-unification]]：既有路径与 fallback 约定；用户已同意路径配置缺损时拒绝启动，具体边界见 §4。
- [[tools/model_proxy/docs/designs/2026-10-28-cli-restart命令设计]]：既有 restart 语义及停机风险。

### 项目内历史直接引用索引（只读，不要求改历史）

下列均相对 `tools/model_proxy/docs/`，按匹配到的名称归组；同一历史文档可出现多组。本列表由文件名精确搜索取得，注释/历史讨论不等于活跃调用：

- `model_proxy.py`：`archive/model_proxy_buildplan.md`；`designs/2026-07-23-readme-ideal-restructure-execution.md`、`2026-07-24-usage-stats-cache-align-safe-rollout.md`、`2026-07-29-sandbox-to-production-sync-plan.md`、`2026-08-07-cli-status-help-assessment.md`、`2026-08-13-版本管理方案.md`、`2026-10-28-cli-restart命令设计.md`、`2026-11-12-model-proxy-Windows兼容性审查与改造方案.md`。
- `_config_ops`：`archive/reasoning_relative_remap_redesign.md`；`designs/2026-07-22-install-manage-sessionstart-hook.md`、`2026-07-23-model-proxy-full-audit.md`、`2026-07-23-usage-totals-incremental-write.md`、`2026-07-23-usage-totals-ledger.md`、`2026-07-28-session-load-balancing-feasibility.md`、`2026-07-28-session-route-dispatch-design.md`、`2026-07-29-sandbox-to-production-sync-plan.md`、`2026-08-04-in-band-route-command-design.md`、`2026-08-04-topic-based-route-dispatch-feasibility.md`、`2026-08-06-session-overrides-single-storage.md`、`2026-08-07-cli-status-help-assessment.md`、`2026-08-07-config-compact-format.md`、`2026-08-07-nation-route-supply-expansion.md`、`2026-08-07-sidecar-delete-bug-and-compact-tiers.md`、`2026-08-08-cli-status-help-improvement-plan.md`、`2026-08-08-code-audit-comprehensive.md`、`2026-08-08-code-audit-verification.md`、`2026-08-08-log-audit-target-design.md`、`2026-08-08-log-optimization-plan.md`、`2026-08-08-status-p0-implementation-plan.md`、`2026-08-08-tool-use-id-guard-implementation.md`、`2026-08-13-runtime-path-constants-unification.md`、`2026-08-17-install-claude-补onboarding跳过登录.md`、`2026-08-22-跨协议静默丢弃审计与修复方案-v3.md`、`2026-08-22-跨协议静默丢弃审计与修复方案-v4.md`、`2026-08-23-异常逃逸与全局潜在风险全面审计.md`、`2026-10-15-supply-test正文摘要与body-error联动.md`、`2026-11-04-tool-config-fix-and-guard-revert.md`、`2026-11-05-modelproxy-protocol-and-stream-quality.md`、`2026-11-12-model-proxy-Windows兼容性审查与改造方案.md`。
- `_format_ops`：`designs/2026-08-07-cli-status-help-assessment.md`、`2026-08-08-cli-status-help-improvement-plan.md`、`2026-08-08-code-audit-comprehensive.md`、`2026-08-08-code-audit-verification.md`、`2026-08-08-status-active-sessions-design.md`、`2026-08-08-status-content-redesign.md`、`2026-08-08-status-p0-implementation-plan.md`、`2026-08-09-proxy-command-namespace-design.md`、`2026-08-09-status-offline-simplify.md`、`2026-08-13-runtime-path-constants-unification.md`、`2026-08-15-supply-cooldown-errorcode设计.md`、`2026-08-22-跨协议静默丢弃审计与修复方案-v3.md`、`2026-08-22-跨协议静默丢弃审计与修复方案-v4.md`、`2026-08-23-passthrough终态与首帧预读failover-v2.md`、`2026-08-23-passthrough终态与首帧预读failover.md`、`2026-08-23-异常逃逸与全局潜在风险全面审计.md`、`2026-08-23-最小可靠透传重构方案-v2.md`、`2026-08-23-最小可靠透传重构方案.md`、`2026-11-12-model-proxy-Windows兼容性审查与改造方案.md`。
- `_install_ops`：`designs/2026-07-22-install-manage-sessionstart-hook.md`、`2026-07-23-model-proxy-full-audit.md`、`2026-07-23-readme-ideal-restructure-execution.md`、`2026-08-06-session-overrides-single-storage.md`、`2026-08-07-cli-status-help-assessment.md`、`2026-08-07-config-compact-format.md`、`2026-08-08-code-audit-comprehensive.md`、`2026-08-08-code-audit-verification.md`、`2026-08-10-proxy-message-inter-session-design.md`、`2026-08-11-proxy-message-implementation-plan.md`、`2026-08-13-readme-restructure.md`、`2026-08-13-runtime-path-constants-unification.md`、`2026-08-17-install-claude-补onboarding跳过登录.md`、`2026-08-18-install-codex-补catalog处理.md`、`2026-08-19-官方session通信对比与自建重启方案.md`、`2026-11-12-model-proxy-Windows兼容性审查与改造方案.md`。
