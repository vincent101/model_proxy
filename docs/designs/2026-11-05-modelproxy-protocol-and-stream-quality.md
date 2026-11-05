---
created: 2026-11-05 10:12:51
type: design-decision
status: confirmed
confirmed_stages: [A, B, C]
target: /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104
tags: [architect, model-proxy, protocol, streaming]
branch: fix/tool-config-retry-20261104
base_commit: e8c1c9ff529ad225361ff50dd1240322a7caea98
---
# 协议终态与流质量：务实增量实施方案

## 1. 背景与问题

> 授权边界：本次只确认 ABC；D 未授权，不实施。

前批已交付；本批只修三处明确协议缺陷，并为读取及时性设置条件性验证阶段，不倒灌全审计。
- 授权依据（用户原话）：“嗯，根据复核，重新整理和修订或重构方案，做必要的有价值的事情”。本轮只设计；方案确认前不实施，不自行派其他 agent。
- 外部根 `W=/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104`；本轮只读确认分支如 frontmatter、HEAD=e8c1c9f、clean，目标记录原不存在。
- 线上 `tools/model_proxy` 仍 ca2b159 未合并由主会话git工具核验，设计agent未复测；本轮不读取线上源码/真实配置、credentials、recorded 日志；不执行产品 API/CLI、网络、服务、reload/restart、merge/commit。
- **前置交付**：W 内 `2026-11-04-tool-config-fix-and-guard-revert.md` 已 confirmed＋实施，原文/状态不改、不 supersede；本记录是新阶段 draft，旧历史设计也不改。
- 本轮证据等级：代码/标准库只读核查＝既有实现；日志元数据＝本会话runner只读统计（主会话转交；设计agent未复测）；下文红绿案例＝纯示意，命令＝未来验收。本轮未运行测试或建立 mock 脚本。

### 1.1 已核实改点与背景

| 项 | e8c1c9f 的具体位置与问题 | 本批性质 |
|---|---|---|
| A | `ResponsesToAnthropicStreamAdapter` 用 `_logical_type` 决定 completed；tool→text 后成 end_turn，非流式已有 `has_tool_use`→tool_use | 整响应普通待执行 function_call 交接一致性 |
| B | `OpenAIToAnthropicStreamAdapter.feed` 已把 finish_reason 转成 Anthropic reason，`terminal_state()` 又送 `map_chat_terminal` | 修映射枚举域；不改 wire 或日志架构 |
| C | server 两个 writer 只分 `b'\n\n'`，helper 只取最后一个 data；Chat writer 另有逐行解析；现有 `pt.SSEFramer` 可复用 | 通用分帧、换行等价与多 data，不是模型 patch |
| D | 四 writer 用 HTTPResponse.read(8192/4096)；本机标准库 `_read_chunked` 可跨 HTTP chunks 聚合至 amt/EOF，`read1` 已存在 | 先证价值和等价性，未过门槛不改 runtime |

runner 元数据（本会话runner只读统计，主会话转交；设计agent未复测）：日志末5000行中提取3053条ACCESS，覆盖 2026-11-04 20:49—2026-11-05 10:03；排 count_tokens 29 后 3024。first_event_ms 有效 2979，均 passthrough（Anthropic 2970、Responses 9），转换缺失 45（a2r 42、r2a 3）。
有效样本 first_event_ms：min=7、median=961、p90=1534、max=6063；duration median=6785 ms，valid=2978/client_disconnect=1。不能分离 upstream/网络/read buffer；转换缺数据不能推断非流式或低延迟，也无典型请求普遍 30s 延迟证据。

### 1.2 必须继承的裁决

