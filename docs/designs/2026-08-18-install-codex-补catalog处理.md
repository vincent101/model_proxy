---
type: design-decision
status: confirmed
target: tools/model_proxy
tags: [architect, model_proxy, install, codex, catalog]
---

# install_codex 补 model_catalog_json 处理

## 1. 背景与问题

model_proxy 的 `install_codex` 函数（`_install_ops.py` L457-528）当前只生成 `~/.codex/config.toml` 的 `[model_providers.model_proxy]` 段 + 顶层 `model`/`model_provider`。但 codex 0.145.0 接入第三方 provider 时，因内置 catalog 不认识 `claude-sonnet`（它是 model_proxy 的档位标识），会报 "Model metadata for claude-sonnet not found. Defaulting to fallback metadata" warning。消除需配 `model_catalog_json`（顶层 config.toml 一行 + 一份 catalog JSON 文件）。

catalog 已手动落地实测通过：
- `~/.codex/model-catalogs/model_proxy_catalog.json`（133KB，三档 ModelInfo：claude-opus/claude-sonnet/claude-haiku）
- `~/.codex/config.toml` 加 `model_catalog_json = "~/.codex/model-catalogs/model_proxy_catalog.json"`（根级）

目标：让新机器跑 `install codex` 后自动完成 catalog 配置，不报 warning。

## 2. 方案设计

### 2.1 catalog 来源/分发：模板内置 + install 时拉 prompt.md 全文拼装

**推荐方案：catalog 字段框架仓库内置（不含 prompt.md），install 时从网络拉取 codex 官方 prompt.md 全文，拼装成完整 catalog 写入目标。**

用户明确要求：prompt.md（codex 官方系统提示词，约 3389 词，是会随 codex 升级变化的内容）install 时从网络拉最新，**不内置静态快照**——避免内置副本过时。catalog 的其余部分（三档定值、字段结构、0.145.0 struct 字段）是稳定的，仓库内置成模板。

**分两块内容**：

| 内容 | 来源 | 性质 |
|---|---|---|
| catalog 字段框架（三档 ModelInfo 定值、字段名、context_window、reasoning 档位、apply_patch、input_modalities 等所有非 prompt.md 字段） | 仓库内置模板 `tools/model_proxy/assets/codex_catalog_template.json` | 稳定，跟 git 走，codex 升级时手动更新 |
| `base_instructions` = prompt.md 全文（codex 0.145.0 顶层必填字段，内容是 agentic coding 提示词） | install 时从 `https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/prompt.md` 拉取 | 动态，总是最新 |

**install 时拼装流程**：
1. 读仓库模板（`base_instructions` 字段留空占位符 `__PROMPT_MD__`）
2. `urllib.request.urlopen` 拉 prompt.md 全文
3. 把 `__PROMPT_MD__` 占位符替换为拉到的全文（Python `str.replace`）
4. `json.dumps` 序列化为完整 catalog（注意：模板里 `base_instructions` 是占位符字符串，替换后变全文；要保证替换后仍是合法 JSON 字符串——用 Python dict 操作最稳：读模板为 dict，dict["models"][i]["base_instructions"] = prompt_content，再 json.dump，Python 自动转义）
5. 写入 `~/.codex/model-catalogs/model_proxy_catalog.json`

**为什么 prompt.md 要拉网络**：它是 codex 官方会随版本迭代的内容（定义 agent 人格、AGENTS.md spec、Planning、工具规范等）。内置快照会过时——codex 升级后 prompt.md 可能改了，但内置的还是旧版。install 时拉最新保证 catalog 里的 base_instructions 总是和当前 codex 版本一致。

**网络失败降级**（用户已确认策略：跳过 catalog 不写 model_catalog_json 行）：

| 场景 | 处理 |
|---|---|
| 拉 prompt.md 成功 | 拼装完整 catalog → 写文件 + config.toml 加 model_catalog_json 行 |
| 拉 prompt.md 失败（断网/GitHub 不通/超时） | 打 warning（"拉取 prompt.md 失败，跳过 catalog 安装，codex 将保留 metadata warning 但能用 fallback 正常运行，网络恢复后重跑 install 补上"）→ **不写 catalog 文件、不在 config.toml 写 model_catalog_json 行** → install 不阻断，config.toml 的 provider/model/model_provider 照常写 |

