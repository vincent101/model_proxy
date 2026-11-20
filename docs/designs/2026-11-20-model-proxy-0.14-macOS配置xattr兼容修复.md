---
type: design-decision
status: draft
target: "[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli]]"
tags:
  - architect
  - model-proxy
  - macos
created: 2026-11-20
---

# model_proxy 0.14 macOS 配置 xattr 兼容修复（务实，待用户确认）

## 背景与问题

目标：优先消除本机业务配置读取、启动和热重载的已知属性阻塞；配置写入不得因放宽读取而丢失属性、覆盖错误对象或弱化备份。本轮只设计，不实施、不启动服务、不读取或输出业务配置；未来上线、生产操作另行授权。

**已确认但非本轮复测**：用户提供 Documents、NoteVault 祖先带 `com.apple.macl`，业务配置目标带 `com.apple.macl` 和 `com.apple.lastuseddate#PS`；0.14 的 `capture_config` 报 `directory_attributes_unsupported`，直接目标文件保护检查报 `extended_attributes_unverified`。`config_unverified` 是多种失败的外层汇总，不足以单独证明唯一原因。

**静态代码证据**：`core/mac_config_path.py:67–128` 的目录 xattr 白名单排除 macl，且从根开始遍历时逐层调用；`core/secure_files.py:509–556` 的文件 xattr 只接受 provenance。二者是独立阻塞。`mac_config_path.py:131–177` 对链接自身也有 xattr 白名单；链中链接可能是第三个阻塞。`core/cli/authority.py:83–85` 把 capture 失败汇总为 `config_unverified`。`mac_config_writer.py:227–248` 用 `_open_private` 创建备份和新文件；`secure_files.py:649–671` 只比较新建文件属性，**不复制源属性**，所以仅放行读取后直接写会失败，或在随意删比较时丢属性。

本机 `man fcopyfile` 显示 `fcopyfile` 接收已打开 FD，`COPYFILE_XATTR` 是复制扩展属性的候选原语；这不是所有属性可复制、含义可迁移或复制成功就保真的证明。macl 不等于 POSIX 模式位或可由本方案解释的完整系统 ACL。不能断言它无安全作用、恶意或在新 inode 上语义相同；也不能从名字推断任意未知属性或系统 ACL 的效果。

## 方案设计

### 选择与适用边界

推荐 **A：配置读取按路径身份和必要保护检查，不按 xattr 名称判恶；写时单独尝试保真并验证**。B 是给三个名称加白名单：难覆盖链接、未知属性和后续系统变化，且不解决写备份；C 是重新设计完整系统 ACL/xattr 安全框架：超出目标、也无法证明全部平台语义。不选 B/C。本方案仅改变 Mac **业务配置**链路，不调整 Windows、客户端安装文件、sidecar、账本、`.runtime` 的共享保护策略；不自动改 chmod、ACL 或真实配置属性。

已确认的 [[2026-11-16-model-proxy统一运行目录与外部权限边界修订]] 和 [[2026-11-13-modelproxy-Mac符号链接配置兼容方案]] 继续适用。本文是对业务配置 xattr 处理的待确认变更，不改变已确认的写授权、固定实例锚、sidecar 定位、受保护暂存/备份、跨卷例外及失败状态协议。目标是去掉误拦截，不把 macl 的存在当作安全证明。

### 1. 读、启动和热重载

