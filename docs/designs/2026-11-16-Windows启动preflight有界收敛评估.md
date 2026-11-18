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

# Windows 启动 preflight 的有界收敛评估

## 背景与问题

在已确认的共享 Python、精简 Windows 方案下，分析实际私有 ACL、同 socket 认证和同一已验证进程 HANDLE 停止不变时，能否去掉启动中重复的全角色 preflight。本记录是候选方案，不修改、不 supersede 既有 confirmed 记录，也不授权实施或部署。

**证据基线**：Whisky/Wine 11.0、假配置及独立端口的预置对象测试，在服务启动约 165 秒的采样截点前完成 15 次 `_windows_open_preflight`；服务没有 READY。冻结源码指纹及 profile 在 `/tmp/mp-b-timings-20261116.LhCmj5CK/`；该目录是临时证据，不保证永久保留。采样的 `server.py` SHA-256 为 `4a4d298b46198a6edfae1d7e787f179e478645dff80e8a96b66f16e1db6e83d8`。本轮读取时 worktree 的 `server.py` 整文件已经不同，候选涉及的六个函数仍与快照相同；实施前须重新核对，并与控制线协调。

## 与已确认方案的对应

[[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy共享Python与Windows最小适配再审视]] 和 [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy-Windows适配复杂度独立复核]] 已确认。前者正文保留了确认前的 draft 措辞；以 frontmatter、后续独立复核的说明和当前用户授权为准，不就地订正。

| 已确认的减法 | 本次 15 次 preflight 中的事实 |
|---|---|
| Windows 首版不以安装 ID/来源、`install.json`、`last_good_paths.json`、三记录事务、旧端口迁移/复制来源为前提；不要求通用写入 alias-freeze key | 本次均为 light 路径，固定目标已是 `anchor.lock`、`control.lock`、`active.json`；不能将这 15 次中某一次直接称为已取消的安装证明或通用 writer 的 alias freeze。单个 `active.json` 的 STARTING/READY 也不等于三记录事务。 |
| 收敛重复全链核验，不再承诺防安装复制、通用别名冻结、同用户恶意变更或全祖先永久证明 | 可以提出减少同一阶段内重复取证；但每次调用还承担角色冲突、私有 ACL、锁和变化检查，不能整包删。候选 A/B 是对重复全链取证的具体设计，不是 confirmed 记录逐句指定的删除点。 |
| 必要祖先危险写权限检查、真实私有源文件、创建时私有及读回；状态目录和自定义路径按既有保护失败关闭 | 保留 `windows_paths.inspect`、实际有效用户 `AccessCheck`、同对象身份和双读；不以文件名、`chmod`、记录内容或过期证据替代。Wine 不能读回 protected 标志时仍失败，不能修真实 ACL 或通过 fixture 假报新建通过。 |
| 两把实例锁及短控制锁、未知实例不接管、真正的连接对象和停止目标 | 两把实例锁按原顺序获取、持有和释放；锁后副作用不前移。发 token 前仍核验同一连接 socket 的真实对端；停止仍在已验证的同一进程 HANDLE 上执行。轻记录仅用于定位，不能授权。 |
| 保留路径类型、明显 reparse、角色冲突、必要祖先检查；Mac 契约保持 | 配置、sidecar、六个 runtime 角色与 state 的现有冲突关系继续检查；不把 `pid`/`start_lock` 存在时 UNKNOWN 的分支顺便删除。Mac、安装及控制 API 不随本设计重构。 |

### 15 次的调用归属

按冻结源码与计数推导：4 次配置目标检查、4 次 `inspect_runtime_identities`、两次实例锁各锁前/锁后共 4 次、3 次持锁 guard 校验，各包含一次 light preflight，共 15 次。每次 light preflight 有 11 个 lease（项目根、六个 runtime、state、三个固定目标），成功路径中每个 lease 在 inspect、批前、批后各复验一次；约 **15×11×3＝495** 次 lease 复验。profile 是函数聚合而非逐调用者轨迹，归属为源码推导；异常分支和之后的启动阶段不适用该算式。

其中四次配置检查还产生 108 次 `may_contain`；本次正常路径中每次涉及两个不同 lease，约 216 次复验。采样共完成 919 次，尚有一次进行中。274,820 是底层 `_identity` 调用数，不是 ACL 查询数；稳定 ACL 双读为 68,705 次。含子调用的耗时不可相加。

## 方案设计：两个独立候选

