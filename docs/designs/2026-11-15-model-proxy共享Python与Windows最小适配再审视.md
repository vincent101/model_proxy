---
type: design-decision
status: confirmed
target: "tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli"
tags:
  - architect
  - model-proxy
  - windows
created: 2026-11-15
---

# 共享 Python 与 Windows 最小适配：务实再审视

## 背景与问题

用户本轮要求重新判断“同一份 Python 业务＋公共 CLI＋薄 sh/bat”是否被额外安全目标淹没，而不是替既有方案解释为何还不能启用 Windows。

**结论：方向正确，当前实现部分做到；Windows 移植被加固工程明显放大，建议修改实施方案，先恢复对等的日常功能。** Python 版本相同不等于其底下的 OS API 相同，但这些差异只要求少量适配，不推出“必须采用现有身份、ACL、安装发现及事务模型架构”。不以原设计、现有拒绝门禁或测试数量作为其必要性的证明。

本记录是新的 **draft**，不修改、不覆盖、不 supersede 两份旧设计；新安全取舍尚未批准。仅完成定位、设计和既有临时测试，不实施代码，不提交、不上传、不部署、不重启。

### 证据边界

- 工作树绝对根：`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli`。下称 **W**；文件操作仍使用 vault 相对路径。
- HEAD：`7547517`，但实际源码包含大量未提交、未跟踪新增模块，不能将 HEAD 或 tracked diff 当作本次实现。旧版行为引用 `git show HEAD:<文件>`；新行为引用实际文件。
- 用户明确的历史：生产在 Mac；共享 Python、务实分阶段、权限 A、Whisky 测试无需复杂隔离；未批准 Windows 强制停止，未授权上传代码或生产操作。未读取生产配置、凭据、日志，未观察生产进程或端口；不臆测真实配置及其使用频率。
- 上一段调查在本次用户重新裁定之前运行的测试，只使用 `py-test` bottle、新假项目、假凭据和 OS 分配的本地测试端口。结果仍是证据，不能替代新方案验收。
- 官方 Python 文档的 `fcntl — The fcntl and ioctl system calls` 标注 Unix-only，此处采用主会话提供的官方核实结果；不将“有 OS 差异”扩大成对既有加固架构的背书。

## 方案设计

### 1. 用户提出的结构原则：哪些已经成立

| 层 | 当前事实 | 判断 |
|---|---|---|
| 模型业务 | 同一份 `server.py` 和业务模块负责路由、转换、JSON/SSE、认证和 reload | **共享成立**，无需 Windows 业务副本 |
| CLI 业务 | 菜单、分发、格式、统计、安装大部分已迁入 `core/cli`，两壳调用同一个 `main.py` | **公共入口成立**；不应再向 bat 放菜单、配置或生命周期逻辑 |
| 生命周期 | Mac 使用 `MacControl`，Windows 使用 `WindowsControl`；后者 on/reload/off/restart 拒绝，status 始终 UNKNOWN | **日常功能未成立**；有公共 dispatch 不等于五个命令已可用 |
| 服务入口 | 普通共同入口可调用 `core.server.main`，另有 Windows 专用严格入口，当前仍直接拒绝启动 | Windows 又叠了一层“启动证明”，不是薄 bat 自然要求 |
| OS 边界 | 锁、文件保护、进程身份/停止、路径、socket 查询分别进入平台实现 | 分支位置合理；其中许多契约比旧版日常行为更强，必须另论必要性 |

源码位置：

- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/model_proxy_cli.sh]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/model_proxy_cli.bat]]：本次读取分别 11、20 行，只选解释器、定位入口、转参和返回退出码。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/dispatch.py]]:677–701：共同命令分发，平台控制器不同。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/model_proxy.py]]:174–190：共同服务入口；Mac 另有启动归一化，非 Mac 可调用共同 main。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/windows_entry.py]]:15–46：Windows 严格参数验证后仍返回不可用，没有导入 server 启动。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/windows_control.py]]:233–305：五个日常命令的真实交付状态。

