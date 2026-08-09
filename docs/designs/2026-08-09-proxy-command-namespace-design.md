---
type: design-decision
status: draft
target: tools/model_proxy
tags: [architect, model_proxy, in-band-command]
---

# $proxy 命令命名空间扩展设计（route 迁移 + status + stats）

## 背景与问题

model_proxy 现只有 `$route` 一个 in-band 命令（消息级拦截）。用户要求建立 `$proxy` 命名空间：`$route` 迁移为 `$proxy route`，新增 `$proxy status`、`$proxy stats`。本文给出解析规则、handler 分发、输出格式与影响面设计。[务实路径：复用现有命令层骨架，不重构。]

## 现状摸底（已核实，非推测）

**$route 完整链路**：
`server.py::_forward`（约 L1242-1258）门控（source==anthropic + session_key 非空 + strategy 命中 + body 含 messages）→ `extract_last_user_message_content`（只取最后一条 user 消息）→ `parse_route_command`（单行 + 首 token 精确 `$route` + ≤2 token，任一不满足 fail-open）→ `_handle_builtin_command`（构造 CommandContext，固定查 `COMMAND_HANDLERS["$route"]`）→ handler 回执 → `_write_builtin_stream/buffered_response`（自造 anthropic 回执，usage 全 0）。ACCESS 日志记 `builtin=route`。

**关键数据结构**：
- sidecar `config/session_overrides.json`：`{client_token: {session_id: {route_id, last_seen, created}}}`，`SessionOverridesSidecar` 在 commands.py 内，mtime 热重载 + 内存 last_seen 记账。
- config：supplies(id,url,protocol,appkey,target_model,reasoning_capability) / routes(id,tiers,failover) / strategies(client_token,route_pool,tiers_source_capability,note) / default_cooldown_seconds / admin_token。
- cooldown：`CooldownStore` 纯内存，supply_id → until epoch；`snapshot()` 返回 {supply_id: 剩余秒}（仅冷却中的）。
- 用量账本 `.claude_model_proxy_totals.json` ↔ 内存 `UsageTotalsStore`（server.py 模块级全局 `usage_totals`）：`{version:3, since, keep_days:400, total, months_archive{"YYYY-MM"}, days{"YYYY-MM-DD"}}`；每个桶 `{requests,ok,fail,sum_ms,max_ms,combos}`，combo 键 `supply=X|route=Y|strategy=Z`，combo 值 `{requests,ok,fail,usage_in,usage_out,attempts,attempt_fail}`。**无成本/价格字段**（配置里也没有定价，stats 不能出成本）。builtin 命令本身也记账（supply=(builtin)、usage 全 0、status=200）。
- **CLI 已有同名能力**：`model_proxy_cli.sh status`（在线走 admin API / 离线读文件）与 `stats [时间]`（读 totals 文件，时间桶选择 + 投影聚合），格式化在 `_format_ops.py`。in-band 版是同一数据源的无 admin token 会话内视图，聚合口径应与 CLI 保持一致，但为遵守命令层边界只能读**内存 stores**，不读文件。

## 方案设计

### 1. 解析规则（commands.py）

`parse_route_command` 改名为 `parse_proxy_command(content) -> (is_cmd, subcommand, arg)`，两级提取（last_text_block + strip_trailing_context）与单行规则原样复用。token 规则改为：

- `tokens[0] == "$proxy"`：
  - 仅 1 个 token（裸 `$proxy`）→ is_cmd=True, subcommand="__help__"
  - 2-3 个 token，tokens[1] ∈ {route, status, stats} → 对应子命令，arg = tokens[2]（若有）
  - tokens[1] 不在注册表、或 `status` 带多余参数、或 >3 token → subcommand="__help__"
- 其余 fail-open 照常转发。**`$route` 旧写法彻底移除、不做别名**（用户已拍板）：旧习惯输入 `$route ...` 不再被拦截，将作为普通消息原样转发给上游模型。README 必须在 §4.6 显著位置提示这一点，避免用户误以为命令失效。

