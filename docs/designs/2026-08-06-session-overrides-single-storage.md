---
type: design-decision
status: confirmed
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, session-routing, in-band-command, sidecar]
---

# session_overrides 单一存储：把主 config 的手工声明并入 sidecar

> [务实] 路径产出：用户已拍板"迁移 + 删除主 config 字段"，本文只做可行性核实、迁移步骤、代码改动范围设计，不重新评估要不要做。
>
> 前置：[[2026-08-04-in-band-route-command-design]]（$route 功能设计，已 confirmed，本方案基于其已完成的实现改动）、[[2026-07-28-session-route-dispatch-design]]（route_pool / session_overrides / 一致性哈希机制现状）
>
> **状态：方案已定稿，可进入实施。**

## 0. 一个决定性前提：功能尚未上线

**这不是"热迁移一个正在跑的系统"，而是"在 `$route` 功能首次上线之前，把数据模型定下来"。**

已核实（2026-08-06）：
- 生产进程（pid 69224，2026-08-03 10:11 启动）跑的是**主工作区**的旧代码——`core/server.py` 里没有 `commands.py`、没有 sidecar 合并逻辑，`extract_route_candidates` 直接读 `strategy["dispatch"]["session_overrides"]`。
- `$route` 的全部实现（`core/commands.py`、sidecar 读写、命令层）目前**只存在于 worktree** `exp/route-cmd-v1`，尚未 commit 到主分支、更未部署。
- 生产 `config/session_overrides.json`（sidecar 文件）**不存在**。

这意味着本方案不需要设计"运行中系统的热迁移"（不存在两套并存代码同时读写同一批 session 的窗口），只需要在 `$route` 功能实施上线的同一批改动里，一次性把数据模型改成单一存储即可。**排期上必须锚定一个先后顺序**（见 §3），搞反会导致现网 5 条记录在新代码上线前的空窗期内失效。

## 1. 可行性核实

### 1.1 现网 5 条记录的确切现状

```
tools/model_proxy/config/model_proxy_config.json（主工作区，gitignored，权限 600）
strategies[0].client_token = "cc"
strategies[0].route_pool = [{"route_id": "nation", "weight": 1}]   ← 只有 nation 一个
strategies[0].dispatch.session_overrides = {
  "7b4cb865-c308-42e3-9fe6-1d61ca48e90a": "nation",
  "6ad2e1b5-8ae4-41f5-b97f-dbb45beb71fb": "nation",
  "4c3ba96f-0148-40f3-a414-f3839b8db586": "nation",
  "c2e29916-326e-443f-91bf-72e5311b514a": "nation",
  "cf9e4ee3-6cc2-40ea-99f4-d26d40cb9dce": "nation"
}
```
5 条全部指向 `nation`，全部是旧式纯字符串值（`session_id: route_id`），无 `last_seen`/`created`。`codex` strategy 的 `dispatch` 为空。

> ⚠️ **关键事实（影响迁移后果判断）**：`cc` 的 `route_pool` 当前**只有 `nation` 一个 route**（weight=1），不是多 route 池。这 5 条 override 指向的 `nation` 与 pool 唯一选项重合——**override 存不存在，对这 5 个 session 的实际路由结果没有任何影响**（命中 override 选 nation，不命中走哈希分配池里也只有 nation，结果相同）。这正是此前 reviewer 复核时"5 条命中 0 次的僵尸条目"判断成立的根因。此事实直接简化了"瞬间消失"的后果评估，见 §2.2。

### 1.2 迁移后清理判据是否成立（用户已拍板：无 last_seen 就填今天日期，正常纳入 7 天清理）