两者均只处理 Windows 启动的指定检查点，保留现有路径比较算法，不实施此前暂缓的两两比较批量化。仅复用当轮由持有 lease 验证后返回的身份基线快照，不缓存卷查询结果、文件句柄、ACL 授权结论或 token，也不将该快照用作访问授权。建议按 A、B 分别实现并分别测量；它们均改变一次时间采样，**不是严格 ABA 等价**，此具体差异须在实施前获得针对性确认。

### A：首次配置检查复用同一轮 runtime 快照

**位置**：`core/server.py` 的 `_windows_main` 首次 `_check_config_targets` 及紧接的 `inspect_runtime_identities(..., windows_light=True)`。

- `_windows_check_config_targets` 取得 `_windows_open_preflight` 的快照与 scope 后，仍用同一 backend 打开三份配置/sidecar，按原规则检查 private ACL、直接父目录、state 包含和配置/所有角色别名；成功前对全部 scope 和输入 lease 按现有顺序复验。全部 lease 仍按原契约关闭，关闭未知不返回成功。
- 对这个 Windows 调用返回上述**已验证的不可变 snapshot**，作为 `_windows_main` 原来随后单独取得的 lock 前基线；它不是授权，也不是仍持有的句柄。取消仅这一处紧接的 `inspect_runtime_identities`。其他调用方可继续忽略返回值；Mac 分支不改。
- 后续 `verify_record_directory`、每把锁各自的锁前新 preflight、同 HANDLE 锁获取、锁后重新检查和所有副作用顺序保留。持锁后仍重新检查配置、路径及未知实例，不依赖本快照发 token 或停止进程。
- **变化/风险**：原先第二次独立打开能在其采样点看到角色变化；复用后该采样点消失。先变化并恢复的短暂 ABA 可能漏过；持续变化应由取得锁前的新 preflight 与基线比较拒绝。先前两次读取间发生持久改变时，原先第二次可能把新状态作为基线，而候选更可能在锁前比较时报 UNKNOWN；不保证完全相同的错误顺序。
- **收益估计**：本次紧接的第二次 preflight 实测 5.864 秒；候选预计少一次 light preflight、约 33 次 lease 复验。该数字不是优化后实测，路径长度和负载不同会变化。
- **15 秒**：本次第一次配置目标检查自身 16.588 秒，且采样从服务启动后约 1.15 秒开始；删除其后的 5.864 秒不能让该次采样在 15 秒内 READY。

### B：首次 guard 中以已存在的配置检查快照替代相邻角色快照

**位置**：`core/server.py` 的第一次 Windows `_verify_migration_guard`，限定为 `_windows_main` 绑定监听之前、`expected_records == {"windows_active": ...}` 且 `allow_log_change=False` 的调用。

- 原顺序为：验证两把持锁 guard；单独 `inspect_runtime_identities` 并与 `legacy.snapshot` 比较；再 `_check_config_targets`，后者内部再次创建全角色 light preflight。
- 候选仍完整执行第一次持锁 guard，并执行原有 `_check_config_targets`。利用 A 所提供的该次配置检查快照，按原来的 `PATH_KEYS`、`state_dir`、`anchor.lock` 字段与 `legacy.snapshot` 比较，替代中间那一次单独全角色 snapshot；比较必须发生在 guard 成功返回前。`active.json`、账本、配置内容/marker、监听配置及最终检查维持原逻辑。
- 不改变 bind 前后、记录提交后的其他 guard 调用；不把快照跨越 bind、记录写入、锁释放或发送凭据复用。若受控 opt-in 影响其它 guard 调用或 Mac 分支，就停止实施，而非扩大范围。
- **变化/风险**：角色身份比较从原来的独立重新打开时点移到随后配置检查的同轮快照；该快照成功前仍以持有 lease 复验。中间短暂改变并恢复可能不再被独立采样；持续改变应在配置检查快照与原基线的比较中失败。原先身份不一致可在配置检查前返回，候选可能先返回配置冲突或证据未知；均不得继续绑定或写记录。原生锁/ACL/对象不可验证仍非零。
- **收益估计**：本次 guard 中相邻 `inspect_runtime_identities` 实测 5.868 秒；预计再少一次 light preflight、约 33 次 lease 复验。依赖候选 A 的可复用返回值，不修改 `verify_held_instance_locks` 的锁 guard 校验。
- **15 秒**：此检查点在本次子进程启动约 132 秒以后才出现，控制器的 15 秒等待已超时；单做 B 不能解决本次超时。

