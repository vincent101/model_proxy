---
type: design-decision
status: confirmed
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, deployment, rollback, session-route-dispatch]
---

# 沙箱 → 生产 同步部署方案（session-route-dispatch 功能上线）

> 前置功能设计见 [[2026-07-28-session-route-dispatch-design]]。本文只解决"怎么把已在沙箱验证通过的改动安全同步回正在跑真实流量的生产，且可备份、可秒级回退"。功能对错已由两轮 reviewer 复核 + 416 单测 + 真实 CC 请求验证确认，不再论证。

## 背景与问题

- 生产：`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/`，端口 18889，进程 pid 61666（已连续运行 2 天 21 小时），持续转发用户所有真实 CC/codex 请求。
- 沙箱：`/tmp/model_proxy_sandbox/`，端口 18899，锁 `/tmp/claude_model_proxy_sandbox.lock`，与生产完全隔离，已实现并验证"session 级多 route 分配（方向二，选项B 粘性+跨route兜底）"。
- 要求：把沙箱功能代码同步回生产，**不影响生产现有 cc/codex 行为**，必须有备份和秒级回退。

已用 `diff -rq` 逐文件核实过两侧真实差异（不依赖文档转述），下面清单是核实结果，非推测。

## 1. 文件改动清单（已逐行核实）

### 1.1 要同步的文件（4 个，全部功能代码）

| 文件 | git 追踪 | 差异摘要（沙箱相对生产新增的功能） |
|---|---|---|
| `core/server.py` | 是 | ① 顶部新增 `import hashlib`（`extract_route_candidates` 的一致性哈希用）；② 新增函数 `extract_session_key(body_json)`（二次 `json.loads` 解析 `metadata.user_id` → `session_id`）；③ 新增函数 `extract_route_candidates(strategy, session_key, routes_map)`（旧单值 route_id 兼容 + route_pool 一致性哈希 + session_overrides 优先 + fallback + 脏配置容错，返回候选 route 列表）；④ ACCESS 日志累加器 `_acc` 新增 `session`/`route_failover` 两字段，日志格式串加 `session=%s route_failover=%s`；⑤ `_forward` 主流程：原 `route = routes_map.get(strategy.get("route_id"))` 单选，改为 `route_candidates = extract_route_candidates(...)` + 在 supply 级 while 循环外再套一层 route 候选 for 循环（选项B：pin route 全 supply 挂/缺 tier 时换下一候选 route，单候选时退化为只跑一轮 = 现状行为）。|
| `_config_ops.py` | 是 | ① 新增 `_strategy_route_desc(st)`（打印时兼容 route_pool 与旧 route_id）；② 新增 `_validate_strategy_route_fields(entry)`（route_id 与 route_pool 互斥，违规则 err+exit(1) 不写盘）；③ list/edit/switch 三处 CLI 对 route_pool 写法做兼容（edit 遇 route_pool 跳过单值录入且保留 route_pool/dispatch 不丢；switch 拒绝对 route_pool strategy 写单值）；④ add/edit 写盘前调用互斥校验。|
| `tests/test_config_ops.py` | 是 | 纯增量：新增 `TestValidateStrategyRouteFields` 5 个用例（互斥校验各分支），import 加 `_validate_strategy_route_fields`。无删改现有用例。|
| `tests/test_session_route_dispatch.py` | 否（新文件） | 全新纯函数测试，23 用例，覆盖 `extract_session_key` / `extract_route_candidates` 全分支。脱网络、纯标准库 unittest，无任何沙箱专属路径/端口引用（已核实 grep 无 sandbox/18899）。`sys.path.insert` 用 `__file__` 相对定位，带回生产可直接跑。|

**同步方式**：`core/server.py` 与 `_config_ops.py` **不能整文件覆盖**（server.py 头部注释和 `_LOCK_FILE` 行是沙箱补丁，见 1.2），需按下面部署步骤用受控方式处理；两个 test 文件无沙箱污染，可整文件复制。

### 1.2 必须排除的沙箱专属补丁（绝对不带回生产）

| 位置 | 沙箱值 | 生产必须保持 |
|---|---|---|
| `core/server.py` 第 8 行文件头注释 | `.../claude_model_proxy_sandbox.lock` | `.../claude_model_proxy.lock` |
| `core/server.py` 第 244 行 `_LOCK_FILE` | `Path("/tmp/claude_model_proxy_sandbox.lock")` | `Path("/tmp/claude_model_proxy.lock")` |
| `model_proxy_cli.sh` 第 13 行 `LOCK_FILE` | `/tmp/claude_model_proxy_sandbox.lock` | `/tmp/claude_model_proxy.lock` |