已读 `SessionOverridesSidecar.apply_command` 清理逻辑（`core/commands.py` §7 天清理 一节）：
```python
for sid in list(sessions.keys()):
    if ct == client_token and sid == session_id:
        continue  # 当前 session 永不被清理
    entry = sessions[sid]
    if not isinstance(entry, dict):
        continue  # 无 last_seen（含手工塞入的旧式字符串）不参与清理
    ls = entry.get("last_seen")
    ts = _parse_iso(ls) if ls else None
    if ts is None:
        continue
    if ts < cutoff:
        sessions.pop(sid)
```
迁移后这 5 条会变成标准新式 dict（`{"route_id": "nation", "last_seen": "<今天>", "created": "<今天>"}`），**完全匹配现有清理逻辑的判据**，不需要新增任何代码分支。清理倒计时从迁移当天开始算，7 天内若无真实请求命中刷新 `last_seen`，会被自动清理——这是用户已知并接受的行为（此前"48h→7天"决策时已表态"误删也无所谓，可以再切回去"）。

**结论：技术上完全可行，不需要新增清理逻辑的特殊分支。**

### 1.3 移除主 config 字段后，哪些依赖会断——已逐一排查

grep 全项目 `dispatch.session_overrides` / `session_overrides` 的所有引用点（不含 worktree 新增的 `commands.py`/`test_route_command.py`，那些是 sidecar 侧代码，本就应该保留）：

| 文件:行 | 用途 | 移除主 config 字段后的影响 |
|---|---|---|
| `core/server.py:609-688`（`extract_route_candidates`） | 读 `dispatch.get("session_overrides")` 作为哈希分配前的优先覆盖 | **主路径**。迁移后这个函数不再需要读 `dispatch.session_overrides`——因为 `$route` 功能已经在其外层用 `build_merged_strategy` 把 sidecar 结果注入到一份浅拷贝 strategy 的 `dispatch.session_overrides` 里再传进来（见 `core/server.py:1032`）。也就是说：**`extract_route_candidates` 函数本身完全不用改**，它读的字段名不变，只是调用方不再需要从主 config 读取原始值来"垫底"——因为主 config 里以后压根不会再有这个字段，`build_merged_strategy` 的合并结果自然退化成"只有 sidecar 一个来源"。 |
| `core/commands.py`（`effective_overrides`） | 合并主 config 基线 + sidecar（sidecar 优先） | 主 config 侧的 `base = dispatch.get("session_overrides") or {}` 以后永远是 `{}`（因为该字段被移除），`merged` 实质上等于 `sidecar.get_overrides_for(...)`。**可以简化**：见 §4 改动范围，`effective_overrides`/`build_merged_strategy` 这套"合并两个来源"的机制可以整体删除，`extract_route_candidates` 直接读 sidecar 结果。 |
| `core/commands.py`（`normalize_override_entry`） | 兼容读取新旧两种格式（纯字符串/dict），因为主 config 是旧格式、sidecar 是新格式 | 迁移后 sidecar 是唯一来源，**只会写入新格式**（`apply_command` 只产出 dict）。但**不能删除这个兼容函数**——sidecar 文件本身允许被人工手改（这是设计既定语义，见 in-band 文档 §4.5"持久化、可见、可手改"），人工手改时完全可能手滑写成纯字符串。保留该函数继续兼容，只是"唯一消费点"从"两个来源"收窄到"sidecar 一个来源里可能出现的两种写法"。 |
| `model_proxy_cli.sh:168`（`status` 子命令展示） | `overrides = ((st.get('dispatch') or {}).get('session_overrides')) or {}`，展示"pool[...] +N个session覆盖" | **会断**。这段代码读的是 `/model_proxy/status` HTTP 端点返回的 `strategies` 原始 JSON（`core/server.py:_handle_status` 里 `cs.get_strategies()` 直接回显），迁移后该字段永远是空，CLI 展示会从"+5个session覆盖"变成不显示，即使 sidecar 里其实有 5 条记录。**必须同步改**：`_handle_status` 需要额外读 sidecar 的 `count_overrides_for(client_token)` 并入返回体，`model_proxy_cli.sh` 改读新字段。这是本次迁移唯一需要动"$route 功能之外"代码的地方。 |
| `README.md`（5 处：§3.4 结构示例、字段说明、字段总表 §"strategies 字段"、§4.4 三阶段匹配描述） | 文档描述 | 需同步改写为"session override 存储在独立 sidecar 文件，通过 `$route` 命令或直接编辑 sidecar 维护，不再是 strategy 的字段" |
| `_config_ops.py` | grep 结果：**无任何引用**（strategy add/edit 明确注释"本 CLI 暂不支持编辑 route_pool/dispatch，如需修改请直接编辑配置文件"） | 无影响，不用改 |
| `config/model_proxy_config.example.json` | 已核实：**不含** `session_overrides` 示例字段，无需改动 | 无影响 |

