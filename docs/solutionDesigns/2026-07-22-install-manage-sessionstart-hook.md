---
type: design-decision
date: 2026-07-22
status: draft
target: "[[tools/model_proxy/_install_ops.py]]"
tags: [architect, model_proxy, install, hooks]
---

# install 流程接管 SessionStart hook 的检测/修复/注入

## 背景与问题

`.claude/settings.json` 的 `hooks.SessionStart` 里有一条手写硬编码的 hook，指向
`tools/model_proxy/hooker/ensure_model_proxy.sh`（幂等确保 model_proxy 在 18889 运行）。
路径是写死的字符串，一旦 `tools/model_proxy/` 目录被移动/改名，这条 hook 会指向
不存在的旧路径而静默失效，model_proxy 不再随会话自启。

诉求：把这条 hook 的维护从"人工手写"改成"由 model_proxy 的 CLI install 流程负责"——
install 时检测已有 hook 条目是否指向当前真实路径，处理路径错误的历史遗留条目，
缺失时注入一条正确条目。写入必须走现有"预览确认"安全模式，且只动这一条，
不碰 settings.json 其他任何字段/其他 hook。

## 现状（已完整核对）

`.claude/settings.json` `hooks` 结构：

- `PreToolUse`（3 条 matcher）：`mcp__open-websearch__search` → ensure_websearch.sh；
  `Write|Edit|NotebookEdit` → guard_home_writes.sh；`Agent` → route_guard.sh
- `PostToolUse`（1 条）：`Agent` → verify_progress.sh
- `SessionStart`（3 个数组元素，均无 matcher）：
  1. `bash "${CLAUDE_PROJECT_DIR}/tools/ensure_proxy.sh"`（v1 代理 18888，与本工具无关）
  2. `bash "${CLAUDE_PROJECT_DIR}/.claude/skills/websearch-router/runtime/ensure_websearch.sh"`
  3. `bash "${CLAUDE_PROJECT_DIR}/tools/model_proxy/hooker/ensure_model_proxy.sh"`（本次目标条目）

关键事实：所有 command 用 `${CLAUDE_PROJECT_DIR}` 字面量，不会被展开，不能直接
`os.path.exists()`；SessionStart 元素结构是 `{"hooks": [{"type":"command","command":...}]}`，
无 `matcher` 键（与 PreToolUse 元素不同）。

`_install_ops.py` 现有可复用资产：
- `preview_confirm_write(cfg_path, old_text, new_text, label, success_msg_lines)`
  （136-184 行）：统一 diff 预览 → `confirm()` → 备份 `.bak.<ts>` → 写入，已被
  install_claude/codex/openclaw 复用，行为契约已有单测覆盖。
- `confirm(prompt)`（来自 `_config_ops.py`，`[y/N]`）。
- `cmd_install`（419-434 行）：交互选 SDK → 逐个 `_INSTALLERS[name]`。
- cli.sh `cmd_install`（304-310）：`python3 _install_ops.py install $CONFIG_FILE $MODEL_PROXY_PORT`。

## 方案设计

### 一、vault 根目录与 settings.json 定位（不硬编码绝对路径）

`_install_ops.py` 位于 `tools/model_proxy/_install_ops.py`，vault 根 = 该文件上溯三级：
`Path(__file__).resolve().parents[2]`（`model_proxy` → `tools` → vault 根）。

新增模块级常量：
```
_MODEL_PROXY_DIR = Path(__file__).resolve().parent          # tools/model_proxy
_VAULT_ROOT      = _MODEL_PROXY_DIR.parents[1]               # vault 根
_CLAUDE_SETTINGS = _VAULT_ROOT / ".claude" / "settings.json"
_HOOK_SCRIPT_REL = "tools/model_proxy/hooker/ensure_model_proxy.sh"  # 相对 vault 根
```
`_HOOK_SCRIPT_REL` 从 `_MODEL_PROXY_DIR.relative_to(_VAULT_ROOT)` 拼 `hooker/ensure_model_proxy.sh`
推导，避免"移动后连正确路径都写错"。期望的 command 字面量：
`bash "${CLAUDE_PROJECT_DIR}/<_HOOK_SCRIPT_REL>"`。