**降级时不写 model_catalog_json 行的理由**：如果写了指向不存在的 catalog 文件，codex 启动会因找不到 catalog 报错（比 fallback warning 更糟）。不写行 → codex 走 fallback（"metadata not found" warning 但能正常跑）。用户网络恢复后重跑 install，catalog 文件和 config.toml 行一起补上。最干净。

### 2.2 改动点清单

#### 2.2.1 新增仓库模板文件

**文件**：`tools/model_proxy/assets/codex_catalog_template.json`（新建）

- 内容：完整三档 ModelInfo（claude-opus/sonnet/haiku），字段按 codex rust-v0.145.0 struct（含 `base_instructions` 顶层必填、`supports_parallel_tool_calls` 必填、ModelMessages 5 子字段）
- `base_instructions` 字段值留占位符 `"__PROMPT_MD__"`（install 时替换为拉到的 prompt.md 全文）
- 三档定值（context_window opus/sonnet=1000000、haiku=200000；apply_patch 三档 freeform；default_reasoning_level opus=high/sonnet=medium/haiku=medium；reasoning 档位 opus/sonnet 全5档、haiku 4档；input_modalities opus/sonnet=text+image、haiku=text）——照 [[2026-08-17-codex-catalog-claude档填法]] 定值
- 顶层加 `_codex_version: "0.145.0"` 和 `_source` 注释字段（serde 忽略未知字段，不影响 codex 消费）
- 约 5-10KB（不含 prompt.md 全文，远小于 133KB 完整版），git 跟踪

#### 2.2.2 `_install_ops.py` 改动

**a. 新增模块级常量**（L34 `_HOOK_SCRIPT_REL` 之后，`_CLAUDE_ONBOARDING` 之前）

```python
# codex model catalog 模板（仓库内置，base_instructions 占位符）与目标路径
_CODEX_CATALOG_TEMPLATE = _MODEL_PROXY_DIR / "assets" / "codex_catalog_template.json"
_CODEX_CATALOG_TARGET = Path.home() / ".codex" / "model-catalogs" / "model_proxy_catalog.json"
_CODEX_CATALOG_TOML_KEY = "model_catalog_json"
_CODEX_CATALOG_TOML_VALUE = "~/.codex/model-catalogs/model_proxy_catalog.json"
_PROMPT_MD_URL = "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/prompt.md"
_PROMPT_MD_TIMEOUT = 30  # 拉取超时秒数
```

**b. 新增函数 `_install_catalog_asset()`**（L456 `install_codex` 之前，紧跟 `_ensure_onboarding_completed` 之后）

逻辑（返回 bool 表示是否装成功，install_codex 据此决定是否写 config.toml 的 model_catalog_json 行）：

1. 读仓库模板 `_CODEX_CATALOG_TEMPLATE`（读为 dict：`json.loads(read_text())`）。模板不存在 → 打 warning，return False。
2. 拉 prompt.md：`urllib.request.urlopen(_PROMPT_MD_URL, timeout=_PROMPT_MD_TIMEOUT)`。失败 → 打降级 warning，return False（**不写文件、install_codex 不写 model_catalog_json 行**）。
3. 拼装：遍历 `template["models"]`，每条 `m["base_instructions"] = prompt_content`。
4. `target.parent.mkdir(parents=True, exist_ok=True)` 确保目录在。
5. 序列化：`new_bytes = json.dumps(template, ensure_ascii=False, indent=2).encode("utf-8")`（注意 indent=2 和实测文件一致；序列化在比较前做，下面按 bytes 比较）。
6. 目标文件不存在 → 打印信息（文件名+大小）+ `confirm()` → `write_bytes(new_bytes)` → return True。
7. 目标文件存在 + bytes 相同（`target.read_bytes() == new_bytes`）→ 打"已是最新"，跳过 → return True（已装，install_codex 可写 model_catalog_json 行）。
8. 目标文件存在 + bytes 不同 → 打印变更信息 + `confirm()` → 备份（`.bak.{ts}`）+ `write_bytes(new_bytes)` → return True。
9. 用户取消（confirm 返回 False）→ 不写入、不备份 → return False。