### 1.4 `effective_overrides`/`build_merged_strategy` 合并机制能否整体简化——成立，且是额外收益

当前热路径（`core/server.py:1032`，**每个命中 strategy 的请求都会执行**）：
```python
merged_strategy = build_merged_strategy(strategy, sidecar)  # 浅拷贝 + 合并两个来源
merged_overrides = merged_strategy["dispatch"]["session_overrides"]
```
迁移后主 config 侧基线恒为 `{}`，`effective_overrides` 退化成：
```python
merged = {}                                   # 主 config 基线，恒空
merged.update(sidecar.get_overrides_for(client_token))  # 唯一来源
```
即"合并"这一步只是把 sidecar 的结果原样拿出来再包一层空字典更新。**可以直接删除 `effective_overrides`/`build_merged_strategy` 两个函数**，`core/server.py:1032` 改为：
```python
overrides = sidecar.get_overrides_for(strategy.get("client_token", ""))  # 唯一来源
merged_strategy = strategy  # 不再需要构造浅拷贝
```
但注意 `extract_route_candidates` 的调用签名仍然期望从 `strategy["dispatch"]["session_overrides"]` 读取——所以浅拷贝构造这一步**不能完全消除**，只是从"合并两个来源"简化成"套一层 sidecar 结果"。仍有净收益：删掉了一次 `effective_overrides` 的字典遍历 + 一次跨来源覆盖逻辑，且消除了"两个函数、两处测试"的维护面。

### 1.5 是否有隐藏坏处

排查了以下几类可能依赖"主 config 独立于 sidecar"的场景，结论均为**无阻塞**：

- **sidecar 文件损坏/丢失时的降级行为**：`SessionOverridesSidecar._reload_locked` 对非法 JSON 保留上次内存值 + warning，不中断请求；文件缺失视为 `{}`。迁移后若 sidecar 损坏，该 strategy 的 override 全部失效、退化为纯哈希分配——这和"主 config 字段还在但被清空"的效果一致，不是新增风险，只是**唯一来源损坏的影响面从"消失部分记录"变成"消失全部记录"**。需要在文档里如实标注这个取舍（见 §5），不属于技术缺陷，是"单一存储"天然要接受的代价。
- **代理重启时的初始状态**：`SessionOversidesSidecar.__init__` 在实例化时读一次文件，和 `ConfigStore` 一致，重启后从磁盘恢复，无特殊问题。
- **`$route` 查询命令的"来源"标注**：迁移前查询回执会区分"来源: 主config" vs "来源: sidecar"（如果这么实现的话——已读代码确认当前 `_handle_query` 并未做来源区分，只统计条数），迁移后这个区分点自然消失，不用额外处理。

## 2. 迁移方案

### 2.1 前提约束

- 生产 config 路径：`/Users/vincentwang/Documents/NoteVault/tools/model_proxy/config/model_proxy_config.json`（主工作区，权限 600，gitignored）。
- **功能尚未上线**（见 §0），当前生产代码不认识 sidecar，仍直接读 `dispatch.session_overrides`。
- `ConfigStore`（主 config）与 `SessionOverridesSidecar`（sidecar）都有基于 mtime 的自动热重载（`maybe_reload`，每请求触发一次比对）。

### 2.2 排期：删字段可与功能上线+数据迁移合并成一步

**原方案曾主张"必须先上线新代码、再删字段，不能颠倒"——该主张基于一个错误前提（`cc` 的 route_pool 含 `claude`），现已推翻。** 重新核算如下：

