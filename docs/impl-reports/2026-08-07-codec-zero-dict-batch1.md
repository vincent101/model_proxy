---
type: impl-report
status: done
target: "[[tools/model_proxy]]"
design: "[[2026-08-07-reasoning-thinking-truncation-and-protocol-consistency]]"
updated: 2026-08-07
---

# 实施交付：codec 层零词表（第一批 ①a+①c+①d）

范围：仅 ①a（encode+decode 双侧去写死字典，含 anthropic 强制化终态）+ ①c（词表不变量单测）+ ①d（README/注释同步）+ 既有单测改动清单。不含 ①b（流式 reasoning 回传，待 SSE 样本）与 ②-⑥（预算治理/监控）。

## 改动文件清单

### `core/reasoning/codecs.py`
- 模块头注释重写：新增"词表约定（codec 层零词表）"段，声明映射唯一权威在 `ladder._NAME_TO_CANONICAL`、encode 直发 `level.name.lower()`、decode 用全表、`"none"` 为唯一保留的协议域常量；OFF/MAX 决策2约束段保留。
- 新增 `import logging` + `log = logging.getLogger(__name__)`；从 ladder 增导 `name_to_canonical`。
- 删除四张写死字典：`_ANTHROPIC_NAME_TO_CANONICAL`、`_CANONICAL_TO_ANTHROPIC_NAME`、`_CHAT_NAME_TO_CANONICAL`、`_CANONICAL_TO_CHAT_NAME` 及各自域注释块。
- `AnthropicReasoningCodec.decode` adaptive 分支：absent（effort 缺失）→ 维持默认 MEDIUM；非空但未识别 → `log.warning` + `RawIntent(level=None, present=False)`（不走 STRIP）；识别走全表（含 `none`/`off`→OFF 的有意行为变化，注释已点明）。
- `AnthropicReasoningCodec.syntax_adapt` adaptive 分支：`_CANONICAL_TO_ANTHROPIC_NAME.get(level, "medium")` → `abstract.level.name.lower()`。
- `_canonical_to_openai_effort_name`：`return level.name.lower()`，docstring 重写（无查表、无兜底；安全性依赖 remap 已收窄到 supply `effort_enum`）。
- `ChatReasoningCodec.decode` / `ResponsesReasoningCodec.decode`：查 `_CHAT_NAME_TO_CANONICAL` → 查全表 `name_to_canonical`（max/minimal 入站意图不再被丢）。
- Chat/Responses DISABLED 分支 `"none"` 硬编码保留，注释显式声明为 openai 域关闭词的协议事实。

### `core/reasoning/ladder.py`（仅注释同步，无逻辑改动）
- 模块头"各协议档名字符串在 codecs.py 里双向映射"（已与现实矛盾）→ 改为"映射唯一权威在本模块 `_NAME_TO_CANONICAL`（codec 层零词表）"。
- `_NAME_TO_CANONICAL` 上方注释补充 codecs decode 也消费本表。

### `tests/test_reasoning.py`
- `TestLadder` 新增 `test_name_to_canonical_enum_name_invariant`（①c）：遍历 `CanonicalEffort` 断言 `name_to_canonical(e.name.lower()) == e`，另断言 off/none 双拼均映射 OFF。
- `TestAnthropicCodecDecode` 新增两条：`test_adaptive_unrecognized_effort_not_silent_medium`（bogus → present=False/level=None）、`test_adaptive_none_off_decode_to_off`（none/off → OFF，固化有意行为变化）。
- chat MAX 断言 `"medium"` → `"max"`，测试改名 `test_syntax_adapt_max_no_special_branch_falls_back_default` → `test_syntax_adapt_max_passthrough`，注释重写。
- responses MAX 断言 `{"effort":"medium"}` → `{"effort":"max"}`（测试名保留）。
- 两处引用已删字典的过期注释/docstring（`test_adaptive_variant_max_level_no_special_branch`、`test_max_walks_same_lookup_path_as_other_levels_in_anthropic_adaptive`）改为描述 `level.name.lower()` 直出路径。

### `README.md`
- supply 字段节注意块（原 143-144）：删"Chat/Responses 协议域 effort_enum 词表本身不含 max/minimal"，改为"档名词表唯一权威在 `ladder._NAME_TO_CANONICAL`，codec 层零词表；supply `effort_enum` 声明的档名即 wire 档名"，保留 none=关闭的协议事实说明。
- §6.1 档名词表段同步：补"codec 零词表、encode 枚举名小写直发、decode 同一全表识别、约束收在 remap"。
- §8 已知限制新增条目：responses→anthropic 方向 reasoning 内容块当前不回传（①b 后续批），且补齐后 thinking block 无 `signature` 字段（转换侧无来源），对回传 thinking 的多轮客户端（Claude Code）是已知限制。

### 未动（按边界）
- `tests/test_translate.py:1657-1662` `test_ar_reasoning_item_dropped`：①b 断言，本批不碰，保持现状仍绿。
- translate.py / server.py / capability.py / 配置文件：均未动。

## 验证结果

- 环境无 pytest（`No module named pytest`），测试为纯标准库 unittest，改用等价的 `python3 -m unittest discover -s tests -q`：**Ran 468 tests, OK（全绿）**。
- 单独复跑 `tests.test_reasoning tests.test_translate`：Ran 250, OK。
- 既有依赖旧行为的断言除设计记录点名的 2 处外**没有**其他被撞：全量回归一次通过，无额外断言需要核对。
- 行为变化（设计已点明的有意变化，单测已固化）：anthropic adaptive `effort="none"/"off"` 由"静默 MEDIUM"变为识别 OFF；adaptive 非空未识别档名由"静默 MEDIUM"变为 warning + present=False。

## 发现的文档/实现不一致

- `ladder.py` 模块头原注释"各协议档名字符串在 codecs.py 里双向映射到这个全序上"在 ①a 终态后与现实矛盾，已同步修正（设计记录 ①d 只点名 codecs.py 头注释，此处是连带发现，仅注释级改动）。

## 其他说明

- 编辑 `codecs.py` 期间 vault 自动备份 cron（19:05）把该文件改动随 vault backup 提交进了 HEAD，git status 因此不显示它——改动无丢失，HEAD 内容与本批交付一致；README/ladder/tests 三处改动尚未被备份提交，保持工作区未提交状态（未主动 commit）。

## 风险自评

低风险：encode 侧改动依赖 remap 已把 level 收窄到 supply `effort_enum` 内（设计记录审核意见第 1 条已实证该不变量），decode 侧两处行为变化均有单测固化；468 全绿。建议复核点：anthropic adaptive 非空未识别 → present=False 后，server.py 主链路对该 RawIntent 的后续处理（STRIP/透传判定）是否符合预期——本批按设计定死的返回值交付，未深入 server 链路验证。
