---
created: 2026-07-24
type: design-decision
date: 2026-07-24
status: confirmed
target: "[[tools/proxy.py]]"
tags: [architect, model_proxy, offline, archive, cleanup]
---

# v1 代理（proxy.py / 18888）下线归档方案

> 路径：[务实]。目标 = 干净、安全（不泄露密钥）、可回滚地把 v1 封存进
> `tools/model_proxy/history_versions/`，不做扩展性设计。

## 背景与问题

vault 内并行两代本地 AI 代理：v1（`tools/proxy.py`，端口 18888，纯 Anthropic appkey/profile 轮转）
与 v2（`tools/model_proxy/`，端口 18889，多协议）。v2 已上线并接管 `~/.claude/settings.json`
的 env。现需把 v1 完全下线：停进程、删自启 hook、把代码/脚本/日志归档封存，且**不能泄露密钥、
不能误伤 v2**。

## 关键事实（本次排查已确认，作为方案前提）

1. **`proxy_cli.sh proxy off` 不可用于停 v1**——它内部用 `pgrep -f "[p]roxy\.py"`，实测该正则
   会**同时匹配到 v2 的 `model_proxy.py`**（`model_proxy.py` 含子串 `proxy.py`）。已验证：
   `pgrep -f "[p]roxy\.py"` 同时命中 PID 5274（v1）与 45195（v2）。用它停 v1 会连带杀掉 v2。
   → 停 v1 必须用精确匹配 `tools/proxy.py`（实测只命中 v1）。
2. v1 日志有两份，互不相同：
   - `tools/.claude_proxy.log`（vault 内，`proxy.py` 用 `logging.FileHandler` 直接写，`_trim_log`
     保留末 1000 行）——归档对象。
   - `/tmp/claude_proxy.log`（vault 外，`nohup` 重定向的运行期临时日志）——非归档对象，直接删。
3. `~/.claude/proxy_config.json` 顶层键：`default_url / profiles / appkeys(7个) / admin_token /
   strategies`。**`appkeys`（7 个真实 key）与 `admin_token` 为敏感值**，绝不能明文进 vault。
4. v2 的 `_install_ops.py::_normalize_session_start` 只归一化 model_proxy 自己的 hook、保留其他
   条目，**没有删除 v1 hook 的能力**。删 v1 hook 只能手工编辑 JSON。
5. `.gitignore` 已有先例 `tools/model_proxy/config/model_proxy_config.json.bak.*`（第41行）——
   本方案沿用"归档产物加 gitignore 豁免"的做法。
6. v1 四个 vault 内文件均被 git 跟踪：`tools/proxy.py`(35306B)、`tools/proxy_cli.sh`(10499B)、
   `tools/ensure_proxy.sh`(1510B)、`tools/.claude_proxy.log`(54275B)。

## 方案设计

### 决策 A：密钥文件的归档策略（核心决策点）

`~/.claude/proxy_config.json` 与 `~/.claude/proxy_state.json` 都在 **vault 外**、本就不受 vault
git 跟踪。三个候选：

| 方案 | 做法 | 评价 |
|---|---|---|
| A1 明文进 zip | 把 proxy_config.json 原样打进 zip 放 vault | ✗ 密钥落进 vault，即便 gitignore 也有被同步/误提交风险，禁用 |
| A2 脱敏后进 zip | appkeys/admin_token 替换为 `<REDACTED>` 再打包 | 保留结构参考，但脱敏后的 config 无实际恢复价值，且要写脱敏脚本 |
| **A3（推荐）不归档密钥文件，原地留存** | zip **只含代码+脚本+日志**；`~/.claude/proxy_config.json`、`proxy_state.json` **原地不动**留在 `~/.claude/` | 最简、最安全、零泄露面。归档物是"legacy 代码存档"，密钥本就不该进代码存档；用户若要看历史配置结构，`proxy.py` 里的默认结构 + `proxy_cli.sh` 的读写逻辑已足够 |

