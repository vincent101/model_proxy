---
type: design-decision
status: pending
target: "[[tools/model_proxy]]"
tags:
  - architect
  - model_proxy
  - cli
  - status
modified: 2026-08-08 20:30:00
created: 2026-08-08 20:30:00
---

# status 子命令内容重构（理想路径 · 运行态优先）

> **注**：P0 档已实施（经 2026-08-08-status-p0-implementation-plan 落地并后续精简）；P1（server 端 started_at 等）/P2 未实施。

> [理想] 路径：不计改动成本，从运维实际使用场景出发，重新定义 status 这个"一眼概览"命令该展示什么。
> 与 [[2026-08-08-cli-status-help-improvement-plan]]（已落地，格式层：单源化/紧凑/停机降级）正交——那篇改"怎么排版"，本篇改"展示什么内容"。不否定格式改进，是在其之上重定内容边界。
> 核实基准：2026-08-08 master 工作区，代理在跑（pid 74845, port 18889）；config = 25 supplies / 6 routes / 4 strategies；账本 `.claude_model_proxy_totals.json` v3。

## 1. 背景与问题

用户原话："cli status 的东西太多了（supply/route/strategy 的配置，对应模块下也可查），核心的状态反而查不到（strategy/route 下的 supply 是否正常有没有 down，服务启动状态和时间等）。"

矛盾点：**status 用大量篇幅展示配置态（静态、菜单 list 也能查），却把运行态（动态、只有 status 能查）挤到角落甚至完全没有。** 本次从内容层重审：status 作为"一眼概览"，该展示什么、不该展示什么。

## 2. 使用场景推演（先定场景，再定内容）

运维敲 `status` 的真实时刻与想立刻回答的问题，逐个判定现状能否回答：

| # | 场景（敲 status 的时刻） | 想立刻知道 | 现状能否回答 | 缺口性质 |
|---|---|---|---|---|
| A | 日常巡检 / 起服后确认 | 代理在跑吗？哪个端口？ | **能**（首行 running/NOT running） | — |
| B | 出了怪问题想"是不是该重启了" | 跑了多久？上次何时重启？pid？ | **不能**（server 无 started_at/pid/uptime） | 运行态缺口，但**可离线推导**（见 §4.2） |
| C | 看到 failover/慢 | 现在有没有 supply 在冷却？ | **能**（cooldown 段），但埋在 25 行 supplies + 6 行 routes + 4 行 strategies 之后 | 排版问题（已部分缓解） |
| D | 刚收到一个 503/失败 | 是哪个 supply 挂的？ | **不能**。cooldown 只在刚跳闸时有提示；`logs req=xxx` 才能定位 | 运行态缺口 |
| E | 某 route 用着不对劲 | 这个 route 现在健康吗？档内 supply 可用几个？ | **不能**（status 只展示 route 静态模板） | 运行态缺口，可从账本推导 |
| F | 怀疑某 supply key 失效 | 这个 supply 是不是 down 了？ | **不能**（proxy 无持久 down 概念，cooldown 是临时态） | 运行态缺口，可从账本推导 |
| G | 想看配置长什么样 | supplies/routes/strategies 配置 | **能**，但与 `supply`/`route`/`strategy` 菜单 list **重复** | 冗余（用户抱怨点） |

**结论**：status 当前能答的是 A、C、G——其中 G 是冗余（菜单重复），A/C 是真运行态。运维更想问的 B、D、E、F（全是"运行健康"）status 一个都答不了。这正是"核心的状态反而查不到"的根因。

## 3. 现状五段的内容定性（配置态 vs 运行态）

`cmd_status`（`model_proxy_cli.sh:127-148`）→ `GET /model_proxy/status` → `_handle_status`（`core/server.py:1946-1976`）→ `_format_ops.py` `status-format`（`_format_status_from_json:267-295`）渲染五段：

