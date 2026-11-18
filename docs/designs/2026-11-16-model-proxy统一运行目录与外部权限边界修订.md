---
type: design-decision
status: confirmed
target: "[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli]]"
tags:
  - architect
  - model-proxy
  - windows
  - macos
created: 2026-11-16
---

# model-proxy 统一运行目录与外部权限边界修订（务实 draft）

## 背景与问题

**建议：保留一份 Python 业务及 CLI、薄 sh/bat；以项目固定 `.runtime/` 收纳运行数据与实例锚点。** 本用户的部署主线是以后经授权准备**完整的新项目交付目录**，内含与当前实际使用的业务配置实质一致的配置，运行路径调整为 `.runtime/`，停稳旧实例后交付替换；业务配置目录及 route sidecar 的相对/链接语义不变。自定义路径兼容是可选能力，不成为本次默认流程的阻塞。文件保护落实到受信项目、实际直接父目录和敏感目标，不再让系统盘根及项目外高层祖先逐级满足现有严格 ACL 白名单。保留真实 socket 对端、Windows 已验证同一 HANDLE 停止、单实例锁、敏感文件保护及 Mac 已批准的符号链接能力。WMI 超时独立查明。本稿仅设计，不实施、不部署。

- 作用根 **W**：`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli`；本文 `W/` 仅表示此 worktree。用户已授权本次订正后继续实施；本轮仅订正文档，**待独立确认复核后由主会话处理状态并继续落地**。迁移和部署仍须事后另行对齐和授权，用户之后手动重启；当前不修改旧设计、代码、线上主区和生产环境。
- 订正授权：用户原话：
  > 为何会有新旧配置的问题，这个不是整个项目目录替换的逻辑么？新的版本里面有 现在使用的实质一致的配置文件就好  
  > “统一 .runtime/”的边界，按建议

  用户明确接受完整项目目录交付主线、外部目标短期暂存及新备份优先 `.runtime`，并要求补充账本/sidecar 写入保护。前轮已接受的安全边界、route 和 Python 版本不扩入仍有效；该轮只授权订正，实施授权以后续明确指令为准，`status` 保持 `draft`。
- 上次补充订正：用户对“为保持现有功能，把你已同意的‘必要时目标旁短期暂存’也覆盖这两种情况；新备份仍优先放 `.runtime/`。可以按这个边界补进方案吗？”回复原话“好的”。两种情况仅指用户明确选择的项目外配置所派生的外部 route sidecar，以及显式自定义的项目外账本，且目标与 `.runtime/` 跨文件系统。该具体例外已接受，不再反问；不改变默认目录。
- 本次复核订正与实施授权：用户就外部实际目标写授权、sidecar UNKNOWN 后验证、账本异常优先不中断对话、Windows 同卷异目录提交、缺失目标别名及 `appkey_file` 相对 cwd 验证的建议，回复原话 **“按建议，修订方案后，开始落地”**。账本异常不中断对话及异常退出可能丢失未持久化统计的权衡已拍板，不再反问；其他安全政策不因此扩权。此轮仅修改该 draft，独立确认复核后主会话处理状态并继续实施；生产部署仍未授权。
- 实施建议：按已接受的不改变正常 route 语义及现有空 override 回退，本方案不新增统一硬失败；细节待测试。
- 原稿形成时静态阅读 worktree 内已确认的 11-15 两份复核、11-13 Mac 符号链接方案、11-12 统一 CLI 方案、11-16 ACL 草案及 preflight 评估，核对源码和测试，另只读用户授权的三个假数据 CI 日志 `run1.log`、`diag1.log`、`diag2.log`；本次订正另只读核对相关写入源码、Git 跟踪/忽略规则及 worktree 元数据。两次均未运行产品、测试、GitHub Actions 或服务；不记日志里的路径和环境敏感内容，未读取真实配置、账本或日志。工作树原有未提交改动不属于本稿产出。

### 新证据与旧建议的适用性

| 证据 | 可得结论 | 不得外推 |
|---|---|---|
| 原生 GitHub Actions 私有 runner：Windows Server 2025 / Python 3.14.10，假数据；`run1.log` | 首次产品启动报 `windows_private_directory_unverified`；后续生命周期命令和 CLI mock 未执行。相关原生 WMI 读取的产品精简环境 8 秒超时 | 不是五命令、协议或产品整体通过；也不是所有 Windows 都失败 |
| `diag1.log` / `diag2.log`，结合用户本轮补充 | 一台 runner 的卷根先报 `owner_untrusted`；另一台卷根及两层非根祖先均被 `ancestor_access_unsafe` 拒绝。旧 ACL 草案只处理卷根的候选，**不足以覆盖后续观察** | 不把一台机器结果当普遍 ACL；公共 allow ACE 的写权限可能为 inherit-only，不能仅凭掩码认定盘根有效可写；也不能把诊断脚本成功当产品验收 |
| WMI 诊断 | 普通继承环境曾约 0.3–1 秒可用；后来 32 次受控组合试验均超时，试验目标环境不同，具体缺项仍未确定 | 不推断是某个环境变量，更不能说迁移目录解决了 WMI；诊断不是产品 reader 成功 |
| 当前源码静态阅读 | Windows 路径检查从盘根遍历，且旧路径冲突检查禁止全部 runtime 目标在固定状态目录内；Windows marker 仍位于系统临时目录 | 这只是代码事实，不是修改后的功能或安全验收 |

旧 [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-16-model-proxy-Windows路径ACL默认目录兼容方案]] 是 draft，针对先前证据的根例外建议不作为本方案；**本稿不修改其内容或状态**。已确认记录同样不更改，若本稿获批准，具体实现按新明确边界执行，不借此宣称其他未讨论的旧约束都被推翻。

## 方案设计

### 1. 选择与边界

| 方案 | 代价和后果 |
|---|---|
| **A：项目 `.runtime/` + 有限边界（推荐）** | 默认集中、减少重复路径；对外部路径只验证实际授权目标及必要直接父目录。保留固定锁和现有底层保护，需修改路径身份检查、两平台创建及回归；承认项目上层可替换导致的残余风险。 |
| B：保留原目录及全祖先严格白名单 | 改动少但已知原生默认环境阻断；仅给诊断不能保证可用。 |
| C：围绕盘根及每一层增加 ACE 特例 | 需维护平台策略矩阵，不能从单一 runner 推出普适规则；复杂度和误放行风险均高，不推荐。 |

不引入第二套 authority、路径策略框架、业务副本、权限修复器或新的隐蔽绕过开关；复用当前路径解析、锁、私有创建、记录和平台原语，按实际调用收敛。保留当前安全失败、失败阶段及 UNKNOWN/部分提交报告，不通过频繁人工确认替代正常操作。用户已授权订正后实施；本轮仅订正文档，复核后主会话继续，不构成部署授权。正常读取和对话不增加重复确认。

### 2. 目录、锚点和自定义兼容

下列是推荐默认布局，不把文件名当作新增用户配置字段；`runtime_paths.json` 的既有六键仍为路径配置真相源：

