---
type: design-decision
status: confirmed
target: "[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli]]"
tags:
  - architect
  - model-proxy
  - windows
created: 2026-11-15 21:56:33
---

# Windows 适配复杂度独立复核：收敛现有 B，而非再次重造

## 背景与问题

重新判断：同一份 Python 业务和 CLI、薄 sh/bat 的目标是否成立，剩余复杂度究竟来自操作系统，还是来自后来增加的证明与测试架构。

**结论：用户对复杂度的质疑成立；当前已实现共享业务与公共 CLI 入口，但尚未完成日常行为对等和原生 Windows 验收。应收敛现有 B，不回退成旧版风险行为，也不再启动一轮平台框架重写。**

特别纠正旧快照：**现在 Windows 五命令、共同服务入口、单份轻记录、有限私有文件写入均已有实际执行代码，不再是全部无条件拒绝。** 当前复杂度的重要来源已经从“尚缺底层实现”变成“新旧路径并存、重复核验、平台控制器内混有公共策略、测试没有围绕产品闭环收口”。

### 调查与授权边界

- 本轮只读产品源码、脚本和文档；使用 AST、源码引用与行数统计，不导入产品模块、不执行 CLI、不运行服务、Whisky 或测试。
- 未读取真实配置、日志、凭据；未查询或操作已有业务进程、端口、服务。唯一工作区写入是本独立 draft。
- 作用根 **W**：`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli`。下文 `W/…` 均指该绝对根下的具体文件；实际文件操作使用 vault 相对路径。
- Mac 主区和 worktree HEAD 均为 `7547517`。主区实际 `model_proxy.py`、`model_proxy_cli.sh`、`core/server.py`、`_config_ops.py`、`_install_ops.py` 与 worktree 的 HEAD 原字节一致，故本轮旧版对照不是猜测线上代码。
- 工作树含 20 个 tracked 状态项、152 个 untracked 项；分析以磁盘源码为准，不只看 diff。它们是已有工作，不是本轮产出。
- 当前顶层授权信息优先于旧记录正文：连接对象核验、同一 HANDLE 强制停止、私有文件保护已获用户批准；本建议保留，不重复要求批准已有决定。
- 已确认的 [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy共享Python与Windows最小适配再审视]] 只读，`status: confirmed` 不变。其正文仍含旧时的 draft/待批准及代码缺口描述，不能用来覆盖当前授权和源码事实。本记录不 supersede 它。
- 本轮源码检查口径：`core/**/*.py`、`tests/**/*.py`、CLI 两壳、hook 两壳共 199 文件，其中 195 个 Python 文件完成 AST 解析；聚合 SHA256 为 `e456d8532b66725659ce94c8abba1f5adb9c2e58a103552079e54d0624af49ff`。这只证明语法可解析和调查快照，不证明 imports、功能或平台通过。

## 方案设计

### 1. 当前是否真正做到“一份共享 Python，sh/bat 只是壳”

| 层次 | 当前代码事实 | 判断 |
|---|---|---|
| 业务内核 | 两个平台进入同一 `core/server.py`；路由、认证、转换、JSON/SSE、统计与配置含义共享 | 已做到；无需 Windows 协议或业务副本 |
| CLI 入口与菜单 | 两壳调用同一 `core/cli/main.py → dispatch.py`，菜单、日志、统计、配置/安装 helper 在 Python | 已做到；sh 11 行、bat 20 行，壳不再是复杂度来源 |
| 服务入口 | Windows 控制器启动共同 `core/cli/model_proxy.py`，传显式 config/port 后进入共同 server；直接 Windows 启动有参数归一化路径 | 已接线；旧 `windows_entry.py` 不是当前正常启动入口 |
| 五命令 | `WindowsControl._run_lifecycle` 实际编排 on/off/restart/status/reload；Mac 走 `MacControl` | 已编码，不能再称全部拒绝；公共生命周期策略仍分散在两个控制器中 |
| 配置与安装写 | Windows `_config_ops` 已接 `capture_target/write_target`；安装写也接有限 writer | 已接线，不能再称“一律禁止 Windows 写入”；真实 ACL/替换结果未由本轮实测确认 |
| 用户可见对等 | Windows status 没有复用详细状态展示；监听与自动 hook 范围还有差别 | 未闭环，入口共享不等于完整行为共享 |
| 原生平台验证 | 源码与替身测试存在，但本轮无实际 Windows 测试结果 | 未完成；不能宣布原生 Windows 可用 |

