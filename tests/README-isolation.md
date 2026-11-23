# 测试运行入口

## 单元测试（每个文件独立进程）

```bash
python3 tests/run_unit_suite.py          # 全部可直跑的单元测试
python3 tests/run_unit_suite.py -j 8     # 并行度
python3 tests/run_unit_suite.py tests/test_x.py
```

**不要直接 `pytest tests/`**，有两个结构性原因：

1. 本目录下有一批隔离回归用例（`test_cli_*`、`test_mac_*`、`test_windows_*` 的一部分）
   在 **import 阶段**就要求「隔离项目 + 假 HOME」，直接收集会让整个会话中止
   （退出码 126/2，且没有任何输出）。它们必须走下面的 `run_isolated.sh`。
2. Windows 仿真用例会临时替换 `os.name`；同进程内跨文件串联会互相污染，
   表现是后面一批用例集体报 `WindowsPath` / POSIX 相关的错。

`run_unit_suite.py` 先探一次可收集性（rc=2 视为需要隔离环境），再按文件分进程
并行执行，因此两类干扰都不存在。

### 结果分类怎么做

平台归属由 **`tests/platform_scope.py`** 声明（唯一真相源）。`run_unit_suite.py`
按它判定，只有最后两类返回非零：

| 前缀 | 含义 |
|---|---|
| `iso` | 运行期才读 `MP_TEST_ROOT` 等隔离变量 → 必须走 `run_isolated.sh` |
| `skip` | 该文件/用例在清单里声明为**另一平台专用**，本宿主上不适用（每次都会打印原因） |
| `win` / `mac` | 原生专测在非对应宿主上失败（`test_windows_*` / `test_mac_*` 前缀兜底，仅当清单未声明） |
| `KNOWN` | 已登记在 `KNOWN_BROKEN` 的历史红灯（两平台都坏，需单独修）；**一旦通过即报 `STALE` 并让 CI 红** |
| `FAIL` | **意外失败**，需要人看 |

清单约定（fail-closed）：**未列出的文件 = `both`**，必须在 macOS 与 Windows 上
都通过；只有显式声明才会被跳过，且跳过要写「为什么换平台不成立」的理由。
新增测试默认走 `both`；要声明平台先自问是语义上只属于该平台，还是夹具偷懒。

```bash
python3 tests/run_unit_suite.py --list-scope   # 打印清单与理由
```

每次运行还会打印两张待办清单：

- **Windows 覆盖缺口**：被测逻辑跨平台、但夹具仍绑定 POSIX（如裸读 tempdir 配置）。
  这是待补项，不是豁免——它们目前只在 macOS 上被验证。
- **已知红灯**（`KNOWN_BROKEN`）：带登记日期与原因，防止沉淀成新的静默掩盖。

### 当前状态

```bash
# macOS 宿主
$ python3 tests/run_unit_suite.py -j 8
files run: 143  ok: 135  needs-isolation: 32  本平台不适用: 0  意外失败: 0

# Windows 宿主（GitHub Actions windows-latest）
files run: 141  ok: 114  needs-isolation: 33  本平台不适用: 20  已知红灯: 2  意外失败: 0
```

- **`iso`（32 个文件）**：`test_mac_config_writer*`、`test_mac_runtime_staging`、
  `test_mac_writer_interruptions` 等，运行时才读 `MP_TEST_ROOT`/`MP_TEST_PROJECT`，
  必须走 `run_isolated.sh`。
- **已知红灯 2 个**：`test_windows_gate`、`test_windows_secure_install_wiring`
  ——两平台都已坏（此前分别被 `test_windows_` 前缀与 `windows-native` 特征串掩盖），
  待单独修复。

### 已修好的历史失败（备查）

| 类别 | 根因 | 涉及 |
|---|---|---|
| 临时目录符号链接 | macOS `/var` → `/private/var`；生产不变量要求父链无链接、固定目录为规范路径 | `test_secure_files`、`test_backup_close_failure`、`test_platform_lock`、`test_discovery_anchor`、`test_windows_gate`(部分) |
| 旧目录名残留 | `.model_proxy_runtime` 自 v0.14 起统一为 `.runtime`，`core/` 已无旧名 | 29 个测试文件 |
| mock 目标过期 | 写入改为 `secure_replace_runtime`（多 `locations`/`boundary_guard`） | `test_backup_close_failure` |
| 模块参数演进 | `read_stable_config` 新增 `trusted_boundary` | `test_usage_totals` |
| 契约变更 | 损坏账本改为降级 `LedgerUnavailable`；0644 业务配置不再被拒 | `test_windows_service_startup_wiring`、`test_install_ops` |
| 惰性导入 | 伪造 `os.name='nt'` 后再导入 `core.server` 会命中 WindowsPath | `test_windows_control_c_review` |
| 假 `os` 缺方法 | `SimpleNamespace` 无 `fspath` | `test_windows_secure_hook_install` |