- W 内 **2026-08-23 最小可靠透传 v2（confirmed）＋2026-08-24 C 档螺旋/B+复盘（closed、B+已实施）**决定当前策略；旧 C 文档 frontmatter confirmed 不代表当前仍执行 C。
- W 内 **跨协议静默丢弃审计 v2**约束 unknown 事件/终态显式失败；`0ff5246/4a950a9` 的末块判断是历史实现，不是模型必须 tool 末尾的契约。官方 SDK/output 顺序结论已给定，不再联网核对。
- vault `docs/designs/2026-09-20-model_proxy接入mcli的supply层扩展.md` 只读：跨协议 failover 计费 system_inject 残留已明示接受；不清理、不改计费注入语义。
- 不做非法 arguments→{} 修订、CLI 解释器、历史 route 续接、ID/store guards 恢复、server-tool bridge、raw_body 重构或无生产证据的条件性 byte 序列化预防修复。
- B+ PASSTHROUGH write-first/observe-second、字节原样、observer 无活流控制权；observer 自身失败不补 error、不扩 failover/cooldown。保持既有 EOF 善后，不恢复 C 档预读、30s 门槛、短超时或强 failover。

## 2. 方案设计

### 2.1 推荐与精确文件边界

推荐复用现有台账/终态映射/SSEFramer，四个小阶段；不建立公共流框架。分别给三套 parser 打补丁会继续分叉；新建第三套 parser/遥测平台/配置开关收益不足，均不选。D 的 read1 只在证据门槛通过后采用，不用 read(1) 性能换延迟。
下表是未来代码/测试/README 的完整允许改动清单；除本记录外，本轮不写任何一项。正文短路径均锚定 W，实际文件操作用绝对路径。

| 绝对路径 | 阶段与增改点 |
|---|---|
| /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/core/translate.py | A：整响应工具交接；B：映射域；C：最小扩展 SSEFramer |
| /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/core/server.py | C：三个转换 writer 复用 framer/去重复 helper；D 过门槛才改四处读取选择 |
| /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_tool_stream_association.py | A：订正一个错误终态期望并补合法混合顺序对拍；保留原 76 项关联/metadata/queue/ID 覆盖 |
| /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_protocol_terminal_handler.py | A/B/C：真实 handler 终态与分帧回归；D：读取异常/终态保护联动 |
| /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_passthrough_sniff.py | C/D：三协议 byte-exact、observer 隔离、EOF 与读取异常不扩错误事件 |
| /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_sse_framing.py | C 新增：纯合成换行/分片/多 data/UTF-8 分帧矩阵 |
| /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/test_stream_reading.py | D 新增：受控 HTTPResponse 门槛、partial/EOF/exception/大数据证据；未过门槛也可只保留验证 |
| /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/README.md | 未来仅 §5/附录B 局部索引新增本记录与两类测试，说明 first_event_ms 转换覆盖缺口；不宣传生产提速 |

### 2.2 阶段 A：Responses→Anthropic 整响应交接

1. `ResponsesToAnthropicStreamAdapter` 成功 completed 时用 **`bool(self._tools)`**（已登记普通 function_call 台账）而非最后逻辑块/当前活动块决定 tool_use；已 done 仅表示参数/输出项封口，不表示客户端已执行。
2. `_logical_type` 只服务旧终态判断，移除其初始化及 `_add_tool/_feed` 中赋值，不另建 has_tool flag；不碰 `_tools/_blocks` 关联、metadata 缺省、队列生命周期、ID 稳定逻辑。
3. 先验现有终态与关联：failed/error/unknown/未绑定参数先走原失败路径，无 message_stop；合法 incomplete(max_output_tokens) 仍 max_tokens；已有显式 stop_reason（含拒绝）不被工具覆盖；只有成功 completed 的默认 reason 才按整响应工具有无取 tool_use/end_turn。
4. 不新增 Responses refusal/server-tool 支持：当前不支持的 refusal 事件/终态仍 fail-fast，不能因先见 tool 而伪装成功；保留现有明确终态后冗余尾事件保护，不重建终态机。
5. `responses_to_anthropic_response` 的整响应 `has_tool_use` 与非法 JSON {} 兜底保持原样。对合法 function_call＋text/reasoning 对拍 content、逐调用 ID/name/arguments、stop_reason；不承诺本批统一全部未知 content/refusal 形态。
6. 订正旧 `test_tool_then_text_preserves_historical_end_turn_not_physical_tool`：更名为 `test_tool_then_text_preserves_response_tool_handoff`、期望 tool_use；保持队列/块序断言。此处是明确纠错，不删除其余 75 项或翻案前批关联交付。
7. 最小红例：added A→合法 arguments→text tail→completed，流式必须与非流式都 tool_use；补 tool→thinking、text→tool→text、多工具及后置 text.delta 的同类对拍。
8. 不触发反例：text-only/reasoning-only completed＝end_turn；tool＋incomplete＝max_tokens；tool＋failed/关联失败/未知终态/当前不支持 refusal 事件不输出成功交接。测试含重复 finalize/冗余 DONE 不双终态。

