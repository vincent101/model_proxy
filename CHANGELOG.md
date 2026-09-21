# Changelog

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
