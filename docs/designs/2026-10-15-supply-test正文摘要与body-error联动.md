---
type: design-decision
status: implemented
target: "[[tools/model_proxy/docs/designs/2026-10-10-正文错误识别与failover衔接方案]]"
tags:
  - architect
  - model-proxy
  - mcli
---

# supply test 正文摘要展示与 body_error 规则联动

## 背景与问题

用户发现：部分上游 HTTP code 200 但正文报"模型不可用/限频"——`supply test`
只看 code 会误判"连通正常"。10-10 已落地 body_error_recognition（failover 侧），
本方案补齐**测试/探测侧**的同类可见性。

## 调研结论（问题确认）

入口：`model_proxy_cli.sh` `supply test <id>`（菜单 `t`）→ `_config_ops.py
supply_check` → `connectivity_test_then_probe` → `run_connectivity_test` →
`probe_effort`。

1. **判活只看 code——确认存在**。`classify_supply_reachability`（:457）对
   2xx 直接判 REACHABLE"连通鉴权正常"，不看正文（:492-494）。400 路径有
   模型关键词判定（`_MODEL_ERROR_KEYWORDS`），是正文判定的既有先例。
2. **"不展示正文"表述需修正**：① 联通状态节 REACHABLE 时确实不显示正文；
   但交互流程的② effort 探测节**会打印全文**（`run_probe_and_maybe_accept`
   两个分支都 `print(text_fixed)`，无截断）。真实问题是：**结论误导**——
   200+错误被判"成功（连通鉴权正常）"，错误正文埋在"探测失败，正则不保证
   准确"的语境下，对"该 supply 是否可用"给出错误信号；且无任何
   body_error_rules 联动。
3. **复用确认（最大机会成立）**：`probe_effort` 是**非流式**小请求
   （urllib 一次性 `resp.read()`，max_tokens=16），全文在手。生产链路的
   `_extract_body_error_text`（bytes→text+结构标记）+ `match_body_error`
   （纯函数，含 max_text_len=200 / 无 tool_use+thinking 三重误伤防护）
   可直接复用——测试与生产**识别面完全一致**（同一对函数）。
   `_config_ops.py` 已 `from core.server import ...`，import 无障碍。

## 方案设计（务实，小改动）

改动全部在 `tools/model_proxy/_config_ops.py`（CLI 入口与 shell 不动）：

1. **常量**：`ReachabilityCategory` 增 `BODY_ERROR = "body_error"`。
2. **新 helper（纯函数，不发请求）**：
   ```python
   def _body_error_check(text_fixed, rules):
       """返回 (errorcode | None, extracted_text)。复用生产识别面。"""
       text, saw = _extract_body_error_text(text_fixed.encode("utf-8"))
       code = match_body_error(text, rules, saw_tool_or_thinking=saw, text_len=len(text))
       return code, text
   ```
   import 增补：`from core.server import _extract_body_error_text, match_body_error`
   （后者本就是公开名）。`text_fixed` 源自 raw bytes，encode 回 UTF-8 走
   `json.loads` 无损。
3. **判定降级**：`run_connectivity_test(supply, body_rules=None)`——classify
   得 REACHABLE 后调 `_body_error_check`；命中则降级
   `category=BODY_ERROR`，`desc=f"HTTP {status} 但正文命中 body_error 规则（{code}），supply 实际不可用或受限"`。
   覆盖 REACHABLE 的两种形态（2xx / 400-探测值被拒），rules 为空时
   `match_body_error` 返回 None，零行为变化（fail-open）。
4. **展示策略（推荐：仅异常展示）**：`_print_connectivity_status` 增可选参
   `body_error_code/text`，仅 BODY_ERROR 分支新增一行：
   `    正文摘要：<extracted_text 截 100 字符，换行折叠为空格>`
   正常 REACHABLE 不附正文——② 探测节已打印全文，重复无益且刷屏；既有
   非 REACHABLE 分支打印全文 text_fixed 的行为**保留不动**（归因需要，另案）。
   截断用提取后的 text（跳过 JSON 噪声，`_extract_body_error_text` 的兜底
   恰是无 content text 时回 raw 全文），非 raw 截断——raw 前 100 字符可能
   全是 id 类 JSON 噪声。