### 2.3 阶段 B：Chat→Anthropic 终态映射域

1. `OpenAIToAnthropicStreamAdapter.terminal_state()` 对已有 `final_stop_reason` 调 **`map_anthropic_terminal`**，订正注释；保留 `_failed` 优先与缺 reason→OPEN。不新增原始 finish_reason 存储/双域兼容，未知 Chat reason 仍由 feed 的 `map_finish_reason` 显式拒绝。
2. 替换 `TestAdapterTerminalState.test_chat_adapter_terminal_states` 手塞 `tool_calls` 的不可能内部值：用真实 `feed`→usage tail→`finalize`→`terminal_state`，覆盖 stop/tool_calls/length/content_filter。
3. 四值预期分别 COMPLETED/end_turn、COMPLETED/tool_use、INCOMPLETE/max_tokens、REFUSED/refusal；unknown、缺 finish、上游 error/工具关联错误沿现有失败路径。tool_calls 用合法调用 delta，不只塞 reason。
4. 经 `_write_translated_stream` 断言真实 HTTP=200、SSE stop_reason 原值不变、terminal_status/reason 正确、stream_integrity 维持既有规则；固定合成 ID 对比原 wire，reason 修正不改 message_delta/message_stop。
5. 此 bug 由 `293eae3` 引入、e8 保留；其余终态修复继承。当前 bookkeeping 按 HTTP/stream_integrity，不能由错域造成 terminal_status=failed 就宣称所有请求失败；既有数据没有常见 a2chat 日志，不扩大日志架构或长期兼容旧错误。

### 2.4 阶段 C：复用 SSEFramer，分帧与终态各管一层

1. 最小扩展 `pt.SSEFramer.feed/finish` 的字节行边界识别：LF、CRLF、CR、逐行混合等价；CRLF 只算一个行末，跨 chunk 的 CR/LF 需保留配对状态。**CR 已终止空行即 dispatch，不等待下一个 chunk 或 EOF**，后到 LF 只作为配对续字节，不额外派发空事件。
2. 缓存原始 bytes，完整 payload 才解码/JSON 解析；UTF-8 字节跨 chunk 不提前 decode。`_parse` 继续多 data 用 LF 连接、只去冒号后一个可选空格；comment/空行/id-only 等既有忽略语义不扩大，event 缺省用 data.type，类型冲突/坏 JSON 保持 TranslationError。
3. 保留 `SSEEvent/observer` 接口、`max_events`、`buffered_bytes/saw_business_event`；不要求 raw 的发布边界模拟 HTTP chunk，不把 raw 重序列化进 passthrough。新增内部行状态只服务边界，不建新 parser/依赖。
4. 三个转换 writer 都用 `framer.feed(data, max_events=1)`，处理并写出一个事件后再 `feed(b'', max_events=1)` 排空当前已完整帧；不能默认一次解析整个 chunk 再 feed adapter，避免后续坏 JSON 抹掉同 chunk 已确认的前帧终态。
5. `_write_translated_stream`：comment 跳过；event.data→Chat adapter；Chat `[DONE]` 保持现有 break＋finalize，不继续消费其后尾流。`_write_responses_stream` 与 `_write_translated_stream_from_responses`：event_type/data→各 adapter，缺 type 显错。
6. 后两 writer 的非 Chat DONE：原有 completed/failed 标志已确认才忽略；终态前 DONE 仍 malformed。正常 EOF 才 `framer.finish()` 一次接纳既有无空行末帧，再 adapter.finalize；读取异常/客户端断连不伪装正常 EOF。
7. 移除已无调用的 `_parse_anthropic_sse_block`，不保留第二套多 data 实现；复用现有各 writer 的序列化、异常、close 与重复终态保护，不合并 writer 生命周期、不新增 HTTP 200 后 failover。
8. fail-fast 边界分别验证：坏帧＝framer；unknown 事件/终态＝原 adapter；无终态 EOF＝原 finalize；终态后冗余 DONE＝忽略。终态＋坏尾同 chunk 必须先保住已写出的终态，再沿既有异常处理，不补第二终态；observer 解析异常仍只观测。
9. 新 `TestSSEFraming`：每种换行、混合、每个单切点/逐字节分片、CR|LF、UTF-8 中间切点、多 data 合法 JSON、comment、data.type fallback、空行即时 dispatch；不借 `finish()` 让合法活流测试变绿。非法多 data JSON/类型冲突仍红。
10. handler 参数化三转换入口：相同语义输入不同换行/分片后 wire 事件内容与终态等价；第一完整帧 feed 后即有输出（下一次 fake read 的断言检查），末帧无空行仅 EOF 容错。标准 LF 单 data 是不触发反例。
11. `test_passthrough_sniff` 补三协议原始 bytes 对拍（含混合换行/UTF-8 分片），以及 observer feed/finish 异常不追加 error、不换家、不冷却；既有正常 EOF 缺终态善后与读取异常行为不变。