关键代码：`W/core/cli/dispatch.py:752–812`、`W/core/cli/windows_control.py:590–880,907`、`W/core/cli/model_proxy.py:178–255`、`W/core/server.py:4202–4207,4730–4803`、`W/core/windows_secure_files.py:360–381`。

**准确业务表述：同一个工具、同一套业务和命令；Python 内部只在锁、进程、权限等系统边界分平台。** “bat 是壳”正确，但“除了 bat，所有 Python 调用都不需适配”不成立。相同 Python 环境消除了包/版本差异，不消除 OS API 差异。

### 2. 原版有哪些真实依赖，哪些是后来加上去的

| 问题 | 原版 Mac 实际代码 | 必须的移植工作 | 不天然必需的附加架构 |
|---|---|---|---|
| 单实例互斥 | `server.py:3751–3763` 的 `fcntl.flock`；锁文件以 `w` 打开 | Windows OS 文件锁；不能把锁文件/PID 文本当锁；避免竞争者截断锁 | 安装来源证明、三记录一致性、线程内可重入 lease 状态模型 |
| 启动/就绪 | CLI `373–394`：nohup、lsof、日志重定向 | 共同 Python subprocess/等待，Windows 创建标志；认出自己的服务 | 完整原始命令行字符串等价、环境摘要、严格专用入口 |
| status/reload | CLI `131–151,173–223`：读 token、curl、lsof、ps/awk | Python HTTP 与状态展示；已批准的真实对端核验 | 多层 authority 才允许任何 HTTP；所有祖先路径作为永久证明 |
| off/restart | CLI `403–454`：pgrep、端口文件名兜底、kill、等待退出 | OS 进程身份与停止；Windows 保留已批准的同 HANDLE 强停 | 安装 ID、按锁所有者建立完整证明、另造优雅排空协议 |
| 路径 | 精确 `/tmp/model_proxy*` 默认值、项目相对路径 | Windows LOCALAPPDATA、盘符/引用/大小写处理；集中解析 | 通用全别名 canonical key、安装复制和任意旧路径接管 |
| 敏感文件 | `_config_ops.py:81–96`：mkstemp/replace 后 chmod；旧 install 普通备份和 write_text | Windows DACL、创建即私有、替换后核验；不能照搬 chmod 私密语义 | 泛化 coordinator、alias freeze、协议状态机和“未来第三方永久冻结” |
| 自动启动 hook | 旧 hook 的 mkdir 启动锁与 CLI 委托 | 锁回收进 Python，sh/bat 委托 on | 给 Whisky 再造与产品同规模的虚拟运行环境 |
| 监听 socket | 共同 HTTP 服务绑定 | 所选监听场景的 Windows 独占绑定及归属查询 | 未测全矩阵作为所有代码开发的总门禁 |

原版 **CLI 自身有 716 行 Bash**，包含菜单、生命周期、日志筛选和统计；所以从原版迁到“公共 Python CLI”本来就需要迁移这一部分，而不是只另写一个 bat。原版核心业务在 Python的判断仍然正确。

本次主区旧代码没有新的 Darwin ACL/libproc authority 体系。worktree 中新增的 `darwin_acl.py`、Mac launch 环境/映像归一化、`MacInstallationAuthority`、符号链接目标快照和多阶段防冲突，是此前改造增加的机制，**不能倒过来归因成旧 Mac 业务固有的平台障碍**。其中已批准的 Mac 链接兼容及安全能力也不能因 Windows 减法而顺带撤回。

官方边界已核实：

- Python `fcntl` 是 Unix 接口；锁必须换 OS 实现。
- Python `os.chmod` 在 Windows 主要控制只读标志，不能据此证明 token 文件只对当前用户可读。
- Python 的 Windows `Popen.terminate/kill` 使用 TerminateProcess，而非 POSIX SIGTERM 收尾；Microsoft 文档说明跨进程调用异步发起终止，需等待进程结束。

这些事实支持有限的系统适配，**不支持必须采用现有全部证明架构**。官方来源见文末；未使用第三方博客推导平台行为。

### 3. 当前规模与复杂度归因

本轮从磁盘重新计数，与主会话 2026-11-15 21:48 的统计一致；包括空行和注释，不作删行 KPI：

