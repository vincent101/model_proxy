---
created: 2026-07-23 21:23:29
type: design-decision
date: 2026-07-23
status: draft
target: "[[tools/model_proxy/README.md]]"
tags: [architect, model_proxy, documentation, refactor]
---

# model_proxy README 理想终态全量重构 · 执行方案

本方案把 2026-07-22 复核报告里的 C 方案（理想终态，按认知路径重排）细化到 implementer 可直接照做的颗粒度。implementer 不做内容组织判断，只按本文执行搬迁、收敛、新增。

现状 README：`tools/model_proxy/README.md`，共 436 行。以下所有"现状 L行号"均指该文件当前版本。

---

## 背景与问题

现状 README 按"配置文件结构"组织（supplies→routes→strategies→字段明细），60% 篇幅压在最庞大最烧脑的配置理论上，缺 quick start，reasoning 语义分散 4 处重复，且 docs 目录描述漏列 `designs/`。重构目标：改为"按读者认知路径组织"（跑通→概念→链路→深入→限制），字段明细降级为速查附录，reasoning 收敛到单一深讲节，修正 A1 事实错误、补 B3 缺失说明。

---

## 一、新 README 完整章节大纲

新结构共 8 个正文节 + 1 个附录。层级用 `##`（一级节）/`###`（子节）。

### `# model_proxy`（标题，保留）

### `## 1. 这是什么`
- **要点**：一句话定位（本地多协议 AI 模型代理，端口 18889 默认可配，同时接 Claude Code / codex-cli 等多 SDK，可跨协议互访）；跨协议一句话举例（Claude Code 里调 GPT、codex 里调 Claude）；与 `tools/proxy.py`（18888，纯 Anthropic 轮转代理）的关系一句话（完全独立并行、互不依赖）。
- **覆盖现状**：L3-L10（现状「这是什么」整节）。
- **改动**：精简到 5-6 行，删掉可下沉的括注，保留结论。

### `## 2. Quick Start`【新增节】
- **要点**：3 步最小可用路径跑通 Claude Code 接入，不涉及多协议/reasoning 理论。每步给命令 + 预期输出。具体文字素材见本文「四·1」。
- **覆盖现状**：无（全新）。放在最前，让只想用起来的人不必先趟配置理论。

### `## 3. 核心概念：三段式配置`
- `### 3.1 三段关系总览`
  - **要点**：一段话讲清 supplies（供给单元=一个上游端点）/ routes（家族模板=一个 id + opus/sonnet/haiku 三档 supply 列表）/ strategies（client_token→route 绑定）三者是什么、如何按 id 层层引用；配一张文字版关系图（素材见「四·3」）。
  - **覆盖现状**：L36-L42（现状「配置怎么写」开头对三段结构的总述）。
- `### 3.2 supplies：供给单元`
  - **要点**：给一个 JSON 示例；逐字段说明 id/url/protocol/appkey/target_model/cooldown_seconds/reasoning_capability 各是什么。**字段的完整明细表下沉到附录 A**，本节只给示例 + 每字段一句话。protocol 推断规则收进本节子块（见下）。
  - **覆盖现状**：L46-L82（supplies 示例 + 字段列表 + anthropic thinking 注意框）。
  - `#### protocol 推断规则`
    - **要点**：可选字段；权威逻辑在 `core/reasoning/registry.py::resolve_protocol`；优先级（显式 > url 尾缀推断 > 都推断不出则 500）；三条尾缀映射表。
    - **覆盖现状**：L84-L100（现状「protocol 推断规则」整节，原为独立 `###`，改为 supplies 下 `####` 子块）。
- `### 3.3 routes：家族模板`
  - **要点**：JSON 示例；id/tiers（固定三档 opus/sonnet/haiku，每档取第一个未冷却 supply）/failover（on 触发冷却+换档重试、off 直接返回）；"多档共享同一组真实上游"说明 + deepseek 例子；供给单元按真实能力命名的建议。
  - **覆盖现状**：L148-L186（现状「routes」整节）。
- `### 3.4 strategies：client_token → route 绑定`
  - **要点**：JSON 示例；client_token/route_id/note 字段；`tiers_source_capability` **只留一句话指针**（见下方 reasoning 收敛方案，指向第 6 节）；"禁用一个 token 直接删记录"。**删掉"为什么 source 能力挂 strategy"和"source 为什么人工填"两段长论证**（下沉到第 6 节）。
  - **覆盖现状**：L188-L226（现状「strategies」整节，其中 L215-L226 两段论证移走）。