- `core/mac_config_path.py`：继续通过已打开目录 FD 逐段走真实路径，保留绝对/相对路径、`..`、多级和最终 symlink 的真实内核语义、环/断链处理、链接文本与 inode 等身份记录、两次 walk 和可观察变化重验；不要先 `realpath` 后绕过链，也不要把路径文本相等当身份相等。配置链上一般高层祖先仅做对象身份/类型与必要的路径可达性和稳定性记录，不逐层要求旧 ACL/xattr 白名单；保留 sticky 目录入口的现有必要归属检查。项目根仍由现有 authority/项目根路径检查承担，不凭配置链接目标另创 authority。**真实目标直接父目录**仍按既有 owner、不可随意写、类型、FD 身份与可验证 ACL 等必要门禁检查；目标文件仍验证普通文件、owner、mode、flags、现有 ACL 门禁及 FD 身份。原有读与写对硬链接的区别不扩权，写仍要求 nlink=1。无权访问、身份/必要保护不可验证仍拒绝。
- 业务配置的目录、链接及目标文件：xattr 名称或值本身不再是读取门禁，也不把任意 xattr 原值或其哈希纳入热重载 marker；不改变 `_directory_attributes` 函数的既有语义，因为 `.runtime` 的 `_directory_proof` 还直接调用它。系统更新 `lastuseddate#PS` 不应单凭该属性值触发无意义重载。保留现有 inode/链、文件数据和必要身份元数据变化检测；若系统更新导致现有 ctime 检测不稳定，须在假数据原生测试中单独查明，不能未经证据就取消身份重验或宣布已解决。
- 链接自身保留现有类型、身份、链接文本、ACL/文件系统及两次重验；去掉**业务配置链接**对属性名称的无依据一票否决，但不声称链接的未知属性没有系统作用。目录/链接的 ACL 不因其为系统产生而一律信任；必要边界处维持现有可验证 ACL 政策，对高层未检查 ACL 的残余风险如实说明。
- `core/config_snapshot.py`、`core/cli/authority.py`、`core/server.py:771–793,910–980` 继续共用同一配置快照。普通重载仅发布成功稳定读取的配置；读取/验证失败不推进 marker、不替换已发布配置；手动重载报告失败。保持既有启动 fail-fast 和现有业务字段校验行为，不借本修复改配置校验或服务控制。普通路径、项目内/外最终目标均适用同一读取语义；读到外部目标不自动授权写。

### 2. 写与备份：先证明属性可保真，再提交

- 保留 `WriteAuthorization` 对**实际目标及直接父目录身份**的授权，保留冲突 preflight、原路径 sidecar 位置、独占私有创建、已批准的 `.runtime/tmp`、`.runtime/backups` 及必要跨卷目标旁暂存。原链接不替换，写的是已授权真实目标；外部目标只在现有明确授权条件满足后写。暂停非协作编辑者；已有检查不是内核 CAS，无法保证最后检查与替换之间无人竞争。
- 写事务中，以已验证源 FD 对目标文件做**单次事务级**完整 xattr 快照与稳定性检查；这是写入保真数据，不作为全局热重载证据。对源、备份、暂存分别核对可见属性名称集合与逐项原始字节，且对照数据、FD/路径身份、mode、owner、ACL、文件类型、link count 和适用文件系统；后验前重验源和两个产物。属性值只在受限内存中处理，禁止日志、异常、设计记录输出；读/列失败、竞态、大小/资源上限、能力未知均明确失败，不漏掉未知属性或把未读取当作空集合。
- 候选实现：对已经独占创建且验证为私有的备份/暂存 FD 使用 `fcopyfile(..., COPYFILE_XATTR)`，**不使用 COPYFILE_ALL/SECURITY 覆盖私有权限或 ACL**；复制后重新验证权限及 ACL，确认私有后再写入敏感数据；随后按实际读取集合和字节核对，不能只看返回 0。目标 FD 有系统自动附加的属性、受保护属性不能复制、源属性在复制前后变化、两端不一致、跨卷过滤或不可读取时，不猜测、不删除系统属性来凑一致、不降级为仅内容备份：提交前拒写并报告备份/清理实际状态。若未来经假数据证明本机原语不支持某属性，不宣称可写；可另提有证据的最小方案，须用户确认。
- 备份要有旧内容及其验证过的完整可见 xattr；暂存要有新内容及相同可见 xattr。二者独立校验，备份跨卷不能冒充 rename 原子；替换前再次验证源、备份、暂存和授权；替换后验证最终 inode、新内容、保护及该可见 xattr 集合，并再次验证备份。保留 `NOT_REPLACED`、`UNKNOWN`、`REPLACED`/后验失败和 `VERIFIED` 的真实状态，不把已替换后的失败说成未修改，也不盲目回滚。失败清理仅针对本次确认创建的对象，清理/关闭未知分别上报。若系统属性会在访问后自行变动导致精确一致无法验证，明确拒写；不擅自把 `lastuseddate#PS` 排除出保真。
- **可证明范围**：可比较当前进程可列出、可读的属性字节和权限证据；字节一致也不能证明 macl、未知属性在新 inode 上所有 macOS/TCC 行为不变，隐藏属性和不可观察状态无法保证。原生测试只能验证指定机器、文件系统、进程权限和样例；无法验证的操作保留不确定性，不能宣传“完全系统语义无损”。