**推荐 A3。理由**：本次是"legacy 代码封存"，不是"配置备份"。密钥类文件留在系统原位（`~/.claude/`）
既不阻碍 v1 下线，也不进 vault，泄露面为零。若日后彻底不用，用户可自行手动删 `~/.claude/proxy_config.json`
/ `proxy_state.json`（本方案不代删，属用户资产）。归档 README 里注明"密钥文件未归档，原位于
`~/.claude/`，如需彻底清理请自行删除"。

### 决策 B：进 zip 的文件清单

zip 内容（全部为 vault 内、非敏感）：

| 文件 | 处理 |
|---|---|
| `tools/proxy.py` | 原样归档 |
| `tools/proxy_cli.sh` | 原样归档 |
| `tools/ensure_proxy.sh` | 原样归档 |
| `tools/.claude_proxy.log` | 原样归档（末 1000 行日志，无密钥；proxy.py 不记密钥明文，归档前可 `grep -i "appkey\|token\|sk-"` 抽查一遍确认，若有命中再决定删日志） |
| 新增 `ARCHIVE_README.txt` | 归档说明：v1 下线日期、原路径、端口18888、为何归档、密钥文件未含且原位于 `~/.claude/`、如何解归档 |

不进 zip：`/tmp/claude_proxy.*`（临时态，直接删）、`~/.claude/proxy_config.json`、`~/.claude/proxy_state.json`（密钥/状态，原地留存）。

### 决策 C：压包格式与命名

- 格式：**`.tar.gz`**（保留 shell 脚本可执行位，比 zip 更贴合 unix 归档语义）。
- 命名：`tools/model_proxy/history_versions/proxy-v1-archived-20260724.tar.gz`
- 目录 `tools/model_proxy/history_versions/` 需新建。

### 决策 D：原文件处理与 gitignore

- 四个源文件从 git 与磁盘一并移除：`git rm tools/proxy.py tools/proxy_cli.sh tools/ensure_proxy.sh tools/.claude_proxy.log`
  （`git rm` 一步到位，同时删磁盘文件+移出索引；`.tar.gz` 已含副本，不怕丢）。
- gitignore：**归档 tar.gz 建议纳入 git 跟踪**（它是纯代码历史存档、无密钥，进版本库有追溯价值，
  体积约 <100KB）。因此**不加** gitignore 豁免。
  - 反方案：若用户不希望 legacy 存档进版本库，则在 `.gitignore` 加
    `tools/model_proxy/history_versions/*.tar.gz`，tar 包只留本地磁盘。**推荐前者（纳入跟踪）**，
    体积小且可追溯；此点请用户确认。

### 决策 E：删 hook

`.claude/settings.json` 的 `hooks.SessionStart` 当前 3 条，删第 0 条（`ensure_proxy.sh`），保留第
1（websearch）、第 2（model_proxy）。无可复用卸载工具，直接编辑 JSON。推荐用 python 原子改写而非
手工编辑（避免破坏 JSON 结构），执行时：

```bash
cd /Users/vincentwang/Documents/NoteVault
python3 - <<'PY'
import json
p = ".claude/settings.json"
d = json.load(open(p))
ss = d["hooks"]["SessionStart"]
before = len(ss)
d["hooks"]["SessionStart"] = [
    e for e in ss
    if "ensure_proxy.sh" not in json.dumps(e)
]
after = len(d["hooks"]["SessionStart"])
assert after == before - 1, f"预期删1条，实际 before={before} after={after}"
json.dump(d, open(p, "w"), indent=2, ensure_ascii=False)
open(p, "a").write("\n")  # 保留文件末尾换行（可选，视原文件风格）
print(f"SessionStart: {before} -> {after}，已删 ensure_proxy.sh 条目")
PY
```

（`assert` 兜底：只允许恰好删 1 条，误匹配/多删/漏删都会报错中止。）

### 执行顺序（含依赖与回滚）

严格顺序，前一步不成功不进下一步：

1. **抽查日志无密钥**（决策 B）：
   `grep -iE "appkey|admin_token|sk-[A-Za-z0-9]" tools/.claude_proxy.log` —— 有命中则先处理再归档。