route 子命令 arg 语义不变：None=查询 / "reset" / route_id。stats arg ∈ {None(默认 today), today, total, month, YYYY-MM, YYYY-MM-DD}，非法值由 handler 回用法提示（不 fail-open，因为已被识别为命令）。

**`$proxy <未知>` 与裸 `$proxy` 回 help 回执而非 fail-open**（用户已拍板）：用户明确在敲命名空间，转发上游只会得到模型胡言；help 回执列出可用子命令，透明可纠错。这是对原"绝不吞用户消息"原则的有限偏离——仅偏离"首 token 确为 $proxy"这一类输入，单行纯 `$proxy ...` 几乎不可能是真实对话内容。

### 2. 分发（server.py）

- `_forward` 调用点改为 `is_cmd, sub, arg = parse_proxy_command(...)`，透传 sub。
- `_handle_builtin_command(sub, arg, ...)`：`handler = COMMAND_HANDLERS[sub]`（注册表必含 __help__，无需 get 兜底）；`self._acc["builtin"] = sub`（ACCESS 可区分 route/status/stats/help）。
- ACCESS `route=` 补记逻辑只在 sub=="route" 时执行现状分支；status/stats/help 直接用查询时算好的 resolved_route_id。
- `CommandContext` 扩字段（server 构造时注入，handler 不 import server）：
  - `subcommand: str`
  - `cooldown_snapshot: dict`（`self.server.cooldown_store.snapshot()`）
  - `stats_store`（模块级 `usage_totals` 对象；为 None 时 stats handler 回"账本未启用"）
  - `supply_count: int`（`len(cs.get_supplies())`）
- `UsageTotalsStore` 新增公开方法 `get_bucket(selector) -> dict | None`：selector 语义与 CLI `cmd_stats` 对齐——None/"today"=今天(CST)天桶；"total"=total 桶；"month"=当月(months_archive 月档 + days 内同月残余天桶无条件合并)；"YYYY-MM"/"YYYY-MM-DD" 同理；取不到返回 None。锁内 deepcopy 返回，handler 不碰内部状态。

### 3. handler（commands.py）

注册表：`{"route": handle_route_command, "status": handle_status_command, "stats": handle_stats_command, "__help__": handle_help_command}`。全部纯读内存（除 route 写 sidecar，同现状），不读任何文件——符合 §7.3 边界（stats 读的是 UsageTotalsStore 内存态，不重新读 totals.json；代价是看不到进程外对账本文件的手工修改，可接受）。

- `handle_route_command`：逻辑不变，仅回执文案 `$route` → `$proxy route`（L426 `_format_cleaned`、L477 "撤销请发"两处）。
- `handle_status_command`：展示当前会话视角的运行态——
- `handle_stats_command`：`get_bucket(arg)` → 按 combo 键解析投影到 route 维度（**剔除 supply=(builtin) 行**，它们是命令回执自身，req 计数会污染真实上游用量）→ 每 route 一行 + 合计行 + total since 一行。
- `handle_help_command`：列出全部子命令及用法。

### 4. 输出样例

`$proxy route`（同现状，仅文案改名）：
```
当前 session a1b2c3d4 生效 route: claude（来源: 自动哈希分配）
可用 route id: claude, nation
该 strategy override 总条数: 2（sidecar 2）
```

`$proxy status`：
```
proxy 运行中 | strategy: cc（token 尾4 0956）| supplies: 8 | routes: 2
生效 route: claude（来源: sidecar（本次会话最近一次 $proxy route 指令））
冷却中 supply: ds-pro-sankuai-3339（剩 42s）、glm-52-sankuai-3339（剩 8s）
本 strategy override 条数: 2；default_cooldown: 60s
```
（无冷却中时第三行显示"冷却中 supply: 无"。）

`$proxy stats`（默认 today，UTC+8）：
```
用量统计 2026-08-09（UTC+8，不含内置命令）：
route=claude  req=812 ok=798 fail=14  in=12.3M out=1.1M  avg=8.2s
route=nation  req=35  ok=35  fail=0   in=0.4M  out=0.2M   avg=3.1s
合计 req=847 ok=833 fail=14；自 2026-07-23 累计 req=37302 ok=33974 fail=3328
```
（avg = sum_ms/requests；token 千分缩写 K/M。`$proxy stats total` 只出合计段。）

