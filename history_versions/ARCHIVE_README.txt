v1 代理归档说明
================

归档对象：proxy-v1-archived-20260724.tar.gz
下线日期：2026-07-24
原端口：18888
原路径：tools/proxy.py, tools/proxy_cli.sh, tools/ensure_proxy.sh, tools/.claude_proxy.log

下线原因：
v1（tools/proxy.py，纯 Anthropic appkey/profile 轮转代理，端口 18888）已被 v2
（tools/model_proxy/，多协议代理，端口 18889）完全接管，~/.claude/settings.json
的 env 已指向 v2。v1 无继续运行必要，故停进程、删自启 hook、代码归档封存。

密钥文件说明：
本归档【不含】密钥文件。~/.claude/proxy_config.json（含 appkeys/admin_token）与
~/.claude/proxy_state.json 均原样保留在 ~/.claude/，未被本次操作触碰、未纳入归档、
未进入 vault/git。如需彻底清理请自行手动删除上述两个文件（本归档不代删，属用户资产）。

包内内容：
- proxy.py            v1 主程序
- proxy_cli.sh        v1 CLI 管理脚本
- ensure_proxy.sh     v1 自启 hook 脚本
- .claude_proxy.log   v1 运行日志（末 1000 行，归档前已抽查确认无密钥明文）

如何解归档：
tar -xzf proxy-v1-archived-20260724.tar.gz