**不使用 `preview_confirm_write`**：catalog 是大 JSON（拼装后约 130KB+），`preview_confirm_write` 的 `unified_diff` 逐行 diff 无意义。用 `confirm()` + 备份逻辑替代，保留"确认后才写 + 备份原文件"契约。

**网络拉取用 `urllib.request`**（标准库，不引入第三方依赖，和 `_install_ops.py` 现有"纯标准库"原则一致——见文件 docstring L8 "纯标准库，不引入第三方依赖"）。

**c. 修改 `install_codex()` 函数**（L457-528）

**首次写入分支**（L486-497）：config.toml 写入成功后调 `_install_catalog_asset()`，**据返回值决定是否在 new_text 里加 model_catalog_json 行**。

因为 new_text 要在 preview_confirm_write 之前构造，而 catalog 装成功与否此时未知——所以首次分支调整顺序：先拼不含 model_catalog_json 行的 new_text → preview_confirm_write 写 config.toml → 若 wrote 则调 `_install_catalog_asset()` → 若 catalog 也装成功，再补写 model_catalog_json 行到 config.toml。

但"补写一行"会再触发一次 config.toml 写入，逻辑绕。**更简方案**：首次分支的 new_text 直接**不含** model_catalog_json 行；先写 config.toml（model/model_provider/provider_block）→ 调 `_install_catalog_asset()` → 若 catalog 装成功，用 `_set_top_key` 把 model_catalog_json 行加进刚写的 config.toml（此时 config.toml 已存在，read→set_top_key→write，走一次轻量写入，不再 preview_confirm_write 因为刚写过、用户已确认过 install 意图）。

```python
# 首次写入分支（改后）
new_text = (
    f'model = "{model_label}"\n'
    f'model_provider = "{provider_name}"\n'
    f'\n'
    f'{provider_block}'
)
wrote = preview_confirm_write(cfg_path, "", new_text, "codex", [
    f"已写入：model_provider={provider_name} base_url={base_url}",
    "已用 experimental_bearer_token 直填（dev-only），免 export；"
    f"若需改走环境变量，见 config 内 env_key 注释",
])
if wrote:
    if _install_catalog_asset():
        # catalog 装成功，补 model_catalog_json 行到已写的 config.toml
        cur = cfg_path.read_text(encoding="utf-8")
        cur = _set_top_key(cur, _CODEX_CATALOG_TOML_KEY, _CODEX_CATALOG_TOML_VALUE)
        cfg_path.write_text(cur, encoding="utf-8")
        print(f"  已在 config.toml 补写 {_CODEX_CATALOG_TOML_KEY} 行")
return
```

**文件存在分支**（L520-528）：先调 `_install_catalog_asset()` 装/catalog；再据其返回值决定 `_set_top_key` 是否加 model_catalog_json 行（装成功才加，装失败/已存在但 catalog 装失败则不加、且若 config.toml 已有该行要保留——其实装失败时保留已有行无害但指向旧文件，故装失败时若有该行也应提示用户旧文件可能过期，但不强行删行）。

```python
# 文件存在分支（改后，在现有 _set_top_key model/model_provider 之后）
text = _set_top_key(text, "model", model_label)
text = _set_top_key(text, "model_provider", provider_name)
# model_catalog_json 行的处理交给 _install_catalog_asset 返回值决定
catalog_ok = _install_catalog_asset()
if catalog_ok:
    text = _set_top_key(text, _CODEX_CATALOG_TOML_KEY, _CODEX_CATALOG_TOML_VALUE)
else:
    # catalog 装失败（网络问题）：若 config.toml 已有 model_catalog_json 行，
    # 保留不动（指向旧 catalog 文件，用户重跑 install 时更新）；若无则不加
    if re.search(rf'^{re.escape(_CODEX_CATALOG_TOML_KEY)}\s*=', text, re.MULTILINE):
        print(f"  catalog 装失败，保留现有 {_CODEX_CATALOG_TOML_KEY} 行（指向旧文件，网络恢复后重跑 install 更新）")
new_text = text
preview_confirm_write(cfg_path, old_text, new_text, "codex", [...])
```

