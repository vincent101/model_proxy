---
type: design-decision
status: confirmed
target: tools/model_proxy
tags: [architect, repo-split, 嵌套repo]
---

# model_proxy 拆独立 repo 执行手册

## 1. 背景

model_proxy 当前是 vault 外层 repo 的子目录（`tools/model_proxy/`），被 obsidian-git 自动备份卷入。需拆成独立 repo，与 [[project/quant_research]] 先例一致：代码工程独立版本控制，obsidian-git 只同步手写文档。

CLAUDE.md "嵌套 repo 治理"规则：新增顶层代码目录时，外层 `.gitignore` 追加 `/<路径>/`，该目录 `git init` 成独立 repo。

## 2. 决策

| 项 | 决策 | 理由 |
|---|---|---|
| 模式 | `history`（subtree split 迁历史） | 与 quant_research 先例一致，保留 149 commit 历史 |
| 远端 | `https://github.com/vincent101/model_proxy.git` | HTTPS，与 quant_research 一致 |
| 仓库 | `vincent101/model_proxy`（private） | gh CLI 创建，gh 已认证 |
| tag 时机 | defer 到版本管理落地后 | 先拆分，VERSION/CHANGELOG 拆分后再做 |

## 3. 前置条件

### 3.1 已满足

| 条件 | 状态 | 说明 |
|---|---|---|
| model_proxy 无 `.git` | ✓ | 未 init 过独立 repo |
| 历史存在 | ✓ | 149 commit（`git rev-list --count HEAD -- tools/model_proxy/`） |
| config/model_proxy_config.json 被 ignore | ✓ | 内层 `.gitignore` 已有规则 |
| SessionStart hook 文件在位 | ✓ | `tools/model_proxy/hooker/ensure_model_proxy.sh` 存在且可执行 |

### 3.2 需用户手动操作

| 条件 | 动作 | 说明 |
|---|---|---| 
| obsidian-git 已禁用 | 从 `.obsidian/community-plugins.json` 移除 `"obsidian-git"` | 脚本 step 1 自动检查。只改 data.json `autoCommitInterval=0` 无效（实测仍有 backup）。**用户手动执行，方案不替用户操作** |

### 3.3 vault 工作区需清理

脚本 step 1 要求 `git status --porcelain` 为空（含 untracked）。当前工作区状态需在执行前清理：

| 文件 | 处理 | 理由 |
|---|---|---|
| `.obsidian/plugins/claudian/data.json` | 若有改动 → discard（`git checkout -- <file>`） | Obsidian 瞬态状态，不 commit |
| `tools/model_proxy/.gitignore` | 若有未提交改动 → commit | 拆分前的改动应入外层历史 |
| `tools/model_proxy/docs/designs/2026-08-13-runtime-path-constants-unification.md` | 若有未提交改动 → commit | 同上 |
| 其他未提交改动（如 cangjie-skill/audio.wav 删除） | commit 或 restore | 工作区必须干净 |

> **执行前先跑 `git status --porcelain` 确认为空。** 以上列表基于文档撰写时的快照，实际执行时以当时的 `git status` 为准。

### 3.4 历史中的 appkey 泄漏（关键发现）

**文档化过程中发现**：commit `5107be17`（2026-08-10）曾将 `config/model_proxy_config.json.bk20260810.json` 纳入 tracking，内含真实敏感信息：

- `admin_token`: `7965ec96...`（64 字符 hex）
- 23 处 `appkey` 字段（如 `1907340802784210956`）

该文件在 commit `550850f2`（2026-08-13）被删除（`git rm`），但 **git 历史中的内容不会因删除而消失**。`history` 模式的 `git subtree split` 会携带所有 149 个 commit，包括含 appkey 的 commit。

**这意味着**：
- `git -C tools/model_proxy log --all -p | grep -i appkey | grep -v PLACEHOLDER` **会输出真实 appkey**（非"期望无输出"）
- 直接 `push` 到远端会将 appkey 永久写入远端历史

**处理方案**（push 前必须执行，详见阶段三 §5.3）：
1. 拆分完成后、push 前，用 `git filter-repo` 或 `git filter-branch` 清洗 `config/model_proxy_config.json.bk*.json` 的历史
2. 验证清洗后 `git log --all -p | grep -i appkey | grep -v PLACEHOLDER` 确实无输出
3. 确认后再 `git push`

### 3.5 .gitignore-rules 模式修正

architect-max 审核指定 `--gitignore-rules` 含 `config/model_proxy_config.json.bk_*`，但实际泄漏文件是 `.bk20260810.json`（无下划线）。修正为 `config/model_proxy_config.json.bk*`（匹配 `.bk<日期>.json` 和 `.bk_*.json`）。

完整 `--gitignore-rules` 参数：

```
".pytest_cache/,.DS_Store,config/model_proxy_config.json.bk*"
```

