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

# model_proxy Windows 兼容性审查与改造方案

## 背景与问题

目标是在**不改动当前线上 macOS 部署**的前提下，判断 Windows 使用方式并设计原生改造。审查基线：`tools/model_proxy` Git HEAD `7547517`、`VERSION` 为 `0.13`；只读源码、示例配置结构及测试，**未读取真实配置、日志内容或凭据，未启动服务、运行测试或访问网络；未在 Windows/WSL 实测**。本记录是静态分析草案，不代表可用性验收。

状态口径：**阻断**＝代码与原生 Windows 环境特征直接矛盾，满足所述执行前提就不能正常完成；**可移植但未实测**＝未发现必然阻断，不能推定可运行；**待验证**＝依赖 OS、环境、第三方客户端或行为实测。下面的行号均相对于 `tools/model_proxy/`。

### 逐项证据

| 项目 | 判断与证据 | 影响/边界 |
|---|---|---|
| Python 主服务 | **阻断**：`model_proxy.py:10-14` 调 `core.server.main()`；`core/server.py:3746-3756` 在启动过程直接 `import fcntl` 并调用 `fcntl.flock`。标准原生 Windows Python 没有 `fcntl`。 | 配置解析和日志初始化若先成功，必在锁处失败；若路径/日志先出错则更早失败。不是说 `import core.server` 必定失败：`fcntl` 位于 `main()` 内。 |
| 依赖 | **可移植但未实测**：`core/server.py:12-32`、`_install_ops.py:9-24` 声明/使用标准库；静态 AST 汇总服务、辅助模块及 tests 的 import，未见第三方包；未发现依赖清单或锁文件。 | 不是运行时依赖完整性证明：动态导入、平台行为和客户端侧依赖仍需测试。无必要先引入大型框架。 |
| Python 版本 | **需要验证**：`core/session_identity.py:26`、`core/server.py:127` 使用未延迟求值的 `datetime | None`；按 Python 类型联合运算的运行时要求，静态推断至少 3.10。仓库未发现明确最低版本、锁定版本或跨版本矩阵。 | 3.10 是候选下限，不是已验证支持范围；仅用当前解释器的 `ast.parse(feature_version=...)` 不能证明对应版本可导入。先在真实 3.10 和选定较新版本运行测试，再定声明。 |
| 实例锁 | **阻断**见上；`core/server.py:3753-3763` 先以 `"w"` 打开再尝试非阻塞锁，锁持有期依赖文件句柄。 | 不可用仅 PID 文件或 `mkdir` 代替进程锁。现有先截断后加锁也意味着竞争者可能先清空锁文件里的 PID；必须分别考虑互斥与诊断信息。 |
| 运行时路径 | **需改造/验证**：`config/runtime_paths.json` 的 lock、pid、ensure_log、start_lock 当前是 `/tmp/...`，log/totals 是项目相对路径；`core/server.py:88-124` 在启动时以 `Path.is_absolute()` 和包根解释路径，缺失/损坏回退仍含 `/tmp/...`。 | 不要把 POSIX `/tmp` 当作 Windows 临时目录或假定其目录存在；盘符路径、UNC、相对路径及父目录权限需 Windows 实测。路径还被 Bash 独立解析（`model_proxy_cli.sh:30-64`，`hooker/ensure_model_proxy.sh:14-26`），不能只修服务端。 |
| 凭据及安全 | **需验证**：`config/model_proxy_config.example.json` 的结构含 `admin_token`、`strategies[].client_token`、`supplies[].appkey`；`core/server.py:1499-1531` 支持 `appkey_file`、`~` 展开并每请求读取；`_config_ops.py:81-97`、`core/commands.py:176-196` 原子替换后尝试 `chmod 0600`。 | Windows 不能将 POSIX mode 视作私密 ACL 的证明；原子替换产生的目标 ACL、文件共享和备份权限必须测。`appkey_file` 相对路径、编码和 Windows 用户目录须单独验证；不能照搬另一系统的绝对路径。现有读取路径没有证明使用系统凭据库。 |
| CLI `.sh` | **原生入口阻断/部分待测**：`model_proxy_cli.sh:1,17,33,373-392,403-453` 使用 Bash、`python3`、`nohup`、`lsof`、`pgrep`、`ps`、`kill`；`173-217` 的状态获取也依赖这些命令，且 `183` 使用 BSD 风格 `stat -f`。 | 标准 PowerShell/cmd 不能直接把 `.sh` 当原生命令执行；装 Bash 不保证上述工具/行为齐全。Linux/WSL 上 `stat -f` 分支也需修正或验证。`on` 仅检查端口有监听（`374-385`），不能据此证明监听者就是本代理。 |
| install | **Python 辅助可移植但未实测，自动接入需改造**：`model_proxy_cli.sh:364-369` 调 `_install_ops.py`；后者 `52-72` 用 `Path.home()` 选择四类客户端配置，`235-237` 写入 `bash ... ensure_model_proxy.sh` hook，`312-342` 整理 SessionStart；`466-504,624-627` 的 Codex catalog 安装会访问网络。 | Windows 下各客户端安装路径、hook 实际 shell、变量展开、路径转义与编码未验证；照搬 hook 不等于可用。install 不应在本审查中执行；自动测试需 mock 网络及隔离 HOME/USERPROFILE，且不能改真实客户端配置。 |
| ensure | **原生入口阻断，WSL 待测**：`hooker/ensure_model_proxy.sh:1-6,28-37` 是 Bash，以 `mkdir` 防并发再调用 CLI；另由服务的 `flock` 兜底。 | 原生方案需要替代入口和跨进程启动协调；不能只改 hook 字符串。现有目录锁异常退出残留时可能使后续调用直接退出，需在验证中覆盖。 |
| 日志/状态 | **待验证**：`core/server.py:127-159,196-222` 启动时按时间裁剪、`os.replace` 后给同一文件装两个 handler；`model_proxy_cli.sh:379` 同时把后台进程输出追加到该日志；`core/server.py:275-304` 保存/恢复 totals，`core/commands.py:176-196` 写 sidecar。 | Windows 文件被 stdout/handler/其他程序持有时的替换与共享语义需实测；失败分支保留日志，不等于裁剪成功。README `198` 仍称保留末 5000 行，而源码是按 7+1 天窗口（`core/protocol_hints.py:5`），以代码为当前证据。编码、并发、损坏恢复、状态格式及敏感信息须覆盖。 |
| 测试 | **有测试但 Windows 未实测**：如 `tests/test_log_retention.py:13-32`、`tests/test_usage_totals.py:60-107,169-178`、`tests/test_listen_config.py:329-363`、`tests/test_install_ops.py:748` 附近。`tests/test_config_ops.py:753-779` 断言 POSIX `0o600`，并非 Windows ACL 断言。 | 已有测试不能证明 `main` 真正启动、flock、Windows 生命周期、hook、ACL、CLI 并发成功。部分测试 mock `urlopen`，但未审计全套是否都完全隔网；不能未经隔离直接运行。README `390-392` 也承认真实上游转发没有端到端自动化。 |