**直接回答用户：是这个业务架构方向，但不是“只改 bat 就完”。不可移植的调用应藏在小的 Python OS 适配里；不应因此再造 Windows 业务逻辑，也不应顺带要求完成一个更高等级的本机安全控制系统。**

### 2. 从原版对等行为出发，不从新门禁出发

旧版 `7547517` 的 `model_proxy_cli.sh`：

| 日常功能 | 原版实际做法 | 移植需要什么；不天然需要什么 |
|---|---|---|
| on | `nohup python3`，端口出现监听即认为就绪（373 起） | Python `subprocess`＋就绪判断；不天然需要安装来源证明、三份发现记录 |
| status/reload | `lsof` 查监听；读取 admin token 后用 curl 请求（130–151、173–223） | 共同 Python HTTP 客户端；原版没有已连接对端身份认证 |
| off | `pgrep` 找绝对入口，另用端口＋文件名兜底，默认 `kill` 即 SIGTERM（397 起） | Windows 的目标选择和停止 API；不能用“都叫 kill”假装 Windows 与 SIGTERM 收尾等价 |
| restart | off 后等旧进程退出且端口释放，再 on；超时不启动（429 起） | 公共编排＋平台退出等待；无需优雅排空框架 |
| 服务互斥 | server 使用 `fcntl.flock`、固定 `/tmp` 路径；锁前初始化日志（旧 server:3746–3772） | 换 Windows 文件锁及路径；应修复竞争者先截断锁/碰日志，不能原样复制此弱点 |

原版是“可信本机用户的工具”，**不是**跨用户抗攻击、抗安装复制、通用文件系统证明、跨客户端写入事务平台。旧版也有真实风险：端口就绪误认、PID/命令行匹配不充分、token 可发给错误监听者、reload 回报不准确。不能把这些风险美化为安全基线，但改善它们和采用现有全部架构不是同一件事。

### 3. 不可避免的 Python OS 差异：保留小适配

| 差异 | 源码证据 | 最小需求 |
|---|---|---|
| 锁 | [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/platform_lock.py]]:294–318；[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/platform_windows.py]] 的文件 HANDLE/锁实现 | Unix flock、Windows 文件锁；进程退出释放；不截断、不删除锁 |
| 路径 | [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/runtime_paths.py]]:18–34、95–122 | 四个精确旧 `/tmp` 默认值映射到 LOCALAPPDATA；中文、空格、盘符不等于 POSIX 路径；保留共同解析器 |
| 启动/停止 | [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/control.py]]:530、611；[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/platform_process.py]]:315–326 | 子进程启动方式、身份和退出等待；Mac SIGTERM 不可直接当作 Windows graceful stop |
| 权限/替换 | [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_acl.py]]；[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_secure_files.py]]:268–315 | chmod 不代表 Windows 私密；敏感临时文件创建时即私有；替换前关闭冲突句柄，替换后检查 |
| socket | [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/server.py]]:4178–4194；[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_process.py]]:214–289、509 起 | Windows 独占绑定设置、TCP owner-PID 和进程创建身份查询；不要求重写 HTTP、路由、协议 |

这些适配是有限问题。Windows process 模块已有 `GetExtendedTcpTable/OpenProcess/GetProcessTimes/QueryFullProcessImageNameW`；锁与固定记录的 native 写入也已接线，**不能继续称其全部未编码**。它们还未原生验收，不等于必须重新造一遍。

### 4. 现有复杂度到底承载什么

主会话给出的量级，本轮 source 分组核对如下（含注释；不是删行 KPI）：

| 分组 | 行数 | 承载内容；处理判断 |
|---|---:|---|
| paths＋ACL＋process＋platform_windows | 2,933 | 包含必要 Win32 适配，也包含多次父链复查、证据/句柄租约、严格权限策略；不能整包删，需把 OS primitive 与更强证明目标分开 |
| windows_secure_files/native/write/model | 1,694 | 快照、候选文件、事务状态机、进程内冻结、别名 key、提交及清理状态；**超过恢复五个日常命令的需要**，配置安装写入可改为有限逐文件流程 |
| windows_listener | 242 | 独立 overlap 诊断，不是业务依赖；保留工具但取消“完整矩阵先全部通过才能开发普通启动”的绝对门禁 |
| Windows CLI | 1,081 | control 351 行；strict entry/launch/authority/CIM 共 730 行。很多代码尚不产生可用控制，服务入口证明框架应缩减 |
| Windows tests | 主会话统计约 12,116 | 大量针对保护模型、异常和门禁的 fake/mock；不能以覆盖这些模型来代替五命令真运行。不要继续扩充同类 fake 体系 |
| 两壳 | 31 | 已经很薄，**卡点不在壳** |