**注意文件存在分支的执行顺序**：`_install_catalog_asset()` 在 `preview_confirm_write(config.toml)` **之前**调（因为 model_catalog_json 行是否加进 new_text，取决于 catalog 是否装成功，要先知道结果再构造 new_text 给 preview_confirm_write 预览）。

### 2.3 config.toml 加行处理

`model_catalog_json` 是顶层（根级）key，和 `model`/`model_provider` 同级，不在 `[model_providers.model_proxy]` 段内。

- **首次写入**：config.toml 先写不含该行的版本 → catalog 装成功后用 `_set_top_key` 补行（见 §2.2c 首次分支）
- **文件存在**：`_install_catalog_asset()` 返回 True 才用 `_set_top_key` 加/改该行；返回 False（网络失败）则不加、已有则保留（见 §2.2c 文件存在分支）
- **路径值**：写死 `"~/.codex/model-catalogs/model_proxy_catalog.json"`（带 `~`，codex 支持路径展开，和手动实测通过的值一致）

### 2.4 catalog 放置/覆盖策略

| 场景 | 处理 |
|---|---|
| 目标目录 `~/.codex/model-catalogs/` 不存在 | `mkdir(parents=True, exist_ok=True)` |
| 拉 prompt.md 失败 | 打降级 warning，不写文件，install_codex 不加 model_catalog_json 行，return False |
| 目标文件不存在 + prompt 拉取成功 | `confirm()` → `write_bytes(拼装后 catalog)` |
| 目标文件存在 + bytes 相同（拼装后） | 跳过（打"已是最新"），return True |
| 目标文件存在 + bytes 不同 + confirm | 备份 `.bak.{ts}` → `write_bytes` |
| 仓库模板不存在 | 打 warning，return False（不阻断 install） |
| 用户取消 | 不写入、不备份，return False |

### 2.5 版本绑定与更新

- catalog 资产文件顶层加 `_codex_version: "0.145.0"` 注释字段（serde 忽略未知字段，不影响 codex 消费）
- **install 时不检测 codex 版本**（过度设计，[务实] 路径不值得）：始终覆盖/安装仓库内置 catalog，用户自负
- codex 升级后：手动更新 `assets/codex_catalog.json`（按新 tag 的 struct 字段重新生成），提交 git，下次 install 自动用新版

## 3. 风险与权衡

### 3.1 catalog 资产与 codex 版本绑定

catalog 字段绑定 codex 0.145.0 struct（有 `base_instructions` 顶层、`supports_parallel_tool_calls` 等）。codex 升级后 struct 可能变，catalog 需手动更新。

- **不自动检测版本**：install 时不跑 `codex --version` 做兼容判断。[务实] 路径下，版本检测是过度设计——catalog 资产更新频率低（跟着 codex 大版本走），且 serde 无 deny_unknown_fields（未知字段静默忽略，向前兼容好）。
- **降级风险**：codex 升级后旧 catalog 可能缺少新字段 → codex 用默认值 → 不报错但可能不最优。可接受（用户手动更新 catalog 即可）。

### 3.2 install 引入网络依赖（prompt.md 拉取）

原 install_codex 不依赖网络（纯本地）。本方案因 prompt.md install 时拉网络，引入了网络依赖。降级策略（§2.1）保证网络失败不阻断 install——config.toml 的 provider/model/model_provider 照常写，只是 catalog 跳过、codex 保留 fallback warning。用户网络恢复后重跑 install 补 catalog。

- **可接受**：install 是一次性配置操作，不是高频调用；网络失败有明确降级路径和提示，用户知道该怎么做。
- **不内置 prompt.md 快照**：用户明确要求拉网络，避免内置副本过时。

### 3.3 catalog 内容 diff 不预览

catalog 拼装后约 130KB+ JSON，不走 `preview_confirm_write` 的逐行 diff。用 `confirm()` + 备份替代。