> **注意**：内层 `.gitignore` 已有 `config/model_proxy_config.json.bak.*`（含 'a'），与此处的 `.bk*`（不含 'a'）是不同 pattern，两者互补，不冲突。`--gitignore-rules` 是**追加**规则到内层 .gitignore，不覆盖已有规则。

## 4. 执行计划（三阶段）

### 阶段一：前置准备

```bash
# 1. 建远端空 repo（gh 已认证）
gh repo create vincent101/model_proxy --private

# 2. 确认远端为空
git ls-remote https://github.com/vincent101/model_proxy.git
# 期望：无输出（空 repo）

# 3. 清理 vault 工作区
git status --porcelain
# 若有输出，按 §3.3 处理，直到 git status --porcelain 为空

# 4. 用户手动禁用 obsidian-git
# 从 .obsidian/community-plugins.json 移除 "obsidian-git"
# （或退出 Obsidian）

# 5. bundle 备份（脚本自动执行，此处仅说明）
# 脚本 step 2 自动生成 .repo-split-backup-model_proxy-<timestamp>.bundle
```

### 阶段二：执行拆分

```bash
SCRIPT=tools/repo-split/scripts/repo_split.py

# dry-run 预览（不执行任何写操作）
python3 $SCRIPT \
  --target tools/model_proxy \
  --remote-url https://github.com/vincent101/model_proxy.git \
  --mode history \
  --gitignore-rules ".pytest_cache/,.DS_Store,config/model_proxy_config.json.bk*"

# 确认 dry-run 输出无误后，正式执行
python3 $SCRIPT \
  --target tools/model_proxy \
  --remote-url https://github.com/vincent101/model_proxy.git \
  --mode history \
  --gitignore-rules ".pytest_cache/,.DS_Store,config/model_proxy_config.json.bk*"
```

> **不要加 `--no-push`**：脚本需执行到 step 16（push）。但由于 appkey 清洗需求，实际执行时应在 step 15（`git remote add origin`）之后、step 16（`git push`）之前插入历史清洗步骤。**或者**：加 `--no-push` 先完成本地拆分，清洗历史后手动 push。推荐后者（见阶段三）。

**推荐执行方式**：加 `--no-push`，拆分后手动处理历史再 push：

```bash
python3 $SCRIPT \
  --target tools/model_proxy \
  --remote-url https://github.com/vincent101/model_proxy.git \
  --mode history \
  --gitignore-rules ".pytest_cache/,.DS_Store,config/model_proxy_config.json.bk*" \
  --no-push
```

### 阶段三：拆分后收尾

#### 5.1 验证 appkey 未入当前 tracked 文件

```bash
# 期望：只有 .example.json + runtime_paths.json
git -C tools/model_proxy ls-files | grep config
# 期望输出：
#   config/model_proxy_config.example.json
#   config/runtime_paths.json
```

#### 5.2 清洗历史中的 appkey（push 前必须完成）

```bash
cd tools/model_proxy

# 方法一：git filter-repo（推荐，需 pip install git-filter-repo）
git filter-repo --invert-paths \
  --path config/model_proxy_config.json.bk20260810.json \
  --force

# 方法二：git filter-branch（无需安装，但较慢）
git filter-branch --force --index-filter \
  'git rm --cached --ignore-unmatch config/model_proxy_config.json.bk20260810.json' \
  --prune-empty --tag-name-filter cat -- --all

# 验证清洗结果（期望无输出）
git log --all -p | grep -i appkey | grep -v PLACEHOLDER
git log --all -p | grep -i admin_token | grep -v 'get_admin_token\|cli.sh\|python3\|描述\|说明\|注释\|read\|注入\|配置'

cd ..
```

> **git filter-repo 会移除 `origin` remote**（若已设置）。拆分脚本 `--no-push` 模式下 step 15 已设置 origin，清洗后需重新设置：
> ```bash
> git -C tools/model_proxy remote add origin https://github.com/vincent101/model_proxy.git
> ```

#### 5.3 push 到远端

```bash
git -C tools/model_proxy push -u origin master --force
# --force 安全：远端是空 repo，不会覆盖任何已有内容
```

#### 5.4 验证服务可用

```bash
# 验证 SessionStart hook 路径仍有效
ls -la tools/model_proxy/hooker/ensure_model_proxy.sh
# 验证 proxy 服务可正常启动
bash tools/model_proxy/model_proxy_cli.sh status
```

#### 5.5 恢复 obsidian-git 时机

拆分完成且外层 `.gitignore` 已追加 `/tools/model_proxy/`（脚本 step 11 自动完成）后，obsidian-git 不会再碰 model_proxy 目录。此时可安全恢复 obsidian-git：将 `"obsidian-git"` 加回 `.obsidian/community-plugins.json`。

#### 5.6 可选清理

