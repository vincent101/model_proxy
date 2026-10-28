---
type: design-decision
status: draft
target: "[[tools/model_proxy/model_proxy_cli.sh]]"
tags: [architect, model_proxy, cli]
---

# model_proxy CLI restart 命令设计（务实档）

## 1. 背景与问题

model_proxy 本周多批 feature 改 `core/server.py`/`core/commands.py`，热 reload 只换配置不换代码——**代码更新必须重启进程**，而 CLI 只有 `on`/`off`，用户被迫手动 off+on 两步。补 `restart` 子命令，纯 CLI 层包装，不动 server.py。

## 2. 方案设计

### 2.1 restart 语义（定稿）

`restart = off（SIGTERM，幂等）→ 等「端口空且无 model_proxy.py 进程」→ on（含既有健康轮询）`；无条件先 off，未运行时 off 为空操作，自然退化为 `on`。

> **订正 v2（实施后，2026-10-28）**：初版按「端口是否监听」判运行态并只等端口释放，复核中发现漏洞——进程活着但没监听（启动中卡住）时仍持有 flock，直走 `on` 会 lock_conflict 退出。改为无条件先 off + 等待条件扩为端口空且进程退出（pgrep 同 `cmd_off` 的绝对路径匹配）。下文 2.1 代码与 2.3 推演已按 v2 重写。

改动只落在 `tools/model_proxy/model_proxy_cli.sh` 一个文件：

1. **新增 `cmd_restart()`**（放在 `cmd_off` 之后，复用 `cmd_on`/`cmd_off`，不复制启动逻辑）：

```bash
# ---- restart ----
# 语义：off（SIGTERM，幂等）→ 等"端口释放且进程退出"→ on。用途：代码更新（reload 只换配置不换代码）。
# 无条件先 off：进程可能活着但没监听（启动中卡住），此时只看端口会漏掉它，
# 它仍持有 flock，新进程会 lock_conflict 退出。
cmd_restart() {
  local target="$SCRIPT_DIR/model_proxy.py"
  cmd_off
  # SIGTERM 异步，进程退出才释放 flock；锁在绑定端口之前获取，
  # 只看端口会在"卡住未监听"时放行并撞锁。
  local waited=0 holder
  while lsof -i :"$MODEL_PROXY_PORT" -sTCP:LISTEN -t &>/dev/null \
        || pgrep -f "$target" &>/dev/null; do
    if (( waited >= 10 )); then
      holder=$( { lsof -i :"$MODEL_PROXY_PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2; \
                  pgrep -fl "$target" 2>/dev/null; } )
      echo "ERROR: 5s 内旧进程未退出/端口未释放，中止 restart（未启动新进程）。残留："
      echo "$holder"
      echo "可手动 kill -9 上述 pid 后执行 on。"
      return 1
    fi
    sleep 0.5; waited=$((waited+1))
  done
  if cmd_on; then
    return 0
  fi
  echo "ERROR: restart 中间态：旧进程已停止、新进程未启动，服务离线（启动日志见上）。"
  return 1
}
```

（cmd_on 失败分支已自带 tail -5 日志，中间态提示只补一句状态定性并指向"启动日志见上"，不重复日志。）

2. **分发处**：主 `case` 的 `off)` 之后加 `restart) cmd_restart ;;`。

3. **help 文本**（`print_help` 进程控制段）：

```
  on                                启动 model_proxy.py
  off                               停止 model_proxy.py
  restart                           停止后重启进程（代码更新后用；reload 只换配置不换代码）
                                    内存学习态清零：pref/thinking_strip 48h 记忆、
                                    cooldown、流健康计数；在途请求会被切断
  reload                            触发配置热重载（无条件清空所有 cooldown）
```

4. **README §5 更新**：命令表加一行 `restart`；"热重载"bullet 后补边界句：`reload=换配置不断流（cooldown 也清）；restart=换代码全量重启（上述内存态全清，在途请求切断，停机窗口约 1-2s）`。

### 2.2 restart vs reload 边界（写入文档的口径）

| | reload | restart |
|---|---|---|
| 换配置 | 是（mtime 检测本就自动，reload 是强制确认） | 是 |
| 换代码 | 否 | 是 |
| 断流 | 否 | 在途请求被 SIGTERM 切断 |
| cooldown | 清空 | 清空（随进程消亡） |
| pref_store/thinking_strip（48h 学习） | 保留 | 丢失→上游重吃一轮 400 后自愈 |
| StreamHealthStore（600s 观测） | 保留 | 丢失（仅影响观测连续性） |
| 停机窗口 | 0 | ~1-2s |

