---
type: design-decision
date: 2026-07-23
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, code-audit, model-proxy, reasoning, concurrency]
---

# model_proxy 全面健康度审查报告

## 背景与问题

对 `tools/model_proxy/` 纯标准库多协议 AI 代理做一次全面审查（非新功能设计），覆盖代码正确性、架构一致性、测试覆盖、配置运维安全、文档一致性、技术债。已完整阅读 `core/server.py`(1731)、`core/translate.py`(1997)、`core/reasoning/*`(4 文件)、`_config_ops.py`(938)、`_install_ops.py`(619)、全部 7 个测试文件、真实与示例配置、`.gitignore`、hook 脚本、README(661)。测试套件 `python3 -m unittest` 388 项全过。

**总体结论：代码质量高。异常路径隔离、多线程锁、原子写盘、凭证脱敏、错误包裹都到位，没有发现"严重"级别（会导致真实故障/数据错误）的确认 bug。** 以下按级别列出发现，严重级别为空（如实说明，不硬凑）。

## 方案设计（审查发现，按级别分类）

### 严重（会导致真实故障/数据错误）

无。这是逐条验证后的结论，不是省略。核心链路（三段式路由、5 种协议转换、reasoning 相对映射、failover、账本记账）的边界与异常处理均经代码逐行确认无硬伤。

### 中等（边界场景触发 / 影响可维护性）

**M1. 配置脏数据静默降级为空能力 —— 确认的数据问题**
- 定位：`config/model_proxy_config.json` L392-411，strategy `obs-yolo` 的 `tiers_source_capability.*.effort_enum` 值为 `"\"low\""`/`"\"medium\""`/`"\"high\""`（带转义双引号的字符串），`note` 为 `"\"xhigh\","`。
- 验证证据：实跑 `name_to_canonical('"low"')` 返回 `None`，`ModelReasoningCapability.from_config({'effort_enum':['"low"']})` 得 `enum=()` 空元组、`off_alias=None`。即 obs-yolo 的 source 侧思考子序列为空，`remap()` 中 `src_think` 为空 → 走 `clamp_absolute` 绝对钳位兜底（capability.py L214-216），偏离该 strategy 本意的相对映射语义。不崩溃，但强度映射结果错误。
- 根因：`_config_ops.py::_extract_enum_candidates`（L140-171）明确"不做 `name_to_canonical` 白名单清洗，人工把关是唯一环节"（L144-146 注释）。obs-yolo 证明人工把关失效、带引号噪音的探测候选被直接写库且运行时静默降级，无任何告警。
- 影响面：仅 obs-yolo 这一条 strategy（绑 nation route），当前主用 cc/codex 不受影响。

**M2. 累计账本每请求全量重写 —— 性能/可维护性隐患**
- 定位：`core/server.py::UsageTotalsStore.record`（L168-197）每次请求持锁 `_atomic_write_json` 全量 dump（L197），`_archive_if_needed` 按 `while len(days) > KEEP_DAYS(400)` 保留。
- 触发场景：账本随天数与 `supply×route×strategy` 组合键增长（400 天桶 × 每天 N 个 combo），高 QPS 下 `record` 持锁期间全量 JSON 序列化 + mkstemp + os.replace 会串行化所有请求线程的记账收口。
- 缓解事实：`record` 在 `_forward_logged` 的 `finally`（L843-846）里、响应已写完之后执行，不算进客户端可见延迟，且 `record` 异常被 try 兜住不影响主流程（L845）。当前是本地单用户低频代理，实际风险低；但若继续迭代提 QPS 或组合键爆炸，这是最先出问题的一块。属"能跑但脆弱"。

### 轻微（整洁度 / 措辞 / 罕见边界）

**L1. README 账本"400 天"措辞与代码"400 个记录桶"语义差**
- 定位：README L366 "天分桶只保留最近 `KEEP_DAYS=400` 天"，代码 `_archive_if_needed` 用 `while len(days) > KEEP_DAYS` 按天桶数量而非日历天数判断。稀疏场景（部分日期无请求不建桶）下 400 个桶可跨远超 400 日历天。建议措辞改为"最近 400 个有记录的天"。