## 方案设计

### 三条使用路径

| 路径 | 适用条件 | 结论 |
|---|---|---|
| **复用已有远端代理** | 已有可达且经授权的服务端、现成安全网络路径与匹配的客户端配置；不要求改动当前线上 macOS。 | 零移植的候选路径。必须先确认现网是否本来就允许访问、客户端协议与 Host/端口可用；默认 loopback 不等于远程可达。不可因本方案擅自改 Mac 监听、放行防火墙或复制上游凭据。 |
| **WSL 内运行现有 Linux 链路** | Windows 机器可用 WSL + Linux 发行版、Linux Python 候选 ≥3.10、Bash 和 `python3`/`lsof`/`pgrep`/`ps`/`curl` 等依赖；隔离副本、Linux 可写路径和独立测试配置。 | 可能比原生改造少，但**尚未证明开箱即用**。先从 WSL 内前台服务和假上游测试；检查 `stat -f` 的 Linux 差异、`nohup` 生命周期、hook 实际运行环境、锁及状态。若 Windows 客户端连 WSL，还须实测 localhost 转发、监听地址、Host、休眠和重启；不要假定 WSL 在 Windows 客户端启动时已运行。建议先放 WSL Linux 文件系统，`/mnt/c` 权限/锁另测。 |
| **原生 Windows** | 有独立 Windows 测试机/VM，可安装受测版本 Python；接受分阶段改造与回归。 | 用户需要不依赖 WSL 的长期方案时推荐本路径；先做前台服务，再补齐操作入口与集成，不把阶段性成功称为全功能可用。 |

如远端既有条件满足，可先用远端而不触碰线上；否则可在 WSL 验证过渡，同时独立推进原生方案。涉及 LAN 时，当前仅有 token/Host 校验的事实不能替代传输加密及网络安全评估。

### 原生 Windows 分阶段方案