### 顺带修好的产品缺陷（非测试）

1. **`install` 的 macOS 边界复核恒失败**：`_install_ops.boundary_guard` 用默认口径
   重采私有目录，而 `_mac_runtime_locations` 以 `business_xattr_compat=True` 建快照，
   目录属性一项（`None` vs `(provenance,)`）永不相等 → 每次安装都报
   「安装目标、角色或私有目录已改变」。已对齐口径。
2. **Windows 首次启动/status 误报证据不可用**：状态目录尚未创建时，其下固定记录的
   父目录缺失会让 `_windows_open_preflight` 以 `boundary_unavailable` 失败。
   现在缺席时合成「明确缺失」身份，其余角色照常核验。
3. **Windows 旧版运行时目录缺席时 `off`/`status` 误报**：`_idle_readonly` 只认
   `ResolvedRuntimePaths` 的溯源标记，而 active.json 反序列化后是普通 dict，
   溯源必然缺失 → 退回逐个 inspect 后失败。改为「三条路径逐字等于映射默认值 +
   仅作缺席观测」。

### 判断某个失败是否「新引入」

先 `git stash` 掉改动复跑一次做对照；`windows-native` 与 `iso` 两类不算回归。

## 临时副本回归

worktree 只是源码隔离，不等于数据隔离。此方案**不使用 OS 全域 sandbox**，也不提供抵御恶意代码或同用户并发篡改的安全边界。仅用于已审阅、协作开发中的 Python 代码；核心和 CLI 修改未完成之前，**只运行 `--fixture` 和夹具单测**。不要运行旧 shell CLI、生产服务或主工作区代码。

## 自动化命令

在此 worktree 根目录：

```sh
# 完全自包含的假 CLI / 假服务 / 本机 mock，不启动项目服务
./tests/run_isolated.sh --fixture --proxy-port 29491 --mock-port 29492

# 清理、路径、端口冲突和 Python 守卫的单测
python3 -I -S -B tests/test_isolation_cleanup.py -v
```

必须显式选两个不同、非线上且非 18889、1024–65535 的本机空闲端口；若被占用直接失败，请换新端口。`SO_REUSEADDR` 仅用于允许关闭后的 TIME_WAIT 复测，不绕过活跃监听冲突。需要 macOS/POSIX、Python 3.10+ 和 `lsof`，使用指定解释器例如：

```sh
MP_TEST_PYTHON="$(command -v python3.13)" ./tests/run_isolated.sh \
  --fixture --proxy-port 29491 --mock-port 29492
```

**真实源码回归的冻结阶段**：先等协调者确认核心/CLI 修改完成，并确认从复制开始到回归结束，`core/`、`VERSION` 和测试设施不会被并行修改。`--worktree-ready` 只是操作者对此作出的确认，脚本不会自动冻结源码；无法确认时不得运行。真实源码回归由协调者选择并确认**两个空闲且非线上端口**：一个代理端口、一个 mock 端口，互不相同且均不得为生产默认端口。占用即停止，不自动更换或接触现有服务。

仅在满足以上条件并确认新端口空闲后，才能在临时副本内运行：

```sh
# 将这两个变量设为协调者选定的新端口
MP_TEST_PYTHON="$(command -v python3.13)" ./tests/run_isolated.sh \
  --worktree-ready --proxy-port "$TEST_PROXY_PORT" --mock-port "$TEST_MOCK_PORT"
```

该命令使用临时副本内的 `core/cli/main.py`，并非旧 shell CLI。真实模式在**本次随机临时假项目的副本**内，对 `core/cli/authority.py` 的初始化提示做一次精确字节字符串替换；只改提示文字，不改启动、锁、来源输入、确认检查或初始化逻辑。替换后的确认仅指向当前随机目录中的假项目、独立假账本和两个测试端口，绝不确认旧服务、自启动、其他账本或云同步状态。

替换前须核对源文件在复制前后字节和文件身份未变、复制件字节相同，旧提示及指定初始化上下文各恰好命中一次；只允许替换那一处字面量，替换后核对整个副本和源文件。执行前、两次 PTY 答复前及回归结束再次核对，异常即拒绝运行或判失败。真实模式输出源文件和替换后副本的 SHA-256 以供检查。**这些检查不是源码冻结或 OS 级隔离**，并行修改仍可能造成竞争，因此仍须按上面的冻结约定操作。