**过度设计的判据是目标错配，不是行数多：**

1. on 被“严格入口＋完整 listener 矩阵”永久封闭；reload 被“启动/有效配置 authority”永久封闭。接口存在但未形成日常闭环。
2. 固定状态三记录、安装 ID、来源确认、STARTING/READY 提交恢复、旧端口迁移及安装复制防护，被捆成首次 Windows 运行的前置；这些不是同 Python 业务所需。
3. ACL 要求 SYSTEM、Administrators、有效用户分别拥有当前操作所需权利。**缺 Administrators grant 不推出其他人能读秘密**；这是额外可用性策略，不是保密性的必要条件。
4. generic writer 先要求“跨别名且替换稳定的全进程 key”，再讨论写一个客户端文件；由进程内冻结契约引出的难题成了普通写入的前置。
5. 原生能力待测是事实；把“所有平台/地址族/不常用边界未全测”做成全平台功能永久关闭，是项目选择，不是 Python 或 Windows 的硬限制。

### 5. 三个选项

| 选项 | 内容 | 成本/风险/结果 |
|---|---|---|
| **A：严格恢复旧版对等功能** | 小 OS 适配，普通本地 HTTP 控制；不增加实际连接身份门槛，普通文件写入 | 最少改造；但 token 误发、PID 误认和 Windows ACL 风险不能忽略。会撤回已批准的更强控制边界，**不推荐** |
| **B：共享业务＋最小防错保障（推荐）** | 五命令闭环；保留两把互斥锁、精确目标、token 前同 socket 对端核验、敏感写保护；删除 Windows 安装证明及泛化事务前置 | 中等：启动/控制、权限/写入两个实施包＋真实验收。安全取舍可解释，现有适配可复用；需要用户批准减法及 Windows stop |
| **C：继续完整加固体系** | 保留全部发现、authority、alias freeze、恢复模型与平台矩阵前置 | 改动/验收最高。能力有独立价值，但仍不是只修 bat；没有明确的对抗威胁需求，不宜继续默认采用 |

选 B 不是宣布 Windows 已安全或已可用，也不只是将旧实现恢复后换个名字。**开发验收可分层；发布声明只能覆盖实际通过的 OS、解释器和功能。**

### 6. 推荐 B：具体保留、删减、重构

#### 6.1 共同业务和 Mac：不陪 Windows 重构

- 保留共同 `server.py` 的路由、协议转换、JSON/SSE、认证和配置含义；保留共同 CLI 的菜单、帮助、输出、统计和安装输出脱敏。
- 保留已批准的 reload 真实结果语义：失败不谎报成功；进入认证 handler 的手动失败仍尝试清 cooldown。这是日常正确性，不是额外安全形式化。
- 不改已确认 Mac 链接/`symlink/..` 兼容目标，不以 Windows 简化删除 Mac 的实际目标写入处理。
- Mac 的现有控制器及三记录契约暂保持；**不要先重写整个 Mac authority 来泛化 Windows**。Windows 用平台适配接入共同 CLI/服务；可在既有 `control.py` 提取少量公共 on→等待、off→等待、restart 编排，不复制模型业务或另造 Windows 菜单。
- 相同业务代码允许不同 OS primitive，不要求每个平台经过完全相同的安全证明。测试合同保证日常语义一致；Mac 接入新的公共编排可另批进行，不阻断 Windows 首次闭环。

#### 6.2 Windows 启动/发现：从“安装证明”退回“互斥＋当前目标”