```text
W/
├── config/
│   ├── runtime_paths.json             # 默认六角色指向 .runtime；显式覆盖仍有效
│   ├── model_proxy_config.json        # 或用户显式指定的业务配置
│   └── session_overrides.json          # route sidecar；仍跟随配置原始路径父目录
└── .runtime/                          # 产品自有、默认私有；版本库忽略
    ├── anchor.lock                    # 安装级固定单实例锚点
    ├── control.lock                   # 控制互斥
    ├── install.json / active.json / last_good_paths.json
    ├── model_proxy.log / model_proxy_totals.json
    ├── model_proxy.lock / model_proxy.pid
    ├── model_proxy_ensure.log / model_proxy_start.lock
    ├── tmp/                           # 产品短期暂存及 CLI marker
    └── backups/                       # 新建受保护备份的优先位置
```

1. 固定 `.runtime/anchor.lock`、控制锁和实例记录从**代码定位的项目根**确定，**不能由六键、业务配置、环境中的自定义日志/锁路径或 cwd 推导**。同一安装即使换端口或更改六键仍争用同一个 OS 锚锁；不同安装是否为同一实例仍按现有证据判断，不用路径字符串或 PID 直接授权。新安装缺目录才按平台安全创建；已有目录/敏感文件验证保护，不自动 chmod、改 ACL 或覆盖。
2. 本用户的新交付包在以后获部署授权后受控准备：业务配置的 route、supply、鉴权引用及其他业务语义与当前使用的配置实质一致，**不静默改变第三方 `appkey_file` 引用**；交付包的 `config/runtime_paths.json` 六键指向 `.runtime` 中不同文件。不需要猜测旧值究竟是不是用户意图，也不要求另选“新安装还是原位置增量覆盖”。现阶段不读取、拷贝或打印真实配置及密钥；以后即使获准本地准备，也不能将它们纳入 Git、提交或 GitHub Actions。
   **可选通用兼容**：保留显式外部绝对/相对路径解析、四个完全匹配旧 `/tmp/...` 的 Windows 映射及其现有限定目录安全创建；不得把任意 POSIX 绝对路径解释为 Windows 相对路径。缺文件/损坏继续报错，单缺键新默认和多缺键拒绝应测试。自定义外部目录不自动递归创建、不静默迁移或删除；直接父目录无法安全使用则说明并拒绝。这是其他安装可选择的能力，不需要本用户为默认整目录交付逐项判别旧值。
3. `MODEL_PROXY_CONFIG`、`--config` 的现有相对、绝对及 Mac 经批准符号链接/`symlink/..` 语义不收窄；链接路径本身与真实目标都按原证据处理。配置依然在 `config/` 或用户明确的外部路径；既有 route 行为和 `session_overrides.json` 位置不改，特别是按配置**原始路径的父目录**决定 sidecar，不能用真实链接目标父目录替代。不同配置原始路径可能有不同 sidecar；切换配置不是自动合并 sidecar。route 与 Python 版本固定均不纳入此次变更。
4. `~/.config/mcopilot-cli/` 是他方登录态，不搬、不 chmod/改 ACL、不新增修复，也不复制其内容；`appkey_file` 维持 `core/server.py:resolve_appkey` 每次按现有方式展开和读取。客户端配置、只读会话元数据、系统 Python 和 PowerShell 不搬、不调整权限；明确的 install 操作仍沿用原有受控写入能力，并非这次迁移自动改客户端。敏感输入不可放入诊断或测试日志。
5. 路由覆盖不是本次欲收拢的 CLI 临时文件：这是现有业务 sidecar，用户明确 route 暂不调整。完整交付包受控承接它，但不改变它相对配置原始路径的位置或搬入 `.runtime`；总账和必要日志承接至新默认 `.runtime/`。显式自定义运行路径是可选兼容例外，不应宣传所有其他安装自动集中。

### 3. 文件身份：允许同目录，禁止角色碰撞

现有 `core/discovery.py:_windows_open_preflight` 和 `inspect_runtime_identities` 明确禁止六角色落入 `.model_proxy_runtime`；简单替换目录名会把新默认全部拒绝。必须将**目录容器与文件角色分开**：

- `.runtime/` 是容器，六角色、固定锁/记录、临时目录可以共享它或其已授权子目录；禁止任一文件角色等于容器或被当目录、禁止文件角色相互嵌套，保留 `start_lock` 历史上可能是目录的类型校验，目录角色不得包含其他受保护角色。`tmp/` 与 `backups/` 是独立目录角色，六角色不得落入或包含它们，marker 和临时/备份文件互不碰撞；`anchor.lock`、`control.lock`、三个记录、配置文件、runtime 路径配置、route sidecar、六角色与实际备份/暂存文件之间均不得同文件冲突。
- 对不存在的目标比较规范化父目录与名字；对存在目标比较平台的真实文件身份（Mac dev/inode，Windows volume/file ID），包含大小写、符号链接、硬链接和 Windows 别名等可能；身份、类型或父路径无法确定时拒绝/UNKNOWN，不能只比较字符串。对于被批准的 Mac 配置链接，同时检查原路径链和实际目标，不能通用放宽其他角色的链接规则。
- **多个目标均不存在时仍须保守判别**：沿用 `core/discovery.py:_component_may_alias`、`_identity_same/_identity_contains` 和 Windows `_windows_evidence_contains` 对共同已验证祖先及未解析后缀的现有保守策略；大小写、Unicode NFD/casefold 或非 ASCII 等价未知，以及 Windows 8.3 短名/`~` 可能别名，不能因两个目标都没有 inode/file ID 就宣称不同或用原始字符串放行。可能同物、包含关系或无法确定时拒绝/UNKNOWN；可证明不同的普通 ASCII 名才按既有规则允许。不得为使默认目录通过而全局放宽未知名字比较。
- 检查后到操作期间保留现有必要的对象/父目录证据、锁及重验；拒绝能观察到的路径替换、硬链接异常及权限变化。不能把“曾检查”说成同用户对抗的原子保证，也不把目录名称相同误认为文件碰撞。
- 首次启动的顺序要专门接线：当前 Windows preflight 要求六角色的直接父目录已被观察到，而新默认六角色都以尚不存在的 `.runtime` 为父目录。先只读验证项目入口、配置和可判定的角色冲突，再仅为固定默认目录安全创建 `.runtime`，随后重新做完整身份/权限 preflight、获取固定锚锁和运行锁，**持锁前不裁剪日志、不写账本或启动服务**；不能把首次目录不存在当作文件身份已验证，也不能为通过而放弃重验。直接入口和 CLI 采用相同顺序；创建状态或关闭状态未知则停止并如实报告。

### 4. 权限与外部边界

**威胁模型改变必须明示，而非悄悄关闭检查**：项目根由使用者选择并视作受信入口；不再把卷根和项目外高层祖先逐段套现有 owner/ACE 严格白名单，也不修改这些目录的权限。对项目根本身、`.runtime`、其直接父目录、实际操作的配置/锁/记录/临时文件和外部自定义目标，保留与操作相关的存在、类型、身份及必要保护检查。路径遍历可能仍需打开/观察组件以识别重解析和绑定；**不等于逐层应用原 ACL 白名单**。