- **可接受**：catalog 内容由模板 + 网络 prompt.md 决定，不是用户手编配置，逐行 diff 无意义。覆盖前自动备份 `.bak.{ts}`，可回滚。

### 3.4 catalog 装失败时 config.toml 的处理

文件存在分支：`_install_catalog_asset()` 返回 False（网络失败）时，config.toml 不加 model_catalog_json 行；若已有该行则保留不动（指向旧 catalog 文件，用户重跑 install 时更新）。返回 True 时才 `_set_top_key` 加/改该行。

- **首次分支**：config.toml 先写不含 model_catalog_json 行 → catalog 装成功才补行（见 §2.2c）。装失败则 config.toml 不含该行，codex 走 fallback。
- **关键**：避免 config.toml 残留指向不存在 catalog 文件的引用（否则 codex 启动报错比 fallback warning 更糟）。

### 3.5 需用户确认的事项

无。所有设计决策已在方案内明确。

## 4. 验证方式

### 4.1 单元测试

`tests/test_install_ops.py` 新增 `TestInstallCatalogAsset` 类 + 修改 `TestInstallCodexFileNotExists`。

**TestInstallCatalogAsset 用例**（全部用 tempfile，绝不碰真实 `~/.codex/`；mock `urllib.request.urlopen` 控制网络结果）：

| 用例 | 场景 | 预期 |
|---|---|---|
| `test_template_not_exists_warns_and_skips` | 仓库模板不存在 + patch `_CODEX_CATALOG_TEMPLATE` 指向不存在路径 | 打 warning，return False，不创建目标文件 |
| `test_prompt_md_fetch_fails_degrades` | 模板存在 + mock urlopen 抛 URLError | 打降级 warning，return False，不写目标文件 |
| `test_target_not_exists_writes` | 模板存在 + prompt 拉取成功 + 目标不存在 + confirm=True | mkdir + 写入，内容含 prompt.md 全文（base_instructions 被替换），无 .bak，return True |
| `test_target_not_exists_confirm_no_creates_nothing` | 目标不存在 + confirm=False | 不创建文件，无 .bak，return False |
| `test_target_same_content_skips` | 目标存在 + bytes 相同（拼装后） | 不调 confirm，文件不变，无 .bak，return True |
| `test_target_different_content_backs_up_and_overwrites` | 目标存在 + bytes 不同 + confirm=True | 备份 .bak + 覆盖，return True |
| `test_target_different_content_confirm_no_unchanged` | 目标存在 + bytes 不同 + confirm=False | 文件不变，无 .bak，return False |
| `test_default_arg_reads_module_constant` | 不传参，patch 模块级常量 → 走默认参数分支 | 验证默认路径生效 |

**TestInstallCodexFileNotExists 修改**：

| 用例 | 修改点 |
|---|---|
| `test_missing_file_writes_via_preview_confirm_write` | mock `_install_catalog_asset` 返回 True，追加断言：config.toml 含 `model_catalog_json` 行 |
| `test_missing_file_confirm_no_creates_nothing` | 追加断言：catalog 文件未创建（patch `_install_catalog_asset` 验证未调用） |
| `test_missing_file_catalog_fetch_fails_no_catalog_key` | mock `_install_catalog_asset` 返回 False（网络失败）| config.toml 写入成功但**不含** `model_catalog_json` 行 |

**TestInstallCodexExistingFile 新增**（当前不存在此类，需新建）：

| 用例 | 场景 | 预期 |
|---|---|---|
| `test_existing_file_adds_catalog_key` | config.toml 已存在 + 无 model_catalog_json 行 + mock catalog 返回 True + confirm=True | 写入后含 `model_catalog_json` 行 |
| `test_existing_file_replaces_catalog_key` | config.toml 已存在 + 已有 model_catalog_json 行（旧值）+ mock catalog 返回 True + confirm=True | 写入后 model_catalog_json 值=新路径 |
| `test_existing_file_catalog_fails_keeps_old_key` | config.toml 已存在 + 已有 model_catalog_json 行 + mock catalog 返回 False + confirm=True | 该行保留不动（指向旧文件），打提示 |
| `test_existing_file_catalog_fails_no_key_not_added` | config.toml 已存在 + 无 model_catalog_json 行 + mock catalog 返回 False + confirm=True | 不加 model_catalog_json 行 |
| `test_existing_file_confirm_no_no_catalog_install` | config.toml 已存在 + confirm=False | catalog 未安装（patch `_install_catalog_asset` 验证未调用） |