- **保留**固定 `.model_proxy_runtime/anchor.lock` 和 legacy/runtime lock：防同安装改变端口/路径后重复启动，以及共用运行文件的竞争；不用 PID 文件代替 OS 锁。
- **保留**短控制锁，串行化 on/off/restart；不再要求 PID 被证明同时拥有多把锁。
- **简化** Windows 到一份轻量 `active.json`：PID、创建时间、映像、入口、显式配置定位及当前监听端点。记录是定位提示，不是身份认证；发 token/停止前仍看 OS 证据。
- **取消 Windows 首版必需的** `install.json`、`last_good_paths.json`、安装 ID/来源誓言、多记录 READY 事务、旧端口迁移/复制来源证明。已有 Mac 分支不改；Windows reader/writer 不再强求 Mac 的三记录齐全。
- 轻记录的首次写入可用 native 私有新文件/替换 primitive；不依赖 generic 客户端 writer 的 alias freeze key。启动失败不开始裁剪真实日志/写账本；有原生提交副作用时准确报 UNKNOWN。
- 状态目录与**默认 Windows runtime 父目录**仅在创建新目录时建立私有 DACL并读回；既有不安全目录不自动修 ACL，自定义不存在的父路径不偷偷修复。
- **取消 Windows 专用严格服务入口作为产品前置**。共同 `core/cli/model_proxy.py` 负责 bootstrap；Windows 在该入口处理必要的内部显式配置/端口参数，在导入 server 前归一化。参数由共同 CLI 构造，不新增用户子命令、不传 token。Mac 启动归一化不改。
- `windows_entry.py/windows_launch.py/windows_authority.py` 不再作为运行必需；确认新入口和引用闭合后才删除无消费者代码，不先物理删除造成断链。
- **保留独立的实际进程入口核验能力**；`windows_command_line.py` 可复用 CIM 读取能力，但去掉与完整 raw command line 逐字符同等、每个环境变量都必须成为远程身份证明的目标。解析后比对真实入口/显式启动参数；不宣称能证明进程内存配置。

影响：不再承诺防安装复制、所有旧端口/路径迁移来源可追溯、严格启动字符串等价。**仍不根据陈旧记录、进程名或“python.exe”终止进程，也不凭记录发秘密。** 路径变化遇到未知实例报冲突/未知，不自动接管。

#### 6.3 控制与 listener：保留有用防错，去掉全局封死

- `WindowsControl.on/status/reload` 的无条件拒绝改为真实操作；具体能力失败才 UNKNOWN/非零。不能“删 return 1”后直接发 token。
- 保留实际已连接 socket 的 server-side owner PID、创建时间/映像及入口核验；认证请求只在该 socket 发出，禁止重定向、代理和自动重连泄露 token。复用现有 process primitive，不再新建一层证据模型。
- on 就绪不能只看“某进程占端口”。检查是本轮启动或可核实的现有目标；foreign listener 报 busy，不发 token、不停止它。
- 保留 Windows bind 前 `SO_EXCLUSIVEADDRUSE` 和所选地址族设置。取消 `server._require_windows_listener_validation()` 的**永久无条件拒绝**，用发布清单和有限真实测试约束支持范围。
- 首阶段先验收用户选定的普通本地监听组合。未测 IPv6/wildcard/LAN 不宣称已支持，也不能为了避测静默更改 listen 语义；若首版暂不发布这些组合，需要用户认可范围。
- 没有记录：检查锁/监听及可观察目标后区分未启动、端口被别的程序占用、未知；不能“记录不存在＝没服务”，也不能永远 UNKNOWN。

这里保留的同 socket 核验比原版更强，但已有底层实现，且已获用户认可。若实际 native 观察依然无法闭合，应把**具体失败 API/用例**报告给用户裁决；不要因此自动转向私有控制协议、更多 fake 或全局安装证明。

#### 6.4 权限/写入：不再让泛化事务冻结挡住日常功能

