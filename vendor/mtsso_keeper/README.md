# vendored mtsso_keeper 认证脚本

本目录是 `mtsso_keeper` 认证脚本的**只读快照**，供没有 keeper 源码的使用者直接使用。
它不包含守护进程，也不包含任何凭据——运行态文件仍写在 `~/.mtsso_keeper/`。

来源：`NoteVault/tools/mtsso_keeper`，版本与逐文件 hash 见 `SOURCE.json`、`SHA256SUMS`。
**不要直接改这里的文件**；修改要回到源目录，然后重跑：

```bash
python3 scripts/vendor_mtsso_keeper.py --sync --source <keeper 源目录>
python3 scripts/vendor_mtsso_keeper.py --check
```

## 两个组件

| 组件 | 文件 | 用途 |
|---|---|---|
| `catpaw-ide` | `catpaw_ide_session.py` `file_lock.py` `mtsso_core.py` | CatPaw IDE 凭据：首次交互登录、日常检查与刷新 |
| `catpaw-cli-refresh` | `mint_mcli_token.py` `mtsso_core.py` | mcli 令牌保鲜（可选；mc CLI 自己登录时可不用） |

`catpaw-cli` 常规用法**不需要本目录任何脚本**：model_proxy 只读 mc CLI 自己写的
`~/.config/mcopilot-cli/.config.yaml`。仅当需要无人值守保鲜时才用 `mint_mcli_token.py`。

## 用法（无守护进程）

```bash
K="$(pwd)/vendor/mtsso_keeper"          # 或任意已解包的快照目录
python3 "$K/catpaw_ide_session.py" --login   # 首次：浏览器完成 SSO
python3 "$K/catpaw_ide_session.py" --check   # 只读探测
python3 "$K/catpaw_ide_session.py"           # ensure：失效才刷新
```

mcli 令牌（可选）：

```bash
python3 "$K/mint_mcli_token.py" --check   # 只读健康检查
python3 "$K/mint_mcli_token.py"           # 失效才铸票
```

- 无参命令都是「检查，需要时才动作」，不是周期盲刷。
- 退出码 `0` 成功 / `1` 本次未达成 / `3` 需人工 / `4` 上游契约变化。
- 代理只读凭据，不登录不刷新；改 proxy 的 `credential_file` 不会改变脚本的写盘位置。

## 合规边界

- 仅限获授权公司/内网环境使用；每人自行登录，**不共享凭据**。
- 快照内不应出现任何 token、cookie、账号、备份或状态文件；`--check` 会拦截。
- 再分发前确认来源许可；本目录不改变 `mtsso_keeper` 的许可归属。
