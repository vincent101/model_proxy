"""core.reasoning — reasoning 强度处理三层正交解耦架构。

依赖单向：ladder ← capability ← codecs ← registry。
领域层（本包）不依赖 server/translate/网络。

新数据流（decode → resolve_source_capability/resolve_target_capability → remap →
abstract_encode → syntax_adapt）：

- ladder.py：canonical 强度全序枚举 + budget↔canonical 锚点表（零依赖）+ RawIntent。
- capability.py：ModelReasoningCapability（source/target 共用能力描述）+
  remap()（唯一的跨模型相对排名映射点，双侧 src_cap/tgt_cap）+
  abstract_encode()（TargetEffort -> AbstractReasoning，OFF→DISABLED 的唯一判断点）。
- codecs.py：各协议（anthropic/chat/responses）的
  decode/syntax_adapt/select_variant/interpret_rejection。
- registry.py：protocol 字符串 → codec 单例。

OFF 允许特殊分支但统一收在 remap()+abstract_encode() 两处；MAX 完全不允许特殊分支，
全包统一走查表/排名路径。resolve_source_capability 定义在 server.py（依赖 config）。
"""