- `model_proxy_cli.sh` **两侧唯一差异就是这一行锁路径**，没有任何功能改动 → **整个 cli.sh 不同步，生产保持原样**。
- `core/server.py` 的 `_LOCK_FILE`（第 244 行）和头部注释（第 8 行）**保持生产原值不动**，只把 1.1 里列的功能片段搬过去。
- 附带提示（非补丁但可优化）：沙箱 server.py 还新增了一行 `import re`，但全文无 `re.` 实调用（已核实），是死 import。同步时**建议不带这行**以保持整洁；带了也无害。

### 1.3 待用户确认，不自行决定的项

- `config/model_proxy_config.json`：两侧唯一差异是沙箱多了一条 `cc-multi-test` 示例 strategy（route_pool claude:2/deepseek:1 + 一条 all-zero uuid 的 session_overrides 示例）。**此文件被 .gitignore（不受版本控制），是用户真实在用的生产配置。默认不动它**（见开放问题 §6-Q1）。

## 2. 备份步骤（先备份，后动任何文件）

生产 4 个源文件 git 干净（`git status --porcelain` 为空，与 HEAD 完全一致），但**双保险**：既做 git 基线记录，也做物理文件副本，因为 config 不受 git 管、且物理副本回退最快最直观。

```bash
cd /Users/vincentwang/Documents/NoteVault/tools/model_proxy

# 2.1 记录当前 git 基线 commit（回退用）
git rev-parse HEAD | tee /tmp/model_proxy_deploy_baseline_commit.txt

# 2.2 物理备份（带时间戳，放 /tmp，与生产目录隔离）
BK=/tmp/model_proxy_backup_$(date +%Y%m%d_%H%M%S)
mkdir -p "$BK/core" "$BK/tests" "$BK/config"
cp core/server.py                         "$BK/core/server.py"
cp _config_ops.py                         "$BK/_config_ops.py"
cp tests/test_config_ops.py               "$BK/tests/test_config_ops.py"
cp model_proxy_cli.sh                     "$BK/model_proxy_cli.sh"   # 虽不改也备，回退全集
cp config/model_proxy_config.json         "$BK/config/model_proxy_config.json"
echo "$BK" | tee /tmp/model_proxy_last_backup_path.txt

# 2.3 验证备份完整可用（比对字节级一致 + 备份的 server.py 语法可解析）
diff -rq "$BK/core/server.py" core/server.py && echo "server.py 备份一致"
diff -rq "$BK/_config_ops.py" _config_ops.py && echo "_config_ops 备份一致"
diff -rq "$BK/config/model_proxy_config.json" config/model_proxy_config.json && echo "config 备份一致"
python3 -c "import ast; ast.parse(open('$BK/core/server.py').read()); print('备份 server.py 语法 OK')"
```

备份验证判定：三条 `diff -rq` 均无输出（一致）、语法解析打印 OK → 备份可用，才继续。任一失败 → 停止，排查。

## 3. 部署步骤

### 3.1 部署前静态验证（不碰生产进程）

先把改动落到生产源文件，但**先不重启**，用静态方式确认不破坏现有行为：

1. 应用 1.1 的功能改动到 `core/server.py` 和 `_config_ops.py`（用 implementer 按 1.1/1.2 清单精确改，**排除 1.2 的锁路径补丁**）；整文件复制两个 test 文件。
2. 语法与测试全绿（在生产目录，用 unittest，因当前解释器无 pytest）：
   ```bash
   cd /Users/vincentwang/Documents/NoteVault/tools/model_proxy
   python3 -c "import ast; ast.parse(open('core/server.py').read()); ast.parse(open('_config_ops.py').read()); print('语法OK')"
   python3 -m unittest tests.test_session_route_dispatch tests.test_config_ops 2>&1 | grep -E '^(OK|FAILED|Ran )'
   ```
   判定：语法 OK 且两个测试模块均 `OK`（新测试 23 + config 相关全绿）才继续。
3. 确认 `_LOCK_FILE` 仍是生产值（防止误带补丁）：
   ```bash
   grep -n 'claude_model_proxy.lock' core/server.py     # 应命中,且不含 _sandbox
   grep -c '_sandbox' core/server.py model_proxy_cli.sh  # 应为 0 0
   ```

### 3.2 重启进程（换代码只有重启才生效）

