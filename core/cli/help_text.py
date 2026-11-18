HELP = '用法: model_proxy_cli.sh <子命令> [参数]\n\n进程控制:\n  on                                启动 model_proxy.py\n  off                               停止 model_proxy.py\n  restart                           停止后重启进程（代码更新后用；reload 只换配置不换代码）\n                                    内存学习态清零：pref/thinking_strip 48h 记忆、\n                                    cooldown、流健康计数；在途请求会被切断\n  reload                            触发配置热重载（无条件清空所有 cooldown）\n\n查询观察:\n  status                            显示运行态总览\n  logs [N] [field=val]              显示最近 N 条日志（默认 30 条 ACCESS）。\n                                    支持按字段过滤：\n                                      logs                最近 30 条 ACCESS\n                                      logs 50             最近 50 条 ACCESS\n                                      logs req=abc12345   按 req_id 过滤所有日志行\n                                      logs level=ERROR    按级别过滤（ERROR/WARNING/INFO）\n                                      logs event=cooldown  按事件关键词过滤\n                                      logs req=abc 50     过滤 + 条数\n  stats [时间]                      统计 supply/route/strategy 用量（独立账本，不受日志截断影响）\n                                    支持按时间过滤：\n                                      stats                          全历史 total\n                                      stats today                    今天（UTC+8）\n                                      stats month                    本月（UTC+8）\n                                      stats 2026-07-23                指定某天\n                                      stats 2026-07                   指定某月\n\n配置管理:\n  supply                            打印 supply list 后进入交互菜单，可选操作：\n                                    [a]dd  交互式新增 supply\n                                    [e]dit 交互式编辑 supply\n                                    [d]el  删除 supply\n                                    [t]est 连通性测试 & effort 探测和登记\n                                    [q]uit 退出\n  route                             打印 route list 后进入交互菜单，可选操作：\n                                    [a]dd  交互式新增 route 家族模板\n                                    [e]dit 交互式编辑 route 的 tiers/failover\n                                    [d]el  删除 route\n                                    [q]uit 退出\n  strategy                          打印 strategy list 后进入交互菜单，可选操作：\n                                    [a]dd  交互式新增 strategy 绑定 route_id/note/source\n                                    [e]dit 交互式编辑 strategy 的 route_id/note/source\n                                    [d]el  删除 strategy\n                                    [q]uit 退出\n  switch <client_token> <route_id>  改 strategy.route_id 后 reload\n  install                           交互式列出四个 SDK + 本机检测状态，选择安装\n\n会话内指令（非 CLI 子命令，在对话里直接发送）:\n  $route                      查询当前 session 的生效 route 与 override\n  $route <route_id>           把当前 session 固定到指定 route（写 config/session_overrides.json）\n  $route reset                清除当前 session 的 override\n\n--help / -h                       显示此帮助\n\n说明: supply/route/strategy/install 的增删改只能通过交互菜单执行，不支持子命令直达。\n      非交互（stdin 非 TTY）环境下进入这些入口只打印一次 list 后直接退出。\n'


def help_for_platform(platform: str) -> str:
    """Keep the legacy macOS help unchanged while naming the Windows entry."""
    if platform == 'win32':
        return HELP.replace('model_proxy_cli.sh', 'model_proxy_cli.bat', 1)
    return HELP


def write_platform_text(text: str, platform: str, stream) -> None:
    """Write Windows text even when a redirected stream has a limited code page."""
    encoding = getattr(stream, 'encoding', None)
    if (platform == 'win32' and encoding
            and encoding.lower().replace('_', '-') not in ('utf-8', 'utf8')):
        reconfigure = getattr(stream, 'reconfigure', None)
        try:
            if not callable(reconfigure):
                raise ValueError('stream cannot be reconfigured')
            reconfigure(encoding='utf-8', errors='strict')
        except (OSError, ValueError, AttributeError):
            # Keep a non-reconfigurable stream in its original encoding; do
            # not mix encodings across calls when it can encode only some text.
            text = text.encode(encoding, errors='backslashreplace').decode(encoding)
    stream.write(text)


def write_help(platform: str, stream) -> None:
    write_platform_text(help_for_platform(platform), platform, stream)
