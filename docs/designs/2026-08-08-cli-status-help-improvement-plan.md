---
type: design-decision
status: pending
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, cli, status, help]
---

# CLI status / --help 改进实施方案（务实路径 · 第一+第二梯队）

> [务实] 路径。范围为用户已拍板的第一梯队 3 项 + 第二梯队 3 项，共 6 项。
> 评估依据：[[2026-08-07-cli-status-help-assessment]]（canonical）。
> 行号已按 2026-08-08 master 工作区重新核实，与评估文档一致处不重复标注，不一致处以本文为准。

## 1. 背景与问题

status/--help 存在硬伤（help 文案与现实矛盾、S4 单值分支漏报覆盖数、80 列折行）与结构性漂移（status 与菜单 list 两份格式化实现）。本方案落地评估 §4 的第一、二梯队，第三梯队及 config 数据问题明确不做。

## 2. 现状核实（2026-08-08 复读源码，行号已验证）

| 对象 | 位置 | 核实结论 |
|---|---|---|
| `print_help` | `model_proxy_cli.sh:18-67` | status 描述在 `:22`，strategy 在 `:38-42`，switch 在 `:44`，`--help` 在 `:61`，尾部说明 `:63-65` |
| `cmd_status` | `model_proxy_cli.sh:112-184` | lsof 判断 `:113-118`（未运行直接 return 1）；内嵌 python `:131-183` |
| S4 bug | `model_proxy_cli.sh:162-171` | `sidecar_count` 取值（`:168`）与拼接（`:170-171`）都在 `elif route_pool:` 分支内，单值 `route_id` 分支（`:162-163`）永不拼 |
| `_mask_appkey` | `_config_ops.py:538-539` | 仅本文件内使用（全仓 grep 确认），可安全迁移 |
| `supply_list` | `_config_ops.py:542-553` | sid:24 + rcap + cooldown 列 |
| `route_list` | `_config_ops.py:738-747` | 与 CLI status routes 段逐字重复 |
| `_strategy_route_desc` | `_config_ops.py:861-875` | 被 `strategy_list`/`strategy_edit:958,985`/`switch:1019` 共用 |
| `strategy_list` | `_config_ops.py:878-885` | 单行 `{tok:16} -> {rid:12} ({note})` |
| `switch` pool 拒绝 | `_config_ops.py:1016-1021` | help 文案要对齐的现实行为 |
| `strategy_edit` pool 限制 | `_config_ops.py:957-960` | 同上 |
| `_handle_status` | `core/server.py:1621-1651` | 本次不改 server |
| `CooldownStore` | `core/server.py:415-454` | snapshot `:444-454`，不触发 |
| sidecar 路径约定 | `core/server.py:2022` | `config_path.parent / "session_overrides.json"`，CLI 离线路径按此推导 |
| `SessionOverridesSidecar` | `core/commands.py:183-` | `count_overrides_for:278-280`；模块 import 链纯 stdlib（已核实头部），可被 CLI 侧安全 import |
| `$route` 语法 | `core/commands.py:40,85,393` | 裸发=查询；`reset`=清除；其他=目标 route id |
| `_config_ops.py` import 机制 | `_config_ops.py:24` | `sys.path.insert(0, dirname(__file__))`，同模式可供新模块复用 |

实测 config（2026-08-08）：25 supplies / 4 routes / 2 strategies（cc/codex 全 pool，均带 note）；**max sid=26 字符，max model=17，max protocol=9**。据此推算：现有四列带标签写法（`protocol=... model=... appkey=...`）动态列宽后最坏行宽约 **87 字符，超 80**——S7 不能只加动态列宽，必须同时压缩列标签。

测试现状：全仓无 `cmd_status`/内嵌 python 的任何测试；无 list 函数输出的 capsys 断言（格式改动不破坏既有测试）；无 `/model_proxy/status` e2e。`grep -c "def test"` 实测 482（派单称 478，执行前以 `python3 -m unittest discover tests` 实跑数重新定基线）。

## 3. 方案设计

### 总览：两个阶段