- 新 `.runtime`、CLI 私有临时目录与敏感文件必须创建时受保护并后验核查：Mac 复用现有私有模式、ACL/FD 原语；Windows 复用 protected DACL、`CREATE_NEW` 等现有实现。已有敏感文件独立验证，未知、宽松或异常时安全拒绝，不先写后修权限；不对已有外部目录/文件自动修复。
- **路径选择、真实目标写授权、邻近暂存授权是三件事**：用户指定外部路径仅确定所选入口，不自动授予经符号链接解析出的任意文件写权；本稿允许目标旁临时文件亦不构成对目标本身或其他邻近文件的写授权。Mac 沿用 `core/cli/dispatch.py:prepare_config_write/_confirm_config_write` 的既有策略：内部按既有授权范围，外部真实目标仍需现有实际目标写入授权；`core/mac_config_writer.py:WriteAuthorization/_authorize` 绑定已批准真实目标及父目录、目标身份，链/身份或授权不匹配即拒绝。对目标旁暂存、`.runtime` 备份分别限定到本次私有对象和已批准用途；调整既有 `allow_backup_and_staging` 的表示时不得把其中一项授权推导为另一项。继续验证实际父目录、类型/单链接/权限及可观察变化；Mac 链接读取、`symlink/..` 和安全写入不收窄。保留现有外部目标写入时的确认机制，不增加每次常规读取、请求或对话的确认。Windows 不因此新增对任意 reparse 的兼容承诺。
- **安全取舍**：如果项目外某个上层目录可被其他用户替换/重定向，验证后、使用前的替换及短暂 ABA 仍可能使后续绝对路径访问指向别处；直接文件私有权限不能单独消除祖先替换风险。现有 HANDLE/FD 身份、前后重验减少可观察竞态，不能保证路径原子绑定或防御同用户、管理员、SYSTEM、受信位置失陷。不同文件系统、网络盘、云同步和域策略不得一概承诺兼容。需要更强防御时让用户选择受信安装位置或另议方案，不自动更改系统 ACL。
- 不改变连接已建立的**同一 socket** 对端归属验证、发送敏感内容前的校验，不用端口/PID 代替；Windows 停止仍打开并验证进程身份后使用**同一个已验证的 HANDLE** 终止和等待，不对新打开的同 PID 直接操作。观察不足或关闭状态不明时报告 UNKNOWN，不靠放宽权限“变绿”。

### 5. CLI marker、临时产物及写入事务

- Mac 写 helper 已用匿名 pipe/FD 协议，无磁盘 marker，**保留它**；只读 helper 不创建 marker。Windows 在 `.runtime/tmp/` 下用高熵随机名创建每次独有的私有目录（独占创建，非“先 mkdir 后改 ACL”），在其中以受保护的 `CREATE_NEW`/现有安全写入创建固定名 `reload`、初值 `started`；保留 marker 初值、身份、内容、受保护快照/重验及提交成功才发 reload 的现有协议。`core/cli/_config_ops.py:_precheck_inherited_marker` 必须把校验根由系统 `gettempdir()` 改为**受信项目固定 `.runtime/tmp`**，不能仅信任子进程传入的任意绝对路径；复用现有 root 来源和边界，不引入可随配置漂移的新 authority。创建、提交、读取、清理过程只针对本次创建对象，异常/未知时报告残留，不批量清空别人的临时目录。
- `_ordinary_marker_scope` 若仍有可达消费者，也使用项目 `.runtime/tmp` 的私有独占目录；不要误把 Mac 匿名 pipe 改成文件，也不能只改一侧路径使 helper 全部拒绝。其他 CLI 自有短期缓存/中间文件按此目录盘点，系统解释器和系统服务自己的临时文件不归产品管理。
- **已接受的事务暂存边界**：项目内部配置、sidecar、账本及默认日志的短期事务暂存归 `.runtime/tmp`；默认日志、账本、锁、状态和 CLI 自有临时数据仍在 `.runtime/`。同文件系统的替换要验证平台支持及目标保护。**仅在下列实际目标与 `.runtime` 跨文件系统、且为保持原子替换需要目标同目录暂存时**，允许在实际目标旁独占创建短期私有暂存：项目外的业务/客户端配置、显式外部日志（含日志裁剪的 `.logtrim`）、用户明确选择的外部配置所派生的外部 route sidecar、用户显式自定义的外部账本。sidecar 仍按配置**原始路径的父目录**定位，不因链接的真实目标而改变；配置写入本身以实际目标及其父目录判断，不以链接拼写误判。正常完成后清理本次暂存；提交失败、清理失败或结果不明时按实际状态报告，不声称已清理。不能以非原子复制覆盖或无保护暂存降级。相同文件系统仍须核实现有实现及 OS 是否支持从 `.runtime/tmp` 原子提交且保留安全语义，不能证明则阻断并报告；不将例外扩展到上述范围之外。
- **备份与暂存分开**：现有旧备份原位不动；需要新建的配置等备份优先写入项目受保护的 `.runtime/backups/`，不是完成后即删的短期暂存。备份以安全快照内容创建、独占私有、按适用平台校验身份/内容/保护，并可供既有恢复流程使用；源和备份跨卷时以受保护的复制及校验实现，不假装跨卷 rename 原子。备份路径、保留/恢复行为、既有 ACL/权限语义与异常状态均须实测并向用户可见；旧备份不移动、不覆盖。无法保持既有安全或恢复语义时阻断提交并说明，不静默改用弱保护或改变原子性。`~/.config/mcopilot-cli/` 不搬、不改权限，也不备份。

### 5.1 账本和 route sidecar 的运行时写入保护

**当前事实，而非既有保障**：`core/server.py:_atomic_write_json`、`core/commands.py:_atomic_write_json` 都以 `mkstemp` + `os.replace` 写入；后者在 replace 后尝试 `chmod(0600)` 且吞掉失败。启动 preflight 和账本初始化快照不证明每一次运行时写入时目标/父目录身份和权限、后验结果已被核验。`UsageTotalsStore` 写入失败由调用侧记录 warning，sidecar 写入路径更新内存和返回回执；不能把它们现状描述成已具有受保护提交协议。

**拟接线，不改变 route 业务语义**：