| 当前分组 | 文件数/行数 | 判断 |
|---|---:|---|
| core 顶层 windows_*.py + platform_windows.py | 9 / **5,420** | ACL/paths/process/platform 共 3,269；secure 四模块共 1,909；listener 242 |
| core/cli/windows_*.py | 5 / **1,745** | control 953、命令行/CIM 513、旧 strict entry/launch/authority 279 |
| tests/test_windows*.py | 54 / **17,480** | 包含真实边界、旧模型、mock、Wine 桥接等不同性质的测试 |
| sh / bat | **11 / 20** | 已经足够薄，无须再折腾壳 |

此外，复杂度不只在 windows 命名文件：共同 `server.py` 从旧版 3,827 到 5,018 行；`discovery.py` 1,851、Mac `authority.py` 1,441、`platform_lock.py` 651。配置 helper 从 1,083 到 1,534，安装 helper 从 814 到 1,390；format helper 780 到 779，属于基本平移。不能把所有新增代码都叫“Windows 无法避免的适配”。

**需要保留的复杂度**：实际 socket→进程归属、创建时间/映像/入口核验、同 HANDLE 停止、私有创建与安全替换；这部分比原版强，且已获批准。

**过度设计/未收口的复杂度**：

1. 已有新产品路径，却仍留严格旧入口、旧三记录 peer 框架和冻结写入模型；文档注释还称它们是运行前置，容易让实施继续追逐已取消目标。
2. 同一份状态展示、HTTP 响应解析、reload 成败解释与 restart 编排在平台控制器内分散；已经发生可见行为分叉。
3. 持有父链、多次全链读取、内层重入/关闭状态与外层重新快照叠加；它们有局部价值，但不应再宣称能消除同用户最终 TOCTOU。
4. 命令行查询的 513 行包含 PowerShell/CIM 读取以及 helper 的线程/队列/清理状态；读取入口有必要，整个异步协调层不是 Python 跨平台的自然要求。
5. `tests/isolation_bootstrap.py` 415 行、`worktree_regression.py` 688 行等测试保护是后来建的测试设施，不是产品需求；而且现有入口依赖 POSIX PTY/lsof，不能直接证明 Windows 产品。

### 4. 具体保留、简化与待删清单

以下都是**确认后的建议**，不是本轮删除授权。删除要先迁移消费者，再查 imports/测试/直接入口，不按文件名整包删。