- 已批准 Everyone A 不改：符合条件的祖先/创建父目录只读遍历上限 `0x1200a9`，不放宽私有文件。
- **提议修改而非宣称已批准**：撤掉“所有祖先、私有对象均须 SYSTEM/Administrators分别具备本次 requested access”的附加条件。保留有效用户所需 AccessCheck；私有目标不得把秘密读写权限授予不受信任主体。SYSTEM/Admin 可作为可信主体，但“可信”不等于必须出现且拥有本次权利。
- 父链的类型、明显重解析/别名、非可信写入危险仍检查；不再以“持有/多次重走所有祖先可证明路径永不替换”为承诺。默认可信本机用户协作，承认最终检查后的 TOCTOU，不对同用户恶意进程提供防护承诺。
- DACL 保护/继承、内容和 owner 的真实读取及替换后保护不忽略。撤掉 Admin 强制 grant **不能**把 Wine 丢失保护标志直接改判为权限保留成功。
- 配置/安装写采用有限逐文件流程：读快照→脱敏预览/确认→备份原字节→创建即私有的同卷暂存→写完关闭→最后复查→native replace/原子不覆盖新建→内容与保护后验。用户暂停外部编辑，不声称 CAS。
- 修改 `windows_secure_files.write_target` 的生产路径，复用候选文件和底层 Win32 操作；**替代** `windows_secure_write_model`＋泛化 coordinator/alias freeze 前置。`canonical_target_key_unverified` 不再是普通写文件的入场券。
- 保留有限结果：未调用提交、已提交待验、已验证、提交/保护未知；native 失败可能有副作用，不能统称未写。后验失败即中止整个本次安装/编辑批次、不自动重试、不静默回滚；下一独立调用/第三方不被承诺永久冻结。
- 先打通五命令，再做客户端写入；供给/路由编辑和 install 的真正缺口不能藏在“仅未测”。当前 `_config_ops.py:106/132` 直接拒绝 Windows，native writer 的 key/capability/replace/create 路径均仍拒绝，需要实际改实现，不是补一份验收表。

影响：撤回泛化 alias freeze 和更强路径抗干扰的保证，保留秘密保护和准确报告。实际 Windows ACL、分享标志、ReplaceFile/move 新建失败仍需 native 案例，不因为流程变短就省掉。

#### 6.5 off/restart：不要为一个未决产品选择造排空框架

- Mac 保持现有 SIGTERM、用户手动重启边界，不添加自动强杀、每次 TTY 确认或 graceful-drain 改造。
- **Windows 推荐待批准**：核验目标 PID/创建时间/映像/入口，使用该目标的同一 HANDLE 执行强制终止，等待退出和锁/端口释放；restart 才进入 on。拒绝 PID-only、按进程名、杀树和未知旧进程。
- 这比新控制 API＋在途排空成本小；但它是强制停止，可能中断流、未完成账本/清理，**不是 Windows 等价 SIGTERM 或已排空**。原版 Mac 日常停止也不承诺排空，不能据此替用户批准 Windows 强停。
- 用户不批准强停时：off/restart 保持清晰不可用，允许先交付 on/status/reload；这是明确的阶段缺口，不可称五命令全部完成。用户也可选择另批 graceful stop，必须接受额外 server 行为和验证成本。

### 7. 当前 Whisky 证据：说明“在哪里卡”，不替新方案背书

本轮既有测试回执在 `/private/tmp/mp-win-architect-3Bdnm9/`，只含假数据和脱敏诊断；临时回执不保证长期保留。

| 证据 | 已证实 | 不可推出 |
|---|---|---|
| `acl-diagnostic.json` | Wine 临时祖先 Everyone 是 `0x1200a9`，当前失败为 `principal_access_missing`；显式创建 user/SYSTEM/Admin＋protected DACL，读回缺 Admin，protected=false | 不是 Everyone A 未实现；不是原生 Windows 也缺 Admin；不能把保护读回差异略去 |
| `native-startup.json` | Windows Python 3.14.10，未替换安全 guards；root ancestor 缺 grant；preflight 不可用；实际 main 报 `config_path_unknown`，退出 1 | 单独诊断 listener gate 返回拒绝，不表示 main 已走到 bind；不是只有 bat 或缺解释器 |
| `injected-startup.json` | 同解释器；测试进程替换路径/ACL、记录/锁及 listener guards 后，真实 `server.main→HTTP server→401/200认证→本地mock→fake-reply` 成功，退出 0 | 不证明 Windows CLI on/status/reload/off 可用，也不证明 native 安全 |