### `## 4. 请求处理流程`【合并节】
- `### 4.1 端到端链路总览`
  - **要点**：一张文字版流程图 + 链路叙事，把"入站鉴权识别 → 入站协议识别 → 三阶段匹配（strategy→route→tier→supply）→ effort 映射 → 出站转换/转发"串成一条完整故事。素材见「四·2」。
  - **覆盖现状**：无独立原文（是把下面几节的割裂内容缝成一条叙事的新导语）。
- `### 4.2 入站鉴权识别（client_token 提取）`
  - **要点**：`extract_client_token` 支持 `Authorization: Bearer` 与 `x-api-key` 两种写法；优先级（Bearer 优先、回退 x-api-key、都无则空 token→401）；两条边界（Bearer scheme 大小写不敏感 RFC 6750、取值 strip）；不支持 Azure `api-key`；与出站双发 appkey 的对称性。
  - **覆盖现状**：L228-L242（现状「入站鉴权识别」整节）。
- `### 4.3 入站协议识别（detect_source）`
  - **要点**：按 path 尾缀判断（大小写不敏感）三条映射；body 特征兜底顺序；unknown→兜底/501；客户端 path 统一丢弃、出站只用 `supply.url`+净化 query（剔除 beta）。
  - **覆盖现状**：L244-L254（现状「入站协议识别」整节）。
- `### 4.4 三阶段匹配`
  - **要点**：三步（client_token 查 strategy 拿 route_id 拿 route → model 精确查表映射 tier → 从 tiers 取 supplies 列表交 failover）；model 非三预设值→400 不兜底；settings.json 三个 `ANTHROPIC_DEFAULT_*_MODEL` 固定值 + 切家族用 switch 不动 model 标签。
  - **覆盖现状**：L256-L267（现状「三阶段匹配流程」整节）。
- `### 4.5 effort 跨模型映射`
  - **要点**：**只留结论 + 链路一句话**（decode→remap→abstract_encode→syntax_adapt），核心思想一句话（相对排名映射非绝对锚定），公式与边界指向第 6 节。**不在本节展开算法**（见 reasoning 收敛方案，本节压缩为导引）。
  - **覆盖现状**：L276-L295（现状「effort 跨模型映射」整节，主体移入第 6 节，本处仅留 3-4 行导引 + 指针）。

### `## 5. 运维与控制`【合并节】
- `### 5.1 启动与停止`
  - **要点**：`model_proxy_cli.sh on` / 手动 `MODEL_PROXY_PORT=18889 python3 model_proxy.py &`；端口默认 18889 + 环境变量覆盖；日志文件 `.claude_model_proxy.log`（启动截断保留最后 5000 行）；进程锁 `/tmp/claude_model_proxy.lock`；SessionStart hook 自动拉起（幂等、PID/锁文件、install 维护 hook 路径正确性）。
  - **覆盖现状**：L297-L319（现状「怎么启动」的 L299-L319，含 hook 段）。
- `### 5.2 日志与观测`
  - **要点**：WARNING 级异常日志 + INFO 级 ACCESS 访问日志（字段列表、token 尾4位）；token 用量统计覆盖范围（转换模式、PASSTHROUGH 非流式/流式）；PASSTHROUGH 流式"转发在前旁路嗅探在后"策略；不做成本折算只统计数量。
  - **覆盖现状**：L321-L334（现状「怎么启动」里 access 日志 + usage 统计两段）。
- `### 5.3 配置热重载`
  - **要点**：`ConfigStore` mtime 比对每请求 `maybe_reload`；手动 `reload` vs mtime 自动 reload 的差异（前者清空全部冷却、后者不动）；解析失败保留旧配置记 warning 不崩。
  - **覆盖现状**：L336-L339（现状「怎么启动」里热重载段）。
- `### 5.4 reasoning debug 旁路日志`
  - **要点**：默认关闭；`MODEL_PROXY_REASONING_DEBUG=1` 启动前 export 打开；只影响本模块 logger；不支持热切换需重启。
  - **覆盖现状**：L341-L343（现状「怎么启动」末段）。
- `### 5.5 CLI 命令参考`
  - **要点**：命令表（status/reload/supply/route/strategy/switch/install/on/off/logs/stats/--help）；交互菜单说明（一级入口进菜单、原子写盘后 reload、非 TTY 只打印 list 不进菜单）；strategy add/edit 录入 tiers_source_capability 逐 tier 人工问答（`-`→空列表、留空→不写/保留）；`supply [t]est` 连通性归因分类（DNS/超时/拒绝/鉴权/模型错误/REACHABLE/未知）+ REACHABLE 才做 effort 探测 + 探测不保证准确需人工核实；`off` 双重匹配（脚本绝对路径 + 端口反查校验 model_proxy.py）。
  - **覆盖现状**：L344-L390（现状「怎么控制（CLI）」整节）。
  - **补充 B3**：`stats` 说明处加一句"stats 基于日志文件现存 ACCESS 行聚合，日志启动时被截断到最后 5000 行，故 stats 只统计现存行、不含已被截断的历史"。