### 2.3 推演（命中：时序衔接/中间态/幂等）

- **正常序**：off → SIGTERM 即时退出（Python 默认处理器，serve_forever 的 finally 不执行，无 drain）→ OS 关 socket、flock 释放 → 端口轮询 1-2 个周期见空 → on → 新进程先抢 flock（旧已释放，无冲突）再绑端口 → ready。停机窗口 ≈ kill 即时 + 0.5-1s 轮询 + 冷启动，实测预期 1-2s〔推算〕。
- **幂等**：未运行 → `cmd_off` 报 not running 返回 0 → 等待条件立即为假 → cmd_on，等价 `on`。运行中 → off+等待+on。两路径同一条代码，无特例分支。
- **失败分支 c（活着但没监听，启动中卡住）**：v1 会误判为未运行、直走 on 撞 lock_conflict。v2 由 `cmd_off` 按绝对路径 pgrep 杀掉它，等待条件含进程存在性，确认退出后再 on。
- **失败分支 a（改坏代码）**：新进程 import 即崩 → nohup 退出 → cmd_on 轮询 5s 超时 → tail 日志 + 中间态定性"已停止未启动"。服务离线但**无陈旧锁/半启动副作用**（main() fail-fast 在锁和日志副作用之前退出，`server.py:3383-3392`）。
- **失败分支 b（端口被非 model_proxy 进程占）**：off 的 cmdline 校验不会误杀它 → 端口 5s 不空 → 报残留、中止、**不起新进程**（避免 on 的纯端口探测误报 "already running"）。此分支同时封掉了 on 的一个既有盲区。
- **边缘**：SIGTERM 后进程不退出（假进程忽略 SIGTERM 已实测）→ 5s 超时报告中止并列出残留 pid，提示手动 kill -9，不自动升级强杀（宁可见失败不静默强杀，符合通知宁滥勿缺）。

## 3. 风险与权衡

- **在途请求被切断**：沿用 off 的 SIGTERM 语义，不新增 drain/优雅停机（务实档不动 server.py；单用户本机代理，代价可忽略，写入文档明示）。
- **corrupt config 时启动"成功"但空配置**：ConfigStore 解析失败不崩溃（保留空态+warning），端口探活测不出。已知既有行为，restart 不额外处理；`status` 可事后人工核对 supplies 计数。
- **cmd_off 自身不验证退出**：restart 靠自己的「端口+进程」等待兜底，不反向改 off。
- 需用户确认：无实质分歧点；SIGTERM 不升级强杀是唯一取舍决策，已按"可见失败优于静默强杀"选 定。

## 4. 验证方式

1. 运行中 restart：`model_proxy_cli.sh restart` → 输出 Stopped → started (pid N)，`status` 显示新 pid 与新的 started 时间；`time` 实测停机窗口（预期 1-2s，替换本文推算值）。
2. 幂等：先 `off` 再 `restart` → 走 "not running, starting directly" 路径。
3. 失败分支：**禁止向 model_proxy.py/server.py 注入故障测试**（实施中曾因注入后会话中断致代理起不来）。改用无害假进程模拟：命令行含 `$PWD/model_proxy.py` 路径的 `python3 -c "time.sleep(600)" $PWD/model_proxy.py`——(a) 不监听端口 → restart 应杀掉它并启动成功；(b) 加 `signal.signal(SIGTERM, SIG_IGN)` → restart 应 5s 后中止、不起新进程、列出残留 pid（测后 `pkill -9 -f` 清理并 `on`）。cmd_on 启动失败的中间态分支未实测。
4. 学习态丢失实证：重启前后各发一个已知触发 thinking 剥离的请求，确认重启后首个请求吃 400 再自愈（对照 ThinkingStripStore 注释预期）。
5. `--help` 与 README 渲染核对，restart/reload 两行口径一致。

## 5. 交付物规范

无新代码产物目录/命名规则——改动仅限既有两文件（model_proxy_cli.sh、README.md），本设计记录本身落本目录（`tools/model_proxy/docs/designs/`，既有约定，与同目录 8 份设计记录同规则）。

## 关联

- [[2026-10-28-thinking空签名剥离与supply级自适应]]
- [[tools/model_proxy/README]]
