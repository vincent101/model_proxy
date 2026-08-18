---
type: design-decision
status: confirmed
target: tools/model_proxy/_install_ops.py
tags: [architect, model_proxy, install, onboarding]
---

# install_claude 补 hasCompletedOnboarding 跳过官方登录引导

## 背景与问题

model_proxy 的 `install_claude` 函数（`_install_ops.py` L370-395）当前只读写 `~/.claude/settings.json` 的 env 段（ANTHROPIC_BASE_URL/AUTH_TOKEN/三个 DEFAULT_MODEL），完全不碰 `~/.claude.json`。

Claude Code 启动时认两个文件：`~/.claude/settings.json`（env 配置）和 `~/.claude.json`（onboarding 状态 + 登录态 + 运行时状态）。新机器上 `~/.claude.json` 不存在 → `hasCompletedOnboarding` 未设 → 首次启动进 onboarding 引导，触发官方 Anthropic 登录/订阅检查。即便 settings.json 已指向 model_proxy，onboarding 这关仍拦。

证据：阿里云 Model Studio 官方文档明确要求 `~/.claude.json` 设 `{"hasCompletedOnboarding": true}` 跳过官方登录验证。Anthropic 官方 code.claude.com 文档确认未配齐网关凭证时进登录屏。

## 方案设计

### 改动点清单

#### 1. 新增模块级路径常量

**文件**：`_install_ops.py`
**位置**：L34 后（`_CLAUDE_SETTINGS` 定义之后，`SDKS` 字典之前）

```python
_CLAUDE_ONBOARDING = Path.home() / ".claude.json"
```

不加进 `SDKS["claude"]` 的 `config_path`——那是 `~/.claude/settings.json`，和 `~/.claude.json` 是不同文件，不能混用。单独常量，与 `_CLAUDE_SETTINGS` 同级。

#### 2. 新增函数 `_ensure_onboarding_completed()`

**文件**：`_install_ops.py`
**位置**：`install_claude` 函数之前（L366 附近，`base_url_for` 之前或之后）

```python
def _ensure_onboarding_completed(onboarding_path: "Path | None" = None) -> None:
    """确保 ~/.claude.json 含 hasCompletedOnboarding=true，跳过 Claude Code
    官方 onboarding/登录引导。新机器首次 install 后直接可用。

    分支（详见设计文档 ~/.claude.json 各分支处理表）：
    - 文件不存在 → 写入最小文件 {"hasCompletedOnboarding": true}
    - 文件存在 + JSON 可解析 + hasCompletedOnboarding 已为 true → 跳过
    - 文件存在 + JSON 可解析 + hasCompletedOnboarding 为 false 或缺失 → merge 写入 true
    - 文件存在 + JSON 解析失败 → 降级打印手动片段

    onboarding_path 默认 None 时运行时读取模块级 _CLAUDE_ONBOARDING（同
    ensure_session_hook 的默认参数设计，支持 patch 模块常量做测试）。
    """
    if onboarding_path is None:
        onboarding_path = _CLAUDE_ONBOARDING

    if not onboarding_path.exists():
        new_text = json.dumps(
            {"hasCompletedOnboarding": True}, indent=2, ensure_ascii=False
        ) + "\n"
        preview_confirm_write(onboarding_path, "", new_text, "claude(onboarding)", [
            "已写入 ~/.claude.json: hasCompletedOnboarding=true",
            "Claude Code 首次启动将跳过官方 onboarding/登录引导。",
        ])
        return

    old_text = onboarding_path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(old_text)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  [claude(onboarding)] {onboarding_path} 解析失败: {e}")
        print(f"  请手动在 {onboarding_path} 中添加: "
              f'"hasCompletedOnboarding": true')
        return

    if cfg.get("hasCompletedOnboarding") is True:
        print(f"  [claude(onboarding)] hasCompletedOnboarding 已为 true，跳过。")
        return

    if cfg.get("hasCompletedOnboarding") is False:
        print(f"  [claude(onboarding)] 检测到 hasCompletedOnboarding=false，"
              f"将覆盖为 true（install 目的即接入 model_proxy，"
              f"保留 false 会被 onboarding 拦截）。")

    cfg["hasCompletedOnboarding"] = True
    new_text = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    preview_confirm_write(onboarding_path, old_text, new_text, "claude(onboarding)", [
        "已写入 ~/.claude.json: hasCompletedOnboarding=true",
        "Claude Code 首次启动将跳过官方 onboarding/登录引导。",
    ])
```