```bash
# server.py.bak 文件已进 repo（tracked），可选清理
git -C tools/model_proxy rm core/server.py.bak-before-nudge-rewrite-20260809
# 或保留，不影响功能
```

## 5. 16 步流程对照

history 模式 16 步，每步打印 `[step N/16]` + 校验，失败即停。

| 步 | 动作 | model_proxy 注意点 |
|---|---|---|
| 1 | 前置检查全通过 | obsidian-git 已禁用（用户手动）、工作区干净、无 `.git`、bundle 待做 |
| 2 | `git bundle create` | 自动生成 `.repo-split-backup-model_proxy-<timestamp>.bundle` |
| 3 | `git subtree split --prefix=tools/model_proxy -b model_proxy-split` | 产出 149 commit 的 split 分支 |
| 4 | 内层 `git init -b master` | `tools/model_proxy/.git` 创建 |
| 5 | 内层 `git remote add temp <vault>` | 临时关联 vault repo |
| 6 | 内层 `git fetch temp model_proxy-split` | 拉 split 分支 |
| 7 | 内层 `git reset --soft temp/model_proxy-split` | HEAD 指向 split 最新 |
| 8 | 内层 .gitignore 补规则 | 写入 `.pytest_cache/`、`.DS_Store`、`config/model_proxy_config.json.bk*` |
| 9 | 内层 `git add -A && git commit` | 产生迁移 commit |
| 10 | 内层 `git remote remove temp` | 清除临时关联 |
| 11 | 外层 `.gitignore` 追加 `/tools/model_proxy/` | 外层不再跟踪该目录 |
| 12 | 外层 `git rm -r --cached tools/model_proxy` | `git ls-files -- tools/model_proxy/` 归零 |
| 13 | 外层 `git commit -m "chore: remove ..."` | 外层移除记录 |
| 14 | 外层 `git branch -D model_proxy-split` | 清理 split 分支 |
| 15 | 内层 `git remote add origin <url>` | 关联远端（`--no-push` 模式仍执行此步） |
| 16 | 内层 `git push -u origin master --force` | **`--no-push` 模式跳过此步**，历史清洗后手动 push |

## 6. 风险与兜底

| 风险 | 说明 | 兜底 |
|---|---|---|
| appkey 泄漏到远端历史 | commit `5107be17` 含真实 appkey/admin_token，history 模式会携带 | push 前用 `git filter-repo` 清洗（§5.2），清洗后验证 |
| bundle 恢复 | 拆分过程中任何失败 | `.repo-split-backup-model_proxy-<timestamp>.bundle` 可恢复整个 vault repo |
| force push 到空 repo | `--force` 到空远端 | 安全，空 repo 无内容可覆盖 |
| pre-rename commit 丢失 | `tools/model_proxy/` 目录创建前，proxy 文件在 `tools/proxy_v2.py` 等路径（Jul 17 前），subtree split 不携带 | 可接受，与 quant_research 先例一致 |
| 内层 .gitignore 规则不全 | 旧 `.bak.*`（含 'a'）不匹配 `.bk20260810`（不含 'a'） | `--gitignore-rules` 追加 `config/model_proxy_config.json.bk*` 覆盖两种 pattern |
| `server.py.bak-before-nudge-rewrite-20260809` 入 repo | 已 tracked，拆分后带入 | 可选 `git rm` 清理，不影响功能 |
| obsidian-git 未禁用 | step 1 会阻断 | 用户手动禁用是前置条件，脚本不替用户操作 |

## 7. 拆分后状态

| 项 | 状态 | 说明 |
|---|---|---|
| 目录位置 | `tools/model_proxy/` 留原位 | 文件不移动，只是 git 归属变了 |
| 外层 `.gitignore` | 追加 `/tools/model_proxy/` | 脚本 step 11 自动完成 |
| 内层 repo | 独立 `.git`，remote origin → GitHub | 149 commit 历史（清洗后不含 appkey commit） |
| qw 形态 | B（独立 repo） | `cwd == repo_root` → 形态 B，worktree 池 `~/Documents/worktrees/` |
| SessionStart hook | 路径 `${CLAUDE_PROJECT_DIR}/tools/model_proxy/hooker/ensure_model_proxy.sh` 仍有效 | 文件留磁盘原位，hook 不受 git 归属变化影响 |
| obsidian-git | 可恢复 | 外层 `.gitignore` 已排除 model_proxy，不再卷入 |

## 8. 关联

- [[project/quant_research]] — 先例（独立 repo，远端 `vincent101/quant_research`，HTTPS）
- [[tools/repo-split/SKILL.md]] — 拆分脚本能力与流程
- [[CLAUDE.md#嵌套 repo 治理]] — 既有规则与 quant_research 先例
- [[docs/designs/2026-07-28-vault级多session并发worktree隔离机制]] — qw 形态 A/B 判断逻辑