1. 在已有账本、sidecar 各自的线程锁与服务已持有的实例锁内，对每次持久化复用现有平台文件身份/私有创建/提交能力：检查受信项目或外部直接父目录及目标类型、身份、必要保护和角色碰撞；从 `.runtime/tmp` 或仅对已列明的跨文件系统目标适用例外创建独占私有暂存；提交前重验可观察变化，以平台支持的原子替换提交，之后核查最终目标身份、保护及内容。缺失目标安全创建；不可验证、路径或保护变化、硬链接/类型异常时拒绝，不能以启动时快照替代本次检查。对实际创建、替换、关闭及清理的 UNKNOWN 单独报告；同用户对抗、上层路径替换、不可观察 ABA 和断电持久性仍不保证。
2. **账本：已批准不中断模型对话**。改 `core/server.py:_atomic_write_json` 和 `UsageTotalsStore.record` 实际链路，以现有线程锁内的小型状态记录（非新框架）区分：
   - `CLEAN`：已验证的磁盘基线 B；每次记录先在内存累计一次，再产生候选完整快照 C。提交及后验均确认后更新基线，统计标为已持久化。
   - `PENDING`：确定替换尚未发生的失败，保留内存累计并标未持久化；给出脱敏 warning/失效状态，模型对话和后续增量继续，不能将单条请求重放而重复计数。仅在重新验证当前目标、父目录、保护和原基线 B 后可重试完整累计结果；观察变化不明则转 UNKNOWN。
   - `UNKNOWN`：替换已调用但结果、后验或资源释放不明时保留 B、候选 C 及各自可验证的身份/内容证据；后续请求只追加到内存累计，不写覆盖磁盘，告警限频并明确统计未确认。安全重读后，只有目标身份、保护和内容与经验证的 C 一致，才把 C 当新基线并只提交之后的增量；若与 B 一致，才以 B 为基线提交包含候选和后续增量的完整内存累计。保持锁及提交前重验，不能把内容相同单独当身份或已提交证明；无证据、第三种内容/身份、保护变化或并发修改时继续告警并暂停账本持久化，不盲合并、不覆盖，模型对话继续。
   - 退出前若未能恢复，未持久化统计可能丢失，**异常退出更可能丢失**；不得声称无损或恢复曾丢失的内存增量。重启后内存状态不存在，只能对磁盘账本作身份、保护、结构及稳定内容核验，成功后作为新的持久化基线；不能推断上次 UNKNOWN 是 B 还是 C、不能重放未知增量。不可验证时禁止覆盖并保持可观测告警；允许对话继续、统计标失效。首次确实安全确认不存在的账本才按既有首次空账本语义处理，不能把读取错误当不存在。必须通过现有服务日志给出脱敏、限频的待确认/失效告警与恢复信息；`core/cli/stats.py` 当前仅读取磁盘文件，不能凭它推断进程内是否存在未持久化增量或把磁盘快照称为完整统计。若现有已验证状态通道可低成本传递该状态，可同时展示；不可引入新控制框架。不输出内容或密钥，不承诺掉电持久化。
   - **启动接线与边界**：当前 `_capture_migration_guard` 调用 `_capture_migration_ledger`，`_verify_migration_guard` 重验它，`UsageTotalsStore.from_verified_snapshot` 将 `None` 视为已证明不存在；账本捕获/解析异常目前可在服务前阻断。实施时仅对**已通过既有角色冲突、固定/运行锁、业务配置、监听及相关身份/权限门禁后的账本内容不可用**引入明确的 `UNAVAILABLE` 失效结果，贯通上述 guard 和 store；不得用 `None` 代替，也不得因统计降级跳过这些门禁、擅自读取/写入保护不合格的文件或吞掉非账本错误。失效状态下不把未知磁盘视为空账本、不提交覆盖；新请求可在内存独立累计并告警，只有后来能验证基线、证明本轮累计与其互不重叠并保留统计口径时才允许受保护合并，否则继续暂停持久化。若启动时路径角色冲突、锁或身份/权限本身无法安全证明，保留原有 fail-closed，不能借账本不中断对话扩权。
3. **sidecar：UNKNOWN 后禁止未核验热加载**。改 `core/commands.py:_atomic_write_json`、`SessionOverridesSidecar.apply_command/_maybe_reload_locked/maybe_reload` 及 `core/server.py` 普通请求调用链。`apply_command` 在现有锁内保留最后已验证快照 B、候选 C 和身份/内容证据，`_mem_last_seen` 的增删也与提交结果一致。只有提交和后验确认后才发布 C、成功回执；确定未提交则保留 B、返回失败。替换发生但后验失败或结果 UNKNOWN 时立即标记隔离态，返回未知，不假报成功。
   - **普通请求**：隔离态下拦住原有仅按 mtime 自动 reload 的路径；继续使用本进程内最后已验证的 B，明确记录降级告警，不能直接使用磁盘 C 或第三种未核验内容，正常协议/路由算法不变。可以在现有锁内有限地尝试安全重验，不每个请求重复询问、不扩建权威框架。
   - **手动 reload / 下一次 `$route`**：先以受保护的直接父目录和目标身份、单链接、权限、完整稳定内容和现有格式语义重验；只有能证实磁盘为 B 或 C 且身份/内容证据相符，才发布该已验证快照并恢复正常写入，原 UNKNOWN 命令是否成功仍按可证明结果报告。目标为第三种内容、身份变化或证据不足时维持 B 和隔离态、对新写返回明确失败/未知，不自动覆盖或重试；若需接受外部编辑，须先通过现有受控编辑/恢复机制及同等安全校验，不能让 mtime 绕过隔离。
   - **进程启动/重启**：不引入持久 UNKNOWN 标记或新 authority；新进程无旧内存 B/C，不得声称知道旧命令是否提交。启动及首次发布必须验证实际 sidecar 的父目录/文件身份、保护和完整格式，再把当前可验证内容作为新基线；安全确认的首次缺失沿用既有空 sidecar 语义。已通过既有启动门禁后，验证失败、内容损坏或状态不明时，不将不可信内容读入业务状态或发布，明确告警并禁止 `$route` 写覆盖未知磁盘状态；没有已验证 B 时使用**空 override**，让普通请求沿既有 route-pool/base 路由回退继续，不新加统一硬失败。该回退可能不保留先前 session override，不能声称历史无损；验证恢复后再发布可信快照并允许写入。源码当前 `__init__` 先置空、`_reload_locked` 对损坏/加载失败保留旧值并告警；`maybe_reload` 对 `FileNotFoundError` 以外的 `stat` 异常没有本地捕获。另有 Windows 启动 `_windows_check_config_targets` 对 sidecar 的私有保护、直接父目录及路径碰撞做门禁，失败可在初始化前拒绝；此类原有安全门禁保持，不以空 override 绕过，也不扩大已有硬失败范围。此处理不证明上次 UNKNOWN 命令或重启前数据状态。
4. 复用现有小原语和错误状态，避免将配置写入的大型备份事务机械套到高频账本；不新增 authority/框架，也不把每次账本写入变成自动备份。平台能力或保护无法成立时，对受影响的敏感写入安全失败；sidecar 读取降级不因此新增 route-pool 硬失败。上述均为待实现与待验证，不承诺绝对无损、跨进程任意写入隔离或宕机后持久性。

### 5.2 Windows 同卷异目录原子提交护栏

**源码现状**：`core/windows_secure_native.py::_CommitCalls._check` 要求 source 与 target 的解析后父路径相同；`ProductWriteBackend.reserve` 也从目标父目录选暂存。因而将同卷暂存改为 `.runtime/tmp` 后，即使确实同卷，也会被现有 `_check` 拒绝；仅改默认路径或仅比较盘符不可完成该接线。

**待实施**：复用现有 `ProductWriteBackend`/`TargetSnapshot`、`windows_paths` lease、私有创建和 `ReplaceFileW`/新目标 move 原语，扩展 `_CommitCalls._check` 及调用契约，而不是直接删除护栏。