**L2. 成功响应路径的 failover 判断为防御性死代码**
- 定位：`core/server.py` L1148-1156。`urllib.request.urlopen` 对 status ≥400 会抛 `HTTPError`（走 L1099 分支），成功路径 `resp_status` 落入 `_FAILOVER_STATUSES`（401/403/429/5xx）几乎不可能，此段实际不会命中。无害冗余，可留可删。

**L3. 配置热重载的 getter 间 TOCTOU**
- 定位：`_forward` 连续调用 `get_strategies()`/`get_routes_map()`/`get_supply_map()`（L880-908），每次各自加锁取快照。若两次 getter 之间恰好发生 mtime reload，strategy 与 routes_map 可能来自不同配置版本，strategy 指向的 route_id 在新表里可能已删 → `routes_map.get` 返回 None → 偶发一次 401。reload 罕见且后果仅单次 401，可接受。

## 架构一致性（核对结论）

三段式转发、`_TRANSLATOR_TABLE` 协议组合表、reasoning `decode→resolve_capability→remap→abstract_encode→syntax_adapt` 链路在代码里被一致遵守，无绕过抽象的补丁式特例：

- `resolve_protocol`（registry.py）是 supply→protocol 的唯一权威实现，`detect_target`/`_config_ops`/`_install_ops` 均复用，无重复判断。
- reasoning 的 OFF 判断严格收敛在 `remap` 的 OFF 吸收态 + `abstract_encode` 两处（capability.py 自证），MAX 无任何专门分支，与设计文档承诺一致。
- 5 个转发分支统一用 `target_url = supply.url + _sanitize_forward_query(path)`，无各分支拼接后缀的旧 bug 残留。
- 三个流式 adapter 统一实现 `usage_tuple()`，server.py 按统一接口取 usage，无按类型分取的散装代码。

**并发修改痕迹核对（重点）**：两个并发 session 落地的"chat 空回答兜底"与"reasoning_tokens 统计修复 + 账本"未留下不一致：空回答兜底非流式（translate.py L523-531）与流式（L804-811）共用同一 `_ENABLE_REASONING_FALLBACK`/`_REASONING_FALLBACK_PREFIX`；`_extract_reasoning_tokens` 单一 helper 被全链路复用；6 个 mode 分支（PASSTHROUGH 流/非流、A2CHAT 流/非流、A2RESP 流/非流、R2A 流/非流）全部填了 `_acc["usage_reasoning"]`，无遗漏。

## 测试覆盖健康度（核对结论）

无发现"假覆盖"。特别核对了任务点名的历史问题类型：

- `usage_tuple()` 第三位（reasoning）：`test_translate.py` 既有 `test_usage_tuple`（断言无 reasoning 时为 0）又有 `test_usage_tuple_reasoning_nonzero`（断言末帧 `completion_tokens_details.reasoning_tokens=35` → `usage_tuple()[2]==35`），三个 adapter 各有非零断言（L747/1420/1779），证明第三位是真实读取而非硬编码。
- `test_usage_totals.py`（519 行）覆盖了记账累加一致性（天桶顶层==combos 之和==total）、归档不丢不重、连续两次归档不重复计、archive+days 分裂月份合并、UTC+8 跨零点边界，断言强、覆盖真实场景。
- `_extract_reasoning_tokens` 四条读取路径（chat/responses/anthropic 顶层别名）各有独立断言。

局限（README L590 已自陈，非隐藏）：转发编排与真实上游网络调用无端到端自动化测试，转换器均为脱网络单测。

## 配置与运维安全（核对结论，均合格）