**关键事实（已核实）**：server 用 `ThreadingHTTPServer`，`daemon_threads = True`（Python 标准库对该类的默认值，已实测确认），且 `main()` 未注册 SIGTERM/SIGINT handler。`model_proxy_cli.sh off` 执行的是 `kill <pid>`（默认 SIGTERM）。后果：

- **正在处理中的请求会被硬截断**：daemon 线程随主进程退出立即终止，`finally: server.shutdown()` 在收到裸 SIGTERM 时不保证执行。**流式/长连接请求（CC 的绝大多数 `/v1/messages` 是 `stream:true`）会在重启瞬间断流**，客户端表现为该次请求中断/报错。
- 没有优雅 drain 机制（代码层面不存在等待在途请求完成再退出的逻辑），本次上线不引入（属功能改造，超出"安全同步"范围）。

因此重启策略：**选一个用户手上没有活跃 CC 会话的时刻**（比如用户主动停下手上的 CC 任务、或明确的空闲间隙）执行，把"被截断的在途请求"降到 0 或 1 次可接受的重试。重启命令：

```bash
cd /Users/vincentwang/Documents/NoteVault/tools/model_proxy
./model_proxy_cli.sh off     # kill 旧进程(SIGTERM),释放 18889 与锁
./model_proxy_cli.sh on      # nohup 拉起新进程,内部会自旋等端口 LISTEN 最多 5s
```

`cmd_on` 已内置"启动后自旋检查端口 LISTEN，最多 10×0.5s，失败则打印 WARNING + 尾日志"。若 `on` 打印 WARNING 未起来 → 直接进 §5 回退。

时机建议：无需严格"低峰时段"（这是个人代理非多租户），只需**用户确认此刻自己没有正在跑的 CC/codex 请求**即可重启，代价最小。

## 4. 验证步骤（部署后按序）

按"现有功能零回归 → 新功能路径无回归"的顺序，前者是硬门槛。

### 4.1 进程与端口存活（最基本）

```bash
lsof -i :18889 -sTCP:LISTEN                       # 应有 python3 model_proxy.py 在 LISTEN
cat /tmp/claude_model_proxy.lock                  # 应是新进程 pid,且与上一行一致
```
判定：端口在听、锁文件 pid 与监听进程一致。否则回退。

### 4.2 现有 cc / codex 行为零回归（最关键,必须通过）

cc/codex 在生产都是**单值 route_id**（cc→claude、codex→openai，已核实），走 `extract_route_candidates` 的"无 route_pool → 旧写法分支"，逻辑与改动前等价。验证：

1. 用户在正常 CC 里发一条普通请求（或跑一个小任务），确认正常返回、无异常。
2. 看 ACCESS 日志确认新字段已出现且 route 正确：
   ```bash
   ./model_proxy_cli.sh logs 10
   ```
   判定：cc 的记录 `route=claude`、`route_failover=0`、`session=<非空 uuid 或空>`；codex 记录 `route=openai`。**`route_failover` 必须是 0**（单值 route 永不触发跨 route）。若 cc 请求 route 变了、或 route_failover=1、或大量 5xx → 判定回归，回退。

### 4.3 新功能路径无回归（可在不碰真实流量下做）

新代码路径（route_pool/session_hash/overrides）**只有配置了 route_pool 的 strategy 才会走**。现有 cc/codex 都不走它，所以：

- **纯函数层已由 §3.1 的 23 个单测覆盖**（生产目录跑绿即证明新函数逻辑在生产代码里无回归），这一步已在部署前完成，是新功能"代码没引入回归"的主要证据，**不需要动真实流量**。
- **是否要在生产做一次端到端活体验证**：需要用户手动配一个真实的多 route strategy 才能触发（例如临时给一个新 client_token 配 route_pool）。这属于"验证新功能本身是否如期工作"，**不是"确认没坏"的必要条件**，建议作为可选项、由用户决定是否要顺便验（见 §6-Q2）。若要验，最小方式：临时加一个 `cc-multi-test` 类的 strategy + 新 token，用 `ANTHROPIC_AUTH_TOKEN=<新token>` 发几条不同 session-id 的请求，看 ACCESS 日志中 route 是否按哈希分散、同 session 是否恒定；**验完删除该临时 strategy**，不污染生产。

## 5. 回退步骤（发现问题时，秒级恢复到改动前）

回退目标 = 恢复"改动前的 4 个源文件 + 重启"。两条路，物理副本为主（最快最稳），git 为校验兜底。