注意：`${CLAUDE_PROJECT_DIR}` 在真实会话里指向 vault 根，install 也在 vault 根下跑，
故用 `_VAULT_ROOT` 代入 `${CLAUDE_PROJECT_DIR}` 做存在性判断是自洽的。

### 二、检测逻辑

标识符：**command 字符串包含子串 `ensure_model_proxy.sh`** 即判定为"本工具的 hook 条目"
（不依赖前缀路径是否正确，才能抓到旧路径残留）。匹配范围仅 `hooks.SessionStart` 数组。

对每个命中条目，把 command 里的 `${CLAUDE_PROJECT_DIR}` 替换为 `str(_VAULT_ROOT)`，
从中提取被引号包裹的脚本路径，`Path(...).resolve()` 后与
`(_VAULT_ROOT / _HOOK_SCRIPT_REL).resolve()` 比较，得到每条的状态：

- `correct`：解析出的路径 == 期望路径（无论目标文件此刻是否存在——路径对就算对，
  文件缺失是另一类问题，见下）。
- `stale`：命中 `ensure_model_proxy.sh` 但解析路径 != 期望路径（旧路径残留 / 手写错路径）。

**"文件不存在" vs "路径不是当前安装位置"是否区分**：判断——**不单独区分**，统一按
"command 里的路径是否等于期望路径"这一个维度裁决。理由：install 的目标就是让 hook
指向当前真实 model_proxy 位置；只要路径字面等于期望值即为正确，此时目标文件必然存在
（就是本工具自己的脚本）；路径不等则一律 stale，需修正。额外只做一个**软告警**：若期望
路径对应的 `ensure_model_proxy.sh` 文件实际不存在（理论上不该发生，除非 hooker 被删），
打印 warning 提示用户，但不阻断注入流程。

### 三、重复/冲突处理（同时多条命中）

期望终态：`SessionStart` 里**恰好一条** correct 条目，零条 stale。归一化规则：

- 命中集合里若已存在 ≥1 条 correct → 保留**第一条** correct，删除其余所有命中条目
  （多余的 correct 重复 + 全部 stale）。
- 命中集合里无 correct、有 stale → 删除全部 stale，末尾新增一条 correct。
- 无任何命中 → 末尾新增一条 correct。

### 四、修复/注入策略：删除重建，不原地改字符串

判断——**统一"删除所有命中条目 → 保留/新增单条 correct"，不做 command 字符串原地正则替换。**
理由：
1. SessionStart 元素结构固定简单（`{"hooks":[{"type","command"}]}`），本工具生成的条目
   没有用户会手动附加的额外字段（不像 PreToolUse 可能带 matcher/多 hooks）；不存在
   "原地改才能保住用户手加字段"的价值。
2. stale 条目本身可能是脏的（错误路径 + 可能的手写笔误结构），删除重建比在脏结构上
   做正则替换更彻底、更可预测。
3. 归一化去重（三、）本就需要删多余条目，与"删除重建"是同一套操作，逻辑统一。

新增的 correct 条目 JSON 结构（与现有第 3 条完全一致，插入 `SessionStart` 数组**末尾**）：
```json
{
  "hooks": [
    {
      "type": "command",
      "command": "bash \"${CLAUDE_PROJECT_DIR}/tools/model_proxy/hooker/ensure_model_proxy.sh\""
    }
  ]
}
```
SessionStart 各条目间无顺序依赖（都是幂等自启脚本），末尾追加即可。

### 五、幂等保证

核心：**先在内存里构造目标 SessionStart 列表，与原列表做值比较；相等则不写。**
流程：
1. 读 settings.json → `json.loads` 成 dict（保序，Python dict 保持插入序）。
2. 取 `hooks.SessionStart`（缺 `hooks`/`SessionStart` 键则视为空列表，注入时补建）。
3. 按二/三/四计算出新的 SessionStart 列表（删命中、去重、必要时末尾追加单条 correct）。
4. 若新列表 == 原列表（结构值相等）→ 打印"hook 已是正确状态，无需改动"，直接返回，
   **不进入 preview_confirm_write、不产生备份**。
