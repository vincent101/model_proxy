# Changelog

## 0.9 (2026-08-13)
- 首个正式版本（model_proxy 从 vault 拆出为独立 git repo）
- 新增 cooldown_rules 策略组（按 errorcode 分组冷却 + failover，URLError sentinel）
- 402 额度耗尽进 failover，6h 冷却
- 新增路径常量统一管理（runtime_paths.json + resolve_runtime_paths，Python/Bash 单一真相源）
- /model_proxy/status 新增 unconfigured_codes 暴露未配策略的 code
- /model_proxy/status 新增 version 字段