两个 startup 回执都报告 owned child exited/reaped、自有 mock 停止；源码 before/after 同为 `a263cf72a67f05dd4d98481ffaa5a58efcbac398c0401993a9adcc05e9e64268`（现有测试的 59 文件口径）。

测试源码：[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/tests/test_windows_acl_wine_diagnostic.py]]:93–106、168–205；[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/tests/test_wine_service_startup.py]]；[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/tests/wine_service_fixture.py]]:140–161。原生注入模式是测试替身，不是产品运行开关。**不再为让 Wine 模拟完整 Windows 安全模型而扩建 fake 体系。**

### 8. 实施顺序、并行范围及用户要做什么

下列均为确认后实施；当前只有分析/新建设计记录授权。

| 步骤 | 交付及具体范围 | 依赖/可并行 |
|---|---|---|
| P0：用户拍板减法 | 选 B；确认 Windows 精简记录/入口和权限策略变化、首版支持范围；Windows 强停单独批准或保持阶段缺口 | 无代码先行；不把沉默当同意 |
| P1：停止加码，固定对等合同 | 用已有测试梳理五命令正常/异常口径；将现有“必须拒绝”用例改为能力失败用例；已有 Mac 案例保留 | 不增泛化模型；读数须注明当前源码指纹 |
| P2a：启动/控制闭环 | 既有 `windows_control.py/windows_command_line.py/windows_process.py`、公共 dispatch/control、共同 bootstrap；开发轻记录，移除 Windows blanket gates | 与 P2b 的 ACL primitive 部分可并行；不能双方同时改 server/discovery |
| P2b：平台最小保护 | `windows_acl.py/windows_paths.py/platform_windows.py`：新私有目录创建/读回、有效访问、锁/轻记录 primitive；撤附加 grant 条件 | API 先约定；Windows record/server 接线由 P2a 单一负责人合并 |
| P2c：Mac 和协议回归 | 既有 Mac/链接、CLI 与 JSON/SSE 测试；只改测试适配，不改 Mac 行为 | 可与 P2a/b 并行；失败阻断共享部分发布，不由 Windows 全套矩阵阻断 Mac |
| P3：实际短验收 | Whisky 假项目直接运行；借用 native Windows 或获批 hosted CI 跑实际五命令/ACL/锁/对端案例，不用 mock 代替 native | 本地测试无需等待 CI；Windows 发布等待实际 OS 案例 |
| P4：配置/安装写入 | `_config_ops.py` Windows 接线、`windows_secure_files/native` 简化提交；四客户端假 HOME；原子新建/部分提交结果 | 可在五命令合同固定后并行开发，不是 on/status/reload 前置 |
| P5：删死代码与独立复核 | 新路径不再引用严格入口/authority/模型后，核对 imports/测试/hook；实现交 reviewer；不误删 Mac 使用的保护模块 | 删除晚于替代；不能以减行数提前删除 |

**助手可以自行完成（在用户确认相应实施后）**：代码改造、已有测试精简、同一 worktree 的 Whisky 临时假数据测试、结果对拍、独立复核交接。无需用户装完整 VM，不要求用户手工改 bottle 祖先 DACL，不需要真实 token/config；测试只停止本轮创建的子进程/线程，不停止既有进程。

**必须用户配合**：

1. 安全/范围取舍与 Windows stop 的批准。不能由助手用“更简单”替代授权。
2. native 验收途径：如有可借 Windows，只需一次授权测试窗口与 Python 环境；不要求在 Mac 装完整虚拟机。没有机器时可选 hosted Windows runner。
3. **CI 若采用**：origin 主机为 github.com，gh 已安装，当前无 `.github/workflows`，但 gh 登录、Actions 权限、私有仓库额度均未知。必须另批代码上传/仓库权限/预算；只上传审查过的代码与假测试材料，排除真实 `config/`、凭据、日志、账本、bottle、整个 vault 与 `.git`。大量 dirty/untracked 源码须先形成经审查的显式快照，不能 CI 只测旧 HEAD，也不能 `git add -A` 全上传。
4. Mac 上线仍由用户单独选择时间、手动部署和重启；Windows/Whisky 通过不产生自动合并、部署或重启授权。