### 2.5 阶段 D：价值验证门槛在前，读取选择在后（条件执行）

1. **门槛 D1（价值）**：`test_stream_reading.py` 用标准库真实 HTTPResponse＋假的 socket.makefile（不创建 socket）＋受控二进制 producer，合成 HTTP chunked headers/body。首 HTTP chunk 包含完整 SSE 首事件且小于 4096/8192，第二 chunk/EOF 由独立 gate 暂不释放。
2. producer 通过条件变量/Event 与操作记录标记“已索取第二 chunk”；现有 read(amt) 对照必须证明此时尚未向 writer 交首事件，而 read1 候选已交首事件；随后释放第二 chunk，双方均正确结束。用 happens-before/记录顺序判成败，等待超时只作防挂 watchdog，不按 flaky 毫秒阈值。
3. 先在测试局部 reader 适配器对照四个现有 writer，不先改 runtime；再过门槛后用真实 HTTPResponse 直入四 writer 验收。只能用立即 EOF 的 BytesIO 测保真，不能用它证明及时性；producer 暂无内容必须阻塞，不能伪造 b''。
4. **门槛 D2（等价）**：小/多次 partial、SSE 跨 HTTP chunks、UTF-8、多事件同块、正常空 EOF、截断 EOF、read1 exception、合法终态后异常、客户端断连分别走既有终态/异常路径；完整结束时 byte 不丢/不重/不乱序，不将 partial 当 EOF。
5. **门槛 D3（吞吐）**：同一受控 producer 另给可用大 payload/≥amt 的大 HTTP chunks，对拍字节/事件/usage/终态与接口操作计数，保留 8192/4096 批量上限，不退化成逐字节/逐行强制读取。线性计数只排除结构退化，不等于生产吞吐保证；离线吞吐对照记录环境/重复结果，时间只作参考，不设毫秒硬阈值；若无法证明大吞吐等价或结果存疑则不改 runtime。
6. 三门槛成立才在 `core/server.py` 增加小私有 `_read_stream_chunk(resp, amt)`，四 writer 仅把原 read 调用替换它：优先现有 read1；仅 reader **没有 read1 属性**时允许既有 read-only fake 走 read，属性存在却不可调用明确报错。
7. helper 接受合法 bytes（partial 或 b''）；None/非法类型显错，不当 EOF。read1 抛异常绝不重试 read、吞错、返回空值或加短超时。read1 的 HTTP framing 本身仍可读取控制行，不宣传“永不阻塞/仅一次系统调用”。
8. 若 D1 无收益、D2 不等价或 D3 无法证明，**D 仅交合成测试与观察结论，server 无 D diff**，不阻塞 A/B/C。保留现有 first_event_ms，明确目前转换链覆盖缺口；不新增遥测平台、配置开关或首帧 probe。
9. 本方案默认全部 socket/network fail-closed，不需要 loopback mockserver；无 socket 证据无法建立就保持观察，不能暗用当前 18889、真实供应商或临时服务绕过门槛。未来需 loopback 时另报随机端口/合成内容/隔离边界再获授权。

### 2.6 方案推演（纯示意，实施后实测才算通过）