**序列化选择**：用 `json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"`，**不用** `compact_config_json`。原因：`compact_config_json` 的正则模式是为 model_proxy config 的特定结构（effort_enum、supply 对象、route_pool）设计的。`~/.claude.json` 是 Claude Code 的运行时状态文件，不应耦合 model_proxy 专属格式化逻辑。已验证现有 `~/.claude.json` 使用 indent=2，re-serialize 仅产生尾部换行 + 实际数据变更的极小 diff。

#### 3. 修改 `install_claude` 函数

**文件**：`_install_ops.py` L370-395

**改动 A**：手动片段分支（L374-382，settings.json 不存在时）——在 snippet 末尾追加 onboarding 提示

```python
    if not cfg_path.exists():
        print_manual_snippet("claude", f"""\
在 {cfg_path} 的 "env" 字段下添加/修改：
  "ANTHROPIC_BASE_URL": "{base_url}",
  "ANTHROPIC_AUTH_TOKEN": "{token}",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet",
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus"

同时在 ~/.claude.json 中添加（跳过官方 onboarding/登录引导）：
  {{"hasCompletedOnboarding": true}}
""")
        return
```

**改动 B**：settings.json 存在分支——在 `preview_confirm_write` 调用后（L395 之后）追加调用 `_ensure_onboarding_completed()`

```python
    preview_confirm_write(cfg_path, old_text, new_text, "claude", [
        f"已写入：ANTHROPIC_BASE_URL={base_url} ANTHROPIC_AUTH_TOKEN={token}",
        "请重启 Claude Code 生效。",
    ])
    _ensure_onboarding_completed()
```

**调用时机说明**：在 settings.json 处理之后无条件调用。如果用户取消了 settings.json 的写入，`_ensure_onboarding_completed` 自身也有 `preview_confirm_write` 的 confirm 步骤，用户可独立取消。无需在 install_claude 里判断 settings.json 写入是否成功——onboarding 函数是自包含的交互式流程。

### ~/.claude.json 各分支处理表

| # | ~/.claude.json 状态 | hasCompletedOnboarding 值 | 处理 | 备份 | 用户确认 |
|---|---|---|---|---|---|
| 1 | 不存在 | — | 写入最小文件 `{"hasCompletedOnboarding": true}` | 无（原文件不存在，preview_confirm_write L188-191 自动跳过备份） | 需要（preview_confirm_write confirm） |
| 2 | 存在 + JSON 可解析 | 已为 `true` | 打印"已为 true，跳过"，early return | 无 | 不需要（无变更） |
| 3 | 存在 + JSON 可解析 | `false` | 打印 warning → set `true` → merge 写回 | 有（原文件存在） | 需要 |
| 4 | 存在 + JSON 可解析 | 键缺失（`None`） | set `true` → merge 写回 | 有 | 需要 |
| 5 | 存在 + JSON 解析失败 | — | 降级：打印手动片段提示，不强行写入 | 无 | 不需要（只打印） |

**关于 false→true 覆盖的判断**：`install_claude` 的目的就是将 Claude Code 接入 model_proxy。`hasCompletedOnboarding=false` 会导致首次启动被 onboarding 拦截，与 install 目的直接矛盾。因此覆盖为 true 是合理的。额外打印 warning 让用户知情。

### 不改动的部分

- `SDKS` 字典（L40-61）：不加 `~/.claude.json` 到 `config_path`，它们是不同文件
- `detect_installed`（L66-69）：检测逻辑不变（基于 `~/.claude/` 目录是否存在）
- `preview_confirm_write`（L160-216）：函数本体不改，复用现有的"原文件不存在跳过备份"和"无 diff 自动跳过"分支
- `compact_config_json`（`_config_ops.py`）：不碰，onboarding 序列化不用它
- `install_codex`/`install_hermes`/`install_openclaw`：不碰
- `server.py`：不碰
- `cmd_install`（L598-614）：不改，onboarding 由 install_claude 内部调起

## 风险与权衡

### 1. re-serialize 全量重写的 diff 可能较大

`~/.claude.json` 在重度使用的机器上可能有 10KB+（含 skillUsage、projects 等）。虽然已验证 indent=2 re-serialize 仅产生尾部换行差异，但如果原始文件使用了非标准格式（如 indent=4 或无缩进），diff 会包含全量格式变更。

**权衡**：install 流程是交互式的，用户可以审查 diff 并决定是否接受。如果 diff 过大，用户可以取消并手动添加。这是 [务实] 方案，不引入 surgical JSON 编辑器。

### 2. 用户取消 settings.json 写入后仍触发 onboarding prompt

如果用户取消了 settings.json 的写入，`_ensure_onboarding_completed` 仍会执行，展示自己的 diff 和 confirm。用户需再次取消。