5. 否则把新列表写回 dict，`json.dumps(cfg, indent=2, ensure_ascii=False)+"\n"` 得 new_text，
   走定向预览确认写入（见六）。

已是 correct 单条时步骤 4 命中 → 重复跑 install 不会新增条目、不写文件。满足幂等。

### 六、安全写入：复用 preview_confirm_write，但补一段定向摘要

**复用** `preview_confirm_write`（备份 + confirm + 写入的机制原样照搬，不重造），
但直接把整份 settings.json 的 old_text/new_text 丢进去，其 unified_diff 只会显示
真正变化的 SessionStart 那几行（unified_diff 本身只输出变更 hunk），**不会**把整个
文件铺出来。所以定向 diff 天然满足，无需另写"只展示 SessionStart 片段"的机制。

唯一增强：调用 preview_confirm_write **之前**，先额外打印一段人类可读摘要，明确本次动作，
避免用户只看 diff 符号判断。摘要内容示例：
```
[model_proxy hook] 检测 .claude/settings.json 的 SessionStart：
  - 发现 stale 条目 N 条（路径不指向当前安装位置）：<列出解析路径>
  - 将删除上述条目，并确保末尾存在 1 条指向 <期望路径> 的 hook
```
随后 preview_confirm_write 展示 diff + 备份路径 + confirm。label 用 `"model_proxy hook"`。
new_text 用完整 settings.json 文本（json.dumps 重序列化）。

**重序列化副作用提示（需用户知情）**：`json.dumps` 重写整份文件会规范化格式
（缩进统一 2 空格、可能改动原手写的空行/键序保持但格式规整化）。由于原文件已是标准
2-space JSON（见现状），实测 diff 应仅落在 SessionStart 变更行。若担心格式漂移，
备份已兜底，且写入前的 old==new 比较是基于**解析后的数据结构**（步骤 4 比的是 list 值，
不是文本），不会因纯格式差异误触发写入——这一点是幂等正确性的关键，实现时务必按
"数据结构比较"而非"文本比较"判定是否需要写。

### 七、CLI 交互接入点

倾向方案：**作为 install 流程无条件自动跑的一个检查步骤，非第五个 SDK 选项。**
- 位置：`cmd_install` 开头，在 SDK 选择之前，无条件调用一次 `ensure_session_hook()`。
  理由：hook 自启是 model_proxy 整体可用性的前置条件，不该藏进"选装某 SDK"里；
  且它与选哪个 SDK 无关，做成第五个可选项反而语义错位。
- 但**不静默写入**：`ensure_session_hook()` 内部即"静默检查 → 若需改动则展示摘要+diff →
  confirm → 用户同意才写"，完全遵循项目一贯的"先展示、再确认、用户同意才写"安全模式
  （与 install_claude 等一致）。无需改动时静默通过只打印一行"已是正确状态"。
- 兼容 cli.sh 只传 `install` 无参场景：该检查在交互选 SDK 之前先跑，用户即使不选任何
  SDK（直接回车）也会先过一遍 hook 检查——符合"install 就该保证自启 hook 正确"的直觉。

（备选：做成 SessionStart 里第五个显式选项——否决，理由如上语义错位 + 用户易漏选。）

## 改动清单

`_install_ops.py`（唯一改动文件）：
1. 新增模块级常量：`_MODEL_PROXY_DIR`/`_VAULT_ROOT`/`_CLAUDE_SETTINGS`/`_HOOK_SCRIPT_REL`
   + 期望 command 字面量常量。
2. 新增纯函数（便于单测，无 IO）：
   - `_expected_hook_command() -> str`：拼期望 command 字面量。
   - `_is_model_proxy_hook(entry: dict) -> bool`：判 SessionStart 元素是否命中
     `ensure_model_proxy.sh`。
   - `_resolve_hook_path(command: str, vault_root: Path) -> Path | None`：替换
     `${CLAUDE_PROJECT_DIR}`、提取引号内路径、resolve。
   - `_normalize_session_start(entries: list, vault_root: Path) -> list`：实现二/三/四/五
     的归一化，返回新列表（不改入参）。