成本口径：B 是两组中等耦合实施＋有限真实验证，不是重写业务；可本地推进，不需要购买虚拟机。CI 增加上传审查、权限/额度核查和运行成本，**不能给免费/即时可用承诺**。C 的成本主要是证明契约和异常模型，不是 bat。精确工期须根据 P2 的 native API 结果再估，不捏造一两小时全交付。

GitHub 候选依据：用户提供的官方 `Choosing the runner for a job` 摘要说明 Windows hosted runner 可用 `windows-latest` 等标签、每 job 新环境；`Billing and usage` 摘要说明私有仓库计划额度及超额计费。不是本轮已验证仓库可用。Hosted 真 Windows OS 可验证 OS API，并不要求物理机器；测试要记录实际 OS/build/架构/Python，不能从 hosted Server 系统直接宣称所有 Windows 10/11 已验收。

### 9. 方案推演：减法后是否自洽

**论证链〔纯示意，不是实测〕**：

- 同 Python 业务成立＋旧版有 Unix API → 需要替换有限 OS primitive；**不能**推出必须引入安装证明/泛化事务。
- 去掉 ancestor 的 Admin 强制 grant＋仍拒非可信主体秘密访问＋有效用户访问检查 → 可消除无关拒绝，同时维持所声明私密性。反例：若将“删 grant 检查”误做成“忽略 Everyone 的私有文件读权限”，保密性立即破坏，必须拒绝。
- 删除三记录安装前置＋固定 OS 锁覆盖所有服务入口 → 同安装改变端口时仍不能双启动。反例：CLI 加锁、直接 server.main 不加锁，则此结论失效；验收要覆盖两个入口。
- 轻记录定位＋真实已连接 peer/进程身份 → 可以向已核实目标请求。反例：只验证 LISTEN 表后重新 connect，抢占者可接 token，故不能删除同 socket 条件。
- 强停后等待退出/锁释放才 on → restart 不与旧目标并行。反例：仅端口消失就启动，旧进程仍持锁；必须同时检查退出和锁，不允许超时强行 on。

**结构检查**：

- 业务正交性：路由/协议不读取平台控制状态；成立目标，P2/P4 不修改这些业务函数。
- 入口覆盖：bat/sh→同 CLI；直接服务入口→同锁/配置解析；Windows 不另造第二套模型主程序。必须实测直接入口，不以入口 wrapper 存在作证。
- 无环：共同 CLI 调生命周期/OS primitive，server 不反向导入 CLI 菜单。bootstrap 只在 import server 前设置非秘密启动输入，不形成 server↔dispatcher 循环。
- Mac 兼容：Windows 轻记录不改 Mac reader/schema/链接契约；若为了共用 reader 顺手改 Mac schema，则违反本次隔离目标，应拆批。

## 风险与权衡

1. **真正需改的不是 bat**：壳已薄，Windows 主功能尚封闭，配置写入实质缺失；仅删除拒绝行不构成实现。
2. **B 明确削弱了哪些承诺**：安装复制/来源、三记录恢复、全祖先强证明、泛化 alias freeze；不是削弱真实线上隔离或默许秘密泄露。已批准 A 和实际对端核验不自行撤回。
3. **Wine 局限与产品策略分开**：缺 Admin grant 是当前策略的拒绝；protected 读回丢失是实测差异。改前者无需谎称后者通过，更无需修 bottle 系统权限。
4. **既有 Mac 不牺牲**：原版行为对照、已确认链接能力、非 TTY、手动 off/restart 兼容保留；共享 server 的 bootstrap/reload 改动仍有回归风险，要独立复核。
5. **停止仍是产品选择**：用户不批强停，就不能宣称 Windows 五命令交付；优雅排空另批，不因为未决持续设计更多控制层。
6. **代码量不是成果**：保留有消费者的测试，移除取消契约的测试，合并重复 fake；以实际日常闭环为验收，而不是扩大测试模型或预定删多少行。

