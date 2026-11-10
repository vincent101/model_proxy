# Changelog

## 0.13 (2026-11-09)

### 跨协议链路语义保真与观测补齐（2026-11-06，a2r/r2a 全向）

- **a2r/r2r 观测补齐**：转换链路 ACCESS 行补 `stop_reason` / `first_event_ms` /
  `terminal_status`（原先全空，跨协议请求无法观测）；上游流内 failed/error 补 WARNING 详情
- **messages 内 system 消息透传（a2r/r2a 双向）**：ClaudeCode/codex 历史中 `role="system"`
  的消息原先被静默丢弃（实测模型收不到，"Today's date" 类注入失效），现按位置原样透传；
  r2a 方向同步支持 input 内 system/developer role
- **encrypted reasoning 回传（a2r→客户端→a2r 闭环）**：Responses 上游的
  `encrypted_content` 藏入 thinking block 的 `signature`（`resp:` 前缀），客户端历史回显时
  代理还原为 reasoning item——多轮推理连续性保持；跨协议防护：passthrough 出站前剥离
  `resp:` 伪签名块
- **builtin 回执观测口径**：`$route` 等内建命令回执的 ACCESS 从误标
  `invalid/open/internal_escape` 修正为 `valid/completed/end_turn`
- **Phase D 读取及时性**：`HTTPResponse.read(amt)` 在 chunked 流上跨块累积到 amt 才返回，
  转换链路首事件被压到流近结束（实测 first_event_ms/total p50≈0.86）；改为 `read1`
  单次读取，首事件 p50 从 ~14.9s（r2a）降至 ~1.5-3.5s；三门槛合成验证（D1 及时性 /
  D2 字节等价 / D3 吞吐不退化）
- **工具 strict 语义保真（supply 级学习）**：Responses 上游对省略 strict 的工具做严格化
  规范化（实测 sol 把 optional 字段扩进 required，模型自行填齐 Agent isolation 等
  可选参数）；a2r 出站默认 `strict:false` 保住可选参数语义；源工具显式声明优先；
  `ToolStrictStore` 支持从上游 strict 类 400 学习升级（48h TTL 自愈）
- **r2a item ID 前缀**：生成的 function_call item id 从 `item_` 改为 `fc_`
  （Responses 上游校验 fc 前缀，旧前缀导致跨上游历史回放 400）
- **CLI status**：active sessions 行增加 `[token]` client 标注（区分 ClaudeCode/Codex）；
  移除 protocol conversions 专属段与跨协议风险 hint（链路已无损，不再特别展示）；
  route 后 strategy token 括注移除（与 client 标注重复）

- **count_tokens 404 本地短接**：mcli 系网关不支持 `/v1/messages/count_tokens`（实测直接
  404），原实现每笔请求白跑一次上游往返（avg 178ms，11-10 单日 444 笔）。新增
  `CountTokensUnsupportedStore`（supply 级学习，48h TTL）：首次 404 后 learn，后续该 supply
  的 count_tokens 请求本地直接返 404（0ms），不打上游。客户端行为不变（CC 拿不到就本地
  估算）；支持该端点的其他 supply（如 ds-flash 返回 `{"input_tokens":N}`）不受影响。

- **存量 item_ id 出站规范化（2026-11-10）**：r2a 早期生成的 function_call item id 为
  `item_` 前缀（规范要求 `fc_`，已由 cd0cd49 修正生成侧），但**存量 codex session 历史**
  里仍是旧前缀，回放到 responses 上游（terra/sol）时上游校验前缀直接 400
  （"Expected an ID that begins with 'fc'"），每次白耗一次 failover（11-10 单 session 42 次）。
  新增 `core/item_id_normalize.py`：responses 目标出站前把 `function_call`/`custom_tool_call`
  的 `item_` id 前缀改写为 `fc_`（保留原 hex、不动 `call_id`，配对安全已上游实测），
  仅改出站副本、不改客户端数据；`target == "responses"` 门控，anthropic 上游零影响。
  属无条件规范化（本方历史格式不合规，非上游怪癖，无需 learn/TTL）。

### 已知事项

- astra 加密块间歇解密失败（0.06%，32 次/3 天，均 503 后自愈）：根因定位中，
  已备学习式剥离预案（docs/designs/2026-11-09-astra加密块间歇解密失败与学习式剥离预案.md），
  再遇到时按预案 §4 继续定位
- count_tokens 404（mcli 网关对该端点不支持）：仅观测噪音，不影响对话

## 0.12 (2026-10-10)

### 新功能：正文错误识别——mcli 200+错误文本转 failover/冷却（2026-10-10）

- mcli 系 supply 上游 HTTP 200 但正文携带错误文本（限频「当前小时请求过于频繁」/
  路由未命中「未命中任何路由策略」），对既有 HTTP-code 驱动的 failover/cooldown 不可见