### `## 6. reasoning 强度映射（深入）`【收敛节，reasoning 唯一深讲处】
- `### 6.1 reasoning_capability 字段语义`
  - **要点**：effort_enum/off_alias 是同一套语义，target 侧（supply.reasoning_capability）与 source 侧（strategy.tiers_source_capability 每个 tier entry）共用、解析一致；effort_enum 四种写法行为表；STRIP/DISABLED 定义；`["off"]` 与 `[]` 强制等价走 STRIP；空列表 vs 不写的语义差异；off_alias 语义 + 两条约束；档名词表（大小写不敏感、未识别忽略、off≡none）；budget 断点全局固定。
  - **覆盖现状**：L101-L146（现状「reasoning_capability 字段语义」整节，原样搬入，作为本节主体）。
- `### 6.2 source 能力为何挂 strategy、为何人工填`
  - **要点**：表面模型名是被多 SDK 共享的 tier 选择器字符串（codex-cli 也发 `model="claude-sonnet"`），真正代表客户端身份的是 client_token，故 source 能力挂 strategy；source 侧无可探测的真实上游端点，故人工填；target 侧（supply）有真实上游可 probe。
  - **覆盖现状**：L215-L226（现状 strategies 节里"为什么挂 strategy"+"为什么人工填"两段，移入此处）。
- `### 6.3 effort 跨模型映射算法`
  - **要点**：链路 decode→remap→abstract_encode→syntax_adapt；相对排名核心思想；映射公式 `floor(i/(m-1)*(n-1)+0.5)` + n==1/m==1/未声明 三条边界；单调不减；off 是吸收态不参与排名。
  - **覆盖现状**：L276-L295（现状「effort 跨模型映射」整节主体，移入此处）。
  - **末尾指针**：`想深入了解完整算法推导、单调性证明与决策记录，见 docs/archive/reasoning_relative_remap_redesign.md`（保留现状 L294-L295 指针）。

### `## 7. 接入各 SDK（install）`
- **要点**：install 命令行为（按协议过滤候选 token、交互选择、已装则备份写入 / 未装则打印片段、只读 strategies 不改绑定）；四 SDK（claude/codex/hermes/openclaw）各自协议 + 写入目标 + 写入策略；四 SDK 协议过滤 + 无匹配提示 + 多匹配交互选择。
- **覆盖现状**：L392-L415（现状「接入各 SDK（install）」整节）。
- **补充 B3（三处）**：
  1. install 各 SDK 的 **base_url 期望形态表**（新增，素材见「四·5·b」）。
  2. **detect_installed 口径**：加一句"install 里的『已装/未装』判定口径是『配置目录存在即视为已装』（不要求配置文件本身存在）"。
  3. **codex 未核对官方文档提示**：加一句"codex 段 base_url 拼到 `/v1` 层级、由 wire_api=responses 自拼 `/responses`，此拼法依据本项目 detect_source 反推、未逐字核对 codex 官方 config.toml 文档；实际接入若报 404/400，请核对 codex 官方文档调整 base_url 层级"。

### `## 8. 当前状态 / 已知限制`
- **要点**：五种协议组合转发/转换支持 + 其余 501；cross-supply failover；thinking/effort 方言自适应（enabled/adaptive 双变体、400 拒绝重试缓存 48h、只重跑 wire 语法不重算强度）；reasoning 强度映射一句话结论 + 指向第 6 节；错误路径加固；SessionStart 只启动一次不自愈需手动 on；未接自动化测试覆盖真实上游网络；effort 探测不保证准确。
- **覆盖现状**：L417-L436（现状「当前状态 / 已知限制」整节）。
- **改动**：其中 reasoning 强度映射条目（现状 L427-L429）**删掉重复的语义复述**，压成一句"reasoning 强度按 source/target 各自声明的档位能力做相对排名映射，非绝对锚定，详见第 6 节"。
- **补充 B3**：新增一条"codex install 的 base_url 字段未逐字核对 codex 官方文档，实际接入报 404/400 时需按官方文档调整（详见第 7 节）"。

### `## 附录 A：配置字段速查表`【新增】
- **要点**：supplies / routes / strategies 三张表，每张列出全部字段名、类型、必填可选、一句话语义、默认值。供懂系统的人快速查字段，不必读正文叙事。
- **覆盖现状**：把 L46-L82（supplies 字段）、L148-L167（routes 字段）、L188-L213（strategies 字段）里的**字段级明细**抽出成表；正文各节只保留示例 + 一句话，字段全量落此附录。
- **附**：`admin_token` / `default_cooldown_seconds` 两个顶层字段（现状 L269-L274「顶层字段」节）并入本附录顶层字段表，`完整样例见 config/model_proxy_config.example.json` 一句放附录末尾。