- **阶段 A（第 1 项）**：help 文案修正，纯文本，独立先行。
- **阶段 B（第 2/3/4/5/6 项，一个内聚改动）**：新建 `_format_ops.py` 单源模块，重写 `cmd_status`，重接 `_config_ops` 三个 list 函数。S4/S7/S3 的代码位置全部被单源化重写覆盖，**不做就地修补**（否则写完即删，双重工时）。唯一例外见 §6 备选快车道。

### 3.1 阶段 A：help 文案修正（H2/H3/H4/H5）

文件：`model_proxy_cli.sh` `print_help`（`:18-67`）。逐条：

1. `:22` status 行改为：
   `status                            显示运行状态 + supplies/routes/strategies/cooldown 概览；代理未运行时仍展示 config 静态段`
2. `:39` strategy add 行尾追加：`（仅录单值 route_id；route_pool/dispatch 写法请直接编辑 config 后 reload）`
3. `:40` strategy edit 行尾追加：`（route_pool 写法仅可编辑 note/source 能力，route 不可经菜单改）`
4. `:44` switch 行改为：
   `switch <client_token> <route_id>  改 strategy.route_id 后 reload。仅支持单值写法；route_pool 写法会被拒绝，请直接编辑 config`
5. `:61` `--help` 行前插入 $route 指引段（语法已核实 `commands.py:85`）：
   ```
   会话内指令（非 CLI 子命令，在对话里直接发送）:
     $route                      查询当前 session 的生效 route 与 override
     $route <route_id>           把当前 session 固定到指定 route（写 config/session_overrides.json）
     $route reset                清除当前 session 的 override
     ※ status strategies 段的 "N个session覆盖" 计数即来自 $route 写入的 sidecar
   ```

不做 H6 分组（不在拍板范围），保持平铺，改动最小。

### 3.2 阶段 B 核心：新建 `tools/model_proxy/_format_ops.py`（第 4 项，单源化）

纯 stdlib 模块，承担 supplies/routes/strategies/cooldown 四段格式化，同时提供 CLI 入口。结构：

**格式化函数（返回 `list[str]`，不直接 print——可单测）：**

- `mask_appkey(appkey) -> str`：自 `_config_ops.py:538` 迁入（`...` + 尾4，空值 `(空)`）。
- `normalize_supply(d) -> dict`：归一两种来源——config 原生（含 `appkey`）与 server status JSON（`appkey` 已剥、含 `appkey_tail4`）——产出 `{id, protocol, model, key_masked, has_rcap, cooldown_display}`。脱敏唯一走 `mask_appkey`。
- `format_supplies(supplies, *, preset) -> list[str]`：动态列宽（每列取最大展示宽度，两空格分隔）。两个显式 preset：
  - `STATUS`：`(id, protocol, model, key)`，**裸值无标签**（仅 key 带 `key=` 前缀），实测最坏 71 列 ≤80；
  - `MENU`：`(id, protocol, model, key, rcap, cooldown)`，保留现有带标签样式（菜单是交互宽屏场景，不受 80 列约束）。
- `format_routes(routes) -> list[str]`：单行式优先；单行展示宽度 >80 时降级竖排：
  ```
    nation1 (failover=on)
      opus:   kimi-k3-sankuai-3339,kimi-k3-sankuai-8101,kimi-k3-sankuai-9907
      sonnet: ...
      haiku:  ...
  ```
  实测 nation 竖排单档行约 75 列。单档仍超 80 时（极端多 supply），按逗号边界折行、续行对齐缩进，**绝不在 id 中间断行**——保证复制后去缩进拼回即还原原逗号串。菜单 `route_list` 共用此函数（nation1/2 在菜单里同步变竖排，见 §4 风险 1）。
- `format_strategies(strategies, *, style, override_counts=None) -> list[str]`：`_strategy_route_desc` 自 `_config_ops.py:861` 迁入本模块（`_config_ops` 反向 import）。
  - `style="status"`：每个 strategy 两行——
    ```
      cc               -> pool[nation1:1,nation2:1]
          覆盖: (无)   note: 默认 Claude 家族（Claude Code SDK）
    ```
    覆盖行**无条件打印**（S3）：0 时 `(无)`；>0 时 `覆盖: N个session（来源: sidecar，由会话内 $route 指令产生）`。**拼接在 route_id/pool 分支之外，两种写法一视同仁（S4 修复点）**。
  - `style="menu"`：保持现有单行 `{tok} -> {rid} ({note})`，不传 override_counts（菜单行为不变）。