这不是原始提示或人工初始化验收，也不证明生产环境或真实 worktree 实例。真实模式尚未运行；若精确文本、上下文、文件身份或其他行为变化，工具会拒绝而不是猜测或绕过。**禁止调用旧 CLI 的 `off` 或其他宽泛 kill，也不得按端口/名称清理无关进程。**回归不能代替部署授权。

## 行为与结果清单

- 显式绑定 mock 端口并预留代理端口，验证二者可用后才暂存和启动；预留释放后的竞争通过 `lsof` 核验监听者属于测试直接子进程，核验失败不发送请求。此机制不是对恶意同用户竞争的原子隔离保证。
- 首次 `on` 在独占 PTY 上运行，stdin/stdout 确为同一终端；worker 控制消息走单独管道。真实模式仅接受包含实际随机临时目录名和两个端口的精确提示 `Confirm only the fake project in randomly generated test directory <目录名> has its own independent ledger and test ports <代理端口> and <mock端口>.`。仅在新建临时根、六个路径、完整假配置和源码/副本检查通过后，按提示顺序输入固定假来源 `isolated temporary test project` 和 `YES`；每次输入前重新检查。夹具有自己的已知假提示。缺少/重复/改变提示时停止；不对未知提示自动回答。
- `on` 后记录拥有 loopback 监听的直接子进程；`status` 支持结构化 JSON，也支持首行为 `service running on port <端口>  pid <PID>` 的可读格式（可附 uptime），并要求输出中有 `config` 和 `health:` 行，且无矛盾状态或错误行。两种格式均须与受管进程及其临时 `.runtime/install.json`、READY 的 `.runtime/active.json` 和 `.runtime/last_good_paths.json` 核对：PID、项目根、监听、六路径、假初始化来源、installation ID 和 instance nonce；数据缺失或不一致不报通过。记录仅是观察信息，不单独授权发信号或发送凭据。未知状态格式必须先适配并加正反例，不能仅凭退出码报通过。
- JSON 请求后修改临时配置目标模型，再执行 `reload`、SSE 请求。调用 `off` **之前**重新核验最初的直接子进程仍存活且仍拥有原监听；否则不调用 `off`。随后检查该子进程退出。存活/监听检查与执行 `off` 之间不是原子操作，不能排除并发退出竞争；该项通过不证明强身份认证。异常时由持有子进程句柄的 worker 清理，**不使用旧 CLI 的宽泛 kill、进程名扫描或按端口 kill**。
- 本地 mock 同时实现窄范围的 Anthropic `/v1/messages` 和 Responses `/v1/responses`，所有上游均为同一 loopback mock 的不同路径与假 key；原 Anthropic→Anthropic 路由及 reload 检查保留。新增两条独立假策略，不改真实配置：

  | 场景 | 客户端 → 假上游 | 计划的真实服务回归断言 |
  | --- | --- | --- |
  | a2a | Anthropic → Anthropic | 原始单轮文本、模型及假 key；JSON 和 SSE |
  | a2r | Anthropic → Responses | 假 key、Responses 路径/目标模型、`instructions`、user `input_text`；下游 Anthropic 文本、`end_turn`/`message_stop` |
  | r2a | Responses → Anthropic | 假 key、Anthropic 路径/目标模型、`system`、user `messages`；下游 Responses 文本和 `response.completed` |

  a2r/r2a 均分别检查 JSON 和 SSE。流式 mock 在发出上游文本 delta 后、发送终止事件前暂停；测试必须先收到下游**文本 delta**才释放终止事件，并检查协议事件顺序、文本和终止状态，不仅比较 HTTP 200 或 EOF 后的完整字符串。
- 本轮只运行了自包含夹具：夹具内的简化语义转换是测试替身，不是 model_proxy 或真实模型；通过仅证明测试线路与断言在夹具上可执行。**尚未用真实源码验证新增 a2r/r2a。**这些用例只覆盖单轮纯文本和正常结束，不覆盖 reasoning/加密签名、tools、图片、多轮、异常/重试、并发、背压、性能或第三方 API 兼容性，不能声称跨协议无损。真实源码回归完成后也只能报告这些窄场景的实际结果；`status/reload` 与目标模型变化不证明完整配置一致性。

历史执行记录（2026-11-13，本机 macOS；不代表本次真实源码结果）：

| 命令 | 结果 |
| --- | --- |
| `MP_TEST_PYTHON="$(command -v python3.13)" ./tests/run_isolated.sh --fixture --proxy-port 29491 --mock-port 29492` | 夹具通过：假项目 PTY、status、a2a JSON/SSE、reload、a2r JSON/SSE、r2a JSON/SSE、off 和临时目录清理 |
| `python3 -I -S -B tests/test_isolation_cleanup.py -v` | 24 项通过：包含跨协议请求形态/假 key、文本/终止状态和 SSE 顺序的正反例，以及既有隔离边界 |
| `--worktree-ready` | **本轮未运行**：核心和控制链路仍并行开发；不代表其他会话此前的结果 |