5. **联动（纳入本期）**：`connectivity_test_then_probe(supply, body_rules=None)`
   透传；BODY_ERROR != REACHABLE → 自动跳过 effort 探测与 rcap 写入，
   `supply_check` 打印"连通性测试未通过，跳过 effort 探测"。成本约 10 行，
   `supply_edit` 的 rcap 重探测分支同语义自动受益。**边界**：测试进程不写
   任何运行时状态（cooldown 是 server 进程内存态，测试侧只影响测试结论，
   不耦合）。
6. **caller 传参**：`supply_add`/`supply_edit`/`supply_check` 三处
   `connectivity_test_then_probe(target, cfg.get("body_error_rules"))`。
7. **文档**：`docs/CONFIG.md` §10 补一句"REACHABLE 判定后叠加 body_error_rules
   正文识别，命中判 body_error 失败"。

`classify_supply_reachability` 签名与语义不动（纯函数单测不破；body_error
需要 rules，属调用方职责，混入会破坏"纯 status/exc 归因"的内聚）。

### 推演（具象走查）

- **正例**〔推算〕：glm-53-mcli-3339 返回 200 +
  `{"content":[{"type":"text","text":"当前小时请求过于频繁"}]}`。
  extract→text="当前小时请求过于频繁"（11 字），saw=False；rule
  pattern contains 命中→`body_rate_limited`。输出：①"失败，HTTP 200 但正文
  命中 body_error 规则（body_rate_limited）"+ 正文摘要行；跳过探测；
  supply_check 退出码 1。符合预期。
- **反例（不误伤正常路径）**〔推算〕：正常 400（探测值被拒）+ 枚举错误
  `{"error":{"message":"Invalid parameter: reasoning_effort..."}}`。
  extract 无 content text→兜底 raw 全文（长 >200）→ max_text_len 短路或
  pattern 不含→未命中→维持 REACHABLE"400 符合预期"。行为与现状一致。
- **误报复盘**：200+正常短回复恰好含规则串 → 误判 BODY_ERROR。后果=测试
  显示失败，人工看摘要即辨；且生产链路同一套规则同样会判（语义一致），
  非新风险面。max_tokens=16 使回复极短，理论可达但概率低。

## 风险与权衡

- R1 规则覆盖面依赖既有 `body_error_rules`（当前仅 2 条：限频/路由未命中）。
  "模型不可用"类文案未配规则时仍判 REACHABLE——但正文摘要行只在命中时展示，
  未命中时依旧靠②节全文。**残余可见性问题如实声明**：若要未命中也展示摘要，
  属展示策略扩展，本期不做（避免正常路径加噪）。
- R2 import 私有名 `_extract_body_error_text` 跨模块：同仓库内部惯例可接受
  （`_config_ops` 已 import core.server 多名）；如介意可在 server.py 加公开
  别名，非必须。
- R3 测试进程与 server 进程规则快照可能不同步（测试读的是磁盘 config）——
  测试语义本就如此，非新风险。

## 验证方式

- 单测（`tests/test_config_ops.py` 增补，纯函数不发请求）：
  `_body_error_check` 命中/未命中/无 rules/长正文短路/含 tool_use 结构标记
  五用例（构造 text_fixed 字符串即可）。
- 手工实测：`bash model_proxy_cli.sh` → supply 菜单 `t` → 选一个 mcli supply，
  核对①节输出含正文摘要行；无 rules 场景（临时清空 body_error_rules）输出
  与现状逐行一致。
- 回归：`python3 -m pytest tests/test_config_ops.py` 全绿（classify 单测不受
  影响）。

## 交付物规范（轻量版）

本设计唯一新产物即本文档，落 `tools/model_proxy/docs/designs/`（仓库既有
设计记录约定目录，frontmatter/命名沿用既有规范，见 2026-10-10 文档同款）。
代码改动均落 `_config_ops.py` 既有结构，无新目录。

## 关联

- [[tools/model_proxy/docs/designs/2026-10-10-正文错误识别与failover衔接方案]]（识别函数来源）
- [[tools/model_proxy/docs/designs/2026-08-15-supply-cooldown-errorcode设计]]