- `display_width(s)`：基于 `unicodedata.east_asian_width`（W/F 计 2），供 80 列判定与测试断言；中文 note 恒置于行尾，不参与定宽 padding。

**CLI 入口（`main()` + `_DISPATCH`，同 `_config_ops` 模式）：**

- `python3 _format_ops.py status-format`：stdin 读 server JSON，打印五段。保留现有容错语义：JSON 解析失败原样透传、含 `error` 键打 `Error:` 后退出码 0。
- `python3 _format_ops.py status-offline <config_file>`（S10）：直读 config + 经 `SessionOverridesSidecar(Path(config).parent/"session_overrides.json")` 取各 token 覆盖数（复用类=复用文件缺失/损坏语义，禁止自行 json.load 造第二份解析）。打 supplies/routes/strategies 三个静态段 + `cooldown: (代理未运行)` + `default_cooldown_seconds`（config 静态字段，已核实存在，正常打印）。
- 模块头部 `sys.path.insert(0, dirname(__file__))`（同 `_config_ops.py:24`），保证任意 cwd 下 `from core.commands import SessionOverridesSidecar` 可用。约束注释：`core.commands` 必须保持纯 stdlib import。

**为什么不是"bash heredoc 内嵌 python + import"**：直接把 `_format_ops.py` 当脚本调，stdin 传 JSON，从根上消除 heredoc 转义（现 `:131-183` 满屏 `\"`），且格式化逻辑首次可被 unittest 覆盖。启动开销与现状同级（现每次 status 本就 fork 一个 python3）。

### 3.3 阶段 B 改动点二：`cmd_status` 重写（`model_proxy_cli.sh:112-184`）

- `:113-118` 未运行分支改为（S10）：
  ```bash
  echo "model_proxy: NOT running on port $MODEL_PROXY_PORT（以下展示 config 静态信息）"
  python3 "$SCRIPT_DIR/_format_ops.py" status-offline "$CONFIG_FILE"
  return 1   # 退出码语义不变
  ```
  config 缺失检查（`:120-123`）上移到分支之前。
- 运行中分支：`:131-183` 内嵌 python 整段删除，替换为：
  ```bash
  echo "$out" | python3 "$SCRIPT_DIR/_format_ops.py" status-format
  ```

### 3.4 阶段 B 改动点三：`_config_ops.py` 重接

- `supply_list`（`:542-553`）：函数体改为循环打印 `format_supplies(..., preset=MENU)`；删除本地格式化。
- `route_list`（`:738-747`）：改为 `format_routes`。
- `strategy_list`（`:878-885`）：改为 `format_strategies(..., style="menu")`。
- `_mask_appkey`/`_strategy_route_desc` 迁出，原地改 `from _format_ops import ...`（`_strategy_route_desc` 调用方 `:958,985,1019` 不动）。
- 增删改逻辑、`_DISPATCH`（`:1037-1052`）、写盘路径一律不动。

### 3.5 已知偏差记录（config 数据，按拍板维持现状）

- 6372 三条 supply 的 id 尾号（6372）≠ appkey 尾4（3672）；nation1∩nation2 共享 3339 整行 3 个 supply。展示层设计已容忍：orphan supply 正常平铺不报错，id 与 key 尾4 并置展示恰好让该笔误保持可见（S13 的独立校验价值保留）。

## 4. 风险与权衡

