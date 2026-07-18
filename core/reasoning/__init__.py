"""core.reasoning — reasoning 强度处理三层正交解耦架构。

依赖单向：ladder ← capability ← codecs ← registry。
领域层（本包）不依赖 server/translate/网络。

- ladder.py：canonical 强度全序枚举 + budget↔canonical 锚点表（零依赖）。
- capability.py：ReasoningCapability（per-supply 能力描述）+ align()（唯一钳位点）。
- codecs.py：各协议（anthropic/chat/responses）的 decode/encode/select_variant/interpret_rejection。
- registry.py：protocol 字符串 → codec 单例。
"""