2. **停 v1 进程**（精确匹配，不用 proxy_cli off）：
   ```bash
   PID=$(pgrep -f "tools/proxy\.py")   # 应只有一个，是 v1(当前5274，勿硬编码)
   echo "将停止: $PID"; ps -p "$PID" -o command=   # 人工确认是 tools/proxy.py 而非 model_proxy.py
   kill "$PID"; sleep 1
   lsof -i :18888 -sTCP:LISTEN -t || echo "18888 已释放"
   lsof -i :18889 -sTCP:LISTEN -t && echo "18889(v2) 仍在，未误伤"   # 校验 v2 存活
   ```
   —— 必须先停进程再删文件，避免文件被占用/进程重启回写日志。
3. **清理临时态**：`rm -f /tmp/claude_proxy.pid /tmp/claude_proxy.lock /tmp/claude_proxy.log`
4. **建归档目录 + 打包**（此时源文件仍在原位）：
   ```bash
   mkdir -p tools/model_proxy/history_versions
   # 生成 ARCHIVE_README.txt（内容见决策B）后：
   tar -czf tools/model_proxy/history_versions/proxy-v1-archived-20260724.tar.gz \
       -C tools proxy.py proxy_cli.sh ensure_proxy.sh .claude_proxy.log \
       -C tools/model_proxy/history_versions ARCHIVE_README.txt   # README 放包内或包外均可，二选一
   tar -tzf tools/model_proxy/history_versions/proxy-v1-archived-20260724.tar.gz  # 验证包内清单
   ```
5. **删 hook**（决策 E 脚本）—— 时机放在归档后、git rm 前均可；放这里是让"配置态"与"文件态"
   一起改，一次 review。
6. **git rm 源文件**（决策 D）：`git rm tools/proxy.py tools/proxy_cli.sh tools/ensure_proxy.sh tools/.claude_proxy.log`
7. **人工核对**（见验证方式），确认后统一 `git add`/`commit`（提交动作留给用户/主会话，本方案不代提交）。

**回滚**：任一步失败，因源文件在第 6 步前始终未删、tar 已生成，可直接 `git checkout .claude/settings.json`
恢复 hook、重新 `nohup python3 tools/proxy.py` 拉起 v1。第 6 步后如需回滚，`tar -xzf` 解包回 tools/
或 `git checkout` 恢复即可。

## 风险与权衡

1. **误伤 v2（最高优先级已规避）**：绝不用 `proxy_cli.sh proxy off`。停 v1 用精确
   `pgrep -f "tools/proxy\.py"`，且 kill 前 `ps` 人工确认、kill 后校验 18889 存活。
2. **密钥泄露**：A3 方案下密钥文件不进 vault，泄露面为零；仅需归档前抽查日志确认无密钥明文。
3. **PID 漂移**：当前 v1 PID 5274 是快照，执行时须重新 `pgrep` 取，勿硬编码。
4. **hook 误删**：脚本用 `assert after==before-1` 兜底，只允许精确删 1 条。
5. **归档 tar 是否进 git**：决策 D 留给用户拍板（推荐进）。

### 其他引用点建议清单（本次仅建议，不执行）