## 隔离方式及限制

- 每次在系统临时目录创建全新 `mp-regression-*`，POSIX 不采用继承的 `TMPDIR`。只从 worktree 暂存 `core/**/*.py` 和 `VERSION`；夹具模式只暂存 `tests/fixtures/worktree` 中两个 Python 文件。不复制 worktree 的 config、secrets、日志、ledger 或运行时文件；源文件拒绝符号链接、硬链接和非普通文件。
- 人工合成假配置、假客户端/管理/上游凭据，生成与新默认值一致的相对路径：`.runtime/model_proxy.log`、`.runtime/model_proxy_totals.json`、`.runtime/model_proxy.lock`、`.runtime/model_proxy.pid`、`.runtime/model_proxy_ensure.log`、`.runtime/model_proxy_start.lock`。状态核对使用它们在临时假项目内的绝对路径；独立假账本为 `.runtime/model_proxy_totals.json`。固定记录同在临时 `.runtime/`，不会使用旧 `.model_proxy_runtime` 或 `project/runtime`。独立 HOME、TMP、XDG、APPDATA、配置、日志、账本及锁都在本次临时根内；mock 上游固定为 127.0.0.1。启动时不继承代理或真实凭据环境。
- worker 以 `-S` 启动并显式安装 Python 护栏；服务使用临时空 venv（禁用户及系统 site-packages）中的同一解释器，保持 CLI 启动的原始两项 `argv`，由临时 `sitecustomize` 在服务代码前安装护栏。启动前复核引导文件和 venv 配置未改变、site-packages 为空；除上述测试副本的提示字面量外不改源码，也不伪造生产身份检查。受控启动允许服务作为可脱离会话的**直接子进程**，worker 始终保留其 `Popen` 句柄；不允许游离后代。
- Python audit/socket/进程护栏限制文件读写、禁止探测旧 `/tmp/model_proxy*`、限制为选定 loopback 端口、拒绝数据报发送、只允许同一解释器执行临时项目内脚本、只向 worker 持有的活跃直接子进程发信号。对 Python `socket.getaddrinfo`、`gethostbyname`、`gethostbyname_ex`、`gethostbyaddr`、`getnameinfo`、`getfqdn`，仅允许数值 loopback/localhost 的受控解析；`getaddrinfo` 使用数值解析标志。**不宣称阻断全部 DNS**：原生库、`ctypes` 或其他绕过 Python 接口的路径不受此护栏保证。CLI 仍经受控 `runpy` 执行，测试不等同于未插桩环境。监听归属检查只用针对已知子 PID 的只读 `lsof`。
- 上述是协作代码的护栏而非内核隔离：native 扩展、`ctypes`、绕过 Python API、同用户并发修改/路径或 PID 竞争、强杀 worker、父进程异常崩溃均不能靠这些钩子彻底防止。不要对不可信代码或正在被并发改写的源码运行。
- 夹具不验证真实 CLI 的 Darwin 进程/连接归属、APFS/ACL 或实际初始化门禁。稳定后真实源码回归若遇这些检查失败，应保留失败结果，不在测试中伪造或绕过。
- 只删除本次创建的临时根；删除前复核目录身份，使用 Python 的 fd-based `rmtree`，不跟随 symlink。无法证明 worker 正常退出或根身份发生变化时保留目录并报错；若 worker 被 `SIGKILL` 等不可处理方式强杀，可能留存子进程/目录，需人工核对归属后处理，不能使用宽泛 kill。无 fd-based 安全删除能力的平台直接保留目录并失败。
- 此入口目前依赖 POSIX PTY/pipe、`select`、`O_NOFOLLOW`、`lsof` 和 fd-based 清理；**原生 Windows 未实现/未验证**。请勿将本机夹具通过解释为真实源码或 Windows 平台通过。

## 本次工具验证（2026-11-18，本机 macOS）

```sh
/opt/homebrew/bin/python3.14 -I -S -B -W error::ResourceWarning tests/test_isolation_cleanup.py -v
MP_TEST_PYTHON=/opt/homebrew/bin/python3.14 ./tests/run_isolated.sh \
  --fixture --proxy-port 29491 --mock-port 29492
```

工具单测 42 项和夹具通过；真实模式暂存分支的单测仅使用合成源码，不复制或运行并行修改的核心。**本次未执行真实模式**，未验收原始提示或人工初始化，也未证明生产环境或真实 worktree 实例。下次须由协调者冻结核心后，选取两个当时确认空闲的非生产端口再执行；若源码不再精确匹配则停止。