**合计边界**：在本次所观测分支，A+B 静态估计 15→13 次 light preflight、约 66 次 lease 复验；以两次样本耗时相加约 11.73 秒，未扣候选额外成本且非优化后结果。919 次是截点前的部分启动计数；绑定、记录和 READY 后续仍未完成，不能据此推算完整五命令成功或承诺 15 秒内 READY。

## 风险与推演

1. **短暂 ABA**：在 A 或 B 被取消的独立扫描恰好发生时，角色从 A→B→A；原检查可能观测到 B，候选可能只见 A。保留真实私密 ACL 和双锁并不能证明能发现这种瞬时变化；原实现也不保证发现所有 ABA。此差异属于已确认方向下的具体采样取舍，不能默认为无风险等价。
2. **持续替换**：A 后到取锁前的运行角色替换，应在锁前重新打开与返回 snapshot 比较时拒绝；B 的持锁验证后角色持续替换，应在随后配置检查得到的 snapshot 与 `legacy.snapshot` 比较时拒绝。若任何路径发现旧记录、PID/start-lock 或实例状态无法判断，仍 UNKNOWN，不自动接管。
3. **权限与竞争**：取消额外全链不取消任何在实际使用敏感对象、锁和记录时的 ACL 验证，也不改变新文件创建时保护/后验失败。两个启动竞争时仍只有持有两把实例锁者可执行日志/账本副作用；短控制锁及同 socket、同 HANDLE 校验保持原路径。
4. **无法保证的范围**：两候选不能证明无恶意同用户 ABA、任意 Windows 文件系统行为、Wine 可创建 protected DACL 或原生 Windows 可用。若需要原先每个独立采样点，则保留它们，不能实施这两个删减；性能目标另行分析。
5. **错误与并行**：只在明确 Windows 分支使用；不得为了处理冲突而改其它调用方的顺序或 Mac 契约。scope/文件/锁释放未知必须报告 UNKNOWN，不能把句柄异常换成“无冲突”。`server.py` 正有控制线并行开发，先协调单一负责人、复核最新 diff 后再动。

## 验证方式

1. **Fake 契约与计数**：在 `tests/test_windows_paths.py`、`tests/test_windows_discovery.py`、`tests/test_windows_server_safety.py` 和 `tests/test_windows_b_startup_concurrency.py` 所属回归中补/运行目标用例。以真实产品比较/ACL 策略配 fake OS backend：A/B 分别核对 preflight 和 `PathLease.revalidate` 计数，保持六角色、三配置、state 检查、直接父目录和关闭失败语义；对注入的持久变更、父链替换、同路径不同 file ID、ACL 不可信、同物理文件别名、8.3/Unicode、PID/start-lock 出现逐项拒绝。用可控注入记录短暂 ABA 的可漏检边界，不把未检出标成安全保证。
2. **控制与 Mac 回归**：双启动竞争、已有目标/foreign listener、日志锁后副作用、记录写入 UNKNOWN、实际同 socket token 与已验证同 HANDLE 停止；只使用自建实例。跑既有 Mac 共享入口、私有配置、链接兼容测试，不修改其分支或三记录契约。
3. **Wine 实测**：在 A、B 及组合的相同源码指纹上各用新 fake 项目、假 HOME/配置/凭据、OS 分配独立端口及原始 Popen 收尾，记录命令、每阶段时间、preflight/ACL 调用次数、退出码与日志；不得只依赖 mock 或延长 15 秒等待宣称成功。预置 unprotected-private fixture 和 fresh 场景分开；fresh 的 protected 读回失败须如实报告，不能借预置结果冒充首次创建成功。
4. **原生 Windows**：在授权后的真实 Windows 环境中以新临时对象重做 protected 新建与读回、锁竞争、角色冲突、同 socket/同 HANDLE 和五命令；只停止本轮自建进程。其前不得声称原生 Windows 或首次安装成功。
5. **门禁与度量**：保留服务实际 15 秒等待，逐项报告 READY/失败/未执行与进程收尾。A/B 的耗时含子调用不可相加作 CPU 时间；须给原版与候选的真实重复计数和端到端 wall time。若不能在基线内完成，不放宽 ACL、不仅靠延长超时，以数据说明剩余耗时并另评估。

**交付物规范**：本记录是 `W/docs/designs/` 下单份 draft；候选只改已有产品/测试文件，测试临时证据放 `/tmp` 或新 fake 项目，无新产品运行产物。

## 关联

- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy共享Python与Windows最小适配再审视]]
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/docs/designs/2026-11-15-model-proxy-Windows适配复杂度独立复核]]
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/server.py]]
- [[tools/model_proxy/.claude/worktrees/qw/feat-windows-unified-cli/core/discovery.py]]