**阶段 0｜独立基线与契约**

1. 在隔离 Windows 副本和隔离 macOS 回归副本使用合成配置、空账本、虚假 token；不拷贝生产配置/账本，不运行线上服务。按本记录验证 3.10 与一个双方选定的新版本，结果决定正式最低/最高版本。
2. 约定同一 Windows 用户默认仅一个实例，CLI、服务及 ensure 共享同一锁身份与路径；如需多实例或跨用户共享须另行设计，不默认支持。端口、配置、状态目录和实例身份关系需明文约束。
3. 明确范围：阶段 1 的成功只意味着前台核心代理；阶段 2 才有生命周期和可观测 CLI；阶段 3 才考虑客户端 install/hook。默认不改变 macOS 既有文件路径、脚本、监听及安装配置。

**阶段 1｜服务可前台运行：平台适配 + 安全存储**

1. 在 `core/platform_runtime.py` 集中定义运行时路径解析与进程锁接口；macOS/Linux 保留原 POSIX 行为，Windows 明确选择 `%LOCALAPPDATA%` 下的用户态运行目录（缺失时报可操作错误，或使用经测试的明确后备目录），创建必要父目录。Windows 侧使用独立本机路径覆盖文件或环境入口；**优先级必须明确为显式 Windows 覆盖 > Windows 安全默认值**，不能把仓库现有 POSIX `/tmp` 条目误当 Windows 可用路径。非 Windows 保留原 `runtime_paths.json` 和回退语义。服务、后续 CLI/ensure 全部调用同一解析器；`MODEL_PROXY_CONFIG` 仍是业务配置选择，不与运行目录混淆。具体覆盖文件名与兼容细节在实施前定稿，不自动改现有配置。
2. 在 Windows 使用标准库 `msvcrt` 的文件区域锁作为**候选**，POSIX 继续 `flock`；先做真实多进程原型确认非阻塞、锁定字节位置、句柄生命周期和崩溃释放。锁文件以不截断方式打开，稳定锁定同一字节，持锁到服务退出；锁文件不因进程退出而删除。仅在获锁后记录 PID/实例身份，诊断记录与锁分离；正确区分锁冲突、权限/目录等 I/O 错误。不得用 PID 或端口代替互斥，也不因 stale 文件存在而判断锁被持有。
3. 配置、`appkey_file`、sidecar、totals、日志/临时文件放入权限受控的独立目录；用 Windows ACL 约束当前用户及必要系统主体，ACL 施加对象、原子替换后继承/保留策略和失败回滚必须在 Windows 验证。不能承诺 `chmod(0600)` 有 POSIX 私密语义；不要输出 token、diff 明文或把秘密放进命令行。沿用 `appkey_file` 能力，不以新增 keyring 作为前置；Windows 路径、格式与编码用假文件测试。
4. 服务前台启动，复用现有协议、路由和标准库 HTTP。Windows 启动顺序为只读校验配置及路径、准备安全目录、获取实例锁、初始化和裁剪日志、加载账本及绑定监听；锁失败只报 stderr，不碰共享日志/账本。现有代码在获锁前初始化日志，见 `core/server.py:3746-3756`；macOS 原有启动顺序默认不变。日志先验证裁剪/替换与 handler 行为。将启动 stderr 独立于应用日志可降低共享冲突，但只有实测后才能决定具体处理；保持 totals、sidecar 的数据格式兼容。Windows 适配失败应清晰报错，不能静默回退至不安全目录。

**阶段 2｜原生运维入口与状态**

1. 新增 Python CLI `model_proxy_cli.py`，复用 `core/listen_config.py`、`_config_ops.py`、`_format_ops.py`、`_install_ops.py` 和统一路径层；用 `py -3 <入口>` 或明确 Python 路径调用，**不依赖 Bash、lsof、pgrep、nohup**。原 `.sh` 暂留不动，macOS 继续原入口；不能将两套独立实现宣称自动一致。
2. `on/off/restart/status` 必须验证进程身份和就绪，不能把「端口有人监听」当成功，也不能凭可复用 PID 误杀。进程记录含 PID、创建时间/启动身份及实例标识；查不到或不匹配则拒绝停止并报告。`restart` 先可靠停止、等待锁和端口释放，再启动；失败返回非零并报告中间态。Windows 需要设计并实测受限的本地优雅停止机制（现有管理 API 只有 status/reload，见 `core/server.py:3215-3235`）；若新增停止控制，应限定 loopback、鉴权、避免死锁及处理中请求策略。强制终止只能显式选择并报告风险，不作为默认退路。
3. 对照旧 CLI 的 `status/reload/supply/route/strategy/switch/install/on/off/restart/logs/stats` 逐项建立入口×行为矩阵。优先交付生命周期、status/logs/stats/reload；配置编辑子命令复用现有 helper 并保留交互/退出码，且任何真实上游探测必须显式告知并授权。`install` 暂明确标为未支持，直到阶段 3；未实现命令必须清楚报错，不能静默成功。
4. 新增 `hooker/ensure_model_proxy.py` 复用上述 on/就绪语义，并用同一套可靠协调机制处理并发；启动失败向调用方返回非零。Python 子进程、Windows 控制台/后台生命周期与权限需实际验证，不假设 POSIX 信号或继承方式成立。