1. **菜单 route_list 视觉变化**（单源化最大牵动面）：nation1/2 从 230 字符单行变竖排。这是统一的代价，也是 S7 的收益面；交互老用户需适应。`supply_list`/`strategy_list` 菜单输出格式不变。
2. **status supplies 列标签消失 + 动态列宽**：列位随最长 id 漂移，若有脚本按固定列解析 status 输出会失效——已 grep 确认仓内无此类消费方，status 定位人读。
3. **S8 字段集差异未 100% 拉平**：status 因 80 列硬约束不放 rcap/cooldown 列（菜单有）。漂移的根治点是"两份实现"，单源化后差异变为**显式 preset 参数**（代码内声明、可审计），符合评估 §3.2 的既定设计；特此明示，不算遗留漂移。
4. **`_format_ops` → `core.commands` import 链**：现纯 stdlib；未来若 commands.py 引入重依赖会拖慢每次 status。以注释约束 + 测试兜底（import 冒烟）。
5. **CJK 宽度**：`display_width` 用 stdlib `unicodedata`，不引 wcwidth 依赖；note 恒在行尾规避 CJK padding 对齐问题。
6. **退出码**：离线仍 return 1（`status || 告警` 类脚本语义不变），仅输出变丰富。
7. **S4 不就地修**：bug 修复随阶段 B 落地。若 B 排期滑期且用户要立即修，备选快车道：`model_proxy_cli.sh:168,170-171` 三行移出 `elif` 分支（取数与拼接提到 if/elif 之前/之后），半小时可交付——但阶段 B 落地时该修补被覆盖，属一次性工时。

## 5. 验证方式

**新增 `tests/test_format_ops.py`**（stdlib unittest，与仓内风格一致）：

| 测试点 | 对应项 |
|---|---|
| 单值 route_id strategy + count=2 → 输出含 `覆盖: 2个session`；pool 写法同断言 | S4 回归 |
| count=0 → 含 `覆盖: (无)`；count>0 → 含来源标注 | S3 |
| 镜像真实 config 极端值的 fixture（26 字符 sid、nation 式 3×20 字符档）→ status preset 每行 `display_width ≤ 80` | S7 |
| 5+ 长 id 单档 → 折行续行全 ≤80，去缩进拼回 == 原逗号串（可复制还原） | S7 |
| status-offline：tmp config + tmp sidecar（1 条 override）→ 计数正确、含 `cooldown: (代理未运行)`；sidecar 文件缺失 → `覆盖: (无)` 不崩 | S10 |
| 空 appkey → `(空)`；tail4 形态 → `...0956` | 脱敏统一 |
| 菜单冒烟：redirect_stdout 调 `supply_list/route_list/strategy_list`，非空且含实体 id（不钉死格式） | 单源化回归 |

**既有套件**：`cd tools/model_proxy && python3 -m unittest discover tests` 全绿（基线以执行时实跑数为准，grep 口径 482）。

**人工/e2e 核对**：
1. `bash model_proxy_cli.sh status`，80 列终端目测不折行（nation1/2 为重点）；
2. 停代理后 `status`：静态三段可见、cooldown 标未运行、`echo $?` == 1；
3. 会话内 `$route nation1` → status cc 行显示覆盖计数与来源；`$route reset` → 回到 `(无)`；
4. `switch cc nation1` 实际拒绝文案与新 help 描述一致；
5. `bash model_proxy_cli.sh route`（非 TTY）list 与 status routes 段同窗对比，同实体渲染一致；
6. `bash model_proxy_cli.sh --help` 通读，对照 H2/H3/H4/H5 修正点逐条核对。

## 6. 实施顺序建议

1. **阶段 A 先行**（help 文案，约 20 行）：零风险，独立成 commit。
2. **阶段 B 随后**（一个 commit 或按 `_format_ops 新建 → CLI 重接 → _config_ops 重接` 三小步）：五项同改一处，不可拆分并行。
3. A、B 均只读改 `model_proxy_cli.sh` 不同区段（`print_help` vs `cmd_status`），理论上可并行两分支、合并冲突可忽略；但 A 极小，串行 A→B 最省。
4. 交付后按 CLAUDE.md 规范派 reviewer 复核阶段 B（有正确性耦合）。

## 关联

- [[2026-08-07-cli-status-help-assessment]]（本方案的评估依据，S/H 编号与梯队划分出处）
- [[2026-08-06-session-overrides-single-storage]]（sidecar 单一存储，覆盖计数来源）
- [[2026-08-04-in-band-route-command-design]]（$route 语法与 sidecar 写路径）
- [[2026-07-28-session-route-dispatch-design]]（route_pool 写法，switch/strategy 限制的根源）
