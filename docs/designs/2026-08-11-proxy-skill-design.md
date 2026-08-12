---
type: design-decision
status: shelved
target: "[[2026-08-10-proxy-message-inter-session-design]]"
tags: [architect, model_proxy, proxy-skill, in-band-command, skill-design, superseded-by-cc-native]
---

# proxy skill 设计与 v3 文档整合

> ⚠️ **搁置（2026-08-12）**：CC 2.1.224+ 官方 cross-session messaging 覆盖本方案核心
> 场景，自建方案搁置。详见 [[2026-08-10-proxy-message-inter-session-design]] 附录 A。
> 若启用官方功能后需要，可建一个教 agent 用 `ListAgents`/`SendMessage` 的轻量 skill
>（非本文档设想的 v3.1 自建命令族）。

> [理想] 路径。设计名为 `proxy` 的 skill（非 `proxy-message`），覆盖 model_proxy 层的
> 完整 in-band 命令机制；并将 skill 设计整合进 v3 设计文档作为实施的最后一步。

## 1. 背景与问题

v3 设计文档（§17）原有一个 14 行的 `proxy-message` skill 占位段，仅列了内容要点、
无结构设计。需要：
1. 把 skill 名从 `proxy-message` 改为 `proxy`（覆盖 $route + $message 两族命令）
2. 按 [理想] 口径做完整 skill 设计（结构、触发、A/B 侧覆盖、子文件评估）
3. 整合进 v3 文档替换 §17，并在 §18 影响面中明确 skill 创建为实施最后一步

## 2. 方案设计

### skill 定位与边界

- **名字**：`proxy`，非 `proxy-message`。覆盖 proxy 层完整 in-band 命令机制。
- **边界**：只通过 in-band 命令（`$route` / `$message`）与 proxy 交互，不直接碰
  proxy 内部（sidecar / inbox / server.py 对象）。listen on/off 由 agent 侧执行
  （CronCreate + 发命令通知 proxy），skill 教 agent 怎么完成这些动作。

### SKILL.md 结构

概念层 → 命令族 → A 侧流程 → B 侧流程 → listen 机制 → 收/发/取回执处理 → 卫生 → 示例。

### 子文件（理想项）

| 文件 | 内容 |
|---|---|
| `references/listen-cron-template.md` | CronCreate 轮询模板 |
| `references/message-wrapper-format.md` | 注入包装格式定义与解析 |

### 理想 vs 务实差异

理想口径增加：2 个 references 子文件、同时覆盖 $route 命令触发面、注入包装识别触发面。

### v3 文档改动

1. §17 整体替换为完整 proxy skill 设计（7 个子节）
2. §18 影响面中 "proxy-message skill" 改为 "proxy skill"，并明确标注为实施最后一步
3. 全文无遗漏的 "proxy-message skill" 字样（关联 wikilink 指向文档文件名不改）

## 3. 风险与权衡

- skill 本身不含 proxy 状态读写逻辑，风险低。
- 理想项（references 子文件）增加 skill 目录体积，但提供更好的可复用性。
- listen 的 CronCreate 动作依赖 Claude Code 原生能力，若 API 变化需同步更新 skill。

## 4. 验证方式

- v3 文档 §17 自洽性检查：结构与 v3 其他章节（§4 通道、§5 listen、§6 投递、§10 类型、
  §14 命令族）交叉引用一致。
- §18 影响面中 skill 排序与其他功能代码的依赖关系正确（skill 依赖所有功能代码存在）。

## 关联

- [[2026-08-10-proxy-message-inter-session-design]]（v3 主设计，本文为其 §17 的产出）
- [[2026-08-11-proxy-message-splice-feasibility]]（splice 回执可行性，§17 回执机制的依据）
- [[2026-08-04-in-band-route-command-design]]（命令层骨架，$route 命令的设计基础）