| 模块/区域（均在 W 下） | 建议 | 风险与边界 |
|---|---|---|
| `core/server.py` 业务、translate/reasoning/commands 等 | 保留 | 不另造 Windows 业务内核；协议回归不因平台减法取消 |
| `core/cli/main.py/dispatch.py`、两壳、菜单/format/stats/BRE | 保留；把状态展示和小段公共控制逻辑复用起来 | 不移回 sh/bat；不为“同一份”重写 Mac authority |
| `core/runtime_paths.py` | 保留并复用解析 | 四个精确旧默认值映射已实现；路径不兼容不能静默转到别处 |
| `core/windows_process.py` 635 行 | 保留实际 TCP owner、held process、terminate/wait | 不删成 PID-only/taskkill；当前同 HANDLE 停止已在 `402–470` 接线 |
| `core/windows_acl.py` 494 行 | 保留私有对象与有效用户 AccessCheck | SYSTEM/Admin“可信但不强制 grant”已经实现，不需再次设计；不因 Wine 丢保护标志判成功 |
| `core/platform_windows.py` 1,487 行 | 保留 Win32 锁、私有创建、文件读回、replace/move 等 primitive；去重上层封装须逐消费者评估 | 真 OS 调用和适配结构不能整体删除；关闭/提交失败报告不能丢 |
| `core/windows_paths.py` 653 行 | 保留路径类型、明显重解析、角色冲突、必要祖先写入危险检查；复用解析并收敛重复全链核验 | 不自动放宽祖先/私有文件策略；若后来确需变更策略，另给具体安全裁决 |
| `core/cli/windows_control.py` 953 行 | 保留已能执行的五命令；抽小型公共展示/响应/编排；旧 RecordClaim/observe 路径待删 | 不先重写；旧 `validate_record_claim/observe_peer` 主要服务旧 authority，正常 B 走轻记录 |
| `core/cli/windows_command_line.py` 513 行 | 保留解析真实入口/config/port及受管 CIM 查询；后续精简 helper 生命周期 | 不换成记录自证；清理只处理本次创建 helper，不能扫进程名或杀树 |
| `core/cli/windows_entry.py/windows_launch.py/windows_authority.py` 共 279 行 | 正常 B 已不消费，核对旧测试和直接入口后可删除 | `windows_entry` 本身仍拒绝，但不能用它代表共同入口也拒绝 |
| `core/windows_secure_files.py` 381 行及 native 中 `ProductWriteBackend` | 保留有限流程；合并重复错误/快照管理 | `write_target` 已走 ProductWriteBackend；不是补空 backend。保留备份、提交未知与后验失败语义 |
| `core/windows_secure_write_model.py` 208 行、secure_write coordinator 主体 | 迁移少量产品结果类型后可删旧模型/冻结路径 | 产品 `_finite_write` 仍从 `windows_secure_write` 导入 `WriteOutcome`；整文件直接删会断引用 |
| `core/windows_secure_native.py` 中旧 `NativeWriteBackend`（324 行）、旧 CandidateAdapter（222 行）及配套 | 若无有效消费者则可删；保留 ProductWriteBackend 与 `_CommitCalls` | `_CommitCalls` 注释虽称 unwired，当前 ProductWriteBackend 已真实调用，不能误删 |
| `core/discovery.py`、server 内旧 Windows 三记录分支 | 保留 Mac 所用契约；逐分支清理 Windows 取消契约 | `read_instance_set`、install/last_good 不可全局删除；单轻记录已在 B 接线 |
| `core/windows_listener.py` 242 行 | 保留为独立诊断或测试材料，不作开发总门禁 | 当前 server 的 `_require_windows_listener_validation` 只检查 SO_EXCLUSIVEADDRUSE 可用，**不是旧版永久封死** |
| Windows tests | 保留业务闭环/系统边界的正反例；退休旧 strict/冻结契约用例，合并重复 Wine wrapper | 不以测试数量当覆盖证明；不删反误杀、反泄露、部分提交与真实 OS 失败测试 |
| 测试隔离设施 | 复用已有能用的部分；Whisky 使用一套临时假项目与受管子进程即可 | 不新造完整隔离框架；旧 Mac PTY runner 不硬搬到 Windows；测试替身标签必须保留 |

### 5. 原版可用行为对照与真正未闭环

| 功能 | 原版 | 当前新代码 | 收口要求 |
|---|---|---|---|
| on /重复启动 | lsof 监听即就绪，nohup 启动 | 两锁、轻记录、目标/连接核验、实际 Popen 与 READY 等待 | 保留新防错，验收重复/并发 on；不能只看某个端口开了 |
| status | PID、启动/运行时间及详细服务状态 | Windows `_request` 解析 JSON 后未返回给公共 format，只输出运行提示；Mac 仍详细展示 | **明确产品缺口**：共用脱敏展示；“未运行”退出码新旧也不同，合同需列明而非默认对等 |
| reload | curl，请求成败解释较弱 | 已走认证后的真实 reload，Windows 检查 ok；公共服务保留加载/清 cooldown 语义 | 共享错误解释；坏 JSON 不换旧配置，失败不能报成功；不把旧风险恢复回来 |
| off/restart | SIGTERM；路径/端口文件名匹配与等待 | Windows 持有验证后的 HANDLE 强停再等锁/端口；已编码 | 已批准，属于首批闭环，不应再以“待批准停止”拖延；明确可能中断流，不称优雅排空 |
| supply/route/strategy/switch | 公共 Python helper 配置写后 reload | Windows snapshot/有限 writer、保存确认和标记已接 | 假配置贯穿“预览→保存→reload”，包括取消/后验失败；不是只补 writer 单测 |
| install | 四客户端、预览备份写入、Mac hook | 四客户端有限 writer已接；Windows `ensure_session_hook:715–718` 明确跳过 | 客户端写入与 bat hook 的存在不等于自动安装已支持；单列阶段范围，不能标全功能对等 |
| logs/stats/help | 混合 Bash/Python | 共同 Python | 使用旧版黄金输出/代表性过滤；现有 help 对拍只验证 help，不代表全部命令对拍 |
| listen | localhost/IPv4/IPv6/指定本地地址等配置含义 | 共同 server仍解析；Windows控制端 `_loopback_endpoint:60–75` 只接受 loopback或通配映射 | 指定 LAN 地址的控制路径尚有范围不一致；不静默改 host，不以未测等于支持 |
| 启动副作用 | 原版锁前已初始化日志；有截断锁风险 | 当前 `_windows_main` 在两把锁前调用 `prepare_windows_log`，可创建空日志 | **顺序缺口**：未必写内容，但不是“竞争失败者零建日志”；应将创建/裁剪/账本副作用置于相应锁后并更新快照 |