| 不变量/链条 | 最小红例 → 推荐预期 | 不触发反例/边界 |
|---|---|---|
| 业务交接不由末块定义；终态优先于工具 | tool→text→completed：旧 end_turn → tool_use；普通调用仍待客户端执行 | 同序列 incomplete→max_tokens；failed/拒绝不转成功 |
| wire /业务结果/流完整性分层 | Chat stop 已转 end_turn，再送 Chat 域＝错；改 Anthropic 域后业务 completed，wire 不变 | HTTP 200、stream valid 可承载 incomplete/refused；不推导所有请求失败 |
| chunk 无关、换行等价 | CRLF/CR/混合首帧或多 data 不能等 EOF 才出；分片不改变 event payload | 单 LF/单 data 原行为不变；坏 JSON/unknown 仍拒绝 |
| 同 chunk 按事件递进、终态只一次 | completed 后坏帧：先确认/发终态再处理坏尾，不能批量 parse 抹掉前帧 | 明确终态后 redundant DONE 忽略；终态前 DONE 失败 |
| partial read 不丢 byte、不提前 EOF | gate 未释放时 read1 partial→writer，随后第二 chunk→相同完整内容 | b'' 仅真实 EOF；异常不转 read 兜底；立即 EOF 测试不证时延 |
| observer 无活流控制权 | observer 注入异常，前缀/后续 bytes 仍原样，EOF 不补伪 error | 真缺终态且 observer 正常，继续既有 B+ EOF 善后 |

## 3. 风险与权衡

- A/B 是局部正确性修复；C 必须先测再复用，尤其 CR 配对、max_events=1 的已处理前帧保护、unknown/DONE/EOF 各层不能互相兜底。严格解析不作为“坏模型”判定或新 cooldown 条件。
- D 只确认可重复合成收益/等价，不把 first_event_ms 分位数归因于 read、不声称已改善或生产改善幅度；吞吐无法证实即可不执行 runtime 修改。
- 无真正用户语义裁决阻塞：推荐 A→B→C，D 有价值才执行。各阶段一个独立变更单元，可独立 commit/回滚（本轮不 commit）；C 不依赖 D，撤 D 不影响前三阶段，撤 C 同时撤其 parser 接入和新增测试。
- 后续按已确认方案由 implementer-high 落地，默认独立 reviewer 复核一次；本轮不派实施/复核。确认本方案不意味着部署、merge/reload/restart 或真实 API 验收授权。

## 4. 验证方式与交付

### 4.1 允许集与命令（仅未来执行）

原允许集基线 **466**、前批工具测试 **76** 为既有基线，不是本轮实测；保留原覆盖，B 改真实 feed 回归、A 一个纠错更名须列旧新 ID 映射，不可靠删测试变绿。四个 recorded case 继续排除，严禁读取 samples/录制日志或无差别 discover。
A/B 用下述命令参数 `AB`；C 完成用 `ABC`；D 验证阶段有新测试后用 `ABCD`（即使 D 未改 runtime）。每阶段记录实际测试数、失败/排除项；D 的时间读数不得写成生产收益。