### `## 附录 B：目录结构`
- **要点**：现状 L12-L34 的目录树，**修正 A1**（补 `docs/designs/`）。素材见「五·A1」。
- **覆盖现状**：L12-L34（现状「目录结构」整节，从正文第二节位置移到附录 B——目录结构对新读者不是认知路径关键，降级为查阅附录）。

---

## 二、内容搬迁映射表

| 现状章节 / 内容块 | 现状 L行号 | 去向（新结构） | 处理方式 |
|---|---|---|---|
| 「这是什么」 | L3-L10 | § 1 这是什么 | 精简搬迁 |
| 「目录结构」目录树 | L12-L34 | 附录 B 目录结构 | 移到附录 + **A1 修正补 designs** |
| 「配置怎么写」总述（三段结构） | L36-L42 | § 3.1 三段关系总览 | 搬迁 + 加文字关系图 |
| 「配置怎么写」热重载一句（见「怎么启动」） | L44 | § 5.3 配置热重载 | 合并（交叉引用改，见清单 #1） |
| supplies 示例 + 字段列表 + anthropic 注意框 | L46-L82 | § 3.2 supplies（示例+一句话）+ 附录 A（字段明细） | 拆分：示例留正文、字段明细下沉附录 A |
| 「protocol 推断规则」 | L84-L100 | § 3.2 下的 `#### protocol 推断规则` | 降级为 supplies 子块 |
| 「reasoning_capability 字段语义」 | L101-L146 | § 6.1 | 整节搬入第 6 节，作主体 |
| 「routes」示例 + 字段 + 多档共享 + 命名建议 | L148-L186 | § 3.3 routes（示例+说明）+ 附录 A（字段） | 拆分：字段明细下沉附录 A、其余留正文 |
| 「strategies」示例 + 字段 | L188-L213 | § 3.4 strategies（示例+一句话）+ 附录 A（字段） | 拆分 |
| strategies 里"为何挂 strategy"+"为何人工填"两段 | L215-L226 | § 6.2 | 移入第 6 节 |
| 「入站鉴权识别」 | L228-L242 | § 4.2 | 整节搬入第 4 节 |
| 「入站协议识别」 | L244-L254 | § 4.3 | 整节搬入第 4 节 |
| 「三阶段匹配流程」 | L256-L267 | § 4.4 | 整节搬入第 4 节 |
| 「顶层字段」（admin_token/default_cooldown_seconds） | L269-L274 | 附录 A 顶层字段表 | 下沉附录（含"完整样例见 example.json"一句） |
| 「effort 跨模型映射」 | L276-L295 | § 6.3（主体）+ § 4.5（3-4 行导引） | 主体移第 6 节、第 4 节留导引 |
| 「怎么启动」on/手动/端口/日志/锁 | L297-L311 | § 5.1 启动与停止 | 搬迁 |
| 「怎么启动」SessionStart hook 段 | L313-L319 | § 5.1 启动与停止（含 hook） | 搬迁 |
| 「怎么启动」access 日志段 | L321-L326 | § 5.2 日志与观测 | 搬迁 |
| 「怎么启动」usage 统计段 | L328-L334 | § 5.2 日志与观测 | 搬迁 |
| 「怎么启动」热重载段 | L336-L339 | § 5.3 配置热重载 | 搬迁（合并 L44 的一句） |
| 「怎么启动」reasoning debug 段 | L341-L343 | § 5.4 reasoning debug | 搬迁 |
| 「怎么控制（CLI）」命令表 + 说明 | L344-L390 | § 5.5 CLI 命令参考 | 搬迁 + **B3：stats 受日志 trim 说明** |
| 「接入各 SDK（install）」 | L392-L415 | § 7 接入各 SDK | 搬迁 + **B3：base_url 表、detect_installed 口径、codex 未核对提示** |
| 「当前状态 / 已知限制」 | L417-L436 | § 8 已知限制 | 搬迁 + reasoning 条目去重压缩 + **B3：codex 未核对一条** |
| —（无原文） | — | § 2 Quick Start | 全新，素材见四·1 |
| —（无原文） | — | § 4.1 端到端链路总览 | 全新，素材见四·2 |
| —（无原文） | — | § 3.1 文字关系图 | 全新，素材见四·3 |

**整节删除**：无。所有现状内容都有去向（部分下沉附录、部分去重压缩）。

---

## 三、reasoning 四处重复内容收敛方案

现状 reasoning 语义散在 4 处，收敛后统一到 **§ 6**，其余处只留一句话指针。

**四处原文范围：**