| 段 | 内容 | 定性 | 菜单是否重复 | 只有 status 能查？ |
|---|---|---|---|---|
| supplies 平铺（25 行） | id/protocol/model/key 脱敏 | **配置态** | 是（`supply` list，单源 `format_supplies`） | 否 |
| routes 家族模板（6 行） | id + tiers + failover | **配置态** | 是（`route` list，单源 `format_routes`） | 否 |
| strategies 绑定（4 行×2） | token→route + override 计数 + note | 配置态 + **override 计数是运行态** | 配置部分重复（`strategy` list）；override 计数菜单没有 | override 计数是 |
| cooldown | supply→剩余秒 | **运行态** | 否 | **是** |
| default_cooldown_seconds | 静态数值 | 配置态 | 是 | 否 |

**净结论**：五段里约 3.5 段是配置态（与菜单重复），1.5 段是运行态（cooldown 独有 + override 计数半独有）。status 当前是"配置复读机 + 一点点运行态"，与用户诉求正好倒挂。

## 4. 运行态信息缺口（server 能否提供，以代码为准）

### 4.1 supply 健康 / 有没有 down

- **proxy 无持久 down 概念**。`CooldownStore`（`server.py:524-563`）自述"不记账、不轮转游标、不写盘"，只记 `_until`（冷却截止时间），`snapshot()` 只返回仍在冷却中的 supply 剩余秒。这是**临时冷却**，不是 down。
- **但健康数据已存在账本的失败计数里**。`UsageTotalsStore`（`server.py:156-292`）按 supply×route×strategy 组合键累加 `ok`/`fail`/`attempts`/`attempt_fail`，按天分桶。**读今日桶即可推导 per-supply 近期失败率**，无需 server 改动（`stats` 子命令已在读同一文件）。
- **实测佐证（2026-08-08 today 桶，按 supply 投影）**：
  ```
  kimi-k3-sankuai-3672   req=15  fail=12  fail%=80.0   ← 疑似失效 key
  kimi-k3-sankuai-9907   req=22  fail=15  fail%=68.2   ← 高失败
  kimi-k3-sankuai-4200   req=25  fail=15  fail%=60.0   ← 高失败
  kimi-k3-sankuai-8101   req=48  fail=8   fail%=16.7
  kimi-k3-sankuai-3339   req=431 fail=6   fail%=1.4    ← 健康
  ```
  这正是用户问的"哪个 supply down 了"——**数据今天就有，status 却一行都不展示**。注意 `supply=(none)` 有 233 请求全失败（未分配到 supply 的请求，多为 401 no strategy/route matched），也是值得暴露的健康信号。
- 粒度说明：账本 today 桶是"今日累计"，非"最近 N 分钟"实时滑窗。要实时滑窗需 server 内存态新增（见 §6 分档）。

### 4.2 服务启动状态和时间

- `_handle_status` 返回体只有 supplies/routes/strategies/cooldown/default_cooldown_seconds（`server.py:1970-1976`），**无 started_at/pid/uptime**。OPT-12 批次三提过未做。
- **但可零 server 改动离线推导**：进程锁文件 `/tmp/claude_model_proxy.lock` 已写入 pid（`server.py:2364`），CLI 侧 `ps -o lstart/etime -p <pid>` 即得启动时刻与已运行时长（实测：pid 74845, STARTED=Sat Aug 8 19:53:05, ELAPSED=10:03）。server 补 `started_at` 更干净（单一事实源），但非必须。

### 4.3 route / strategy 健康度

- 账本 combo 键是 `supply=|route=|strategy=`（`server.py:211-217`），**可按 route 或 strategy 维度聚合近期 ok/fail**，推导"某 route 今日成功率""某 strategy 下各 supply 可用性"。同样两条路：CLI 读账本（无 server 改动）或 server 内存滑窗（实时，需新增）。

## 5. 理想形态（目标结构）

**设计原则：status 是运行态健康仪表盘，"异常优先、无消息即好消息"；配置详情交还菜单 list。** 健康时一屏 ≤15 行；出问题时第一眼就看到是哪个 supply/route。

### 5.1 status 该展示（运行态，status 独有或运维第一诉求）