### 3. 文件级修改点与影响隔离

1. `core/mac_config_path.py`：局部调整 walker 的高层身份证据和真实父目录保护证据、链接属性策略；目标文件通过显式的业务配置读取选项取得无 xattr 名称门禁的保护证据。保留 `_directory_attributes` 给现有消费者，不全局删白名单。
2. `core/secure_files.py`：若复用 `_mac_fd_protection`，仅加**显式、仅业务配置调用**的文件 xattr 策略参数，默认保持严格旧行为和现有返回 tuple 布局；目录默认和所有非配置调用不变。不得把忽略属性后的空 tuple 当作源的完整属性证据。`_open_private` 默认及 `mac_private_protection`、`_directory_proof`、`secure_replace_runtime` 保持旧策略；业务配置另加局部 FD 创建/复制能力，或以显式选项复用，非配置默认调用必须保持不变。若不影响共享调用，可将配置专用小 helper 放 `mac_config_writer.py`，避免框架化。
3. `core/mac_config_writer.py`：不再把 `snapshot.protection[1][-1]` 当作属性全量，单独获取/复制/验证写事务 xattr；修改 `_check_created`、备份、暂存、最终后验比较。保持先验证私有再写数据、授权、角色、同卷、marker 及 UNKNOWN 清理协议。
4. `core/cli/dispatch.py`、`core/cli/_config_ops.py`：核对写前/写后比较及 helper、marker、reload 的快照契约；若字段布局不变且能正常工作，则不改。`core/config_snapshot.py`、`core/server.py`、`core/cli/authority.py` 同理，必要时仅修改适配/错误传播，不额外引入状态机。`core/darwin_acl.py` 不改；不放宽共享 sidecar/账本/安装文件和 `.runtime` 检查。
5. 修改既有相关测试并增加业务配置假数据案例：`tests/test_mac_config_path.py`、`tests/test_mac_config_writer.py`、`tests/test_mac_config_links_integration.py`、`tests/test_mac_config_server_integration.py`、`tests/test_mac_config_control_symlinks.py`；共享隔离与 `.runtime`/sidecar 回归覆盖 `tests/test_mac_runtime_staging.py`、`tests/test_mac_private_config.py`、相应 secure/sidecar 测试。原本“未知业务配置 xattr 必拒”断言按新业务边界替换，而不是删除所有安全测试。

**纸面推演（非实测）**：真实目标经项目内链接指向项目外，祖先含 macl，文件含 macl+lastuseddate。读取：链和目标身份稳定、父/文件必要保护通过时，不再因属性名称拒绝；sidecar 仍在原配置路径父目录。写：即使读成功，仍需外部实际目标授权；如新 FD 不能复制或不能逐项验证 macl，则目标不替换。反例：只对祖先加白名单仍在文件上失败；只跳过目标的 xattr 检查并继续旧写法可能丢属性。最后检查后的链竞争、系统对相同字节的新 inode 解释不同，均不在可保证范围内。

### 交付物规范

实现预计只改既有源码和测试；**无新运行产物**。本方案本身为用户指定工作区 `docs/designs/` 的单份新设计记录，按现有日期+主题命名，不修改 README 或其他旧文档。

## 风险与权衡