1. 暂存源须来自本次经授权的 `.runtime/tmp` 或 §5 外部目标旁例外，目标须有独立写授权；分别观察并绑定源文件、源父目录、目标父目录、已有目标或目标不存在状态的身份和必要 DACL。新文件创建即私有，验证与各角色不冲突；对不存在的目标沿用 §3 的保守别名规则。
2. 从受验证对象/父目录取得并比较**实际卷身份**，确认是同一个支持所需操作的卷，不能把相同盘符、字符串前缀或配置路径当成卷身份。允许同卷不同受保护父目录；目标/源等同、跨卷、目标未获授权、源/目标或父目录替换、reparse/别名不明、保护不合格、身份/句柄未知均在调用前拒绝。必要的现有 lease/证据保持到原生调用所允许的最后阶段并重验；若平台操作必须先释放句柄，记录释放及最后重验顺序，关闭/释放未知不得当成功。
3. 已存在目标继续调用经验证的原生原子替换；目标不存在继续用不可覆盖既有文件的原生创建式提交，并在调用前后分别核验不存在/最终目标。不得降级为跨卷复制后覆盖，也不能以路径检查冒充原子 compare-and-swap。调用抛错、部分替换、后验或关闭失败按现有 `UNKNOWN/POSTCHECK_FAILED` 报告，保留本次残留并按状态安全处理，不盲重试；非协作修改、绝对路径 ABA 和最后检查后的替换仍是残余风险。
4. 必须有真实 Windows 同卷异目录正例、缺失目标/已有目标及异卷、不同授权、父目录和源文件替换、硬链接/名称冲突、原生调用/关闭/后验 UNKNOWN 的负例；身份/ACL、结果和清理由产品真实链路核验。mock、Wine 或同目录成功不能证明这条同卷异目录链可用；未覆盖即记缺口。

### 6. 明确待改代码点（实施清单，不是本轮变更）

| 位置 | 改动及不变项 |
|---|---|
| `config/runtime_paths.json`、`core/runtime_paths.py` | 六键默认和单键缺省改 `.runtime`，完整交付包受控采用该布局；保留可选的显式外部路径及 Windows 四项精确旧值兼容，解析错误行为不静默变更。 |
| `core/discovery.py` | 固定状态路径、Windows/POSIX 双分支的 role 身份检查及目录重叠逻辑按 §3 调整；保留 `_component_may_alias` 和 Windows 8.3/未知 Unicode 的缺失目标保守比较；`prepare_windows_runtime_directories` 创建固定默认目录；`verify_record_directory` 等目录假设同步；保持锁和记录现有状态机。 |
| `core/cli/authority.py`、`core/cli/windows_control.py`、`core/cli/control.py` | 所有固定状态、锚锁、控制锁、状态读取、缺目录判断、last-good 和路径比较统一指向固定 `.runtime`；确保 CLI、直接启动、off/restart 使用同一安装根与证据。保留现有未知、同 socket、同 HANDLE 契约，不新造 authority。 |
| `core/server.py`、`core/commands.py`、`core/cli/dispatch.py`、必要的现有 stats/status 展示 | server 默认值及 `_capture_migration_ledger/_capture_migration_guard/_verify_migration_guard`、`UsageTotalsStore.from_verified_snapshot/record` 的账本失效态、写入告警和不中断对话接线；不能将不可用当 `None` 或绕过角色/锁/身份门禁。commands 的 sidecar 写入、普通请求热加载和重启验证按 §5.1；CLI 路径冲突和 marker scope 同步。外部账本/sidecar 暂存按 §5；保留 route 算法和配置原始路径 sidecar 语义。 |
| `core/cli/_config_ops.py` | Windows marker 的固定 tmp 根、完整对象验证及清理协议与 dispatch 对齐；Mac 管道协议不变；配置安全提交后才 reload。 |
| `core/windows_paths.py`、`core/windows_acl.py`、`core/platform_windows.py`、`core/windows_secure_files.py`、`core/windows_secure_native.py` | 核对祖先/直接对象验证；按 §5.2 调整 `_CommitCalls._check`、调用方 `ProductWriteBackend` 的卷/源目标身份绑定和关闭/UNKNOWN 顺序，保留原生原子提交；`reserve` 区分 `.runtime` 备份与跨卷暂存，不能只删同父护栏或改路径字符串。 |
| `core/secure_files.py`、`core/darwin_acl.py`、`core/mac_config_path.py`、`core/mac_config_writer.py`、`core/cli/dispatch.py` | 核查 Mac 记录目录祖先策略和外部目标直接保护；保留 `prepare_config_write/_confirm_config_write` 对真实外部目标的授权及 `WriteAuthorization` 身份绑定、已批准配置链/链接写入；按 §5 分离备份与短期暂存授权、不同目录 FD、同卷提交及跨卷备份恢复，不把链接可读当作可写授权。 |
| `hooker/ensure_model_proxy.sh`、`hooker/ensure_model_proxy.bat`、`model_proxy_cli.sh`、`model_proxy_cli.bat` | 核查薄壳、ensure、启动参数、路径与退出码；只改必要调用，不把业务重写回壳。明确旧 hook/入口停用和迁移后手动启用另获授权。 |
| `.gitignore`、`README.md`、`docs/CONFIG.md`、`docs/ARCHITECTURE.md` | 实施时记录 `.runtime/` 忽略、六键、自定义例外、迁移/回滚及风险；不要把 route sidecar、外部凭据或用户日志加入仓库。 |
| `tests/test_runtime_paths_cross_platform.py`、`test_discovery_anchor.py`、`test_windows_paths.py`、`test_windows_acl.py`、`test_directory_protection_races.py`、`test_cli_*`、`test_mac_config_*`、`test_windows_control*` 等 | 扩充受影响的现有测试和实际闭环；先确认测试目标均为假数据/自建进程，避免引入额外隔离框架。实际测试清单随实现 diff 核定。 |

`core/commands.py` 需要改写入保护，但不改变 route 业务语义或将 sidecar 迁到 `.runtime`；若无法同时做到，停止并另问用户。上述是根据当前 worktree 源码的影响清单，实施前仍需重核并处理新增调用点，不保证只改这些文件即可完成。

### 7. 迁移与回滚：另获授权后操作

以下是用户选定的**完整项目目录交付主线**，具体部署须事后对齐并另获授权，当前不执行。开发继续在 W；旧、新锚锁不同，不能靠新锚锁证明旧版已退出。

