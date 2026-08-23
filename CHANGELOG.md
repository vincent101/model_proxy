# Changelog

## Unreleased
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