另一个易用性收口点：Windows on 将子进程 stdout/stderr 送 DEVNULL，启动失败只报简短错误；原版能提示启动日志。应在本次假环境验收可诊断性，不读取或展示真实秘密，不另建日志平台。

### 6. 两种可落地选择

| 选择 | 怎么做 | 适合与代价 |
|---|---|---|
| ① 当前 B 原样收尾 | 不动架构，补 status/副作用顺序/范围说明，完成业务对拍和原生验收 | 最少改动；但新旧框架与重复测试继续维护，后续仍易重新追逐旧门禁 |
| **② 收敛现有 B（推荐）** | 保留已接线 primitive 和批准保障；补具体功能缺口，复用小型公共策略，删除无消费者的旧证明/事务路径，验收分层 | 清晰降低维护复杂度；需要一次实施授权和跨文件消费者核对，不需要新平台/事务框架 |

不建议第三条“回到旧保障、全部 stdlib 加薄 bat”：Windows chmod 私密和 SIGTERM 语义并不等价；若再用 PID/进程名停止、端口开放即发 token，会撤回已批准保障。这不是本轮必要的简化选项。

**推荐 ②，不需要新的安全让步。** B 的核心方向可保留；需要调整的是实施计划、完成判据和无用架构收口，而不是自动推翻 confirmed 决策。本轮不修改该记录；用户采纳后另行决定确认本 draft 或如何记录后续决策。

### 7. 首批必须完成与可以后置

**五命令首批必需**：

1. 共同业务与 CLI，不让 sh/bat 承载策略。
2. 两把有明确用途的 OS 实例锁，加短控制锁；一份轻定位记录，记录不是授权证明。
3. token 发送前核验实际同 socket 的目标；停止在已验证的同 HANDLE 上执行并等待退出/锁端口释放。
4. 敏感源文件读取与新建的真实私有保护；无能力则该操作非零，不跨目标泄露。
5. 正确的 status/reload 展示、启动竞争副作用顺序和失败诊断；明确所支持的监听范围。
6. worktree 内假数据回归与独立实现复核；部署重启仍由用户决定。

**可分批后置，不挡其它开发**：

- install 和配置编辑的完整四客户端端到端验收可以独立一批；源码已接线，不应再泛称未实现。
- Windows自动 hook、完整 IPv6/wildcard/指定 LAN 范围的发布验证可分批，但必须披露阶段缺口；若要实质缩减此前承诺范围，再请用户裁决。
- 防安装复制、三记录事务恢复、泛化别名冻结、全环境远程身份证明、抗同用户恶意修改，不进入普通工具首次交付。
- hosted CI/代码上传/额度、真实 Windows验证属于后续发布通道，缺它不阻塞完成代码和本地回归。
- 不新建 graceful-drain API；已经批准的强停继续执行原定语义。

### 8. 可并行实施与用户需要做什么

2026-11-16 用户同意按本顺序在原 worktree 实施；部署/合并/线上重启仍需另行确认。