### 5. $route 旧写法处置：彻底移除，不留别名（用户已拍板）

解析层删除 `$route` 分支，`CMD_PREFIX` 直接换为 `$proxy`，无 LEGACY 常量。行为后果必须写进 README §4.6 显著提示：**旧习惯输入 `$route` / `$route reset` 不再被拦截，会作为普通消息发给上游模型**（无副作用——不会写 sidecar、不会触发任何代理状态变更，只是浪费一次上游调用并得到模型的无关回答）。session 中已存在的 override 不受影响，仍由 sidecar 生效。

## 风险与权衡

1. **`$proxy <未知>` / 裸 `$proxy` 吞消息**：help 回执偏离 fail-open 原则，用户已拍板接受。
2. **stats 只读内存**：进程外手改 totals.json 不反映（CLI stats 读文件反而能看到）。口径差异需在 README 注明。
3. **builtin 行剔除（用户已拍板）**：in-band stats 剔除 supply=(builtin) 行，CLI stats 不剔除，两者口径有轻微差异，需在 README 注明。
4. **status 显示 token 尾4**：回执仅发给持有该 token 的客户端本人，风险等同现有 ACCESS 日志口径，可接受。
5. **$route 移除的迁移风险**：用户旧习惯输入会被静默转发上游。无状态副作用，README 显著提示即可。

## 影响面与工作量

**代码**：
- `core/commands.py`：parse 函数改名重写（~35 行）、CMD_PREFIX→`$proxy`（无别名常量）、两个新 handler + help handler（~120 行）、回执文案 2 处、CommandContext 扩字段。
- `core/server.py`：import 更新、`_forward` 调用点、`_handle_builtin_command` 分发与 ACCESS 分支、ctx 注入（cooldown_store / usage_totals / supply_count）、`UsageTotalsStore.get_bucket`（~50 行，含 month 合并逻辑）。

**测试**：
- `tests/test_command_match_rules.py`：用例整体改写为新拼写 + help 分支 + **`$route` 旧写法不再识别**的回归用例（确认 fail-open 转发）（22 处 $route）。
- `tests/test_route_command.py`：import 与回执断言更新（12 处）；handler 主逻辑不变。
- 新增 status/stats handler 测试（bucket 选择、builtin 剔除、usage_totals=None 分支、help fallback、非法 stats 参数回用法提示），约 200 行。
- `tests/test_format_ops.py` L898-906：断言串 "（仅 $route)" 随 `_format_ops.py` L439 注解一并改为 "（仅内置命令）"。

**文档**：
- `README.md` §4.6 整节改写（命令清单、判定规则 token≤3、help 行为、stats 口径注记、**$route 旧写法已移除的显著提示**）；散点提及约 12 处（L96/257/268/425/431/434/463/470/475/480/488/536/885）同步措辞；L488"日后若要加 $status"段落删除（已实现）。
- `model_proxy_cli.sh` help 文本 L66-68。
- `docs/designs/` 历史设计文档不动（历史记录）。

**工作量估计**：implementer 约 0.5-1 天（代码 ~250 行 + 测试 ~250 行 + README 一节）。

## 验证方式

1. `python3 tests/test_command_match_rules.py`（合成用例 + --replay transcript 回归）。
2. `python3 -m pytest tests/test_route_command.py tests/test_format_ops.py` + 新增 status/stats 测试。
3. 人工核对：起代理后发 `$proxy` / `$proxy route` / `$proxy status` / `$proxy stats` / `$proxy stats total` / `$proxy foo`，核对回执样例与 ACCESS 日志 `builtin=` 字段；发旧写法 `$route` 确认被当普通消息转发（未被拦截、sidecar 无变化）；发一条含 `$proxy` 的多行正文确认 fail-open 不误拦。

## 关联

- [[2026-08-04-in-band-route-command-design]]（命令层骨架与 §7.3 边界，本方案的前提约束）
- [[2026-08-06-session-overrides-single-storage]]（sidecar 结构）
- [[2026-07-23-usage-totals-ledger]]（账本 schema）