1. 事后确认切换窗口、需保留日志和备份保留位置，停用旧自动 hook/入口，并通过旧版受保护控制路径停止旧实例，等待实际进程、监听和 OS 锁均释放。状态未知、锁未释放或陌生监听时停下；不得杀陌生进程。不能在旧服务仍写账本时复制，也不能让旧入口在切换期间拉起。
2. 停稳后在本机受控准备完整新交付目录：在新版本代码基础上承接与当前使用内容**实质一致**的业务配置及鉴权引用，保留其 route、supply 等业务语义；六个运行路径使用新项目 `.runtime/`。同时从旧安装承接最新账本、与现用配置原始路径对应的 route sidecar、需要保留的日志和其他必要但未跟踪的业务配置；验证账本可解析、大小/摘要及必要合计，保留旧数据和可回滚备份。sidecar 仍按配置原始路径父目录定位，不改路由语义，不对多个 sidecar 盲合并；外部链接配置维持已批准的链接链语义。第三方 `appkey_file` 仅保留引用，不读取、拷贝、搬迁或备份其登录态。缺失、损坏、目的冲突或一致性无法证明时停止，不编造连续性。
3. **不能只用 Git 快照当完整交付包**：元数据只读核查显示 `config/runtime_paths.json` 受跟踪，真实 `config/model_proxy_config.json`、`config/session_overrides.json`、`.model_proxy.log` 和 `.model_proxy_totals.json` 被 `.gitignore` 排除；这些未跟踪业务配置和数据需在以后获授权的本机操作中受控承接，具体以真实安装实际位置为准。当前不读取或记录其内容，也不将真实内容纳入 Git、提交、GitHub Actions 或日志。
   **`appkey_file` 相对路径门禁**：源码 `core/server.py:resolve_appkey` 对字符串仅做 `os.path.expanduser`，其后 `open` 的相对路径基于**服务进程实际 cwd**，不是配置文件父目录；`~` 展开还取决于平台和服务环境。完整目录替换及回滚计划须核对旧服务与预期新服务（含 CLI、直接入口/hook）的实际 cwd 和 HOME/平台用户目录环境，按目标平台实际规则解析同一引用，确认新旧指向同一预期目标及可用性；临时交付包的 cwd 不能代替最终启动 cwd。保持原引用，不默改绝对路径；不读取、复制、输出第三方凭据内容。如无法证明解析目标一致则阻断部署并说明，不能凭业务 JSON 文字相等判定鉴权行为等价。此门禁只在未来获授权的准备/切换中执行，本轮不检查真实配置或凭据。
4. **不能物理替换时丢失开发环境**：主区 `tools/model_proxy/.git` 是共用 Git 目录，当前开发 worktree 实体位于主区 `.claude/worktrees/qw/...`，其 `.git` 文件指向主区共用 Git 元数据。直接删除、覆盖或挪走主目录可能删掉 worktree、未跟踪数据或破坏绝对路径引用；单纯保留 `.git` 文件名也不足以证明 worktree 仍可用。完整交付是产品交付逻辑，**不是现在授权递归替换物理主目录**。后续须先设计并核验不破坏共享 Git、worktree、其他已有未跟踪数据及路径引用的具体切换方式；无法证明则阻断，部署方式和回滚事后与用户对齐。
5. **绝不复制持有中的锁，也不把旧 PID、旧 `active.json`、`install.json` 或 `last_good_paths.json` 当新实例授权**；旧记录保留备查，新实例由自身机制重新持锁并建立可信记录。交付完成后由用户手动重启，核查真实身份、锁、路径、账本历史起点及 sidecar；不自动启动。
6. 回滚先停新版、禁用新版入口并确认锁和端口释放；保全新版本运行期间的最新账本、日志和 sidecar，比较旧备份与新数据，明确单一权威及安全回写方式后再恢复旧交付，由用户手动重启。不能用旧备份直接覆盖新版新增账本或盲合并；冲突或状态未知时停止。原有 Git、worktree 和未跟踪文件在回滚中同样受保护。

### 8. 具象推演（设计推算，非实测）

1. **同目录合法**：`log=.runtime/model_proxy.log`、`totals=.runtime/model_proxy_totals.json`、固定 `.runtime/anchor.lock`，目录相同而对象不同 → 允许；把 totals 改为 log 的硬链接或同路径 → 启动前拒绝。若 `start_lock=.runtime` 或 `start_lock=.runtime/tmp`，目录覆盖其他角色 → 拒绝。反例：只把旧 `state_conflict` 换名字，首例会错误拒绝；只删该检查而无文件身份核验，第二例会错误放行。
2. **固定锚点**：项目 W 的 CLI A 指向外部自定义 lock A，CLI B 指向 lock B 或新端口，但二者使用同一个 `W/.runtime/anchor.lock` → 同一安装只有一个持锁实例。反例：用可配置 lock 的父目录推导 anchor，A/B 将各自成功启动；若旧版只认识旧锚，仍不能避免跨版本双启动，所以必须执行 §7 先停旧入口。
3. **外部路径及替换**：用户指定外部配置，其目标与直接父目录验证安全 → 不要求从盘根逐层符合旧白名单；若未受信上层在检查后被替换，目标私有 ACL 不证明后续路径仍指原对象 → 依现有可观察重验拒绝，瞬时或检查后的未观察替换仍是残余风险。反例：声称“只要文件 0600 / 私有 DACL 就绝无路径替换”不成立。
4. **route 和迁移**：Mac 输入配置链接 `A/config.json → B/config.json`，原逻辑 sidecar 在 A，账本迁往 `.runtime`；迁移后仍读 A 的 sidecar。若顺手改为 B 或以旧账本覆盖新版已写账本，则破坏既有会话覆盖或历史；必须停止并分别处理，不以迁移成功假设掩盖。

### 9. 开发阶段、并行与交付物规范

- **P0 边界与实施已获授权**：整目录交付、§5 的跨文件系统暂存及新备份优先 `.runtime`、§5.1 账本不中断对话权衡均已接受，不再重复询问。此轮只订正文档，保持 draft；先独立确认复核，由主会话处理状态并开始实施，核对工作树现状及测试边界。
- **P1 路径与保护**：单一实施负责人处理固定目录、六角色与缺失目标别名、外部真实目标授权、marker、账本/sidecar UNKNOWN 状态机、Windows 同卷异目录与暂存/备份的耦合改动；公共 `discovery.py`/`server.py`/`commands.py` 不由多人同时改。
- **P2 可并行**：一组只读准备 Mac 真实生命周期、外部链接授权与 local mock 假数据用例；另一组准备私有 GitHub Actions 原生 Windows 同卷异目录/身份/UNKNOWN 及生命周期假数据用例；另一项可准备账本/sidecar 故障状态与相对 `appkey_file` 的假数据测试。共享文件由 P1 协调，WMI 另行定位；不引入新 workflow 或隔离框架。
- **P3 复核与验收**：独立复核外部真实目标写授权、缺失别名、Windows 卷/源目标绑定、sidecar 自动 reload/重启、账本失败告警与无盲覆盖、单实例和同 socket/HANDLE、Mac 链接及数据连续性；完成下节测试。失败/UNKNOWN 阻断未经验证的写入、路由状态发布和部署，**不违背已批准的账本异常不中断模型对话**。记录未覆盖环境；测试不等于部署授权。
- **P4 部署门禁**：方案确认、实现和独立复核通过，Mac 及原生 Windows 指定闭环达到下节标准，WMI 对实际控制链问题有独立处理或用户明确接受受限范围；再与用户单独确认迁移、窗口、目标及回滚。当前不部署、不操作生产、不自动重启。

**交付物规范**：原轮唯一新建、本轮唯一订正 `W/docs/designs/2026-11-16-model-proxy统一运行目录与外部权限边界修订.md`，沿用 `W/docs/designs/<日期>-<主题>.md`，`status: draft`；消费者为用户和后续实施/复核者，本轮不改 README 索引或其他记录。后续计划新增的长期运行目录 `W/.runtime/` 及其消费者见 §2，名字固定；运行数据忽略版本管理，测试仅使用 W 的测试专用假配置/假路径和新端口，GitHub Actions 假数据 fixture 使用 runner 自建临时目录；不新增长期框架或额外产品文件。若实施需要新增长期源码、CI 配置或其他目录，先列路径/用途并按项目 README 产物索引登记，不借本稿自行创建。

