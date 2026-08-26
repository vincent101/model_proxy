"""core/listen_config.py — listen / allow_hosts 配置解析（单一真相源）。

server.py（启动绑定与 Host 校验装配）与 model_proxy_cli.sh、hooker/ensure_model_proxy.sh
（端口判定/拉起，经脚本模式）共用本模块，三处行为严格一致。回退矩阵：

- 无 listen 段：host 固定 127.0.0.1，port 取 MODEL_PROXY_PORT 环境变量（合法时），
  否则 18889。向后兼容旧部署（现状即环境变量控端口）。
- 有 listen 段：host 与 port 必须齐全且合法，缺任一或非法值 → ListenConfigError
  （fail-fast，不静默回退）；此时配置为唯一权威，MODEL_PROXY_PORT 不再生效。
- MODEL_PROXY_PORT 本身非法（非数字/越界 1-65535）时，无论哪条路径都失败。
- allow_hosts：非字符串数组或含空串 → 失败；条目结构必须可被 parse_host_authority
  解析、带端口时端口必须等于监听端口（请求 Host 端口恒等于监听端口，不符 = 永不
  匹配 = 必为配错），任一违规启动失败；listen.host 非 loopback 而 allow_hosts
  为空 → 失败（开放监听必须声明放行目标）；绑 loopback 时不启用 Host 校验。
- listen 仅启动时读取，修改后需重启生效，不做热重载。

脚本模式（bash 侧调用）：
    python3 core/listen_config.py --shell <config_path> [env_port]
成功输出 LISTEN_HOST=... / LISTEN_PORT=...（可 eval 的 shell 赋值，值已做单引号转义），
失败向 stderr 报错并 exit 1。本模块仅依赖标准库，可直接以脚本运行（无包内相对导入）。
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18889

# loopback 判定集合（listen.host 与 Host 头校验的内置放行集合保持一致）
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ListenConfigError(Exception):
    """listen/allow_hosts 配置非法。fail-fast：调用方（server 启动 / CLI）应终止。"""


def _parse_port_literal(s: str) -> int:
    """端口字面量校验：纯数字且 1-65535，否则 ValueError。"""
    if not (s.isascii() and s.isdigit()):
        raise ValueError(f"端口非数字: {s!r}")
    port = int(s)
    if not (1 <= port <= 65535):
        raise ValueError(f"端口越界: {s!r}")
    return port


def parse_host_authority(authority: str) -> tuple[str, int | None]:
    """结构化解析 Host 头/白名单条目的 authority → (hostname 小写, port|None)。

    自 core/server.py 下沉至此（listen_config 是仅依赖标准库的单一真相源模块，
    server/cli/脚本模式共用一份实现）。

    契约（见 docs/designs/2026-08-26-本地三服务开放家庭局域网-v2.md §2.1）：
    - 拒绝 userinfo（@）、路径字符（/?#）、空 hostname、非法/越界端口、尾随点；
    - 括号 IPv6（[::1]:18889）走独立分支结构化解析，未加括号的多冒号串拒绝
      ——禁止简单 split(":")；
    - hostname 按 ASCII 统一小写后返回（大小写不敏感比较由调用方对小写值进行）。
    畸形输入一律 ValueError，不抛其他异常。
    """
    if not isinstance(authority, str) or not authority:
        raise ValueError("authority 为空")
    if any(c in authority for c in "@/?#"):
        raise ValueError(f"含 userinfo/路径字符: {authority!r}")
    if authority.startswith("["):
        close = authority.find("]")
        if close == -1:
            raise ValueError(f"IPv6 括号未闭合: {authority!r}")
        host = authority[1:close]
        rest = authority[close + 1:]
        if not host:
            raise ValueError("IPv6 字面量为空")
        try:
            ipaddress.IPv6Address(host)
        except ValueError as e:
            raise ValueError(f"非法 IPv6 字面量: {authority!r}") from e
        port = None
        if rest:
            if not rest.startswith(":") or len(rest) == 1:
                raise ValueError(f"非法端口后缀: {authority!r}")
            port = _parse_port_literal(rest[1:])
    else:
        if "[" in authority or "]" in authority:
            raise ValueError(f"括号错位: {authority!r}")
        if authority.count(":") > 1:
            raise ValueError(f"未加括号的多冒号（IPv6 须用 [] 括起）: {authority!r}")
        if ":" in authority:
            host, _, port_s = authority.partition(":")
            port = _parse_port_literal(port_s)
        else:
            host, port = authority, None
        if not host:
            raise ValueError("hostname 为空")
    host = host.lower()
    if host.endswith("."):
        raise ValueError(f"不接受尾随点: {authority!r}")
    return host, port


def _valid_port_value(value) -> bool:
    """listen.port 合法性：int（非 bool）或数字字符串，且 1-65535。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 1 <= value <= 65535
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return 1 <= int(value) <= 65535
    return False