3. 新增 IO 函数：
   - `ensure_session_hook(settings_path: Path = _CLAUDE_SETTINGS) -> None`：读文件 →
     `_normalize_session_start` → 数据结构比较判是否变化 → 无变化打印后返回 / 有变化
     打印摘要 + 调 `preview_confirm_write`。settings.json 不存在或无 hooks 时的兜底：
     文件不存在 → 打印提示、跳过（install 不负责创建整份 settings.json）；文件存在但无
     `hooks`/`SessionStart` → 视为空列表并注入。
4. `cmd_install` 开头无条件调 `ensure_session_hook()`（在 `_interactive_select()` 之前）。

`model_proxy_cli.sh`：**无需改动**（现有 `install` 子命令即触发；不新增子命令）。
可选增益（非必须）：新增独立子命令 `install-hook` 只跑 hook 检查——本次不做，保持最小改动。

`ensure_model_proxy.sh` / settings.json：**本次不手动改**，交由新逻辑首次 install 时纳管。

## 风险与权衡

1. **重序列化格式漂移**：json.dumps 重写整份文件。已论证原文件为标准 2-space JSON，
   diff 应仅落在 SessionStart；且幂等判定基于数据结构而非文本，纯格式差不会误写。
   备份兜底。低风险，但 implementer 落地后需人工核对首次 diff 只含 SessionStart 变更。
2. **`${CLAUDE_PROJECT_DIR}` 代入假设**：以 `_VAULT_ROOT` 代入该变量做路径判断，前提是
   install 在 vault 根下、且会话的 CLAUDE_PROJECT_DIR 也指向 vault 根。当前布局成立；
   若未来 model_proxy 脱离 vault 使用需重审。
3. **只认 `${CLAUDE_PROJECT_DIR}` 形式**：若用户手写过绝对路径形式的 command（未用变量），
   `_resolve_hook_path` 仍能 resolve 并比对，判为 stale → 归一化为标准 `${CLAUDE_PROJECT_DIR}`
   形式。行为正确（统一成标准形式）。
4. **多条命中删除**：删除逻辑只作用于命中 `ensure_model_proxy.sh` 的元素，不碰 v1
   ensure_proxy.sh、websearch 那两条。子串匹配足够精确（`ensure_model_proxy.sh` 不与
   `ensure_proxy.sh` 混淆——前者含后者但判定用完整文件名子串，`ensure_proxy.sh` 不含
   `ensure_model_proxy.sh`，无误伤）。

## 验证方式

新增 `tests/test_install_ops.py` 用例（tempfile，绝不碰真实 `.claude/settings.json`）：
- `_normalize_session_start` 纯函数用例（不涉 IO，直接喂构造的 list）：
  1. 已存在唯一 correct 条 → 返回列表 == 入参（幂等，触发 no-write）。
  2. 仅存在 stale 条（旧路径）→ 删 stale + 末尾追加 correct，其他条目原序保留。
  3. correct + stale 混存 → 保留首条 correct、删其余，其他非命中条目不动。
  4. 多条 correct 重复 → 只留第一条。
  5. 完全无命中 → 末尾追加 correct，原有 v1/websearch 两条不动、顺序不变。
- `ensure_session_hook` IO 用例（tempfile 造 settings.json）：
  6. 已正确 → 不写、不产生 .bak（patch confirm 不应被调用 / 或断言文件字节不变且无 bak）。
  7. 需修复 + confirm=True → 写入后 SessionStart 恰一条 correct，且 PreToolUse/PostToolUse
     等其他字段逐字节不变（解析比对）。
  8. 需修复 + confirm=False → 文件不变、无 bak。
  9. settings.json 无 `hooks` 键 → 注入后结构正确、其他键不丢。
- 人工核对：在真实 vault 跑一次 `model_proxy_cli.sh install`（不选任何 SDK 直接回车），
  确认 hook 检查步骤先跑、当前已 correct 故打印"已是正确状态"、不产生 settings.json.bak。
- 回归：`cd tools/model_proxy && python3 -m unittest discover tests -v` 全绿。

## 关联

- [[tools/model_proxy/_install_ops.py]]
- [[tools/model_proxy/hooker/ensure_model_proxy.sh]]
- [[.claude/settings.json]]
- [[tools/model_proxy/tests/test_install_ops.py]]