## 风险、未解决问题与待用户决策

1. **已决定，不再反问**：完整项目交付、§5 所列跨文件系统外部目标（含明确选择的外部 sidecar 和外部账本）短期同目录暂存，以及新备份优先受保护 `.runtime`。待实测的是同文件系统和跨文件系统实际原子提交、暂存私密保护、故障/清理状态、跨卷备份与恢复，以及现有 Mac 链接兼容；不能证明则阻断，不静默降低保护，也不保证没有路径竞态等残余风险。
2. **待事后对齐**：部署窗口、必要日志/备份的保留范围、完整交付在本机的具体物理切换及回滚方法。特别是共享 Git、嵌套 worktree、未跟踪业务数据如何原样保全必须先验证；不要求用户另选“新安装或增量覆盖”。
3. 旧/新版本共享的 OS 锁发生改变，离线迁移期间**必须停旧实例与旧自动入口**；新锚锁不能证明旧版已停。版本混跑并不能被本方案全面自动预防。
4. 弱化外部高层祖先 ACL 是明确的安全取舍，不能宣称绝对路径不可替换、对所有其他用户有完整隔离或 Windows 默认目录必然成功。直接父目录、文件及项目根不满足必要保护时仍会拒绝；实际环境需原生验收。
5. sidecar 受控承接但不搬入 `.runtime`、不改 route 业务语义。UNKNOWN 时未核验内容不可热加载；新进程无法从无持久标记恢复上次提交结论。账本写入不确定时已批准保持对话、告警并暂停不安全持久化；进程异常退出前未持久化统计可能丢失，无法判断的数据不得覆盖或盲合并。回滚中新数据权威状态仍须事后对齐。
6. WMI 根因、最佳修复、实际 Windows 端到端耗时仍未知；禁用 WMI、延长超时、继承全部环境、引入依赖或改变已验证 HANDLE 身份契约均不在本稿内。不能用普通继承环境一次成功证明安全可用。
7. 现有工作树改动多、两平台状态机有耦合；同卷不同目录 Windows 原生提交、多个不存在目标别名及相对 `appkey_file` 指向一致均需实测。估时和适用平台范围要依据实施 diff、真实测试、复核结论再定。本次没有做实测，不保证业务链路、协议或历史数据无损。

## 验证方式

在方案与相应开发授权后，按用户要求使用**实际 W + 新测试端口 + 假配置 + 自建进程**；不引入新的重量级隔离框架。运行前明确 W、假 HOME/业务配置、六路径及 `.runtime`、客户端目录、假凭据、mock 地址、端口和自建进程身份，确认不指向主区/真实个人配置或服务；端口冲突则选新端口，清理只操作本次创建的文件与进程。涉及外部路径的测试用自建假目录，不碰其他用户文件；所有网络调用只指向本地 mock，严禁真实上游和真实凭据。每次留脱敏命令、源码指纹、OS/Python、端口、通过/失败/未执行/跳过、清理状态；本稿不固定 Python 版本或要求更改现有版本策略。

固定锚点必须使用 W 的真实 `.runtime`，不能在测试里悄悄换成可变配置假装覆盖固定锚点。测试前先确认 worktree 不存在正在运行或身份未知的实例，并确认 `.runtime` 及旧状态目录不会覆盖既有数据；若已有内容归属不明或状态无法证明，暂停而不是清空、改名或强行接管。假业务配置、六角色与 mock 端点使用明确的测试专用路径，测试后只清理本次可证明创建的对象。