```bash
cd /Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104
export PYTHONDONTWRITEBYTECODE=1
export MODEL_PROXY_CONFIG=/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/tests/__missing_real_config__.json
test ! -e "$MODEL_PROXY_CONFIG" && test ! -L "$MODEL_PROXY_CONFIG" || exit 1
unset CONFIG_OPS_RELOAD_MARKER MODEL_PROXY_REASONING_DEBUG
git diff --check
/opt/homebrew/bin/python3 -B - ABC <<'PY'
import sys, unittest
from unittest.mock import patch
base = ['tests.test_tool_stream_association', 'tests.test_responses_guard_removal',
        'tests.test_config_compact_format', 'tests.test_config_ops', 'tests.test_route',
        'tests.test_translate', 'tests.test_protocol_terminal_handler',
        'tests.test_thinking_strip_retry', 'tests.test_budget_retry', 'tests.test_body_error']
extra = {'AB': [], 'ABC': ['tests.test_passthrough_sniff', 'tests.test_sse_framing'],
         'ABCD': ['tests.test_passthrough_sniff', 'tests.test_sse_framing', 'tests.test_stream_reading']}[sys.argv[1]]
excluded = {'tests.test_translate.TestChatReasoningMirror.test_chat_reasoning_mirror_nonstream_from_kimi_sample',
            'tests.test_translate.TestChatReasoningMirror.test_chat_reasoning_mirror_stream_from_kimi_sample',
            'tests.test_translate.TestARResponse.test_ar_reasoning_nonstream_from_glm_sample',
            'tests.test_translate.TestARStream.test_ar_reasoning_stream_from_glm_sample'}
def flatten(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t
with patch('socket.socket', side_effect=AssertionError('offline: sockets forbidden')), \
     patch('socket.create_connection', side_effect=AssertionError('offline: connections forbidden')), \
     patch('urllib.request.urlopen', side_effect=AssertionError('offline: real HTTP forbidden')):
    raw = list(flatten(unittest.TestLoader().loadTestsFromNames(base + extra)))
    assert excluded <= {t.id() for t in raw}, 'recorded exclusions changed; stop'
    suite = unittest.TestSuite(t for t in raw if t.id() not in excluded)
    print('Allowed count:', suite.countTestCases(), 'Excluded:', sorted(excluded))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
PY
```

网络保护在 import 测试前建立；既有内层 fake urlopen 只处理合成响应。合成配置/临时数据只放 TemporaryDirectory，不调用产品 CLI/main，不写真实 config、runtime_paths、credentials、日志/账本。D fake socket 只是 makefile 接口对象，不是真 socket，不取消上述 guard。
验收按阶段对拍：A stop_reason＋关联/metadata/队列/ID；B 真实 feed→finalize＋handler acc/wire；C 换行/分片/多 data＋活流首帧＋DONE/unknown/EOF＋B+隔离；D 三门槛及四 writer 接入。保持 466 原覆盖，报告允许集而非“全仓库测试通过”。
确定性 diff 复核：只出现 §2.1 文件；无 config/guards/raw_body/请求计费注入/路由/日志架构/旧 designs diff；没有新服务、部署、failover/cooldown/probe；D 不过门槛时读取路径保持原样。README 索引在相关阶段完成后登记，不提前写落地/提速结论。

### 4.2 交付物规范

沿用 W 的 README 附录 A/B、既有 `tests/test_*.py` 标准库 unittest 与 `docs/designs/YYYY-MM-DD-主题.md` 约定，无领域 CLAUDE.md、无新目录/第三方依赖；仅有本增量测试/记录，偏离＝无。新增部分锚定绝对根：
```text
/Users/vincentwang/Documents/worktrees/model_proxy/fix-tool-config-retry-20261104/
├── docs/designs/2026-11-05-modelproxy-protocol-and-stream-quality.md
└── tests/
    ├── test_sse_framing.py
    └── test_stream_reading.py  # D 验证阶段条件新增
```
命名/用途/消费者：本记录 `status:draft` 供用户确认、implementer/reviewer；`test_sse_framing`＝通用 SSE 不变量，`test_stream_reading`＝条件性读取证据，均由 unittest/验收消费。README 未来只登记这些索引，既有测试就地扩展；不落独立 fixture、mock 工程、报告平台或一次性脚本。
实施摘要交付：基线/分支、阶段/文件清单、允许集命令/实际计数/四项排除、纠错测试映射、D 门槛证据与执行或保持观察的结论、未读真实配置/日志且未联网/部署声明。设计状态变更另需用户授权，不改前批 confirmed 记录。

## 关联（均只读，不追读其日志/外链）

- W 下 [[2026-11-04-tool-config-fix-and-guard-revert]]：confirmed＋已实施前置交付，保留原文/状态。
- W 下 [[2026-08-23-最小可靠透传重构方案-v2]]、[[2026-08-24-modelproxy-C档螺旋与B+收敛复盘]]：B+ 当前裁决及 observer_error 不补 error。
- W 下 [[2026-08-22-跨协议静默丢弃审计与修复方案-v2]]：跨协议终态/unknown 严格边界，不搬入本批排除项。
- vault 只读 [[docs/designs/2026-09-20-model_proxy接入mcli的supply层扩展]]：计费注入残留已接受；本批不修改。