```bash
cd /Users/vincentwang/Documents/NoteVault/tools/model_proxy
BK=$(cat /tmp/model_proxy_last_backup_path.txt)   # §2.2 记下的备份目录

# 5.1 恢复源文件(物理副本,一步到位)
cp "$BK/core/server.py"           core/server.py
cp "$BK/_config_ops.py"           _config_ops.py
cp "$BK/tests/test_config_ops.py" tests/test_config_ops.py
# 新增的 test_session_route_dispatch.py 是新文件,回退时删掉即可(留着也无害,不被现有代码引用)
rm -f tests/test_session_route_dispatch.py
# cli.sh 本次未改,无需恢复; config 本次默认未改,无需恢复(若 §6-Q1 选了改则一并 cp 回)

# 5.2 重启到旧代码
./model_proxy_cli.sh off && ./model_proxy_cli.sh on

# 5.3 验证已回到旧行为
lsof -i :18889 -sTCP:LISTEN
./model_proxy_cli.sh logs 5      # 旧日志格式无 session=/route_failover= 字段即已回退
```

**git 校验兜底**（确认物理恢复确实等于改动前）：
```bash
git status --porcelain tools/model_proxy/core/server.py tools/model_proxy/_config_ops.py tools/model_proxy/tests/test_config_ops.py
# 应为空(与 HEAD 一致 = 与改动前一致); 有输出说明物理副本与 git 基线不符,需人工核对
```

回退耗时 ≈ 几个 `cp` + 一次 `off/on`（`on` 自旋最多 5s），秒级完成。回退同样会截断当时在途请求（同 §3.2），影响可忽略。

## 风险与权衡

- **重启硬截断在途请求（唯一实质风险）**：`daemon_threads=True` + 无 SIGTERM handler，重启瞬间在途流式请求断流。缓解=选用户自己无活跃请求的时刻重启，代价最小。彻底解决需引入优雅 drain（超出本次范围，不做）。
- **config 不受 git 管**：`model_proxy_config.json` 被 gitignore，回退只能靠 §2 物理备份，不能 `git checkout`。已在备份步骤覆盖。若本次不动 config（默认），此风险不触发。
- **cc/codex 零回归的信心来源**：二者是单值 route_id，走的是与改动前逻辑等价的旧分支；§3.1 单测 + §4.2 活体各验一层。风险低但仍需 §4.2 实测一条，不跳过。
- **未同步 cli.sh 是有意为之**：两侧唯一差异是沙箱锁路径补丁，同步反而会把沙箱锁带进生产、破坏生产进程互斥。保持生产 cli.sh 原样是正确的。

## 6. 待用户确认的开放问题

- **Q1｜`config/model_proxy_config.json` 是否要带 `cc-multi-test` 示例 strategy 过去？**
  - 默认建议：**不带**。它是沙箱验证用的测试数据，生产 config 是用户真实在用的；带过去会多一条对生产无用的 strategy（虽无害——无客户端用 `cc-multi-test` 这个 token 就永不命中）。
  - 若用户希望生产也留一个多 route 示例供日后参考/直接启用 → 明确告知，我把它列入同步范围（并相应纳入 §2 备份与 §5 回退）。
- **Q2｜要不要在生产做一次新功能的端到端活体验证（§4.3）？**
  - 不做：靠单测覆盖新函数逻辑，现有 cc/codex 零回归即可上线。
  - 做：需用户临时配一个 route_pool strategy + 新 token 发几条测试请求，验完删除。请用户表态是否要顺带验、以及愿不愿意配这个临时 strategy。
- **Q3｜`import re` 死 import 带不带？** 建议不带（无实调用）。若用户想与沙箱保持逐字节一致以便未来 diff，可带，无害。
- **Q4｜重启时机**：请用户指定一个"手上没有活跃 CC/codex 请求"的时刻执行 §3.2，或确认"随时可重启、断一次在途请求可接受"。

## 验证方式（本方案自身是否成立的核对点）

- 文件差异清单已用 `diff -rq` + 逐段 diff 核实，非文档转述。
- 沙箱新测试 23 全绿、config_ops 54 全绿（已跑）。
- 生产源码语法可解析、生产 4 源文件 git 干净（已核实,回退基线可靠）。
- 生产 cc/codex 均为单值 route_id（已核实,决定其走旧兼容分支）。
- `daemon_threads=True` 已用 `python3 -c` 实测确认（决定重启对在途请求的影响判断）。

## 关联

- 功能设计：[[2026-07-28-session-route-dispatch-design]]
- 目标代码：`tools/model_proxy/core/server.py`、`tools/model_proxy/_config_ops.py`
- 启停脚本：`tools/model_proxy/model_proxy_cli.sh`（`cmd_off`=kill SIGTERM / `cmd_on`=nohup 重启）