**权衡**：多一次 confirm 不影响正确性。边缘场景（取消 settings.json 但批准 onboarding）会导致 Claude Code 跳过 onboarding 但无 proxy 配置——但用户可以重新运行 install。保持调用逻辑简单比处理这种极端边缘场景更重要。

### 3. hasCompletedOnboarding=false 覆盖策略

用户可能有意设为 false（如想重新走 onboarding）。方案选择覆盖为 true。

**权衡**：install 的目的就是接入 model_proxy，false 会阻止这一目的。warning 打印让用户知情。如果用户确实想保留 false，可以在 onboarding 的 confirm 步骤取消。

### 4. 新机器 ~/.claude.json 不存在时是否该建文件

方案选择直接建最小文件 `{"hasCompletedOnboarding": true}`。

**权衡**：Claude Code 首次启动时如果发现 `~/.claude.json` 已存在且 `hasCompletedOnboarding=true`，会跳过 onboarding 直接进入主界面。此时 settings.json 已指向 model_proxy（install 刚写好），用户可以直接使用。如果不建文件，Claude Code 会自己建但会进 onboarding 流程。

## 验证方式

### 测试清单

**文件**：`tests/test_install_ops.py`

新增 `TestEnsureOnboardingCompleted` 测试类，覆盖以下用例：

| 测试方法 | 场景 | 断言 |
|---|---|---|
| `test_file_not_exists_creates_minimal` | ~/.claude.json 不存在 | confirm=True → 文件被创建，内容为 `{"hasCompletedOnboarding": true}`，无 .bak |
| `test_file_not_exists_confirm_no_creates_nothing` | 不存在 + 用户取消 | 文件不存在，无 .bak |
| `test_already_true_skips` | 文件存在，hasCompletedOnboarding=true | confirm 不被调用，文件不变，无 .bak |
| `test_missing_key_adds_and_preserves_others` | 文件存在，无 hasCompletedOnboarding 键，有其他键（mcpServers, projects 等） | confirm=True → hasCompletedOnboarding 被设为 true，其他键逐字节/结构保留，有 .bak |
| `test_false_overwritten_to_true` | 文件存在，hasCompletedOnboarding=false | confirm=True → 被覆盖为 true，有 .bak |
| `test_false_confirm_no_unchanged` | false + 用户取消 | 文件不变，无 .bak |
| `test_invalid_json_degrades_no_write` | 文件存在但非合法 JSON | 不调用 confirm，不写入，打印手动片段提示 |
| `test_default_arg_reads_module_constant` | 不传 onboarding_path，patch 模块级 `_CLAUDE_ONBOARDING` | 走默认参数分支读到 patch 后的临时路径（同 `test_default_arg_path_reads_module_constant_at_call_time` 模式） |

修改 `TestInstallClaudeExistingFileFlow`：

| 测试方法 | 新增断言 |
|---|---|
| `test_confirm_yes_writes_expected_fields` | 验证 `_ensure_onboarding_completed` 被调用（可 patch 检查 call，或验证 onboarding 文件也被处理） |
| `test_confirm_no_leaves_original_untouched` | 验证 onboarding prompt 仍出现（install_claude 无条件调用），但 onboarding 也可独立取消 |

修改 `TestInstallClaudeFileNotExists`：

| 测试方法 | 新增断言 |
|---|---|
| `test_missing_file_prints_snippet_and_creates_nothing` | 验证 snippet 内容包含 `hasCompletedOnboarding` 提示 |

### 手动验证步骤

```bash
# 1. 在沙箱/新机器上运行 install claude
cd tools/model_proxy
python3 _install_ops.py install config/model_proxy.yaml <port> claude

# 2. 确认 settings.json 写入后出现 onboarding 预览
# 3. 确认 onboarding 预览正确显示 diff
# 4. 确认 ~./claude.json 含 hasCompletedOnboarding=true
# 5. 启动 Claude Code，确认不进 onboarding 引导

# 回归：确认已有机器（hasCompletedOnboarding 已 true）运行 install 时打印"已为 true，跳过"
```

### 运行测试

```bash
cd /Users/vincentwang/Documents/NoteVault/tools/model_proxy
python3 -m pytest tests/test_install_ops.py -v
```

## 关联

- [[2026-07-22-install-manage-sessionstart-hook]] — ensure_session_hook 的设计先例（merge 模式、默认参数设计、preview_confirm_write 复用）
- [[2026-08-13-runtime-path-constants-unification]] — 路径常量统一方案
- `_install_ops.py` L300-359 `ensure_session_hook` — merge 写回模式参考
- `_install_ops.py` L160-216 `preview_confirm_write` — 复用的备份+预览+确认写入工具
- `_install_ops.py` L497-542 `install_openclaw` — JSON 解析失败降级模式参考
