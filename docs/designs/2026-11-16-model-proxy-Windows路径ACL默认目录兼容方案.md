---
type: design-decision
status: draft
target: "[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli]]"
tags:
  - architect
  - model-proxy
  - windows
created: 2026-11-16
---

# Windows 路径与 ACL 默认目录兼容方案（draft）

## 背景与问题

目标仍是**共享 Python 业务及 CLI、薄 sh/bat**；让普通 Windows 默认目录可用，同时不绕过配置/私有文件保护，保留真实同 socket peer 与已验证同 HANDLE 停止。本文只设计路径和 ACL，WMI 另案；不改变产品、线上配置或既有 confirmed 记录。**任何产品 ACL/祖先策略放宽均须用户明确裁决，且 CI 补测不能代替裁决。**

工作树记为 `W=/Users/vincentwang/Documents/NoteVault/tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli`。来源：既有 confirmed [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy-Windows适配复杂度独立复核]]；本轮只读离线 `/private/tmp/modelproxy-ghci.OSXGDnWO/run1.log`、`diag1.log` 与指定工作树源码，未运行产品、CI 或 gh。

### 实测、源码和推断分层

1. **实测**：授权的私有 GitHub Actions Windows Server 2025/Python 3.14.10 上，`run1.log:226–227` 产品目录、文件首次创建均 `observation_unavailable`，`created=False`；第 436 行 fake-home 集成 fixture 失败，`created: []`。这说明该批创建没有通过，不说明产品全部行为或所有 Windows 均失败。
2. **实测**：`diag1.log:147–156, 300` 同一自建目标 baseline 与 fake-home child 都在卷根 component 0 的产品 ancestor 观察先报 `owner_untrusted`；`158–162, 303–306` 是**非原子只读重放**，不是同一次证明。`185, 329` 根 ACL 原始汇总有 unknown principal、public write/excess rights；根 identity 汇总无 reparse、是目录且 file index 非零。原始汇总**不区分** owner 是谁、每条 ACE 的主体/掩码/继承 flags 和有效权限，也不证明 public write 是否可删除已有子目录。不要把 `last_error_hint_not_attributed` 当作故障原因。路径位于 runner 临时盘 `D:`；这是**卷根**，证据没有证明它是 Windows 系统安装盘根，更不代表所有机器 `C:\` 必然相同。
3. **源码**：`core/windows_paths.py:441–481` 对每个绝对盘符路径从盘根先按 `ancestor` 观测，再逐段打开；`core/windows_acl.py:138–178` owner 只能是当前 token user、SYSTEM、Administrators，且 ancestor 的 Everyone/Users/Authenticated Users 仅允许 read/traverse；其他允许主体、deny/不支持的 ACE（包括 inherit-only）也拒绝。`owner_untrusted` 出现在 ACE 检查之前；**只加 owner 白名单并不解决已观察到的其他潜在拒绝**。`windows_paths.py:411–419` 将内部异常收敛为 `observation_unavailable`，因此产品外层不直接显示底层 owner 原因。
4. **因果链〔实测+源码推算〕**：目标不论是否位于假 HOME，只要路径同卷且 `_walk` 首先检查同一卷根，都会先遇到该根的策略拒绝；换 HOME 无法消除。默认运行时目录映射到 `LOCALAPPDATA\model_proxy\runtime`，固定状态目录位于项目根下，安装会触及 `Path.home()` 下客户端目录；这些路径同样先经过各自卷根。但**不能从 runner 结果推断普通用户默认 profile 的每一层都符合或不符合策略**，也不能声称只修盘根即可成功。
5. **未证实**：产品链在根即终止，现有证据不足以知道 `Users`、profile、`AppData`、`Local`、项目目录、客户端目录等更深祖先逐层 owner/ACE/继承/有效权限、reparse 和身份是否通过；自建最终 fixture 的观察不等于中间祖先通过。产品在其余 Windows 版本、安装方式、ACL 配置与真实客户端文件上未验收。

## 方案设计

### 安全语义：三个问题不能互相替代

- **新敏感对象**：先确认可信、稳定的直接创建父目录及创建权限；不存在不等于安全。新文件须 `CREATE_NEW`，创建时即使用明确的受保护用户 DACL，不能先公开写数据、再补 ACL；新目录同样创建即私有。已有目录不自动修 ACL，不能覆盖已存在文件；创建后核查同对象身份、owner、ACL、内容/大小与路径绑定，失败报告创建状态，不写敏感数据。`platform_windows.py:1402–1480, 1514–1528` 已有大部分机制，直接复用。
- **已有敏感对象**：不得把“目标自己 DACL 私有”当成父链安全；现有文件独立检查 owner/DACL、单链接、身份及内容，既有目录按其用途检查；无法验证即拒绝，不默默改 ACL、覆盖、把新文件规则套到旧文件或降级。`windows_secure_files.py:152–195` 的授权根和目标前缀/链一致性仍须成立。若已有客户端文件默认 ACL 被拒绝，只报告、给诊断，不默认修复。
- **身份、ACL、路径各有边界**：持有 HANDLE 上的 volume/file ID、类型、link count、reparse、owner/DACL/AccessCheck 是对象观察；路径各段对照与重走用于检测路径指向发生变化。只比较路径字符串，或只检查 ACL/hash/只检查有效 token，均无法证明三者绑定。`windows_paths.py:216–257, 545–570` 的持有句柄、绝对路径前后检查和 revalidation 应保留；它们不是原子相对父句柄打开、不是 CAS，无法防同用户/特权 ABA 及最后检查后的变化。

**Windows 权限事实**：新对象继承 ACL 与创建时显式 descriptor 是不同路径；父目录权限不等同于子文件 DACL。父目录 `FILE_ADD_FILE/FILE_ADD_SUBDIRECTORY` 是创建入口，`FILE_DELETE_CHILD` 可影响子对象删除；还需考虑对象 `DELETE`、`WRITE_DAC`、`WRITE_OWNER`、更高层可删除/替换下一级、重解析和攻击者预先抢占名字。见 Microsoft [File Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights) 与 [File Access Rights Constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-access-rights-constants)。`AccessCheck` 通过当前 token 只证明当前 token 能做什么，不证明其他用户不能做什么。ACL 中有 write 并非自动证明某人能改某个既有子项；也绝非可直接忽略的证明。

### 选择 A：窄化兼容方案（条件推荐；当前尚未授权实施）

**先只读补测，再由用户对具体扩权拍板；若测得的根 ACL 不满足以下条件，停止，不做“例外通过”。** 保持现有路径租约与私有对象策略，以最小规则区分“受严格约束的卷根”和其后所有祖先：

1. 第一轮候选仅覆盖**经过可信 OS 信息确认的系统安装卷根**，而非按路径长相、环境变量、盘符、`LOCALAPPDATA`、“本地 NTFS”或根名自动信任任何目录；卷根身份、类型、卷、非 reparse 等仍须由持有句柄检查，不能由字符串代替。当前 CI `D:` 是否为系统卷**未知**：若不是，A 不承诺解决这个 CI 路径。将范围扩展至其他固定本地卷是独立政策决定，既不能自动推导为可信，也不能承诺普通用户多盘可用。
2. 根 owner 只在只读证据确认其真实主体、并由用户明确接受该具体系统主体后，加入根专用信任规则；不是把所有未知 owner、管理员之外的任意服务、SID 字符串或当前机器观察到的任意主体一概视为可信。若无法确认其能力/身份，拒绝。用户信任 SYSTEM/Administrators之外的系统服务，意味着也信任其更改根 DACL 的能力。
3. 根 DACL 必须有完整、可解析、稳定的观察。对每条不属于已批准可信主体的 ACE 及其有效/继承含义，明确判断：只允许已批准且**不允许其删除/替换或控制已存在下一段名称、根自身 owner/DACL、重解析行为**的窄权限。公共仅新增无关根子项的权限，与 `DELETE_CHILD` 或可改安全描述符的权限分别处理；同一 ACL 若存在不明 ACE、无法排除危险有效权限或不能正确处理 deny/条件 ACE，保持 fail-closed。inherit-only 如果拟豁免，仅可在确认不影响当前目录、后续每个现存子对象均按各自策略检查、且新敏感对象创建即 protected DACL 的前提下，单独列明主体和类型经用户批准；不能忽略未来子对象上的实效。
4. **绝不让放宽后的卷根充当敏感对象的创建父目录**；根下下一段必须已存在，且其对象 ACL、可删除性及整条后续路径仍按既有严格 ancestor/creation-parent/private 检查。若公共 `DELETE_CHILD`、不可信者对第一子项的 `DELETE`、可以取得控制权，或无法排除，则拒绝。名字抢占/已有对象替换不能仅靠“该对象看着私有”解决。后续任何目录若也不符合既有严格策略，先停下取证并另请裁决，不把根例外向下扩散。
5. 不新增产品 unsafe 开关、自动修改 ACL、管理员权限要求、完整 ACL 引擎/平台框架；不调整共享内核、sh/bat、socket 或 stop 身份合同。`core/windows_acl.py` 只在明确定义的根证据范围内增加候选判定，`core/windows_paths.py` 只把已核实的根身份、规则和完整原始保护证据贯穿首次检查、`_check_parent`、`_walk`、重走与 revalidate；不能从 `ancestor` 全局放行。`core/platform_windows.py`、`windows_secure_files.py` 现有最终对象与创建规则不得因为新根规则放松。先设计和测试证据表示，确保同一对象跨 policy 的观察相等性与保护变化仍可检测；不以漏记不认可的 ACE 换取一致性。

**可行性条件**：具体 Windows 根/更深目录的 ACE 明细和身份尚未测到。若公共根权限确有有效 `DELETE_CHILD`、危险控制权，或现有观察不足以可靠判断有效性，A 当前不成立；不以路径重走伪装为完全排除抢占。若只改根无法通过更深层，必须另出范围和授权，不扩大此候选。该方案是“规则较少、边界受限”的务实候选，不保证一次补丁就让典型 Windows 用户成功。

### 选择 B：保留现状（安全基线）

不变更任何 ACL/祖先规则；拒绝仍是正确的 fail-closed。只允许在已有权限已满足严格规则的环境运行，提供**脱敏只读**失败阶段/目录组件位置与可操作说明；用户或管理员自行评估环境，产品不自动改系统根、用户目录或已有客户端 ACL。鉴于所有绝对路径都从盘根检查，搬 HOME、项目或手工建一个私有目录**不一定能绕过**，改到另一固定卷也要先测其卷根，不能建议用户大范围修改根 ACL 来安装。成本低、风险边界与当前一致；在本次 runner 的目标卷上敏感创建不可用，对普通 Windows 用户可能阻断首次运行/安装，覆盖率未知，不能宣称可部署。

| 对比 | A：受限卷根兼容候选 | B：保留现状 |
|---|---|---|
| 产品策略 | 仅用户批准的根 owner/ACE 例外；无证据仍拒绝 | 无变化 |
| 默认部署 | 有条件改善；可能被 D 盘、更深祖先或现有客户端 ACL 阻断 | 已知本次 runner 阻断；其他机器未知 |
| 复杂度/成本 | 中：原生 ACL 精确分类、根身份与绑定、fake/native/对抗回归、独立复核 | 低：只读诊断及文档；仍需 Windows 实测 |
| 攻击者 | 排除管理员、SYSTEM、经批准可信服务及同用户恶意程序；对其他本机用户依赖上述 ACL 条件与绑定检查 | 同样不保护特权/同用户，当前祖先规则更严格 |
| 竞态 | 保留绝对路径 ABA、最后检查后变化、外部内容并发；不能称原子或零风险 | 现有同类竞态仍在；严格祖先并不消除同用户/特权竞态 |

### 具体推演（前提→结论→反例）

- **案例 1〔既有读数 + 推算〕**：根 owner 非当前/ SYSTEM/Administrators → 首次 `_validate` 拒绝 → 后续目标根本未观察；把假 HOME 改到同一个根之后不影响第一步。反例：换到 ACL 恰好满足旧策略的另一卷，首个失败点可以不同；它不是普遍结论。
- **案例 2〔纯示意〕**：根对另一本机用户仅有新增文件权限，系统可信 owner、无更改根/删除下一段能力，现有 `Users` 及后续各段严格通过、敏感文件私有 → 在用户认可该具体例外后，可继续旧的链检查；若该用户另有 `FILE_DELETE_CHILD` 或能对 `Users` 取得 `DELETE`，则无需更改私有文件 DACL 就可能破坏名称绑定，A **必须拒绝**。一个“公共 write”布尔值无法区分二者。
- **案例 3〔纯示意〕**：已存在配置文件 DACL 允许其他用户写，即便根和父链通过，也必须拒绝；新配置文件则只允许在已验证父目录创建即私有。若文件本身私有而父目录可删子项，也不能安全放行。A 改的是受限根策略，不是私有对象不变量。
- **案例 4〔纯示意〕**：根与第一子目录被持有时攻击者在两个检查之间替换再恢复，或最后一次验证后改内容；现有绝对路径重走可能看不见 ABA，不能宣称原子操作。发现状态变化就拒绝不代表能检测全部变化。

## 风险与权衡 / 待用户裁决

**建议顺序**：先批准只读、脱敏逐层补测（不改变产品规则）；取得主体类别、ACE 明细和普通 profile 实况后，再在 A/B 中裁决。若希望立即选 B 可直接拍板；A 当前只是范围受限的候选，**不是已授权的实现计划**。需要用户明确回答：

1. 是否允许先在已授权的私有 Windows runner 只读检查真实卷根与用户 profile 的祖先、在隔离临时目录做受控 fixture（仅合成配置，不触碰个人真实客户端配置）？若额外使用 Windows 11 或第二用户/ACL 变更，需要对应环境和权限授权。
2. 条件允许时，是否选择 A 而不是 B？A 的**允许清单要在补测后给出具体主体、ACE 类型/标志/掩码**再确认；不能用“普通 Windows 兼容”隐含批准任何 public write 或 unknown SID。
3. 根例外范围只限经 OS 验证的系统卷根，还是另行覆盖经核实的非系统本地固定卷根？现有 D 盘 CI 结果不能自动回答。禁止把卷根、系统根、用户 profile 和任意自建目录混为一类。
4. 后续若 profile 深层目录或已有客户端敏感文件也被拒绝，是继续拒绝并提示，还是对**具体层级/ACL**另设例外？本文默认继续拒绝，不允许自动修 ACL、转存后覆盖或跳过敏感检查。

安全边界不包括系统管理员、SYSTEM、批准的可信主体、具备备份/还原等特权或同用户恶意进程；也不保证任意 NTFS 配置、网络/UNC、reparse profile、重定向、企业域组、云同步、多盘或全部 Windows 版本可用。未明确的环境继续 fail-closed。不能修改系统根 ACL 作为 CI 通过手段。

## 验证方式

### 先补充原生只读证据（不改产品、不搜秘密）

- 对本次**相同自建目标**和真实默认 profile 下的**独立合成测试目标**分别按卷根至直接父目录逐段采集：卷类别/文件系统、是否系统卷、组件序号与角色（而非用户名/实际私有路径）、reparse/文件 ID/卷 ID 的脱敏稳定比较、owner 类别、每个 ACE 的主体类别/类型/具体权限位/继承和 inherit-only flags、DACL 是否受保护、当前 token 所需 AccessCheck。只读 ACL 元数据，不读文件正文、环境变量值、token/密钥或真实客户端配置；未知主体只汇报分类/必要的脱敏关联标记。必要时核实可信系统主体，不能把 unknown 当作可信。
- 每一段分别报告“原策略是否通过/首个失败/后续仅 raw 诊断”，不得因 `_walk` 失败就省略更深段，也不得把绕开产品校验的 raw 诊断称为安全通过。重放读数是非原子；报版本、路径角色、对象持有范围、是否同对象，避免采信跨时点拼接。模拟默认路径同时核实首个缺失层与创建父目录。若更深目录不可读、ACL 不支持、身份无法判定，标记 unknown，先停。
- 比较 CI 临时卷 `D:`、系统卷及实际 HOME/LOCALAPPDATA 所在卷，不假定三者相同；如条件允许由用户另授权在 Windows 11 普通用户 profile 复测。测得公共写时分离增、删、改权限/继承，仅凭 ACE 枚举和当前 token AccessCheck 不宣称对所有其他用户有效权限已经证明。

### Fake 单元/协议回归（策略获批后）

复用 `tests/test_windows_acl.py`、`test_windows_paths.py`、`test_windows_secure_creation.py`、`test_windows_secure_files_writer.py` 等已有骨架，针对批准的唯一窄规则构造：

1. 当前 CI 根 owner_untrusted + unknown principal + public write 的数据结构重现，旧策略拒绝；新策略**只有匹配批准清单且各危险位排除时**放行，不能把汇总布尔值当可放行输入。owner 不匹配、未知 ACE、有效 dangerous write/delete/WRITE_DAC/WRITE_OWNER、非法/空 DACL、身份不可确认、根 reparse、非系统卷默认拒绝。按批准结果区分 inherit-only，测试继承到下级后仍被独立验证。
2. 根规则不得在根以下自动生效；普通祖先、直接创建父目录和已有敏感文件的 owner/DACL 不合格必须拒绝。公共可删第一段、父路径/目标替换、文件硬链接、缺多级父、target 已存在、ACL 改变、内容变更、关闭失败均 fail-closed；不创建或准确报告部分创建及资源释放状态。
3. 对同一对象验证 ACL/身份/路径三项一致、`_check_parent` 前后、`PathLease.revalidate` 新旧观察与 policy 一致性；故障注入更改后拒绝，恢复型 ABA 明确列为边界而不是宣称必然检测。新目录/文件创建即私有并后验校验；已有对象未被重写或修权限。Fake 通过仅证明协议实现，不能替代真实 NT ACL。

### 原生 CI：真实 OS、隔离 fixture、可复现结果

- 仅用户确认方案及授权执行后，复用现有私有 GitHub Actions Windows Server 2025/Python 3.14.10 的闭环；不把 Wine、mock ACL、注入桥接冒充 native。先采样根和各祖先，再执行真实产品无替身新建目录、新建文件、已有私有文件 read/write、客户端配置合成安装等，真实 profile 下只用自建随机测试子目录/假数据；必须校验确实写到隔离 fixture，严禁真实用户配置/密钥或在线产品变更。
- 独立用原生 API 在测试专用目录构造“已存在安全/不安全”与“公开可新增/可删子项”等 ACL，采集自己创建的对象身份、ACL 和结果；不改盘根/真实 profile ACL。作为控制组分别验证现有安全对象成功、不安全对象拒绝且内容/ACL 未改、缺失父/已存在/替换冲突结果和 `created` 状态；固定 volume、reparse、hardlink 等有能力则覆盖，否则报告未覆盖而非通过。两账户对抗需要显式授权与隔离 runner；不能仅由当前 token 的 AccessCheck 替代第二用户攻击测试。
- 在本次 runner 上先验证首次创建能否走出 index 0，再逐层确认哪个条件通过或拒绝；若 A 只限系统卷而 CI 工作路径是非系统卷，应报告“范围不覆盖”，不得篡改安全规则使 CI 变绿。记录 Windows 版本、Python、文件系统、系统/临时卷角色、批准策略版本、测试数量、passed/failed/skipped 及诊断产物；被拒绝的负例只有断言拒绝和无副作用才算通过。
- 保留同 socket peer、同已验证 HANDLE stop、共享 Python 的既有真实回归，不以文件兼容通过替代；WMI 精简 env 超时与继承环境表现交独立分析，既不以 WMI 结果证明 ACL，也不趁此单更改 WMI。需要 Windows 11、真实账户、云目录/域环境时分别列覆盖缺口，未测不能宣布普遍部署可用。

### 实施分工与门禁

1. **runner**：用户授权后，仅负责上述脱敏只读逐层诊断及自建隔离 fixture，附环境、原始汇总与失败位置，不搜秘密、不改产品/在线文件；出入网或 CI 操作另按用户授权范围执行。
2. **architect-xhigh**：根据测得 ACL 明细形成需用户确认的精确根例外及若有更深问题的单独范围；不得自行修改既有 confirmed 设计。用户选择 B 则不设计例外实现。
3. **implementer-high**（若规则/证据跨文件耦合增加再按既有规则选更高档）：仅在用户明确接受具体政策与实施方案后，改现有 `core/windows_acl.py`、`core/windows_paths.py` 及相关既有调用和测试；复用现有创建/写入机制，不改 peer/stop 的安全语义，不扩大目录策略。实现前列出修改点与断言。
4. **reviewer**：独立检查根例外的主体/有效权限/继承语义、危险父链、创建时保护、身份/ACL/路径绑定与失败处理；随后 runner 执行 fake 与真实 native CI。任何异常或新增策略范围退回用户裁决。不得将 CI 绿灯替代用户对安全策略的批准。

### 交付物规范

本轮唯一新增 `W/docs/designs/2026-11-16-model-proxy-Windows路径ACL默认目录兼容方案.md`，沿用 architect 设计记录命名与目录；`status: draft`，供用户裁决，不改 README 或其他设计。后续以既有产品/测试文件为主；是否新增持久化 CI 文件须另行列出目录、命名、消费者与获得授权。本轮无在线产物、产品文件或新测试产物。

## 关联

- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy-Windows适配复杂度独立复核]]（confirmed，仅引用）
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_acl.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_paths.py]]
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/platform_windows.py]]、[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/windows_secure_files.py]]
