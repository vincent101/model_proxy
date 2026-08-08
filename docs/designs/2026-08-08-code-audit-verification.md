---
type: review
status: draft
target: "[[tools/model_proxy]]"
tags: [architect, model_proxy, code-audit, verification]
created: 2026-08-09
---

# model_proxy 全面代码审核报告 — 独立核实（第二 architect）

核实对象：[[2026-08-08-code-audit-comprehensive]]（architect-max 产出）。
性质：独立第二意见，只挑硬伤、不重做审核。方法：逐条对代码 + 对生产 config/日志实测，
不接受被审报告自述的"已实测"作为证据。只读核实，未改任何代码/被审报告。

---

## 结论总览

| 论断 | 判定 | 说明 |
|---|---|---|
| B1 install 不识 route_pool（阻断） | **属实** | 独立复现成立 |
| S1 测试污染生产日志 | **属实**（一处数字小偏差） | import 文件数 8≠9 |
| S2 handle_error 死代码 | **属实** | 框架语义实证 |
| S3 成功路径 failover 不可达 | **属实** | 控制流+stdlib 实证 |
| S6 README 两处写错 | **属实** | 且有自矛盾加强证据 |
| D1-D4 死代码抽查 | **全部属实** | 计数精确 |

**总体判定：报告可信，可按清单执行。** 未发现误报/夸大。唯二偏差不影响任何结论：
S1 的"9 个测试文件"实为 8 个；S1 的"1197 行噪音"是过期快照（现值更高）。

---

## 逐条核实

### B1. install 对 route_pool strategy 失效 —— 属实（阻断成立）

- **代码事实**：`_install_ops.py:89` `route = routes_map.get(st.get("route_id"))`，
  只读单值 `route_id`；`:90-91` `route is None → continue`。
- **生产 config**：4 条 strategy（cc/codex/eval-kimi/eval-dsp）全部只有 `route_pool`
  键、无 `route_id`（已逐条列出 strategy 的 key 集合实证，均
  `[client_token, route_pool, tiers_source_capability, note]`）。
- **独立复现**（`cd tools/model_proxy`，import `_install_ops` 不引入 `core.server`，
  核实过程无污染）：
  ```
  candidate_tokens(cfg, 'anthropic') -> []
  candidate_tokens(cfg, 'responses') -> []
  candidate_tokens(cfg, None)        -> []
  ```
- **根因叙事佐证**：展示侧 `_format_ops.py:67-82`（strategy 描述兼容 route_pool/route_id
  两种写法）、`:162-170`（route_tokens 同样兼容）确实都处理了 pool，唯独 install 的
  candidate_tokens 没跟上——报告根因成立。
- **"用户卡死"叙事佐证**：`_config_ops.py:896` `err(f"client_token 已存在 strategy 绑定")`，
  install 找不到候选 → strategy add 又因 token 已存在拒绝——成立。

### S1. 测试污染生产日志 —— 属实（一处小偏差）

- **模块级副作用实证**（import 时即执行，非 main() 内）：
  `core/server.py:66` `_trim_log(LOG_FILE)`、`:85-104` `_root_handler/_access_handler`
  两个 FileHandler 绑生产路径、`:292` `usage_totals = UsageTotalsStore(TOTALS_FILE)`。
- **路径实证**：`LOG_FILE = .../model_proxy/.claude_model_proxy.log`（:51），
  即生产日志本体。
- **污染实证**（直接 grep 生产日志）：`supply=s1` 命中 **1510** 行、`test_rid` 命中
  **312** 行，全部为 WARNING `budget_retry: supply=s1 ...` 测试链噪音；生产 config 无
  `s1` 这个 supply，确为测试写入。
- **偏差**：报告称"9 个测试文件 import core.server"，实测 **8** 个
  （test_req_id/test_budget_retry/test_usage_totals/test_session_route_dispatch/
  test_route/test_route_command/test_reasoning/test_passthrough_sniff）。轻微高估，不改结论。
- **说明**：报告"1197 行测试噪音"是过期快照——噪音随每次跑测试持续增长且日志被截到
  5000 行，当前仅 supply=s1 一项已 1510 行。数值随时变，定性（污染存在且持续）成立。

### S2. handle_error 死代码 —— 属实

- `core/server.py:1037-1042` 在 `ModelProxyHandler(BaseHTTPRequestHandler)` 上定义
  `handle_error(self, request, client_address)`。
- **框架语义实证**：`hasattr(BaseHTTPRequestHandler, 'handle_error')` → **False**；
  `handle_error(request, client_address)` 是 `socketserver.BaseServer` 的方法
  （签名 `(self, request, client_address)`，由服务器实例调用，非 handler 钩子）。
  handler 类上定义它框架永不调用。死代码判定成立。

### S3. 成功路径 failover 分支不可达 —— 属实

