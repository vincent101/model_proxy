---
type: design-decision
status: draft
target: "[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli]]"
tags:
  - architect
  - model-proxy
  - windows
created: 2026-11-20
---

# Windows 配置与 sidecar 校验收敛诊断

## 背景与问题

目标是在既有权限与私有对象保护下，找出类似 macOS `com.apple.macl` 过度校验、会阻碍正常有权运行的 Windows 路径、ACL 和元属性检查。**只诊断和提出候选，不修改产品、测试或既有设计；不访问生产服务、真实配置或凭据。** 原生 Windows 未在本轮运行。

作用根 W：`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli`；检查版本 `2450c38`，版本号 0.14。已有未跟踪的 macOS 设计记录不属于本轮，不修改。当前环境没有可用的 architect/reviewer 子代理；本稿由主会话直接诊断，不冒充独立复核。

### 证据分层

1. **本轮本机 fake 实验**：以现有 `WindowsConfigSnapshotTests` 内存文件系统，将假卷根 owner 改成非白名单主体，目标与项目边界仍满足 fake 私有 ACL。`read_stable_config` 返回 `config_read_error`；同一目标用现成 `read_guarded_file(..., trusted_boundary=project_root)` 成功，假句柄数归零。只证明代码接线差异，不证明真实根 ACL 或原生 API 行为。
2. **本轮本机 fake 测试**：8 个指定用例通过，覆盖 ACL 拒绝、reparse 拒绝、配置读取、sidecar fake 新建/替换，以及 sidecar 后验 `ChangeTime` 漂移变 UNKNOWN。包括旧测试要求拒绝，不代表该要求合理或 Windows 本机可用。首次启动默认 Python 3.9 因类型注解不兼容而无法加载测试；改用 Python 3.13.5 后通过。
3. **既有历史记录，非本轮复测**：[[2026-11-16-model-proxy-Windows路径ACL默认目录兼容方案]] 记载某次 Windows Server 2025 私有 CI 的 D: 卷根被旧严格 ancestor 策略以 `owner_untrusted` 拒绝；只读重放及主体/掩码存在记录所述限制。该结果不能推定其他盘或所有 Windows 机器失败。
4. **官方语义与静态推断**：Microsoft `FILE_BASIC_INFO` 说明 `ChangeTime` 反映元数据变化，包括重命名和属性变化；它不是只读内容的版本号。不能据此断言每次 Windows `MoveFile`/`ReplaceFile` 都改变它。本轮未做原生 rename 实测。

## 方案设计

### 1. 优先候选：把已批准的边界接到配置读取

**发现**：`core/config_snapshot.py:91–98` 调用 `read_guarded_file` 时未传 `trusted_boundary`。`platform_windows.py:1355–1384` 因而使用 `windows_paths.inspect` 默认策略；`windows_paths.py:477、509` 在缺边界时从卷根开始逐级执行严格 ancestor ACL。相反，`discovery.py:253–266` 与 `server.py:4116–4137` 的启动路径预检已选择项目根或授权外部目标直接父目录作为边界；sidecar 的 `capture_target` 则通过 `_read_target` 传递边界。

调用链包括 `server.py:783–788` 的业务配置读取与 probe，`ConfigStore` 的初始化、自动/手动 reload，以及 `server.py:4531–4564、4802–4870` 的 runtime-path 配置确认。由此可能出现：预检已按边界通过，但后续读取仍因卷根/高层祖先的 owner 或 ACE 失败；启动受阻，或已经运行时保留旧配置、热重载不生效。外部配置也受同一遗漏影响。不是证明用户实际遇到该故障。

**最小候选**：只给 `config_snapshot.py` 的 Windows 稳定读和 marker probe 增加可选内部 `trusted_boundary` 并传给现成 `read_guarded_file`；在 `server.py` 的**有明确项目根和目标授权**的业务配置、runtime-path 配置、相应启动/迁移复核位置传递与预检相同的选择。检查 `server.py` 其他 ledger/legacy marker 调用，只有其已有受信角色与边界可证实时才明确接入；未经判定的调用保留严格默认，不按任意用户路径自动降级。外部配置采用已明确授权目标的直接父目录，路径大小写/8.3 歧义沿用 `_windows_trusted_boundary` 拒绝规则，不用字符串猜测外部授权。