def _env_port_int(env_port: str | None) -> int | None:
    """MODEL_PROXY_PORT 校验：None/空串 → None（未设置）；非法 → ListenConfigError。

    注意：非法即失败，不静默回退默认端口（无 listen 段路径同样受此约束）。
    """
    if env_port is None or env_port == "":
        return None
    if not (env_port.isascii() and env_port.isdigit()):
        raise ListenConfigError(
            f"MODEL_PROXY_PORT 非法: {env_port!r}（须为 1-65535 的数字）")
    port = int(env_port)
    if not (1 <= port <= 65535):
        raise ListenConfigError(
            f"MODEL_PROXY_PORT 越界: {env_port!r}（须为 1-65535）")
    return port


def _load_config(config_path: str | os.PathLike) -> dict:
    """读 config JSON。文件缺失 → {}（视同无 listen 段，走回退矩阵）；
    JSON 损坏 → ListenConfigError（与 server 启动时 ConfigStore 的 fail-fast 一致）。"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ListenConfigError(f"config JSON 解析失败: {e}") from e
    if not isinstance(config, dict):
        raise ListenConfigError("config 根节点必须是 JSON object")
    return config


def _validate_allow_hosts(allow_hosts: list[str], listen_port: int) -> None:
    """allow_hosts 条目结构/端口校验（fail-fast，不静默全拒）。

    运行时匹配语义（core/server.py is_host_allowed）：请求 Host 的端口恒等于
    监听端口，条目端口与监听端口不符 → 永不匹配，必为配错；结构无法解析的条目
    同样永不匹配。启动即拦截并报条目原文与原因。无端口条目合法（缺省匹配，
    现有语义保留）。loopback 监听同样校验（与类型校验一致，不因无暴露面豁免），
    避免日后切开放监听时才暴露配错。
    """
    for entry in allow_hosts:
        try:
            _, entry_port = parse_host_authority(entry)
        except ValueError as e:
            raise ListenConfigError(
                f"allow_hosts 条目结构非法: {entry!r}（{e}）") from e
        if entry_port is not None and entry_port != listen_port:
            raise ListenConfigError(
                f"allow_hosts 条目端口与监听端口不符: {entry!r}"
                f"（条目端口 {entry_port} ≠ 监听端口 {listen_port}；"
                "请求 Host 端口恒等于监听端口，端口不符的条目永不匹配，必为配错；"
                "不带端口的条目合法）")


def resolve_listen(config_path: str | os.PathLike,
                   env_port: str | None = None) -> dict:
    """解析 listen 段 + allow_hosts。

    返回 {"host": str, "port": int, "allow_hosts": list[str],
          "host_check_enabled": bool}；host_check_enabled = listen.host 非 loopback
    （此时请求 Host 头校验启用）。任一非法 → ListenConfigError。
    """
    env_port_int = _env_port_int(env_port)
    config = _load_config(config_path)

    listen = config.get("listen")
    if listen is None:
        host = DEFAULT_HOST
        port = env_port_int if env_port_int is not None else DEFAULT_PORT
    elif isinstance(listen, dict):
        host_raw = listen.get("host")
        port_raw = listen.get("port")
        if not isinstance(host_raw, str) or not host_raw.strip():
            raise ListenConfigError(
                "listen.host 缺失或为空（写了 listen 段则 host/port 必须写全，"
                "不部分猜默认）")
        if not _valid_port_value(port_raw):
            raise ListenConfigError(
                f"listen.port 缺失或非法: {port_raw!r}（须为 1-65535）")
        host = host_raw.strip()
        port = int(port_raw)
    else:
        raise ListenConfigError("listen 段必须是 JSON object")

    allow_raw = config.get("allow_hosts")
    if allow_raw is None:
        allow_hosts: list[str] = []
    elif (isinstance(allow_raw, list)
          and all(isinstance(e, str) and e.strip() for e in allow_raw)):
        allow_hosts = [e.strip() for e in allow_raw]
    else:
        raise ListenConfigError(
            "allow_hosts 必须为非空字符串数组（类型不符或含空串均启动失败，"
            "不静默忽略）")

    _validate_allow_hosts(allow_hosts, port)

    host_check_enabled = host.lower() not in LOOPBACK_HOSTS
    if host_check_enabled and not allow_hosts:
        raise ListenConfigError(
            f"listen.host={host!r} 为非 loopback 而 allow_hosts 为空——"
            "开放监听必须声明放行的 Host 白名单（DNS rebinding 防护用，"
            "非访问控制）")

    return {
        "host": host,
        "port": port,
        "allow_hosts": allow_hosts,
        "host_check_enabled": host_check_enabled,
    }


# ---------------------------------------------------------------------------
# 脚本模式（bash 侧）
# ---------------------------------------------------------------------------

def _shell_quote(value: str) -> str:
    """单引号转义，保证 eval 注入安全。"""
    return "'" + value.replace("'", "'\\''") + "'"


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "--shell":
        print("usage: listen_config.py --shell <config_path> [env_port]",
              file=sys.stderr)
        return 2
    config_path = Path(argv[1])
    env_port = argv[2] if len(argv) > 2 else None
    try:
        cfg = resolve_listen(config_path, env_port)
    except ListenConfigError as e:
        print(f"listen config error: {e}", file=sys.stderr)
        return 1
    print(f"LISTEN_HOST={_shell_quote(cfg['host'])}")
    print(f"LISTEN_PORT={_shell_quote(str(cfg['port']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