- 顶层 `body_error_rules` 配置文案→`body_*` 错误码映射（contains/exact/prefix 三档，
  不做 regex）；冷却时长仍在 `cooldown_rules` 单一来源配（合并式与 int code 同行）
- 非流式：读响应后判定，命中即换 supply 同请求重发（客户端零感知）；未配 code 原样透传
- 流式：B+ 边界内旁路累计收口判定（finally 覆盖断开/读错路径），命中冷却 + ACCESS 记
  `body_error=`；当前请求透传错误文本，下一条请求自动换 supply
- 误伤防护：无 tool_use/thinking 块 + text ≤200 字（rule 级可覆盖）+ 模式命中
- 已知限制：流式仅 anthropic SSE text 形态；转换路径/count_tokens 插点为 Phase 2 暂缓
- 设计：docs/designs/2026-10-10-正文错误识别与failover衔接方案.md（两轮复核修订）；
  测试 +39（全量 925 绿）

## 0.11 (2026-09-21)

### 修复：passthrough 观察器对 mcli 流的 [DONE] 尾巴误报（2026-09-21）

- mcli 网关在语义完整的 Anthropic 流（message_stop 后）追加 OpenAI 风格 `data: [DONE]`
  冗余尾巴，观察器对非 chat 源无条件报 malformed_stream → catpaw 系流式请求
  observer_error 100%（09-20 达 943 条），stream_integrity/stream_health 全盲
- 修复：终态已确认（confirmed）则忽略冗余 [DONE]，未确认维持报错；
  真实 34KB mcli 流样本事件全集核对无第二不兼容点

### supply 层扩展：接入 mcli（CatPaw）等自定义网关（2026-09-20）

- supply 新增三可选字段（设计：docs/designs/2026-09-20-model_proxy接入mcli的supply层扩展.md）：
  - `extra_headers`：出站 header 注入（最高优先，禁 authorization/x-api-key/content-length）
  - `system_inject`：计费标识 system 块前置合并（幂等，仅 anthropic supply）
  - `appkey_file`：鉴权值文件来源（每请求读，与 appkey 互斥）
- 配置校验：`ConfigStore._validate_config` 新增三字段校验（启动 fail-fast，热重载
  降级为 warning——现有吞异常行为不变）；CLI supply_add/supply_edit 写盘前同款校验
- CLI 探测感知新字段（设计：docs/designs/2026-09-20-model_proxy-CLI探测感知supply扩展字段.md）：
  probe_effort 补三字段；`classify_supply_reachability` 新增 `CONFIG_ERROR` 分类
  （附带：非法 protocol 从 NETWORK_OTHER 迁入 CONFIG_ERROR）
- 测试：tests/test_supply_ext.py（44 用例）+ test_config_ops.py 扩展（6 用例）

- fable 档支持：_MODEL_TIER_MAP 新增 `claude-fable`→fable 映射（config 示例与文档同步补齐）
- session 身份展示：新增 core/session_identity.py 只读解析 ~/.claude/sessions 注册表
  （按次扫描不缓存，坏 JSON/缺字段跳过，同 UUID 多进程取 procStart 最新）；
  CLI status 活跃 session 行首附 `name · uuid8`（段标题注明 name 为当前注册快照），
  $route 回执由 server 层把各行 `session <uuid8>` 统一升级为 `session <name · uuid8>`
  （首行内嵌；未命中保持 uuid8；无 session 不替换）
- 流式响应改为 HTTP 2xx 后立即提交；仅 HTTP 状态/网络错误参与 failover
- PASSTHROUGH 原始字节即时转发，旁路观察终态、usage 与首事件时间
- 正常 EOF 的空流/缺终态追加协议 error；observer 故障与客户端断连独立记账
- 删除首事件 probe、预读缓冲、流级 failover 与超时切换子系统

## 0.10 (2026-08-20)
- CLI status cooldown 列表展示每个 supply 的触发 errorcode（http_429/net_error:...）
- codex 接入全链路：ModelProxyHandler 升 HTTP/1.1（修 HTTP/1.0+chunked 非标组合致
  codex hyper 流式断连）；install_claude 补 ~/.claude.json hasCompletedOnboarding；
  install_codex 补 provider name/experimental_bearer_token/model_catalog_json
  （仓库模板 + install 时拉网络 prompt.md 拼装 + 网络失败降级）
- config 紧凑格式器扩展：route/strategy/cooldown_rules/budget_retry 整对象单行
  （正则6-9，键序锚定，失配回退多行不丢数据）

## 0.9 (2026-08-13)
- 首个正式版本（model_proxy 从 vault 拆出为独立 git repo）
- 新增 cooldown_rules 策略组（按 errorcode 分组冷却 + failover，URLError sentinel）
- 402 额度耗尽进 failover，6h 冷却
- 新增路径常量统一管理（runtime_paths.json + resolve_runtime_paths，Python/Bash 单一真相源）
- /model_proxy/status 新增 unconfigured_codes 暴露未配策略的 code
- /model_proxy/status 新增 version 字段