不得绕过卷类型、持有句柄的路径绑定、reparse/类型、目标私有 ACL、所需访问、身份/内容稳定性或关闭错误检查。边界以上从严格 owner/ACE 转为既有 identity-only traverse 是**安全取舍**：不再声称高层祖先满足旧 ACL 白名单，绝非安全等价；路径重走也不是 CAS，不能排除同用户 ABA 或最后校验后的替换。此取舍与 [[2026-11-16-model-proxy统一运行目录与外部权限边界修订]] 的已确认方向一致，具体接线仍待用户确认和复核。

### 2. sidecar 提交后标记：有根据的兼容候选，须先测真实行为

`windows_secure_native.py:480–510` 对不存在目标使用 move，对已有目标使用 `ReplaceFileW`；`windows_secure_files.py:495–503` 已用目标 postcheck 核对 stage 身份、内容和保护。随后 `sidecar_secure_io.py:626–643` 又要求 postcheck 的**完整 marker 与提交前 stage 完全相等**，并以提交前 marker 构造 C；其中 marker 包含 `ChangeTime`。若合法 rename/replace 仅使该时间变化，源码路径将把已提交的 sidecar 报为 UNKNOWN，保留 B、隔离后续写；旧 C 的完整 marker 也可能不能与磁盘重新捕获匹配。此为官方语义支持的**条件推断**，不是本轮 Windows 观察。

**候选**：仅在 native commit 返回成功、有限写入成功、现有 postcheck 已验证 stage 文件身份、预期字节和私有保护后，以该 postcheck 的真实目标 marker 建立 C；然后再次安全捕获目标，要求身份、字节、保护和**postcheck 后 marker**完全一致再发布。提交前的 stage marker 仍用于提交前稳定检查；commit 调用抛错、关闭/清理不确定、postcheck 或再次捕获失败时仍 UNKNOWN，不由“内容相同”猜测成功，也不借此次改动放松 B/C 恢复规则。目标/父路径与角色碰撞、私有 ACL、句柄释放语义不变。

`_finite_write` 的 `FileIdentity` 还包含完整 attributes；替换前后属性若被系统改变也可能导致较早的 postcheck 失败。`platform_windows.py:237–248` 会比较同一文件两次 attributes；这不是无条件拒绝普通 archive/hidden/compressed 等属性。**本轮没有证据证明某个普通属性必然触发失败，也不建议整体忽略属性。** READONLY 对文件写/删除有实际影响，directory READONLY 语义不同；reparse 影响目标解析。应先用受控 fixture 分别测状态变化、ACL、最终内容及元属性保留，再对具体可变位定范围；不要把只读文件的真实系统拒绝伪装为兼容 bug，也不要静默修改属性。

Windows 账本 `ledger_secure_io.py:269–290、400–414` 的身份比较并不使用 marker；不得照搬 sidecar 的假设或盲目修改账本。仍应在共同 writer 回归中确认账本正常、不重复累计和 UNKNOWN 行为。

### 3. 暂不扩大 ACL 与路径支持