**"瞬间消失"的真实后果（已核实）**：`cc` 的 `route_pool` 当前只有 `nation` 一个 route（§1.1 已纠正）。这 5 个 session 命中 override 选 `nation`、不命中走哈希分配池里也只有 `nation`——**override 删不删，路由结果都是 `nation`，不会换模型**。所以"删字段导致瞬间跌回哈希分配"这件事在本配置下**不构成服务退化**，用户已明确接受这一后果（"可以接受 5 条记录从生产逻辑里瞬间消失"）。

**合并成一步的可行性**：把"$route 功能上线 + 迁移脚本写 sidecar + 删主 config 字段"放进同一次发布。脚本内部仍按 §2.3 的"先写 sidecar、再写主 config"顺序执行（这一层顺序是为了迁移脚本自身的幂等与可重跑，与发布排期无关）。

**合并方案的真实风险（已重新评估，均可控）**：

| 风险 | 评估 |
|---|---|
| 迁移瞬间 5 个 session 换模型 | **不成立**。pool 只有 nation，override 冗余，删不删都走 nation |
| 脚本中途失败留下"半迁移"状态 | "sidecar 已写、主 config 未删"是安全中间态（新代码 sidecar 优先），可重跑（幂等）；"主 config 已删、sidecar 未写"则这 5 条暂时消失——但如前所述，消失不改变路由结果。脚本按"先 sidecar 后主 config"顺序可规避后者 |
| 回滚复杂度 | 备份主 config 后，回滚 = 还原主 config 备份 + 清空 sidecar 对应条目（或直接删 sidecar 文件，反正它本来就不存在）。比两步方案稍复杂但仍是确定性操作 |
| 新代码首次上线本身的风险 | **这是合并方案唯一真正放大的风险**——若新代码（含 sidecar 合并、命令层、死锁修复）有未发现的 bug，和数据迁移同次发布会增加排查难度。但该代码已通过 reviewer 复核 + 452 个单测，且合并方案不影响"上线后立刻验证 `$route` 查询能正确读到 nation"这个检查点 |

**结论：可以合并，且比两步分开更简单**（少一次发布窗口、少一次中间验证发布）。§2.4 的"代码简化延后到第二次发布"建议仍然成立——简化 `effective_overrides`/`build_merged_strategy` 这一步不要和数据迁移揉在一起。

### 2.3 迁移脚本（与功能上线同次发布时执行）

**前置条件检查**（脚本执行前必须确认）：
1. 新代码（含 `commands.py`、sidecar 合并逻辑）已部署、生产进程已重启为新代码。
2. 用 `$route`（查询模式，无参）对现网 5 个 session 之一发起查询，确认能正确读到 `nation`（证明新代码的合并链路工作正常；此时数据源仍是主 config，因为 sidecar 还没写入）。

**迁移动作（单次操作，写成一次性脚本而非手工编辑，降低出错概率）**：

```python
# 伪代码，实际实现由 implementer 写成脚本，放 tools/model_proxy/ 下临时用一次即可
import json, datetime
from pathlib import Path

CONFIG = Path("config/model_proxy_config.json")
SIDECAR = Path("config/session_overrides.json")

cfg = json.loads(CONFIG.read_text())
today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

sidecar_data = json.loads(SIDECAR.read_text()) if SIDECAR.exists() else {}

for strategy in cfg["strategies"]:
    legacy = (strategy.get("dispatch") or {}).pop("session_overrides", None)
    if not legacy:
        continue
    client_token = strategy["client_token"]
    bucket = sidecar_data.setdefault(client_token, {})
    for sid, route_id in legacy.items():
        if sid in bucket:
            continue  # sidecar 已有该 session 的更新记录，不用旧数据覆盖
        bucket[sid] = {"route_id": route_id, "last_seen": today, "created": today}
    if not strategy.get("dispatch"):
        strategy.pop("dispatch", None)  # dispatch 变空字典就整体移除，保持配置整洁

# 写 sidecar（先写，用同一套 core/commands.py 里的 _atomic_write_json 逻辑：
# mkstemp 同目录 + os.replace + chmod 0600）
# 写主 config（后写，用既有 _config_ops.atomic_write，同样是原子替换）
```