```
model_proxy: running on port 18889  pid 74845  up 2d4h  (started 08-06 16:20, config mtime 08-08 20:01)

health: cooldown 1/25 · degraded 3 · overrides 1 · orphan 2
                                                    ↑ 一眼总览；全 0 时即"系统健康"

degraded supplies (today fail%):                    ← 只列异常的，健康的不列
  kimi-k3-sankuai-3672   fail 80% (12/15)   ⚠ 疑似失效，建议 supply test
  kimi-k3-sankuai-9907   fail 68% (15/22)
  kimi-k3-sankuai-4200   fail 60% (15/25)

cooldown (剩余秒):
  kimi-k3-sankuai-8101   42s

route health (today):
  nation1   ok 99% · 档内可用 opus 2/3（9907 高失败）
  nation2   ok 96%

config: 25 supplies / 6 routes / 4 strategies / cd=60s   ← 紧凑计数一行，不展开
       （配置明细: supply / route / strategy 菜单 list）
```

各段来源与缺口标注：
- **进程行**：port（现有）+ pid/uptime/started（离线 ps 推导，或 server `started_at`）+ config mtime（`os.path.getmtime`）。
- **health 总览行**：cooldown 数（CooldownStore 现有）+ degraded 数（账本推导）+ overrides 数（sidecar 现有）+ orphan 数（config 静态可算）。
- **degraded supplies**：账本 today 桶按 supply 投影，fail% 超阈值（如 >30% 且样本 ≥5）才列。**这是把 stats 已有的能力"上提"到 status 第一眼**。
- **cooldown**：现有段上移（从尾部提到异常区）。
- **route health**：账本按 route 聚合 + 档内 supply 可用数（cooldown ∪ degraded 反推）。
- **config 计数行**：只给数量与 cd，不展开平铺。

### 5.2 status 不该展示（配置态，菜单重复，砍或收敛）

| 段 | 处置 | 理由 |
|---|---|---|
| supplies 25 行平铺（含 key 脱敏） | **砍出默认 status**，收敛为一行计数 | `supply` 菜单 list 已单源展示，且更全（多 rcap/cooldown 列）。status 平铺 25 行是把配置复读一遍 |
| routes 家族模板展开 | **砍出默认 status**，收敛为计数 + 健康 | `route` 菜单 list 重复。status 只保留 route 的"健康度"（运行态），不保留"模板组成"（配置态） |
| strategies 绑定展开 | **砍出默认 status**，收敛为计数 + override 活跃数 | `strategy` 菜单 list 重复。override 计数上提到 health 行 |
| default_cooldown_seconds 单独行 | 并入 config 计数行 | 一行太碎 |

**不是删信息，是归位**：配置详情仍在 `supply`/`route`/`strategy` 菜单 list（单源 `format_supplies/routes/strategies` 的 MENU preset 继续服役）。status 只留"配置有几条"的方向感 + "哪些出问题"的运行态。需要看全量配置时一条菜单命令即达。

### 5.3 保留的逃生门

- `status --json`：直通 server 原始 JSON（机器消费/调试），保留。
- `status config`（可选）：想在 status 里看全量配置时显式展开，等于在 status 里调一次 MENU preset——满足"偶尔想一屏看全"的习惯，但非常态默认。

## 6. 实施分档（按 server 改动量）

> 理想路径定目标，落地按改动量分档，供排期参考（不因代价改目标结构）。

**P0 · 零 server 改动（CLI + `_format_ops.py` 即可达成）**——性价比最高，先做这个：
- 进程行 pid/uptime/started：读 `/tmp/claude_model_proxy.lock` 取 pid + `ps -o lstart/etime`。
- degraded supplies / route health：读账本 today 桶（复用 `stats` 的 `select_bucket`/投影逻辑，抽成 `_format_ops` 可 import 的函数）。
- 砍掉 supplies/routes/strategies 全量平铺，收敛为 config 计数行 + 异常区。
- cooldown 段上移。