- `windows_acl.py:107–122、154–178` 拒绝所有 deny/非普通 ACE、未知主体，也逐条检查 `INHERIT_ONLY`；即使当前 token 的 `AccessCheck` 可成功，也可能拒绝合法 ACL。继承并不等于当前对象的生效授权；但是子对象可能继承，且当前 token 有权访问不证明其他主体无法读写。应先测具体 ACE 与受影响对象，不把所有 deny、组、继承 ACE 自动视为安全，也不自动修改既有 ACL。已有敏感目标私有保护和新建敏感对象从创建时保护保持不变。
- `windows_paths.py:92–97、216–217` 对每一祖先打开时请求 `READ_CONTROL | FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY`，即使 `traverse` 只承诺身份观察；不具目录枚举权但可按已知路径访问的账户**可能**被阻断。它还持有不共享 delete 的句柄，会影响并发重命名；这是防替换设计的一部分。先对照实际调用和原生 fixture 测最小访问掩码与共享兼容，不全局改成零权限或共享 delete。
- 同文件 `159–179、278–290` 只接受固定本地 NTFS、特定卷设备格式、无 reparse；`windows_acl.py:125–135` 对文件一律要求单硬链接。UNC、SUBST、非 NTFS、junction/云重定向、硬链接可能合法可用，但改变这些边界会影响路径身份、别名与竞争安全。先报告限制，不在本候选放开。

## 风险与权衡

1. 两项候选都不是用“当前用户有权读写”替代目标私密性；前者改变高层祖先信任范围，后者改变成功提交后如何选定 C 的观察时点，不能标为完全等价。
2. postcheck 前后身份、ACL 或内容变化，无法确认是否提交，或清理/释放未知时保持 UNKNOWN；不能重试覆盖、静默回滚或发布未核验内容。未被识别的同用户竞争仍是既有非 CAS 边界。
3. 配置 reload 失败沿用旧快照，并应有可辨识但脱敏的失败状态；不推进 marker。不打印配置正文、路径中的隐私、SID、令牌或凭据。`listen` 配置仍按现有规则需重启。
4. 不拟引入新安全框架、全量 ACL 解释器、管理员操作、自动 `icacls`/chmod、修改系统根或全局允许 reparse。是否扩大不可信祖先/ACE 的信任属于单独授权问题。

## 验证方式

修改须经用户确认；先做本地 fake 测试，再由用户另授权可用的原生 Windows 环境，不调用生产服务。至少覆盖：

1. `tests/test_windows_config_snapshot.py`：从真实配置入口模拟高层祖先不符合严格 ACL、边界内目标私有；业务配置初始化、marker probe、自动/手动 reload 与 runtime-path 启动复核均通过。目标或边界 ACL 不安全、reparse、路径别名歧义、父链/文件身份改变和关闭失败仍拒绝，失败不推进旧快照。
2. `tests/test_sidecar_secure_io.py`：以真实产品编排的 fake backend 在 move/replace 后仅改变 `ChangeTime`，成功 postcheck 后再次捕获一致才发布；内容、file ID、ACL 变化，commit 抛错、后验读失败和关闭不确定仍 UNKNOWN、不盲写。为原有 `test_windows_writer_postcheck_rejects_changed_stage_marker` 调整断言前须区分 rename 时预期变化与 postcheck 后的额外漂移；保留后者拒绝和 B/C 安全负例。
3. `tests/test_ledger_secure_io.py` 或既有 writer 测试：共享 writer 没有使账本重复累计或把 UNKNOWN 当成功，且 Mac 行为不变。
4. 原生 Windows 最小 fixture：自建本地 NTFS 私有目录、假 JSON 和独立对象；采集 move 与 `ReplaceFileW` 前/后文件 ID、LastWriteTime、ChangeTime、attributes、DACL 与内容，分别覆盖缺失/已有目标及正常可读写属性；测拒绝的 ACL/reparse 与实际访问权。只报告所测系统、账户、Python、卷和结果；没有原生环境则该项未执行，不把 fake/Wine 结果当原生通过。

**交付物规范**：仅本单份 draft 落 W 既有 `docs/designs/`，命名按该目录日期主题约定；候选只改既有产品/测试文件，无新产品产物。实现、复核及任何原生 CI 不在本轮执行。

## 关联

- [[2026-11-16-model-proxy统一运行目录与外部权限边界修订]]
- [[2026-11-16-model-proxy-Windows路径ACL默认目录兼容方案]]
- Microsoft Learn：`FILE_BASIC_INFO`、`File Attribute Constants`、`ACE Inheritance`、`File Security and Access Rights`。