1. **静态/单元**：路径默认/显式/坏 JSON/单缺键与四个旧 Windows 精确值；同目录不同文件通过、相同规范路径/硬链接/别名拒绝、固定锁/记录/config/sidecar/marker/备份碰撞拒绝；两平台未知身份、父目录/目标替换、已存在不安全目标、不自动修 ACL、创建时保护和清理失败。各入口采用相同固定锚点；不同端口/路径并发最多一个实例。对可能出现的 ABA 只记录限制，不声称必检。
2. **Mac 真实 W 生命周期 + local mock**：用假业务配置和新端口实际执行 `on → status → reload → restart → off`，核进程、端口、锚锁、日志/账本与退出码；构造并发两次 on、旧实例停稳前禁止混跑、陌生监听零凭据零停止。mock 对共享 Python 真实服务走实际请求/响应，覆盖现有主要 JSON 和 SSE、需要的跨协议转换、鉴权及失败路径；以请求/响应字段和流终态断言而非只看端口存活。核配置编辑提交后的 marker/reload、失败不发 reload，账本跨重启连续；测试数据不能代替生产无损证明。
3. **Mac 已确认链接回归**：实际普通/相对配置、最终链接、目录链接、多级和 `symlink/..`；链替换、断链、权限异常；读取、启动、控制、reload、配置写保持链接和目标保护。专门检查原配置路径父目录 sidecar 与实际目标父目录不混淆；控制中的配置路径漂移不能导致错实例停止。复用现有 `test_mac_config_*`，在隔离的假目录运行后再执行真实闭环，不减少原有被批准的能力。
4. **私有 GitHub Actions 原生 Windows**：私有测试仓库及授权 CI 可用；只上传明确审查过的本次源码/测试和假配置，不能上传真实配置、日志、账本、token 或凭据。按可审计清单在 runner 自建临时项目/假 HOME、假客户端目录、假 local mock、新端口；核实现有非根祖先案例不再因旧高层白名单一律失败，但直接父/目标不安全仍拒绝且未改 ACL。以真实 Windows API/真实产品检查首次私有目录及文件创建/已有文件读写、创建即 DACL、替换/权限失败与角色冲突；两个自建启动并发只能一个持锁；同一实际 socket 对端核验、伪监听拒绝，以及同一个已验证 HANDLE stop/wait。真实执行五命令和 local mock JSON/SSE/转换闭环，明确每一步执行与结果；不得拿单个 API 测试或诊断脚本冒充整链。
5. **WMI 单独复测**：沿 `core/cli/windows_command_line.py:PowerShellWmiReader` 原产品调用链，以同一自建目标、精简与普通继承环境、明确 8 秒及实际耗时重复对照，记录脱敏结果和变动条件；诊断矩阵不等于产品调用链。未证实缺项时不写根因、不直接改安全约束；若仍超时，涉及其实际控制能力的发布门禁未通过，另形成修复决策。
6. **账本/sidecar 运行时写入专项**：假数据覆盖首次创建、重复写入及两者各自并发；在启动后、提交前和提交后分别注入目标/直接父目录替换、硬链接、权限变化、暂存/close/replace/后验/清理失败和结果 UNKNOWN。核对写前失败不假报提交、写后未知不报成功或盲目重试，账本内存增量与持久化状态可区分，sidecar 失败不发成功回执、不改 route 语义；可观察竞态应阻断，短暂 ABA 和断电不冒称覆盖。真实 Mac 与原生 Windows 都对无替身产品写入检查身份、私密权限及结果，不能仅以 fake 或启动 preflight 证明。
7. **暂存/备份和跨卷**：在假目标测试同文件系统从项目内 `.runtime/tmp` 暂存及原子提交；再测试跨文件系统的外部配置、显式外部日志（含 `.logtrim`）、用户所选外部配置派生的外部 sidecar 和显式外部账本的目标旁私有短期暂存，校验实际原子替换、创建时保护、正常清理和故障/清理未知的报告。检查 sidecar 仍依配置原始路径的父目录定位、route 语义不变；账本只按显式配置指向外部。在确有两个文件系统/卷的授权环境复测，若环境不具备则标未覆盖而不是模拟成原生通过。既有旧备份不变，新备份优先 `.runtime/backups`，比较内容、私密权限和恢复结果；注入跨卷复制/保护/空间/提交/后验失败，验证失败状态、备份及目标均如实可恢复或报告未知，不降低原子性。绝不创建或备份真实 mcopilot 登录态。
8. **完整交付/回滚只用假数据演练**：构造实质一致的假业务配置、未跟踪假配置与账本、需保留日志、sidecar、旧入口/旧锁及新账期；验证先停止、禁止混跑、完整包不只靠 Git 快照、账本解析/摘要/起点、sidecar 与配置原始路径对应、新锁新记录、回滚前保存新数据；复制持有中锁、旧 PID 授权、覆盖双方数据均应被流程禁止。以自建假 Git/worktree 目录结构检查保全共享元数据、嵌套工作树及未跟踪数据的候选切换与回滚，不对真实主区做替换；文件身份、引用或恢复不能证明时停止，不以演练成功授权真实部署。
9. **外部目标写授权和缺失别名**：Mac 用假配置和假外部链接分别验证：已授权真实外部目标可按既有确认完成安全写、读取/普通对话不增加确认；仅选择链接或允许目标旁暂存但未授权其真实目标时写入拒绝，确认后将链接/目标/父身份替换亦拒绝，备份及 staging 的授权不能互相代替。对两个以上均不存在的 role，在共同已验证祖先下构造 `Name`/`name`、Unicode 组合/分解及其他无法确定等价的非 ASCII 名、Windows 8.3/`~` 可能别名和父子包含，均应保守拒绝/UNKNOWN；不同的普通 ASCII 名可通过。再分别测不存在 config/sidecar/runtime 文件之间的碰撞，不因全部缺文件而跳过；按各平台现有规则，不将 Windows 短名语义误称为全部 POSIX 文件系统行为。
10. **sidecar 可执行 UNKNOWN 序列**：用假 B/C 和本地 mock 请求，先让真实替换发生，再注入后验失败；立即发普通请求触发原 `maybe_reload`/mtime 路径，断言只使用已验证 B、不发布 C、对命令返回 UNKNOWN。分别在安全重验为 B、为 C、第三种内容/不同身份、ACL 不安全及读失败时执行手动 reload/下一 `$route`，核对只有 B/C 可发布并解除隔离、未知或第三种状态不盲覆盖、`_mem_last_seen` 和回执一致。随后重启：对受保护且格式有效的实际文件安全验证后作为新基线，不假称旧命令结果；既有启动门禁已通过但内容损坏、加载失败或不可验证时，以空 override 继续 route-pool/base 普通请求，比较既有算法与同一假配置、session 的回退选择，确认未核验内容不生效、告警存在且 `$route` 不覆盖未知文件；不得声称保留旧 override。安全确认首次缺失按原语义；另对 Windows 原有私有保护/路径碰撞启动门禁及现有源码本来硬失败的异常作对照，不绕过或额外扩充硬失败。均不触碰真实 route 或凭据。
11. **账本状态机锚值**〔假数据〕：先建立已验证 B 的请求数 100，某次累计后候选 C 为 101，模拟替换后 UNKNOWN，再产生两个请求，内存累计应为 103，模型响应均继续且有未持久化告警。重验磁盘为 C 时仅追加后两次、最终 103；为 B 时提交含三次增量的完整 103；为第三种内容/身份或不可验证时保持内存累计、暂停写入且对话继续，不产生盲覆盖或 104 的重复计数。另测确定未替换 `PENDING`、重试前父/目标变化、后验失败、正常重启和异常退出：重启只能采用经验证的 B 或 C 等实际磁盘基线，丢失的内存增量不得虚报已持久化；在身份/角色/锁等安全门禁已通过时，构造损坏账本触发 `UNAVAILABLE` 而非 `None`，验证磁盘不被覆盖、统计告警且模型对话继续；相反，角色冲突/锁或必要身份保护无法证明仍保持 fail-closed。核对服务日志状态转换及限频；离线 `stats` 仅反映磁盘快照，不应推断进程内数据均已持久化；若现有状态通道有展示则核对一致，不打印账本内容。
12. **Windows 同卷异目录及相对鉴权引用**：私有原生 CI 用假文件在同卷不同父目录实际完成已有目标 replace 和不存在目标 no-overwrite 提交，核对源/目标和两父目录真实身份、私有保护、提交后身份与清理；用另一卷或不能确认同卷的证据、父目录/源替换、同物别名、缺失目标并发出现、权限不合格作拒绝例，并注入原生调用/释放/后验 UNKNOWN，核对状态不谎报且不退化为复制覆盖。实际第二卷不可用则报告跨卷未覆盖。独立假数据演练旧/新/回滚的 CLI、直接入口/hook：相同 `appkey_file` 相对串在 cwd 不同应解析到不同假目标并阻断；cwd 相同且 `~` 所用平台用户环境一致应保持目标一致；HOME/平台用户目录变化或软链接目标漂移应被发现。只比较路径解析和假对象，不读取或复制第三方凭据内容；项目真实配置及密钥不进测试或 CI。

**证据分级与门禁**：单元/注入只能证明分支；Wine/Whisky 不是原生 Windows；上述某一测试或一次诊断成功不是全链无损。原生 CI 只代表该 runner、Python、文件系统、账户和测过的场景；Windows 11、其他卷/域/云目录、第二用户主动攻击、实际客户环境及全部协议组合未覆盖时如实列出。真正部署前另行评估目标环境与剩余风险，由用户裁决；所有未执行项不得填通过。

## 关联与来源

- 用户本轮明确的订正授权及对共享 Git、嵌套 worktree、跟踪与忽略文件的补充；前轮原生测试/route/测试范围说明。未提供的历史逐字对话不作虚构。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy-Windows适配复杂度独立复核]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy共享Python与Windows最小适配再审视]]：confirmed，仅读。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-13-modelproxy-Mac符号链接配置兼容方案]]：confirmed，保留其已批准链接能力。
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-12-model-proxy统一内核与原生Windows-CLI设计]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-16-Windows启动preflight有界收敛评估]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-16-model-proxy-Windows路径ACL默认目录兼容方案]]：历史/草案证据，原记录均不改。
- 用户授权的私有 CI 假数据 `run1.log`、`diag1.log`、`diag2.log`：只引用测试结论，不记录日志内路径和上下文。`diag2.log` 的 32 次受控试验统计是诊断，不是产品验收。
- worktree 当前源码：[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/runtime_paths.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/discovery.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/server.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/commands.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/authority.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/dispatch.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/_config_ops.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/cli/windows_control.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_paths.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_acl.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/mac_config_writer.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_secure_native.py]] 及 §6 列明的其余入口/测试。