- **P1 = L101-L146**「reasoning_capability 字段语义」整节：effort_enum 四写法行为表、STRIP/DISABLED、off_alias 约束、档名词表、budget 断点。这是**主体**。
- **P2 = L207-L226**「strategies」节内 `tiers_source_capability` 字段说明（L207-L213）+ "为何挂 strategy"（L215-L221）+ "为何人工填"（L222-L226）。与 P1 反复强调"同构、复用同一套解析"。
- **P3 = L276-L295**「effort 跨模型映射」整节：链路、相对排名思想、公式、边界、单调性、off 吸收态。
- **P4 = L427-L429**「已知限制」里 reasoning 强度映射条目：又复述一遍"source/target 各自声明档位、相对映射非绝对锚定"。

**收敛后各处保留内容：**

| 位置 | 保留什么 | 指向 |
|---|---|---|
| § 6.1（P1 落点） | P1 全文原样，作为 reasoning 语义唯一权威处 | — |
| § 6.2（P2 论证落点） | P2 的 L215-L226 两段论证原样搬入 | — |
| § 6.3（P3 落点） | P3 全文原样，含末尾 archive 指针 | archive/reasoning_relative_remap_redesign.md |
| § 3.4 strategies（P2 字段处） | 只留一句：`tiers_source_capability`（可选）声明该 client_token 各 tier 的 source 侧 reasoning 能力，结构与 supply.reasoning_capability 同构、解析复用同一套；某 tier 未声明或整条 strategy 无此字段则回退默认 5 档。**字段语义、为何挂 strategy、为何人工填详见 § 6** | § 6.1 / § 6.2 |
| § 4.5（P3 导引处） | 只留 3-4 行：一句话核心思想（客户端档位是相对自己表面模型的排名、按比例映射到真实上游档位排名，非全局绝对锚定）+ 链路一句话（decode→remap→abstract_encode→syntax_adapt）。**完整公式、边界、单调性详见 § 6.3** | § 6.3 |
| § 8 已知限制（P4 处） | 压成一句：reasoning 强度按 source/target 各自声明的档位能力做相对排名映射、非绝对锚定，**详见 § 6** | § 6 |

**交叉引用改法**：现状 L209「见上『reasoning_capability 字段语义』」→ 改为「见 § 6.1」；现状 L294-L295 archive 指针原样保留在 § 6.3 末尾；现状 L429 archive 指针在 § 8 删除（因 § 8 已改为指向 § 6，而 § 6.3 已含 archive 指针，避免重复）。

---

## 四、新增内容具体文字素材

### 1. Quick Start（§ 2 完整素材，implementer 可直接采用）

> ## 2. Quick Start
>
> 三步让 Claude Code 通过本代理跑起来（假设已配好 `config/model_proxy_config.json`，
> 至少含一条 `client_token` 的 strategy，未配则先看 § 3、§ 7）。
>
> **① 启动代理**
> ```bash
> tools/model_proxy/model_proxy_cli.sh on
> ```
> 预期输出：
> ```
> Starting model_proxy.py on port 18889...
> model_proxy started (pid <PID>), ready (N * 0.5s)
> ```
> （已在监听则输出 `model_proxy already running on port 18889`。）
>
> **② 接入 Claude Code**
> ```bash
> tools/model_proxy/model_proxy_cli.sh install
> ```
> 交互式列出四个 SDK 与本机检测状态，选 `claude` 对应序号（如 `0`），
> 再从协议匹配的候选 client_token 里选一个。确认 diff 后写入
> `~/.claude/settings.json` 的 `env`：
> ```
> ANTHROPIC_BASE_URL=http://localhost:18889/
> ANTHROPIC_AUTH_TOKEN=<你选的 client_token>
> ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus
> ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet
> ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku
> ```
> （写入前会先备份原文件到 `settings.json.bak.<时间戳>`。）
>
> **③ 重启 Claude Code 生效并验证**
> 重启 Claude Code 后，用 `logs` 看是否有转发记录：
> ```bash
> tools/model_proxy/model_proxy_cli.sh logs
> ```
> 出现形如 `ACCESS ms=... status=200 source=anthropic route=claude tier=... supply=... token=...`
> 的行即接入成功。想看运行状态与配置概览用 `model_proxy_cli.sh status`。
>
> 日常挂载：`tools/model_proxy/hooker/ensure_model_proxy.sh` 已注册到 SessionStart hook，
> 每次开 Claude Code 会话自动拉起，一般无需手动 `on`（详见 § 5.1）。

（注：`install` 会先跑 `ensure_session_hook` 确保 SessionStart 里存在正确 hook 条目，此点在 § 5.1 / § 7 说明，Quick Start 不展开。）

### 2. 请求处理流程 · 端到端链路叙事（§ 4.1 完整素材）