**P1 · server 只加不改（前向兼容）**：
- `_handle_status` 补 `started_at`/`pid`/`config_mtime`（进程行单一事实源，替代 P0 的 ps 推导）。
- `CooldownStore.cooldown()` 签名补 `last_trigger`（触发状态码+时刻），让 cooldown 段能显示"因何跳闸"。松动"不记账"定位，内存态 O(supply 数)、不写盘。

**P2 · server 新增内存态（实时健康滑窗）**：
- per-supply 近期滑窗（最近 N 分钟 ok/fail + last_error + last_success_time），替代账本 today 粒度，做真正的"实时 down 检测"。这是 D 场景（刚才那个 503 是谁）的彻底解法。

**迁移/落地代价提示**（仅供知情，不改设计）：P0 会让 status 默认输出从"40+ 行配置平铺"骤变为"~15 行健康摘要"，习惯看全量配置的用户需改用菜单 list 或 `status config`；`_format_ops` 需新增账本读取路径（注意保持纯 stdlib import 链约束）；P1/P2 需 server 回归测试。

## 7. 风险与权衡

1. **degraded 判定阈值是拍脑袋风险**：fail%>30% 且样本≥5 是经验值。账本 today 是累计值，跨时段失败后已恢复的 supply 会被误标 degraded——缓解：P2 实时滑窗，或 CLI 同时展示 fail 数让运维自判。**阈值需用户确认**。
2. **账本读延迟**：status 每次读 `.claude_model_proxy_totals.json`（当前 54KB）解析 JSON，与现有 fork python3 开销同级，可接受；账本涨到 MB 级需评估。
3. **`(none)` supply 的失败归属**：233 全失败的 `(none)` 请求多为路由未匹配（401），不是 supply 健康问题，展示时需单列避免误读为"有个叫 none 的 supply 挂了"。
4. **cooldown "不记账"定位**：P1 补 last_trigger 松动了 CooldownStore 的精简定位（[[2026-08-07-cli-status-help-assessment]] S11 已标权衡）。坚持不松动的替代是运维 `logs` grep 503 自行关联。
5. **与格式改进的关系**：本篇砍掉的 supplies/routes/strategies 平铺段，正是 improvement-plan 花了大力气做单源化+紧凑+80 列的段。**格式工作不白费**——那些 MENU preset 函数继续供菜单 list 使用；只是 status 默认不再调 STATUS preset 的全量平铺。需在实施时明确：STATUS preset 是缩成计数行还是整体下线，**这点请用户拍板**。
6. **单用户内部工具的"一屏看全"习惯**：完全砍配置可能让习惯 status 看全量的用户不适。`status config` 逃生门（§5.3）是缓冲。

## 8. 验证方式

- **场景验证（对 §2 七个场景逐个核对）**：改造后 A/B/C/D/E/F 应能直接从 status 输出回答，G 由菜单 list 接管。逐个构造场景验证。
- **健康时收敛**：全部 supply 正常时 `status` ≤15 行，无平铺列表。
- **异常时可见**：手工 `supply test` 制造一个高失败 supply（或直接读现有账本 3672），`status` 应在 degraded 区列出它。
- **uptime 准确**：`status` 显示的 up/started 与 `ps -o lstart -p $(cat /tmp/claude_model_proxy.lock)` 一致。
- **不破坏菜单**：`supply`/`route`/`strategy` 菜单 list 输出不变（仍走 MENU preset）。
- **停机降级**：代理不在跑时 `status` 仍显示 config 静态计数 + 标 `(代理未运行)`，退出码 1。
- **既有套件**：`cd tools/model_proxy && python3 -m unittest discover tests` 全绿。

## 关联

- [[2026-08-07-cli-status-help-assessment]]（评估依据，S12 started_at 缺口、S11 cooldown 权衡出处）
- [[2026-08-08-cli-status-help-improvement-plan]]（已落地格式层改进，本篇在其上重定内容边界；MENU preset 函数归菜单继续用）
- [[2026-08-06-session-overrides-single-storage]]（overrides 计数来源 sidecar）
- [[2026-08-04-in-band-route-command-design]]（$route 产生 override）
