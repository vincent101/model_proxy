# vendored mtsso_keeper 认证脚本

本目录是 `mtsso_keeper` **认证脚本的只读快照**，供只有 model_proxy、没有 keeper 源码的人
直接使用。它**不是** keeper 守护进程：没有定时任务、没有 launchd、不含任何凭据。

来源 `NoteVault/tools/mtsso_keeper`；版本与逐文件 hash 见 `SOURCE.json`、`SHA256SUMS`。
**不要直接改这里的文件**——修改要回源目录，然后重跑：

```bash
python3 scripts/vendor_mtsso_keeper.py --sync --source <keeper 源目录>
python3 scripts/vendor_mtsso_keeper.py --check
```

## 两个组件（各成闭包，互不依赖）

| 组件 | 文件 | 什么时候需要 |
|---|---|---|
| `catpaw-ide` | `catpaw_ide_session.py` `file_lock.py` `mtsso_core.py` | 用 `protocol: catpaw-ide` 的 supply 时**必需** |
| `catpaw-cli-refresh` | `mcli_token.py` `keeper_common.py` `mtsso_core.py` | 用 `protocol: catpaw-cli` 时**可选**（只在要无人值守保鲜时） |

两组声明的文件是各自完整的依赖闭包：只把这几个文件拷到任意目录就能跑，不依赖本目录
其它文件，也不依赖 keeper 源码（有测试守着）。

## catpaw-ide（必需脚本）

前置：Python 3.10+（纯标准库）、能完成内网 SSO 的帐号与网络、本机浏览器、本平台桥动态库
（`libcatpaw_bridge.{dylib,dll,so}`，取法见 model_proxy 的 `docs/CONFIG.md`「桥库从哪来」）。

```bash
K="<model_proxy 根>/vendor/mtsso_keeper"
python3 "$K/catpaw_ide_session.py" --login    # 首次：打开打印的 URL 完成 SSO
python3 "$K/catpaw_ide_session.py" --check    # 只读探测
python3 "$K/catpaw_ide_session.py"            # ensure：有效则秒退，失效才刷新
```

凭据写到 `~/.mtsso_keeper/catpaw_ide_session.json`；代理只读它，不登录不刷新。

## catpaw-cli（脚本通常不需要）

model_proxy 只读 mc CLI 自己写的 `~/.config/mcopilot-cli/.config.yaml` 的 `AUTHORIZATION` 行。
**本机 mc CLI 已登录就不需要本目录任何脚本。**

只有两种情况需要 `mcli_token.py`：

| 场景 | 命令 |
|---|---|
| 想先确认现网 token 还有效 | `python3 "$K/mcli_token.py" --check` |
| token 失效、要重新铸一张 | `python3 "$K/mcli_token.py"`（失效才铸）/ `--force`（无条件铸） |

铸票需要 SSO 会话来源，二选一：

- **默认（cookie 重放）**：读 `~/.mtsso_keeper/sso_session.json`。没有就先建：

  ```json
  {"cookies": "k1=v1; k2=v2", "source": "ego|chrome", "updated_at": 1764000000}
  ```

  cookie 从 `ssosv.sankuai.com` 域取（浏览器开发者工具或 ego-browser）。
- **不碰 cookie 文件**：`--force --activator browser`，脚本打印激活 URL，你用浏览器完成
  SSO，它自己轮询（最长 60s）。

**首次铸票要求 `~/.config/mcopilot-cli/.config.yaml` 已存在**（schema 护栏拒写不存在的文件，
避免凭空造配置）。没有 mc CLI 时先建空文件：

```bash
mkdir -p ~/.config/mcopilot-cli && : > ~/.config/mcopilot-cli/.config.yaml
```

回写只动 `AUTHORIZATION` 行，备份落 `~/.mtsso_keeper/backups/mcli/`（不留在消费目录）。

## 通用约定

- 无参命令都是「先检查、需要时才动作」，不是周期盲刷；不要拿它当 cron 反复跑。
- 退出码：`0` 成功或已有效 / `1` 本次未达成 / `3` 需人工（SSO 失效、被拒）/ `4` 上游契约变化。
- 两个组件写的运行态都在 `~/.mtsso_keeper/`（目录 0700），**不进项目目录**。
- `file_lock.py` 已同时支持 POSIX `flock` 与 Windows `msvcrt.locking`；但 IDE 登录链路的
  浏览器回调尚未在 Windows 验收，Windows 上请按试运行对待。

## 合规边界

- 仅限获授权公司/内网环境使用；每人自行登录，**不共享凭据**。
- 快照内不应出现任何 token、cookie、账号、备份或状态文件；`--check` 会拦截。
- 再分发前确认来源许可；本目录不改变 `mtsso_keeper` 的许可归属。