**阶段 3｜客户端接入与交付**

1. `_install_ops.py` 按平台生成 hook；先确认各客户端在原生 Windows 下实际使用的 shell、变量展开与项目根，测试带空格/中文路径、引号和退出码。无可靠契约前只输出人工配置指引，不写入不能执行的 hook；预览和明确确认后才写客户端配置，保留回滚路径。
2. 分别核验 Claude、Codex、Hermes、OpenClaw 的 Windows 路径和行为，未验证的显示不支持/手动配置；原样复用 `Path.home()` 并不自动等于受支持。Codex catalog 的网络拉取及供应商 probe 必须单独获得网络授权；离线验收用 mock。备份、diff 与日志须脱敏并保护 ACL。
3. 在独立环境完成 Windows 与 macOS 回归后，才可请求用户决定是否发布；生产 Mac 的升级、配置迁移、自启动切换均不包含在本任务，也不自动执行。

### 推演：入口、锁和状态的一致性

- **入口覆盖**：前台 `model_proxy.py`、原生 `on`、原生 ensure 都解析同一配置和锁路径，最终都由服务持锁；ensure 只协调启动，不充当最终互斥。macOS `.sh` 继续走原路径；Windows `.sh` 不作为受支持入口。若某入口仍各自解释路径，则不变量违反，禁止发布。
- **并发反例**：两个 `on` 几乎同时启动，A 先获锁、B 看到锁冲突。B 不能仅因端口刚好被第三方占用就报告「代理已启动」；须通过实例身份及就绪鉴定，否则失败。获锁前不截断记录；A 意外退出后，由 OS 释放锁，留下的记录不阻止重启。以上是设计推演，非实测。
- **PID 复用反例**：代理退出、PID 被无关程序复用，`off` 只看 PID 会误杀；要求创建时间/实例身份复核，任何不匹配或不可确认均 fail closed。若系统权限不足无法验证，不能声称 stop 成功。
- **状态分离**：Windows 不复用生产 Mac 日志/账本，避免两端以不同 OS 锁保护同一物理文件；后台状态只反映当前实例。该方案不支持跨 OS 对同一数据目录的并发写入。

### 交付物规范（未来实施，非本次新增）

目标根 `/Users/vincentwang/Documents/NoteVault/tools/model_proxy/`；既有源码/测试目录承载跨平台改动，不另建项目。候选新增：

```text
core/platform_runtime.py             # Windows/POSIX 路径与实例锁，服务/CLI 共用
model_proxy_cli.py                   # 原生 CLI，供用户与 ensure 使用
hooker/ensure_model_proxy.py         # 原生启动守卫，供经验证的客户端 hook 使用
tests/test_platform_runtime.py       # 锁、路径与异常契约
tests/test_native_cli.py             # 原生生命周期与入口契约
tests/test_native_install.py         # 合成客户端环境与 hook 契约
```

命名沿现有 `core/*.py`、`test_*.py`、`hooker/ensure_model_proxy.*`；实际拆分可在实施设计时调整，但不得重复维护路径和锁真相源。以上属既有目录约定；实施时在项目 `README.md` 的目录/入口索引补充新增项，并标明各平台支持矩阵。运行态目录是每用户本地数据，不作为仓库交付物；具体覆盖文件若新增，须同步明确忽略规则，不能纳入任何真实凭据。上表是未来实施规范，本次只新建设计记录。

## 风险与权衡