- `core/server.py:1600-1609` `if failover=="on" and resp_status in _FAILOVER_STATUSES`。
- `_FAILOVER_STATUSES = {401,403,429} ∪ range(500,600)`（:580），全为 4xx/5xx。
- **控制流实证**（:1541-1609）：try 成功 `resp_status = resp.status`（:1543）；
  `except HTTPError`（:1544-1583）与 `except (URLError,OSError)`（:1584-1597）各分支
  均以 `continue` 或 `return` 结束。故只有 urlopen 成功才落到 :1599，此时 resp_status
  必为 2xx。
- **stdlib 实证**：默认 opener 含 `HTTPErrorProcessor`，其 `http_response` 对
  `not (200 <= code < 300)` 一律 `parent.error`（重定向成功仍归 2xx，否则抛 HTTPError）。
  成功返回 ⇒ 2xx ⇒ 不在 _FAILOVER_STATUSES ⇒ :1601-1609 恒不可达。成立。

### S6. README 两处写错 —— 属实（有加强证据）

- **级别表错误**：`admin.auth_fail`（server.py:1930 `log.warning`）、`request.reject`
  （:1384 `log.warning`）实为 **WARNING**；README :532（级别规范 INFO 行）与 :536
  （OPT-08 段）却把二者列入 INFO——README 写错。
  **加强证据（报告未点破）**：README :531 的 WARNING 行已写明"401/501 拒绝"属 WARNING，
  与 :532/:536 把 admin.auth_fail(401)/request.reject(501) 列 INFO **自相矛盾**；
  代码(WARNING)与 :531 一致，坐实"代码对、README 错"。
- **stats 过度声称**：`model_proxy_cli.sh:439 VAL_FIELDS = (requests/ok/fail/usage_in/
  usage_out)`，无 attempts/attempt_fail/max_ms；attempt_fail 只落账本 JSON（README:577），
  CLI stats 不投影。README :664-665 "attempt_fail 为 attempt 级失败计数……supply 真实
  失败率可观测"经 CLI 不可见——措辞误导成立。

### 死代码抽查（D1-D4，含任务点名的三条）—— 全部属实

- **D1 handle_error**：见 S2，死。
- **D2 成功路径 failover 分支**：见 S3，不可达。
- **D3 resolve_route**（server.py:740）：生产零调用。全仓 grep 调用方仅
  `tests/test_route.py:63,68,73,265`（server.py:848 只是注释提及）。仅测试引用成立。
- **D4 gen_tooluse_id**（translate.py:174）：与 `gen_toolu_id`(:145) 重复。
  `gen_toolu_id` 4 处调用（:660,970,1972,2170）、`gen_tooluse_id` 仅 :1214 一处——
  报告计数精确。

> D5-D11 与 S5、S7 全表、O1-O10 未逐条独立复核（超出本次点名范围）。其中 S7 的
> server.py:296-299 一条已顺带确认：注释"root 仍是 WARNING"与 :89 `basicConfig(level=
> INFO)`、:95-97"root 已开 INFO"自相矛盾——属实。

---

## 风险与权衡

- 本核实覆盖任务点名的 B1 + S1/S2/S3/S6 + 死代码 D1-D4。**未独立复核** D5-D11、S5、
  S7 全表、O1-O10；若要按完整清单执行，这些条目仍以被审报告为准（本核实未否证它们，
  但也未背书）。
- 核实过程严格只读：未 import `core.server`（避免触发 _trim_log/FileHandler 加重 S1
  污染），S1 结论基于读代码 + grep 生产日志的既有污染证据，证据充分。
- 两处数字偏差（8≠9、噪音行数为动态值）建议主会话在执行时按实值表述，不影响修复动作。

## 验证方式

- B1：`cd tools/model_proxy && python3 -c "import sys;sys.path.insert(0,'.');from _install_ops import load_config,candidate_tokens;cfg=load_config('config/model_proxy_config.json');print(candidate_tokens(cfg,'anthropic'))"` → `[]`（修复后应为 4 个候选）。
- S1：`grep -c "supply=s1" tools/model_proxy/.claude_model_proxy.log`（当前 1510）；
  `grep -rln "core.server\|core import server" tests/ | wc -l`（当前 8）。
- S2：`python3 -c "from http.server import BaseHTTPRequestHandler as H;print(hasattr(H,'handle_error'))"` → False。
- S3：读 server.py:1541-1609 控制流 + :580 `_FAILOVER_STATUSES`。
- S6：server.py:1930/:1384 级别 + cli.sh:439 VAL_FIELDS + README:531/532/536/664。

## 关联

- [[2026-08-08-code-audit-comprehensive]]（被核实对象）
- [[2026-07-28-session-route-dispatch-design]]（B1 根因改动来源）