**顺序：先写 sidecar，再写主 config。** 理由：如果中途失败（比如进程被杀），"sidecar 已写入新记录 + 主 config 字段还在"是安全的中间态——新代码的合并逻辑里 sidecar 优先，行为不受影响，且可以重跑脚本（脚本对已存在于 sidecar 的 session 会跳过，幂等）；反过来"主 config 先被删、sidecar 还没写成功"则会出现短暂丢失。

**是否需要停代理进程**：不需要。两个存储都有 mtime 热重载，脚本用原子写（`os.replace`）替换文件，下一次请求触发的 `maybe_reload` 会自动感知到变化。但建议避开高峰使用时段执行，因为迁移瞬间前后，若恰好有请求落在两次热重载之间的极短窗口，合并逻辑仍然是 sidecar 优先、正确的，只是审慎起见没必要在使用高峰折腾配置文件。

**回滚方案**：迁移前脚本先把 `model_proxy_config.json` 完整备份一份（复用 `_install_ops.py:172` 已有的 `.bak.<时间戳>` 命名约定）。若迁移后发现问题，直接用备份覆盖回去即可——此时新代码仍然兼容"主 config 有该字段"的读取路径（除非同一批改动已经把 `effective_overrides` 简化删除了，见 §2.4 的顺序说明）。

### 2.4 与"代码简化改动"的排期关系

§1.4 提到可以删除 `effective_overrides`/`build_merged_strategy` 做进一步简化。**建议这一步简化代码延后到迁移脚本跑完、确认现网 5 条记录在 sidecar 里工作正常之后再做**，不要和数据迁移在同一次发布里合并：

- 第一次发布：`$route` 功能上线（含合并两来源的兼容代码，此时主 config 字段还在）
- 数据迁移：跑迁移脚本，主 config 字段清空
- 第二次发布（可选，建议做但不紧急）：确认数据迁移无问题后，删除"合并两来源"的兼容代码，回归到"sidecar 是唯一来源"的简化实现

这样任何一步出问题都容易定位、回滚，不会因为"新代码 + 新数据模型 + 简化重构"三件事捆一次发布而增加排查难度。

## 3. 代码改动范围

基于 worktree `exp/route-cmd-v1` 已完成并通过 reviewer 复核（含死锁修复）的实现：

| 文件 | 改动 | 命运 |
|---|---|---|
| `core/commands.py` | 删除 `effective_overrides`、`build_merged_strategy` 两个函数；保留 `normalize_override_entry`（sidecar 允许人工手改，仍需兼容纯字符串写法） | **简化删除**（§2.4 建议延后到第二次发布） |
| `core/server.py:1026-1043` | 不再调用 `build_merged_strategy`，直接 `sidecar.get_overrides_for(client_token)` 构造浅拷贝 strategy 视图喂给 `extract_route_candidates` | 简化 |
| `core/server.py:_handle_status` | 新增：把 `sidecar.count_overrides_for(client_token)` 并入 `/model_proxy/status` 返回体，供 CLI 展示 | **必须做**（§1.3 唯一会断的现有功能） |
| `model_proxy_cli.sh:168` | 改读新的 status 返回字段，不再读 `dispatch.session_overrides` | **必须做** |
| `config/model_proxy_config.example.json` | 已核实不含该字段，无需改动 | 无 |
| `README.md`（§3.4 结构示例、字段说明段、字段总表、§4.4 三阶段匹配描述，共 5+ 处） | 改写为"session override 唯一来源是 sidecar，通过 `$route` 命令或直接编辑 `config/session_overrides.json` 维护" | 必须同步，否则文档与代码矛盾 |
| `tests/test_route_command.py`：`TestLegacyMainConfigOverridesNeverTouched` | **前提被推翻**——该测试断言"主 config 里的旧格式条目在 `$route` 写操作后原样保留在主 config"，但迁移后主 config 不再存这个字段，该测试场景不再存在 | **删除**，替换为新测试：验证迁移脚本本身的行为（幂等性、sidecar 优先不覆盖已有记录、`dispatch` 变空字典后被移除） |
| `tests/test_route_command.py`：其余测试（`TestNoAliasPollution`、`TestCleanupThreshold`、`TestSingleAtomicWrite`、`TestHotPathNoDiskIO`、死锁回归测试等） | 不受影响，因为它们测的是 sidecar 自身逻辑，不依赖主 config 是否有该字段 | 保留 |
| `tests/test_command_match_rules.py` | 不受影响（测的是指令匹配规则，与存储位置无关） | 保留 |
| 新增：一次性迁移脚本 | 见 §2.3，建议放 `tools/model_proxy/` 下（不放 `core/`，因为这是运维脚本不是产品代码），跑完可以删除或归档 | 新增 |
| 新增：迁移脚本的测试 | 至少覆盖：幂等性（跑两次不重复写）、sidecar 已有记录不被旧数据覆盖、`dispatch` 清空后被移除 | 新增 |