### 4.2 手工验证

1. 新机器跑 `./model_proxy_cli.sh install` → 选 codex → 确认
2. 检查 `~/.codex/config.toml` 含 `model_catalog_json` 行
3. 检查 `~/.codex/model-catalogs/model_proxy_catalog.json` 存在，且内容含 prompt.md 全文（base_instructions 字段是完整 prompt，非占位符）
4. 启动 codex（`model="claude-sonnet"`），确认无 "Model metadata not found" warning
5. 重跑 install → 选 codex → 确认 → catalog "已是最新"（跳过，因 prompt.md 未变 bytes 相同）
6. 模拟 catalog 更新（改仓库模板一个字段，或 prompt.md 上游变更）→ 重跑 install → 确认 → 备份 + 覆盖
7. 模拟网络失败（断网或改 _PROMPT_MD_URL 指向不通地址）→ 重跑 install → 打降级 warning、不写 catalog 文件、config.toml 不加 model_catalog_json 行（首次）或保留旧行（已存在）

### 4.3 运行命令

```bash
cd tools/model_proxy && python3 -m unittest tests.test_install_ops -v
```

## 5. 落地步骤

1. 建目录 `tools/model_proxy/assets/`
2. 生成仓库模板 `tools/model_proxy/assets/codex_catalog_template.json`：从已实测通过的 `~/.codex/model-catalogs/model_proxy_catalog.json` 取结构，但把三档的 `base_instructions` 字段值改为占位符 `"__PROMPT_MD__"`；顶层加 `_codex_version: "0.145.0"` 和 `_source` 注释字段。约 5-10KB（不含 prompt.md 全文）。
3. 改 `_install_ops.py`：
   - L34 后加模块级常量（`_CODEX_CATALOG_TEMPLATE`/`_CODEX_CATALOG_TARGET`/`_CODEX_CATALOG_TOML_KEY`/`_CODEX_CATALOG_TOML_VALUE`/`_PROMPT_MD_URL`/`_PROMPT_MD_TIMEOUT`）
   - L456 前加 `_install_catalog_asset()` 函数（读模板→拉 prompt.md→拼装→confirm→备份→写入，返回 bool；网络失败 return False）
   - 首次写分支：new_text 不含 model_catalog_json 行；`preview_confirm_write` 写 config.toml → `if wrote and _install_catalog_asset()` 返回 True 则 `_set_top_key` 补 model_catalog_json 行
   - 文件存在分支：先调 `_install_catalog_asset()` → 返回 True 才 `_set_top_key` 加/改 model_catalog_json 行；返回 False 则不加、已有则保留
4. 改 `tests/test_install_ops.py`：新增 `TestInstallCatalogAsset`（含网络失败降级用例，mock urlopen）、`TestInstallCodexExistingFile`，修改 `TestInstallCodexFileNotExists`
5. 跑测试：`cd tools/model_proxy && python3 -m unittest tests.test_install_ops -v`
6. 手工验证：新机器跑 install codex → 检查 config.toml 含 model_catalog_json 行 + catalog 文件含 prompt.md 全文 → codex 启动无 warning；再测网络失败降级

## 6. 关联

- catalog 三档定值依据：[[2026-08-17-codex-catalog-claude档填法]]
- catalog 通用分析：[[2026-08-17-codex-model-catalog通用填法分析]]
- install_claude onboarding 先例：`_install_ops.py` L373-422 `_ensure_onboarding_completed()`
- model_proxy 档位映射：`core/server.py` `_MODEL_TIER_MAP`
- codex ModelInfo 源码：`codex-rs/protocol/src/openai_models.rs`（struct 定义）