- 真实凭证保护：`config/model_proxy_config.json`（含真实 appkey/admin_token）已被 `.gitignore` 忽略且未被 git 跟踪（`git ls-files config/` 仅 example）；`config/model_proxy_config.example.json` 全用 `<APPKEY_PLACEHOLDER>`/`<ADMIN_TOKEN_PLACEHOLDER>` 占位（grep 真实 appkey 命中 0）。
- 日志脱敏：ACCESS 日志 `token=token[-4:]`，failover warning `key_tail4`，reasoning debug 只打 effort 字段，均无完整凭证。
- 原子写盘：`_config_ops.atomic_write` 写 0o600；`UsageTotalsStore._atomic_write_json` 与 `atomic_write` 均 mkstemp+os.replace，损坏时账本自动改名 `.corrupt.{ts}` 备份并从空起步（有单测）。corrupt/bak 文件均在 .gitignore。
- SessionStart hook 归一化：`_normalize_session_start` 正确处理"已有 correct/仅 stale/无命中"三态，`is`+`==` 混用去重逻辑经核对正确；`ensure_session_hook` 运行时读模块常量而非参数默认值（避免 def 期绑定死，注释已说明）。install 一律预览 diff + 备份后写入，openclaw json5 解析失败降级打印不强写。

## 文档与代码一致性（核对结论）

README 三项新改动描述与当前代码一致：账本（KEEP_DAYS=400、combos 结构、UTC+8 固定偏移、只增不截、stats 查询）、chat 空回答兜底（常量名 `_ENABLE_REASONING_FALLBACK`、前缀文案、非流/流式函数名、stop_reason 不变）、reasoning_tokens 统计。唯一措辞瑕疵为 L1（"400 天" vs "400 个记录桶"）。

## 待用户决策的开放问题

1. **M1 obs-yolo 脏数据**：是否修复该条配置（把 `"\"low\""` 改回 `low`）+ 是否给 `_extract_enum_candidates`/`prompt_source_capability` 加一道 `name_to_canonical` 软校验（命中非法档名时告警而非静默）。当前设计是"人工把关"，obs-yolo 说明该假设不牢。
2. **M2 账本写盘策略**：是否需要改为批量/异步落盘或加写盘节流，取决于对未来 QPS 的预期。当前低频场景无需动。
3. **codex install base_url 层级**（README L584、`_install_ops.py` L377-386 已自陈未核对官方文档）：是否需要实测确认。

以上均非 bug，是设计取舍/流程假设，交用户判断。

## 明确区分

- **确认的真 bug**：无严重级；M1 是确认的配置数据错误（非代码 bug，代码按容错原则正确降级了，是数据+流程问题）。
- **设计取舍（我认为可讨论但不算 bug）**：PASSTHROUGH 现在也对 anthropic→anthropic 做 reasoning 相对映射、会改写客户端原始 thinking（有意为之，README 已说明）；chat 空回答兜底把 reasoning 内容当正文回填（加了前缀提示，可整体关闭）。
- **待决策开放问题**：见上节 3 条。

## 验证方式

- 测试基线：`cd tools/model_proxy && python3 -m unittest discover -s tests -p 'test_*.py'` → 388 passed（本次审查已跑）。
- M1 复现：`python3 -c "import sys;sys.path.insert(0,'.');from core.reasoning.capability import ModelReasoningCapability as C;print(C.from_config({'reasoning_capability':{'effort_enum':['\"low\"']}}))"` → `enum=()`。
- 凭证保护：`git ls-files config/` 应仅列 example；`grep -c 190734... config/model_proxy_config.example.json` → 0。
- M2 观察点：`ls -la tools/model_proxy/.claude_model_proxy_totals.json` 随使用增长的文件大小 + `record` 持锁全量 dump 的代码位置 server.py L197。

## 关联

- [[docs/solutionDesigns/2026-07-23-usage-totals-ledger]]
- [[docs/solutionDesigns/2026-07-23-chat-reasoning-content-fallback]]
- [[docs/solutionDesigns/2026-07-23-usage-reasoning-extraction-unify]]
- [[docs/solutionDesigns/2026-07-23-readme-sync-3changes]]
- [[docs/solutionDesigns/2026-07-22-inbound-auth-header-asymmetry]]
- [[docs/model_proxy_translate_spec]]