**返工代价评估**：已完成并通过 review 的核心实现（匹配规则、sidecar 读写、7 天清理、死锁修复、命令层骨架）**全部保留复用**，不受这次改动影响。需要改的是"如何构造喂给 `extract_route_candidates` 的 override 视图"这一小段胶水代码（删两个函数、改一处调用点），以及 `_handle_status`/CLI 展示这个此前没被覆盖到的边界。整体是小范围改动，不是重做。

## 4. 设计文档更新

`docs/designs/2026-08-04-in-band-route-command-design.md` §4.5"主 config vs 独立 sidecar：建议 sidecar"一节，当时的决策理由包含"主 config 是人工声明、sidecar 是代理自动维护，两者语义不同不该混在一起"。这次改动等于推翻了这部分理由，**需要在该文档追加一条 changelog 式的说明**（不要静默覆盖原文，保留决策演变的可追溯性）：

> **2026-08-06 补充决策**：原方案保留主 config 的 `dispatch.session_overrides` 作为"人工永久声明"与 sidecar 的"代理自动维护"并存、按优先级合并。用户后续决定简化为单一存储：把主 config 里的手工记录一次性迁移进 sidecar，移除主 config 的该字段。取舍变化：迁移后手工声明与代理自动写入的记录在存储上不再区分（都在同一个 sidecar 文件、格式一致），且手工记录会失去"永久不清理"的特殊地位、随 7 天不活跃阈值一起清理——这是用户已知并接受的行为（此前拍板 7 天清理时已表态"误删也无所谓，可以再切回去"）。详细方案见 [[2026-08-06-session-overrides-single-storage]]。

## 5. 分步实施计划（供派 implementer 执行）

1. **实现迁移脚本**（§2.3 伪代码 → 实际代码），本地用临时目录跑通单测（幂等性、sidecar 优先、dispatch 清空移除）。
2. **同一次发布合并三件事**：`$route` 功能（worktree `exp/route-cmd-v1`，已通过 reviewer 复核含死锁修复）+ 迁移脚本写 sidecar + 删主 config 字段。三者进同一批改动。
3. **部署后立刻验证**：跑 `$route`（查询）确认 5 个已知 session 之一能正确读到 `nation`（此时数据源已是 sidecar）。因 `route_pool` 只有 nation，即使迁移有偏差也不改变路由结果，验证点只在于"sidecar 记录存在且格式正确"。
4. **补 `_handle_status`/CLI 改动**：新增 sidecar 条目数展示，跑 `model_proxy_cli.sh status` 确认展示正常（§1.3 唯一会断的现有功能，必须与本次发布同步修，否则 CLI 展示会显示"无覆盖"）。
5. **同步 README 五处引用**。
6. **（可选，建议延后）第二次发布**：删除 `effective_overrides`/`build_merged_strategy`，简化为"sidecar 是唯一来源"，跑全量测试确认无回归。
7. **删除或归档一次性迁移脚本**（迁移只做一次，不需要作为长期维护的产品代码保留）。

每一步都建议派 implementer 执行、reviewer 复核第 1/2/6 步（涉及数据正确性与生产配置改动），与此前 `$route` 功能的复核方式一致。
