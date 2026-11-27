# vendored mtsso_keeper 认证脚本

本目录只解决一件事：**没有安装 `mtsso_keeper` 的人，怎么把 model_proxy 的两条内置
CatPaw 通道跑起来**。它不是 keeper，也不做凭据管理——没有守护进程、没有定时刷新、
没有铸票工具，不含任何凭据。

来源 `NoteVault/tools/mtsso_keeper`；版本与逐文件 hash 见 `SOURCE.json`、`SHA256SUMS`。
**不要直接改这里的文件**——修改要回源目录，然后重跑：

```bash
python3 scripts/vendor_mtsso_keeper.py --sync --source <keeper 源目录>
python3 scripts/vendor_mtsso_keeper.py --check
```

## 两条通道，只有一条需要脚本

| protocol | 凭据从哪来 | 本目录需要什么 |
|---|---|---|
| `catpaw-cli`（mcli 网关） | mc CLI 自己登录时写 `~/.config/mcopilot-cli/.config.yaml` | **什么都不需要** |
| `catpaw-ide`（CatPaw IDE） | OIDC，没有第二条产出途径 | `catpaw_ide_session.py` + `file_lock.py` + `mtsso_core.py` |

`catpaw-cli`：**本机 mc CLI 登录过就能用，零脚本**。装好 mc CLI、浏览器登录一次，
model_proxy 直接读那份配置的 `AUTHORIZATION` 行。

`catpaw-ide`：凭据只能由 `catpaw_ide_session.py` 产出，所以脚本是必需的。除此之外
不需要任何东西。

## catpaw-ide 怎么用

前置：Python 3.10+（纯标准库）、能完成内网 SSO 的帐号与网络、本机浏览器、本平台桥动态库
（`libcatpaw_bridge.{dylib,dll,so}`，取法见 model_proxy 的 `docs/CONFIG.md`「桥库从哪来」）。

```bash
K="<model_proxy 根>/vendor/mtsso_keeper"
python3 "$K/catpaw_ide_session.py" --login    # 首次：打开打印的 URL 完成 SSO（只做一次）
python3 "$K/catpaw_ide_session.py" --check    # 只读探测：现在还能不能用
python3 "$K/catpaw_ide_session.py"            # 不可用时才刷新
```

凭据写到 `~/.mtsso_keeper/catpaw_ide_session.json`（状态、候选、锁、备份也在
`~/.mtsso_keeper/`，目录 0700）。**这个目录不进项目、不进 git、不分享。**

不装 keeper 时的节奏：首次 `--login` 一次；之后凭据有效期内（实测 72h）无需动作。
到期或上游返回 401/403 时跑一次无参命令。因为没人在后台续期，**不能指望「一次登录
长期有效」**——要么按到期时间重跑，要么装上 keeper 让它自动维护。

退出码：`0` 成功或当前有效 / `1` 本次未达成 / `3` 需人工（SSO 失效，跑 `--login`）/
`4` 上游契约变化（升级快照，别改代理绕过）。

## 不要在本目录里做凭据管理

本快照**刻意不含** `mcli_token.py`（探测/铸票/回写）、`keeper_common.py`（state 记账）
和 keeper 的编排层（`keeper.py`、`ciba_chain.py`、`mcli_chain.py`、`event_scheduler.py`、
`deploy_txn.py`、launchd 模板与 install/uninstall 脚本）。原因：

- `catpaw-cli` 的令牌由 mc CLI 自己维护，不需要第三方工具
- 铸票与 keeper 的 6h tick 共享 `state_mcli.json` 与配置写锁，是两个合法写者；只拿走
  铸票脚本而丢掉 keeper，会变成"半套凭据管理"，反而在配置写入上与 mc CLI 打架

需要无人值守续期 → 装 `mtsso_keeper`（见其源仓库 README），不要从本目录拼装。

## 合规边界

- 仅限获授权公司/内网环境使用；每人自行登录，**不共享凭据**。
- 快照内不应出现任何 token、cookie、账号、备份或状态文件；`--check` 会拦截。
- 再分发前确认来源许可；本目录不改变 `mtsso_keeper` 的许可归属。