| 步骤/负责人 | 明确范围与产出 | 依赖及并行边界 |
|---|---|---|
| P0 主会话＋执行者 | 固定当前含 untracked 的源码快照、正常/失败/输出/退出码合同；列“编码、mock、Wine、native”四栏 | 不用旧 HEAD 构建验收；用户批准收敛实施即可，不重新问已批准保障 |
| P1a implementer-high：控制/入口 | `windows_control.py/windows_command_line.py`；共同控制展示/小段编排；`server.py` 锁后副作用；轻记录接线与旧 strict 消费者收口 | 单一负责人负责共享 `server.py/discovery.py/dispatch.py`；不要多人并发改这些文件 |
| P1b implementer-high：文件写入 | `windows_secure_files/native/write/model` 内有限流程收口、结果类型迁移、旧 coordinator 清理及对应测试 | 与 P1a 并行，但先固定已有 `capture_target/write_target` 外部接口；涉及 discovery 变更交 P1a 整合 |
| P1c runner：只读回归证据 | 盘点/运行已审查的 Mac、协议与CLI代表性假数据用例，输出失败归因和覆盖边界 | 可并行；不改 Mac 行为，不重造隔离。测试/代码修复回到负责 implementer |
| P2 各实施包＋reviewer | 删除取消契约及无消费者代码/测试；查 references、imports、hook、CLI；独立复核耦合改动 | 删除晚于替代与类型迁移；主会话只轻量抽查，不全量重做 reviewer |
| P3 runner：Whisky | 使用现有 bottle/Python、一套新临时假项目、假 HOME/配置/凭据、临时测试端口与仅本轮子进程 | 与纯代码开发分层；真实 Wine 成功/失败和注入桥接分别报告，不修 bottle 祖先 ACL 来“造通过” |
| P4 另获授权后 | 借用真实 Windows 或 hosted runner，验证发布范围内的实际锁/ACL/连接归属/停止及五命令 | 不等 P4 才允许 P1/P2 写代码；Windows发布声明等待 P4，Mac部署重启仍单独授权 |

无需用户逐步提供 Mac 系统资料、真实 token、业务配置、端口或日志，也无需现在安装 VM。**现在用户只需决定是否授权“按推荐②在原 worktree 收敛实施”。** 这不是默认改旧 confirmed 文档的授权。

只有真正出现新的安全策略变更，才给出具体失败场景和一条简短裁决，不把“再简化一点”当 blanket waiver。采用 hosted CI 时另请授权代码上传、目标仓库与预算/额度；只上传显式审查过的源码与假测试材料，不上传真实 config、日志、凭据、账本、bottle 或整个 vault，也不能 `git add -A` 把已有全部未跟踪文件盲目打包。

### 9. 方案推演与反例

命中论证链、业务状态和跨步骤衔接，做纸面走查，不作为实测：

1. **反例推翻“Python 相同就只改 bat”**：相同版本调用旧 server 的 fcntl，Windows 没有该 Unix 接口；私有文件 chmod 也不能推出相同保护。结论是要有限 OS adapter，不是要完整 authority 框架。
2. **反例推翻“有特殊拒绝入口所以 Windows 全部未实现”**：strict `windows_entry` 拒绝，但当前 on 启动共同 `model_proxy.py` 并进入 `_windows_main`。必须沿实际 dispatch 调用链判断。
3. **陌生进程占测试端口**：on/status 应报告 busy/unknown，零 token、零误杀；删 strict entry不能删 actual peer 核验。
4. **旧 PID 已复用**：轻记录只是提示；核对创建时间/映像/入口，不能因为 JSON 里 PID 相同就停止；终止仍落在同一验证 HANDLE。
5. **日志缺失、两个启动竞争**：当前锁前创建空日志会产生可见文件副作用；“没写内容”不能证明零副作用。改成持锁后创建时要同步更新快照，不让旧 absent 证据误判后续文件冲突。
6. **替换调用后失败**：target 可能已变；保留 UNKNOWN/POSTCHECK_FAILED，不报未写，不自动重试/回滚。删除冻结模型不等于删准确结果。
7. **Wine 新文件丢 protected 标志**：敏感写入这一项保留失败/不可验证；其它业务逻辑可由明确替身独立开发，不能据替身通过宣称真实 ACL 或 Windows 通过。
8. **指定 LAN监听配置**：server 允许不代表控制端 loopback-only 能连；要补能力或明确不发布该组合，不能静默改用户 host。

## 风险与权衡

- 最大风险不是代码删少了，而是删掉仍被产品消费的类型、native调用或 Mac 三记录分支。精简采用消费者清单和确定回归，不以行数目标验收。
- 未授权收敛前不动产品；在原 worktree 实施也不意味着允许合并到主区、部署、重启或停止既有进程。
- 本建议不新增安全让步，也不宣称既有 ACL 策略在所有 Windows默认目录下都可用。祖先白名单/拒绝未知 ACE 是产品策略，不是 Win32必然要求；遇原生失败先归因到具体策略/API，另行裁决。
- 小型共享展示/编排可复用，但不要强迫 Windows采用 Mac 完整 authority；这会再次放大任务。Mac 现有契约和符号链接兼容保持。
- 本轮没有重跑任何测试，也没有验证既有临时回执是否存在。文件名为 native、测试注释称已通过或 fixture成功均不是本轮实测。
- 不编造工期。当前有已接线基础和可清理旧路径，足以判断可按上述两实施包推进，不能据行数估算完成时间。