- **安全与权限**：ACL、备份、替换后的临时文件、日志和客户端配置都可能泄漏凭据；不能把 `chmod` 或用户目录默认权限当验收结论。默认 loopback；远程访问、TLS、授权和防火墙需单独审批。
- **macOS 回归**：共享 `server.py` 和辅助模块的改动仍有回归风险。「不动线上」是部署边界，不等于无需在隔离 Mac 副本跑回归。必要时平台分支 fail closed；不能自动更新线上配置或 hook。
- **可观察性与失败恢复**：日志被占用导致替换失败、损坏账本、异常杀进程、启动中途宕机、端口被冒用都会影响状态；不能仅靠进程名/PID/端口判定健康。
- **WSL 特有**：Linux 依赖、文件系统、Windows↔WSL localhost、后台常驻和 hook 来源都需要实测，WSL 不是「原生 Windows」；客户端在不同环境时用户名、配置和凭据路径彼此独立。
- **外部契约待主会话核查**：本次按要求未联网。实施前须核对所选 Python 版本的 Windows `msvcrt`/文件共享/ACL 相关官方文档、目标 Windows 及 WSL 行为、各客户端 Windows 配置与 hook 官方契约；本记录的候选机制不替代这些核查。
- **需用户确认**：最终要求仅前台服务还是完整 CLI + 自动接入；Windows 版本、Python 版本、客户端清单及是否允许 WSL；是否需要后台常驻、非 loopback 访问或多实例。未经确认不扩大范围。

## 验证方式

所有验证先在独立 Windows VM/测试用户及独立 macOS 回归副本执行；测试配置、客户端 HOME/USERPROFILE、运行路径、端口及 token 均为合成值；网络使用**本机假上游或 mock**，并在系统/测试层阻断非 loopback 出站。先审查测试的 subprocess、环境变量与网络访问，禁用真实 install/probe；确保不会触及现有生产路径。可在隔离副本使用 `python -m unittest discover -s tests`，但仅在上述隔离与审查完成后执行，原生、WSL、macOS 分别记录解释器、OS 和实际结果。本次未执行该命令。

1. **版本与入口**：在候选 3.10 和较新版本分别 import、前台启动、收到 `KeyboardInterrupt`/停止、正常退出；日志、配置均落隔离目录。缺目录/不可写/配置损坏/含空格中文/盘符与 UNC 路径逐项验证。确认 Windows 不依赖 `fcntl`，macOS 旧默认不变。
2. **锁与启动并发**：两个及多进程同时启动，只允许一个成功持锁；检查获锁前记录不被截断；崩溃/强制杀死后可重启、锁文件存在不等于锁被占用；权限错误不误判为竞争；50 次并发 ensure 后只有正确实例、其余结果可解释。测试 PID 复用/身份不明时 fail closed、无关监听不被误报为代理、意外端口冲突给非零退出码。
3. **本机假上游端到端**：仅向 loopback 假服务发送非流式与流式请求，覆盖路由/协议转换、reload、鉴权、错误和中断；对照响应、日志、totals 和 sidecar；确认不需要任何真实密钥。既有单测与端到端均通过才称核心链路通过。
4. **日志与状态**：日志被持有或不可写、启动裁剪、无合法时间戳、并发写、中文/异常编码、totals 损坏恢复与替换、sidecar 原子写；确认日志裁剪失败有提示且原文件未丢失。CLI status/logs/stats/reload 的字段、退出码、无服务/伪服务分支分别核对。
5. **权限与秘密**：用独立测试用户验证配置、appkey_file、备份、临时文件、替换后目标的 ACL 与继承，不满足策略时失败；采样检查 stdout、diff、日志、进程命令行不包含测试 token 明文。保留 `0o600` 的 POSIX 测试，Windows 用 ACL 专项测试，不能简单跳过后宣称安全。
6. **安装/hook**：对四类客户端分别用临时 HOME 和伪配置测试存在/不存在/取消/备份恢复、重复 install 幂等、路径空格/中文、实际 hook 调用和无 Bash；模拟网络失败且确认没有外联。未测客户端保留「待验证」状态。
7. **WSL 与 Mac**：WSL 内完成上述独立测试，再单测 Windows 客户端到 WSL 服务的寻址、生命周期和休眠恢复；在隔离 macOS 副本跑现有 CLI、ensure、锁/日志及单测回归，比较路径和行为，不碰当前线上服务。发布前形成平台×Python×入口×功能的通过/失败/跳过矩阵和复现证据。

## 关联

- [[tools/model_proxy/README.md]]
- [[tools/model_proxy/docs/designs/2026-08-13-runtime-path-constants-unification.md]]
- [[tools/model_proxy/docs/designs/2026-10-28-cli-restart命令设计.md]]