> ## 4. 请求处理流程
>
> ### 4.1 端到端链路总览
>
> 一个客户端请求进入代理到发往真实上游，经过下面这条链路。各环节的细节见对应子节。
>
> ```
> 客户端请求
>   │  Authorization: Bearer <token>  或  x-api-key: <token>
>   │  POST <任意 path>   body: {"model": "claude-sonnet", ...}
>   ▼
> ┌─────────────────────────────────────────────────────────────┐
> │ ① 入站鉴权识别 extract_client_token          （见 § 4.2）      │
> │    Bearer 优先→回退 x-api-key→都无则空 token→401             │
> │    → client_token                                            │
> ├─────────────────────────────────────────────────────────────┤
> │ ② 入站协议识别 detect_source                  （见 § 4.3）      │
> │    path 尾缀（大小写不敏感）→ body 特征兜底 → unknown          │
> │    → source ∈ {anthropic, responses, chat, unknown}          │
> ├─────────────────────────────────────────────────────────────┤
> │ ③ 三阶段匹配                                  （见 § 4.4）      │
> │    a. client_token ──查 strategies──▶ route_id ──▶ route      │
> │    b. body.model ──精确查表──▶ tier(opus/sonnet/haiku)        │
> │    c. route.tiers[tier] ──▶ supplies 列表 ──failover──▶ supply│
> │    （查不到 strategy→401 / tier 非预设→400 / 无可用 supply→503）│
> ├─────────────────────────────────────────────────────────────┤
> │ ④ effort 映射                                 （见 § 4.5、§ 6） │
> │    decode(source) → remap(source_cap, target_cap)            │
> │      → abstract_encode → syntax_adapt(target)               │
> │    supply.protocol 决定 target 协议（见 § 3.2 protocol 推断）  │
> ├─────────────────────────────────────────────────────────────┤
> │ ⑤ 出站转换 / 转发                                             │
> │    (source,target) 组合 → PASSTHROUGH 或 转换（core/translate）│
> │    出站 URL = supply.url + 净化 query（丢客户端 path、剔 beta） │
> │    出站头双发 Authorization: Bearer <appkey> + x-api-key      │
> │    失败(401/403/429/5xx)且 failover=on → 冷却+换同档下一 supply │
> └─────────────────────────────────────────────────────────────┘
>   ▼
> 真实上游（supply.url / supply.target_model）
> ```
>
> 关键点：入站阶段代理**不关心**客户端把 base_url 后面拼了什么 path（③ 用 body.model
> 选 tier，出站 ⑤ 只用配置的 `supply.url`），所以各 SDK 的 path 拼接差异不影响转发目标。

### 3. 三段关系图（§ 3.1 文字版素材）

> ### 3.1 三段关系总览
>
> 配置由三种可复用单元组成，按 id 层层引用：
>
> ```
> strategies（token→家族绑定）        routes（家族模板）          supplies（供给单元=上游端点）
> ┌──────────────────────┐          ┌──────────────────┐        ┌────────────────────────┐
> │ client_token: "cc"   │          │ id: "claude"     │        │ id: "claude-sonnet-k0" │
> │ route_id: "claude" ──┼──route_id─▶ tiers:            │        │ url / protocol         │
> │ tiers_source_        │          │   opus:  [id...] ─┼─supply─▶ appkey / target_model  │
> │   capability (可选)   │          │   sonnet:[id...] ─┼─id引用─▶ reasoning_capability   │
> └──────────────────────┘          │   haiku: [id...]  │        └────────────────────────┘
>       ▲ 一个 client_token         │ failover: on/off │              ▲ 一个 id=一个上游端点
>         一条 strategy             └──────────────────┘                多 route 可共享同一 supply
> ```
>
> - **supplies**：每条 = 一个上游端点（url + 协议 + appkey + target_model + 可选能力）。
> - **routes**：家族模板，固定 opus/sonnet/haiku 三档，每档一个按优先级排列的 supply id 列表，
>   取第一个未冷却的；route 本身不含 token，可被多条 strategy 复用。
> - **strategies**：把某个 client_token 绑定到某个 route 家族；运行时切家族用 `switch` 改 route_id。
>
> 请求匹配时反向走这条链：token → strategy → route → tier → supply（见 § 4.4）。

### 4.（本节留空——四·1/2/3 已覆盖全部新增文字，四·5 为 B3 素材）

### 5. B3 补充素材

**a. detect_installed 口径（§ 7 加一句）**
> install 的"已装/未装"判定口径是：SDK 的配置**目录**存在即视为已装（如 `~/.claude/` 存在即认为 claude 已装），不要求配置文件本身存在。已装则备份原文件后写入，未装则打印配置片段供手动粘贴。