- 本方案有意不再对高层祖先全部 ACL/xattr 作安全判断，降低误阻塞，同时无法保证可写上层不会在检查后替换路径；保留直接父目录和目标的必要保护、身份复核及现有项目根 authority，不宣称防同用户或瞬时 ABA。
- 当前观测的属性组合并不证明新代码能在本机写入；`fcopyfile` 的复制、macl/lastuseddate 的可读取、可复制和稳定性均待原生假数据验证。甚至读取也可能被独立的 ACL、`.runtime`、sidecar 或其他条件拦截，不能预告启动必成功。该记录不授权对现有保护或已确认边界作进一步放宽。
- 若写操作需要按名称忽略某属性或接受复制前后不一致，属于降低保真/恢复保证，必须先展示实测证据、影响和替代方案，经用户明确裁决；本稿不默认批准。可观察字节一致仍不等于所有系统授权语义一致。

## 验证方式（后续实施；本轮未执行）

1. 静态/单元：高祖先、直接父、目标和链接分别构造 macl、lastuseddate 及未知 xattr；读取不按名字拒绝。目标 owner/mode/flags、实际父目录不安全、ACL 不可验证、断链、链替换、目标/父 inode 替换、数据或权限变化仍拒绝；高层变化可观察时重验拒绝。测试项目内、项目外、相对、目录/最终/多级链接和 `symlink/..`；写外部未授权必拒、授权后仍操作实际目标，不动链接及原路径 sidecar。
2. 独立原生 macOS + APFS + 临时假数据：由系统实际产生 macl 的假目录/文件（如需用户交互单独获授权），而非用伪造字节冒充系统产生；同时人工建立可写自定义 xattr 和 lastuseddate 样例，注明其来源、是否由系统产生及不能建立的场景。只打印属性名称、比较结果和错误码，不打印值、配置或凭据。测试与实际进程身份、目录可访问性一致的可控假环境；系统未产生对应属性、无法复制或无法设置时标记未覆盖/失败，不作通过。
3. 逐项实际测试源 FD→独占备份/暂存 FD 的 `fcopyfile COPYFILE_XATTR`，比较前后完整可见名称与值、mode/owner/ACL、旧/新内容及恢复可读性；在读取/多次复制/重载/跨卷场景中测属性是否波动。无法建立真实跨卷则明确未覆盖。注入枚举失败、值变化、额外属性、复制 API 部分失败、不可读/不可复制、后验不一致、身份替换、清理/close 未知；替换前失败断言原文件未变，替换后失败按状态报告，不假装回滚。
4. 热重载使用假配置：内容/目标身份真实变化才按既有规则发布；仅属性值变化不应因属性值本身引发重载，验证反复读取时是否由于系统改变 ctime 造成额外变化；读失败保留旧配置且不推进 marker；修复后重试成功。控制、helper、marker 和原链接不变。
5. 执行现有 Mac 配置路径/链接/写入、sidecar、账本、`.runtime` 与 Windows 回归，核对共享函数默认仍严格、原证据 tuple 调用方未损坏。允许 mock/unit 证明分支，但不把 mock、单次属性复制或非原生平台当作实际系统权限与全链验收。只在临时目录和 mock 环境演练；任何服务闭环仅限未来获授权的隔离假服务，绝不运行或连接生产。

## 关联与证据

- 已确认边界：[[2026-11-16-model-proxy统一运行目录与外部权限边界修订]]；链接兼容：[[2026-11-13-modelproxy-Mac符号链接配置兼容方案]]。
- worktree 源码：[[core/mac_config_path.py]]、[[core/secure_files.py]]、[[core/mac_config_writer.py]]、[[core/config_snapshot.py]]、[[core/server.py]]、[[core/cli/authority.py]]、[[core/cli/dispatch.py]]、[[core/cli/_config_ops.py]]、[[core/darwin_acl.py]]。
- 本轮仅静态阅读以上源码与既有设计、查看本机 `man fcopyfile` 和 worktree 状态；用户提供的真实属性与报错为前期已确认信息，未重新触碰真实业务文件。无运行服务、无配置内容输出；上述测试均未执行。