## 验证方式

### 最小验收合同

1. **壳与路径**：从任意 cwd、中文/空格安装路径调用同 CLI；传参/退出码正确；Windows 四个旧默认路径映射；坏 runtime JSON 在业务写入前失败。不得访问生产默认路径验证。
2. **on/互斥**：首次启动、重复 on、两个 CLI 并发、直接服务入口并发；只有一个持锁并写测试账本，不截断对方锁。陌生程序占测试端口时 busy，零 token、零误杀。
3. **status/控制身份**：自己的目标有意义地报告就绪；不存在/陈旧 PID/创建时间不符/foreign listener/对端查询失败分别处理；认证前同 socket 核验，不自动重连。不要沿用“所有情况均 UNKNOWN”作为通过标准。
4. **reload/业务**：正常加载 200；坏 JSON 保留旧配置、报告真实 loaded；错误 token 401；认证前不清 cooldown。跑 Anthropic JSON/SSE 和现有跨协议案例；本轮已有 Wine fake-reply 不等于所有协议通过。
5. **off/restart（批准后）**：只终止自己的测试目标；重复 off 幂等；PID 复用/入口不同不停止；旧进程退出及锁/端口释放后重启；超时不得启动第二实例；在途流中断如实报告。
6. **敏感写入**：当前用户私有与 Everyone 只读祖先分别正确；Everyone 能读私有文件拒绝；新目录/临时文件创建即保护；existing/new file、预览后改写、提交失败/后验失败、备份和多文件部分完成；不依赖全局 alias key 获得“安全”结论。
7. **Mac 对照**：旧 CLI 正常语义、已批准异常非零、直接相对/链接路径、hook/非 TTY、手动停止；用假 HOME/配置、独立测试端口和受管子进程验证。旧代码/新代码全量对拍尚未在本轮完成，不能借 pytest cache 宣称已全绿。

### 执行边界与验收分层

- **本地开发层**：现有 worktree 源码＋一个新临时目录、假配置/HOME、OS 分配端口、本地 mock；不再叠第二套隔离框架或产品 unsafe 开关。已有测试先检查边界再复用。
- **Whisky 层**：现有 Windows Python 可跑共同代码和实际 Wine API；优先真实操作，不为缺 API 造 fake 系统。不支持的权限/进程观察明确记“Wine 不可验证”，不做产品放行依据。
- **native 层**：仅对将发布的 OS/Python/监听范围跑上述实际用例、真实 ACL/锁/peer 查询；失败给出 API/场景和具体修复，不泛称“还需确保安全”。无平台版本全覆盖承诺。
- **发布层**：实施完成、native 对应功能通过、Mac/协议回归通过、独立 reviewer 后再讨论发布。当前分支不自动部署；生产路径、服务、配置和重启另需用户授权。

### 交付物规范

本轮唯一新增产物为本设计记录，落既有 **W/docs/designs/**，命名 `2026-11-15-model-proxy共享Python与Windows最小适配再审视.md`，供用户裁决与后续实施派单；遵从 architect 设计记录约定，frontmatter 为 draft，不更改 README 索引或旧设计。

后续推荐方案优先改既有模块和测试；**不新增产品安装记录、持久冻结资源、控制端口/私有协议、用户配置字段或用户子命令**。已有轻记录/锁沿用固定目录；临时假配置和回执只在 `/tmp`/本轮自有测试临时目录，不成为产品配置。CI workflow 是另批基础设施产物，须先授权，不包含在当前交付。

## 关联

- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-12-model-proxy统一内核与原生Windows-CLI设计]]：旧目标与用户已批准事项，不自动成为本方案前置公理。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-13-modelproxy-Mac符号链接配置兼容方案]]：Mac 已确认兼容边界，本次不撤回。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/README]]：工作树状态；本轮只读。