**b. install base_url 期望形态表（§ 7 新增表）**
> | SDK | 写入的 base_url | 说明 |
> |---|---|---|
> | claude | `http://localhost:18889/` | 写入 `ANTHROPIC_BASE_URL` |
> | codex | `http://localhost:18889/v1` | 拼到 `/v1` 层级，`wire_api="responses"` 由 codex 自拼 `/responses` |
> | hermes | `http://localhost:18889/` | 打印片段供手动粘贴（standard lib 无 yaml writer） |
> | openclaw | `http://localhost:18889/` | 写入 `providers.<name>.baseUrl`；json5 专属语法则降级打印 |
>
> （端口随 `MODEL_PROXY_PORT` 变化，上表以默认 18889 为例。）

**c. codex 未核对官方文档提示（§ 7 + § 8 各一句）**
> § 7 codex 段：codex 的 base_url 拼到 `/v1` 层级、由 `wire_api="responses"` 让 codex 自拼 `/responses` 后缀——此拼法依据本项目 `detect_source` 对 `/v1/responses` 尾缀的识别反推，**未逐字核对 codex 官方 config.toml 文档字段名**。实际接入若报 404/400，请核对 codex 官方文档调整 base_url 层级。
> § 8 已知限制：codex install 写入的 base_url 层级未逐字核对 codex 官方文档，实际接入报 404/400 时需按官方文档调整（详见 § 7）。

**d. stats 受日志 trim 说明（§ 5.5 加一句）**
> `stats` 基于日志文件中现存的 ACCESS 行聚合；日志在进程启动时被截断到最后 5000 行，故 `stats` 只统计现存行、不含已被截断的历史请求。

---

## 五、A1 修正 + B3 放置汇总

| 项 | 内容 | 放在新结构哪节 |
|---|---|---|
| **A1** | 目录树补 `docs/designs/`（当期设计记录，区别于 `archive/` 历史归档） | 附录 B 目录结构 |
| B3-1 | detect_installed"目录存在即已装"口径 | § 7 接入各 SDK（四·5·a） |
| B3-2 | install base_url 期望形态表 | § 7 接入各 SDK（四·5·b） |
| B3-3 | codex base_url 未核对官方文档提示 | § 7 + § 8（四·5·c） |
| B3-4 | stats 受日志 trim 到 5000 行影响 | § 5.5 CLI 命令参考（四·5·d） |

**A1 目录树修正后的 docs/ 行**（附录 B 用）：
> ```
> ├── docs/                              # 文档
> │   ├── model_proxy_translate_spec.md  # 协议转换活规格
> │   ├── designs/               # 当期设计记录（如入站鉴权/access日志/SessionStart hook）
> │   └── archive/                       # 历史设计记录归档
> ```

---

## 六、交叉引用检查清单

现状 README 所有"见上/见下/见 docs/xxx"引用，逐条给出重构后应指向的位置。implementer 搬迁时逐条核对，避免断链。

| # | 现状位置 | 现状引用原文 | 重构后改为指向 |
|---|---|---|---|
| 1 | L44 | 热重载"（见「怎么启动」末尾）" | 该句并入 § 5.3，此内部引用**删除**（本身已在 § 5.3） |
| 2 | L70 | protocol"（详见下方「protocol 推断规则」）" | 改为"（详见 § 3.2 protocol 推断规则）" |
| 3 | L76 | reasoning"（见下方「reasoning_capability 字段语义」）" | 改为"（见 § 6.1）" |
| 4 | L106 | "档名见下表" | 表随 P1 一起进 § 6.1，就地引用，**不变**（改为"见本节下表"） |
| 5 | L144 | "供人工核实，见「怎么控制」" | 改为"见 § 5.5" |
| 6 | L203 | client_token"提取规则见下方「入站鉴权识别」" | 改为"提取规则见 § 4.2" |
| 7 | L209 | "解析逻辑复用同一套（见上「reasoning_capability 字段语义」）" | 该字段说明压成一句留 § 3.4，引用改为"见 § 6.1" |
| 8 | L217 | "codex-cli 也固定发 model=claude-sonnet（见 install 逻辑）" | 该段移入 § 6.2，引用改为"（见 § 7 install）" |
| 9 | L253 | "拼真实上游请求（见 `_sanitize_forward_query`）" | 保留代码引用**不变**（指代码函数，非文档节） |
| 10 | L274 | "完整样例见 `config/model_proxy_config.example.json`" | 随「顶层字段」下沉附录 A，引用**不变**（指外部文件） |
| 11 | L294-L295 | "见 docs/archive/reasoning_relative_remap_redesign.md" | 随 P3 进 § 6.3 末尾，引用**不变**（指外部文件） |
| 12 | L373 | "只能人工填，原因见「strategies」一节" | 该说明在 § 5.5，原因段已移 § 6.2，引用改为"原因见 § 6.2" |
| 13 | L429 | "设计细节与决策记录见 docs/archive/reasoning_relative_remap_redesign.md" | § 8 该条压成一句指向 § 6，archive 指针**删除**（§ 6.3 已含，避免重复） |