## 验证方式

### 代码与业务验收

1. 含全部拟发布 untracked 源码的明确快照；语法/导入检查、消费者核对，不能只测试旧 HEAD。两壳保持定位、选解释器、转参、退出码职责。
2. 从中文/空格路径、任意 cwd、hook非交互入口启动；对照旧版 help/logs/stats及五命令正常/失败/输出/退出码合同。
3. 两 CLI同时 on、重复 on、直接入口竞争：一个服务持锁；竞争者不截断/删锁，不写日志内容/裁剪/账本，不在锁前创建普通日志；陌生监听者零 token、零停止。
4. status详细展示与 Mac复用、脱敏；未启动、陈旧记录、PID复用、目标不符、ACL/查询失败可区分。端口配置变化不自动接管未知实例。
5. reload有效配置成功、坏 JSON保留旧配置、认证失败、加载/清 cooldown结果如实；配置保存确认后才 reload，取消/提交未知不谎报完整成功。
6. off/restart只停止本轮被核验目标；同 HANDLE终止与等待；旧锁/端口未释放不启动第二实例；在途流中断如实说明。
7. 敏感写的 new/existing file、备份创建即私有、替换后内容/保护核验、预览后外部改写、部分提交、关闭失败、后验失败；不声称 CAS或永久冻结第三方。
8. 四客户端假 HOME下的写入与部分完成报告；Windows自动 hook另列范围。现有JSON/SSE/跨协议回归使用实际共享内核，不能由简化 fixture转换替身代替。

### 四层证据，不互相冒充

| 层 | 能证明什么 | 不能证明什么 |
|---|---|---|
| 静态/单元替身 | 源码接线、算法分支、失败结果、公共行为合同 | 实际锁、DACL、进程与 socket归属 |
| Whisky无产品替身 | 同一 Windows Python代码在真实 Wine API上的实际观察 | 原生 Windows或全部文件系统/权限场景 |
| Whisky注入桥接 | 明确被替代 OS边界后的共享业务运行 | 被替代的ACL、启动参数/身份、记录与锁保护 |
| 真实 Windows | 选定 OS/Python/文件系统/监听范围下的实际产品验收 | 未测版本、监听组合或所有部署；也不等于已获部署授权 |

`tests/test_windows_service_startup_a_wine_main.py:24–52` 明确 patch记录、目录/日志准备和启动核验；该入口运行成功也只能按“桥接业务”登记。`tests/test_windows_secure_wine_product.py`与`test_windows_config_wine_product.py`自称 Wine诊断，同样不能改标签成原生结果。

Wine ACL不满足时，不无限扩充 fake OS：登记具体失败，保持敏感操作不放行；完成静态/单元、其它可真实验证部分及明确桥接的业务验证，再等待发布通道。不得加入产品 unsafe开关或把 skip归为通过。

### 交付物规范

本轮唯一新增产物为 **W/docs/designs/2026-11-15-model-proxy-Windows适配复杂度独立复核.md**，供用户裁决与后续派单；沿用 architect设计记录目录与命名规范，`status: draft`，不改 README索引、不改既有记录或状态。后续实施优先改既有代码/测试，不新增安装证明、冻结资源、控制协议、用户配置字段或完整隔离架构；CI基础设施须另授权。

## 关联与核实来源

- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy共享Python与Windows最小适配再审视]]：已确认 B，仅只读参照；本记录未替代它。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-13-modelproxy-Mac符号链接配置兼容方案]]：Mac已有兼容边界，本建议不撤回。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/windows_control.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/server.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_secure_files.py]]：当前实际生产路径。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/tests/README-isolation]]：既有测试设施的证据边界。
- 官方文档核实遵守 websearch-router：英文检索选 Friday google-search、bing fallback，随后只取官方正文。查询中无本地业务配置、凭据或私有源码。
- Python官方：`https://docs.python.org/3/library/fcntl.html`、`https://docs.python.org/3/library/os.html#os.chmod`、`https://docs.python.org/3/library/subprocess.html#subprocess.Popen.terminate`。
- Microsoft官方：`https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess`。