| # | 位置 | 建议 | 理由 |
|---|---|---|---|
| 1 | `~/.claude/settings.json` env | **不动** | 已正确指向 v2(18889)，与本任务无关 |
| 2 | `config_backup/lib/backup.sh:73` | **本次可一并改**：从备份列表移除 `.claude/proxy_config.json`，或保留并在行尾加注释 `# legacy v1(proxy.py 已下线归档 20260724)` | 实测该行备份的是 `~/.claude/proxy_config.json`（v1 的密钥）。v1 下线后该备份项失去意义。倾向"移除该项"，但因 config_backup 是独立工具、改动有正确性耦合（涉及 restore 侧），**建议单独一轮处理并派 reviewer**，不混进本次下线 |
| 2 | `config_backup/lib/restore.sh:509-514` | 同上，**建议与 backup.sh 同轮处理**：若 backup 不再备份 proxy_config.json，restore 的跨机跳过分支可一并删或保留为防御性代码 | 与 backup 侧成对，单独处理避免遗漏 |
| 2 | `config_backup/README.md:223` | 改「代理由 `tools/ensure_proxy.sh` 自动管理…CLI 用 `proxy_cli.sh`」为「代理由 `tools/model_proxy/hooker/ensure_model_proxy.sh` 管理（v1 已下线归档）」 | 文档同步，与上两条同轮 |
| 3 | `tools/model_proxy/README.md:12-13` | 改「与 proxy.py（18888）…完全独立并行…可同时保留」为「v1(proxy.py/18888) 已于 2026-07-24 下线，归档于 `history_versions/proxy-v1-archived-20260724.tar.gz`」 | 措辞已过时。**可本次一并改**（纯文档、无耦合） |
| 4 | `CLAUDE.md` 目录结构章 提到 `switch_model.sh`/`thinking_proxy.py` | 可顺手删这两行 | 实测两文件磁盘上已不存在，属更早期遗留文档漂移；非必答，低优先级 |
| 5 | `docs/superpowers/plans|specs/2026-07-02-proxy-routing-*.md` | **不动** | v1 当年设计文档，历史存档价值，删之无益 |
| 6 | `.obsidian/plugins/yolo/data.json` provider→18888 | **不动**（低优先级） | 插件当前未在 community-plugins.json 启用列表，非活跃依赖；用户日后想用再自行改 18889 |
| 7 | `AI_MEMORY/L1_情境层/2026-07.md` 提及并行 | **不动** | 记忆系统的历史事实记录，非运维配置，改动反而污染历史 |

**建议分批**：本次下线只做「停进程 + 删 hook + 归档 + git rm 源文件 + 改 model_proxy/README（#3）」；
config_backup 三处（#2）因有 backup/restore 正确性耦合，**单独一轮 + reviewer 复核**；#4 可顺手；
#1/#5/#6/#7 不动。

## 验证方式

执行后逐项核对：

```bash
cd /Users/vincentwang/Documents/NoteVault
# 1. v1 停、v2 活
lsof -i :18888 -sTCP:LISTEN -t || echo "OK: 18888 已停"
lsof -i :18889 -sTCP:LISTEN -t && echo "OK: 18889(v2) 存活"
pgrep -f "tools/proxy\.py" || echo "OK: 无 v1 进程"
pgrep -f "model_proxy\.py" && echo "OK: v2 进程在"
# 2. hook 只剩 2 条、无 ensure_proxy
python3 -c "import json;d=json.load(open('.claude/settings.json'));ss=d['hooks']['SessionStart'];print('条数:',len(ss));assert not any('ensure_proxy.sh' in json.dumps(e) for e in ss),'仍残留 ensure_proxy';print('OK: 无 v1 hook')"
# 3. 源文件已移除、归档存在且可解
ls tools/proxy.py tools/proxy_cli.sh tools/ensure_proxy.sh tools/.claude_proxy.log 2>&1 | grep -q "No such" && echo "OK: 源文件已移除"
tar -tzf tools/model_proxy/history_versions/proxy-v1-archived-20260724.tar.gz
# 4. 临时态已清
ls /tmp/claude_proxy.* 2>&1 | grep -q "No such" && echo "OK: /tmp 已清"
# 5. git 状态含 4 个 D（deleted）+ 新 tar
git status --short | grep -E "proxy\.py|proxy_cli|ensure_proxy|claude_proxy|history_versions"
```

**人工确认点**：kill 前用 `ps` 目视确认 PID 是 `tools/proxy.py`；决策 D（tar 是否进 git）需用户拍板。

## 关联

- [[tools/proxy.py]]（归档对象，v1 主程序）
- [[tools/model_proxy/README.md]]（#3 需同步措辞）
- [[tools/config_backup/lib/backup.sh]]（#2，建议单独一轮处理）
- [[tools/model_proxy/docs/solutionDesigns/2026-07-22-install-manage-sessionstart-hook.md]]（v2 hook 安装逻辑，说明为何无法复用其卸载 v1）