**新增引用（重构后新结构内部产生的、implementer 需一并写对的指针）：**

| 新位置 | 新增指针 | 指向 |
|---|---|---|
| § 3.4 strategies | tiers_source_capability 一句话后 | § 6.1 / § 6.2 |
| § 4.5 effort 映射导引 | 公式/边界详见 | § 6.3 |
| § 4.1 链路图各环节 | ①②③④标注 | § 4.2 / § 4.3 / § 4.4 / § 4.5 |
| § 4.4 三阶段 | 反向匹配链 | 呼应 § 3.1 |
| § 8 reasoning 条目 | 详见 | § 6 |
| § 8 codex 条目 | 详见 | § 7 |
| Quick Start ① | hook 自动挂载 | § 5.1 |

---

## 风险与权衡

- **章节编号硬引用**：新结构大量用"§ 4.2"式编号交叉引用。若后续增删章节导致编号漂移，所有引用需同步改，维护成本比现状"见「章节名」"式引用略高。权衡：编号引用对读者定位更快、对 implementer 一次性搬迁更明确；建议 implementer 落地时若嫌编号脆弱，可改用"见「入站鉴权识别」"式**章节名**引用（不带编号），本方案的指向映射同样适用（把"§ 4.2"读作"『入站鉴权识别』节"即可）。
- **两张 ASCII 图的渲染**：文字版流程图/关系图在等宽字体下对齐，Obsidian 阅读视图可能因比例字体错位。已用 ```代码块``` 包裹保证等宽。implementer 落地后需在 Obsidian 里目视确认对齐无错行。
- **附录下沉的取舍**：把目录结构、字段明细全下沉附录，是"认知路径优先"的选择；代价是习惯"打开 README 先看目录树"的老用户需多翻一下。这是 [理想] 路径的既定取舍，不因此调整。
- **本方案不含正文逐字全文**：§ 1/§ 3/§ 4.2-4.5/§ 5/§ 6/§ 7/§ 8 的正文以"搬迁现状原文 + 按映射表增删"方式产生，implementer 按搬迁映射表 + 收敛方案 + 交叉引用清单操作即可，无需再做内容组织判断；仅新增节（§ 2/§ 3.1/§ 4.1）和 B3 补充给了逐字素材。

**落地代价提示**（[理想] 路径知情项，不因代价改设计）：本重构等于重写 README 骨架（436 行全量重排 + 2 张新图 + 3 处 B3 补充 + 13 条交叉引用改写 + 3 张附录表抽取）。工作量约半天。重构后建议再派一次 reviewer 对照本方案 + 现状代码做一遍"信息未丢失、交叉引用无断链、字段无遗漏"的核对，因为大规模搬迁最易引入"某字段搬迁时漏抄"或"引用指向错节"的问题。

## 验证方式

implementer 落地后，按以下清单人工核对（或派 reviewer）：

1. **信息无丢失**：现状 436 行每个内容块都能在新结构找到落点（对照本文「二、搬迁映射表」逐行勾）。
2. **交叉引用无断链**：按「六、交叉引用检查清单」13 条 + 新增指针逐条核对，每个"见 § X"或"见「X」"都能在新结构找到对应节。
3. **reasoning 不重复**：全文搜 "STRIP" / "DISABLED" / "相对排名" / "off_alias"，确认深讲只出现在 § 6，其余处只有一句话指针。
4. **A1 已修**：附录 B 目录树含 `docs/designs/`。
5. **B3 四条已补**：§ 7 有 detect_installed 口径 + base_url 表 + codex 提示；§ 5.5 有 stats trim 说明；§ 8 有 codex 条目。
6. **图对齐**：Obsidian 阅读视图打开，§ 3.1 关系图、§ 4.1 链路图无错行。
7. **与代码一致性抽查**：Quick Start 命令实跑一遍（`on` / `install` / `logs` / `status`）确认输出与素材描述一致；base_url 表对照 `_install_ops.py` 实际写入值。

## 关联

- [[tools/model_proxy/README.md]]（重构目标文件）
- [[tools/model_proxy/docs/designs/2026-07-22-inbound-auth-header-asymmetry]]
- [[tools/model_proxy/docs/designs/2026-07-22-access-log-and-latency]]
- [[tools/model_proxy/docs/designs/2026-07-22-install-manage-sessionstart-hook]]
- `tools/model_proxy/docs/archive/reasoning_relative_remap_redesign.md`
