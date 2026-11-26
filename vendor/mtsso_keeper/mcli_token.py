#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcli_token.py — mcli token 生命周期：探测 / 铸票 / Authorization 行回写（动作 C）

协议逆向快照：mcopilot-cli v0.2.3（方案 §2，pclntab + 反汇编逐段还原）：
  - token = uuid.NewString() 去横线（32 hex，uuid v4）——服务端不签发秘密，
    只"激活"客户端自造的 uuid，铸票无取回凭证环节
  - 激活链（§4.2）：GET /user/login?uuid= → 302 sson/login（client_id 0fcdafb24e）
    → SSO 认证 → mcli/sso/callback → original-url → 服务端激活 uuid
  - 轮询：POST /user/info {"uuid","cliVersion","osInfo"}，1s 间隔 / 60s 超时；
    未激活 {"code":500,"message":"can't find user"}；激活后 code=0 data 含 misId
  - 健康探测：GET /user/info_by_uuid?uuid=<token>（零副作用，真假 token 实测对照）；
    /v1/models 公开、/user/info 不认已持久化 token，均不能作探测

与动作 A/B（CIBA token-exchange）不同凭证域：mcli 不走 CIBA，exchange/refresh
不适用；行式 KV 等新原语留本文件内（方案 §4.5：mtsso_core.py 不改）。

用法：
  python3 mcli_token.py                      # ensure：探活，有效秒退；失效才铸
  python3 mcli_token.py --check              # 只读健康检查（不铸票）
  python3 mcli_token.py --force              # 无条件铸票（SSO 重建后人工在场用）
  python3 mcli_token.py --activator browser  # c 路径：打印激活 URL 等待 ego/人工浏览器

回写四护栏（§4.3）：行级重写只动 AUTHORIZATION 行 / schema 断言拒写 /
备份落 ~/.mtsso_keeper/backups/mcli/（绝不落消费目录）+ 原子替换 /
回读验证失败回滚。先 verify-new（新 token info_by_uuid code==0）再回写。

超时策略（§5 复核订正）：b（cookie 程序化重放）轮询超时 → 换新 uuid 重试一次；
c（浏览器激活）轮询超时 → 直接 exit 3 不换 uuid（避免浏览器侧移动目标）。

不做周期盲刷：token 观察有效 6 个月+，有效期内重铸 = 无谓制造新 token
且触发未验证的互斥风险；只在探测失效时铸（tick 段同此判据）。

退出码：0 成功或已有效 / 1 铸票或校验失败 / 3 需人工（SSO session 失效）/
4 上游契约变更。

凭证纪律：token 不进 argv、不进日志（只 core.fingerprint 指纹）、不进 vault；
SSO session cookie 只存 ~/.mtsso_keeper/sso_session.json（vault 外 0600）。
keeper state 的 mcli 段只经 state_hook 由 keeper_common.apply_mcli_minted 写，
本模块不直接读写 keeper state。

设计记录：docs/designs/2026-09-20-mtsso_keeper铸mcli票方案-动作C.md
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuid_mod

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import mtsso_core as core  # noqa: E402
import keeper_common  # noqa: E402

# ── 常量（协议逆向快照，勿凭猜改）─────────────────────────────────────
MCLI_CONFIG = "~/.config/mcopilot-cli/.config.yaml"   # 消费端配置（model_proxy appkey_file 亦读此）
BACKUP_DIR = "~/.mtsso_keeper/backups/mcli"           # 备份绝不落 ~/.config/mcopilot-cli/ 内
SSO_SESSION_FILE = "~/.mtsso_keeper/sso_session.json" # SSO web session cookie（人工建立）

API_BASE = "https://mcli.sankuai.com/api/v1.0"
CLI_VERSION = "0.2.3"        # 对齐逆向样本 v0.2.3 的 post body
OS_INFO = "darwin/arm64"

POLL_INTERVAL = 1.0          # mc 本体同款：1s 轮询
POLL_TIMEOUT = 60.0          # 静态推导魔数 0x3c（V3 实测复核）
ACTIVATE_MAX_HOPS = 6        # S3 起手工逐跳上限（S3/S4 具体跳数以 V3 实测为准）
HTTP_TIMEOUT = 15            # 单请求超时（对齐 core._HTTP_TIMEOUT）

AUTH_PREFIX = "AUTHORIZATION: "
LINE_RE = re.compile(r"^[A-Z_]+: .*$")
MAX_CONFIG_LINES = 64

# ── 异常 ─────────────────────────────────────────────────────────────
class McliNetError(Exception):
    """网络层错误（exit 1 语义：可由上层按节奏重试）。"""


class SsoSessionDeadError(Exception):
    """SSO web session 失效/缺失或被风控拦截，无法程序化重建（exit 3，需人工）。"""


class PollTimeoutError(Exception):
    """轮询超时（60s 未激活）。"""


_SSO_DEAD_HINT = (
    "SSO session 失效或缺失，无法程序化激活。人工重建步骤：\n"
    "  1. 用 ego-browser（或 Chrome）打开任一 sankuai SSO 登录页完成登录\n"
    "  2. 提取 ssosv.sankuai.com 域 cookie，写入 ~/.mtsso_keeper/sso_session.json：\n"
    '     {"cookies": "k1=v1; k2=v2", "source": "ego|chrome", "updated_at": <unix秒>}\n'
    "  3. 重跑: python3 mcli_token.py --force"
)


def _v(verbose: bool, msg: str) -> None:
    if verbose:
        print(f"[mint] {msg}", file=sys.stderr)


# ── 网络层（模块唯一出口，单测 patch 点）──────────────────────────────
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """30x 不跟随（等价 requests allow_redirects=False）：激活链需逐跳观测。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _fetch(url, data=None, timeout=HTTP_TIMEOUT, opener=None, method=None):
    """GET/POST（不跟随重定向）。返回 (status, 小写 headers dict, body bytes)。

    30x：no-redirect 下 urllib 以 HTTPError(30x) 上抛，此处转为正常返回；
    4xx/5xx：McliNetError；URLError/超时原样抛（调用方归类网络错）。
    """
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        resp = (opener or _OPENER).open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            e.close()
        except Exception:
            pass
        if 300 <= e.code < 400:
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            return e.code, hdrs, b""
        # P0 修复（复核发现）：异常消息只留 scheme://netloc/path，去 query——
        # info_by_uuid 的 query 即 token，原样进消息会被 keeper log 的 detail 带出
        u = urllib.parse.urlsplit(url)
        safe_url = f"{u.scheme}://{u.netloc}{u.path}"
        raise McliNetError(f"HTTP {e.code} {safe_url}") from None
    with resp:
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, hdrs, resp.read()


def _login_url(mcli_uuid: str) -> str:
    return f"{API_BASE}/user/login?uuid={mcli_uuid}"


def _sanitize_url(text: str) -> str:
    """错误文本中若含 URL query（可能带 token），剥掉 query 只留主体。"""
    m = re.search(r"(https?://[^\s'\"]+)", text)
    if not m:
        return text
    u = urllib.parse.urlsplit(m.group(1))
    return text.replace(m.group(1), f"{u.scheme}://{u.netloc}{u.path}")


def _info_by_uuid_url(token: str) -> str:
    return f"{API_BASE}/user/info_by_uuid?uuid={token}"


def _parse_code_body(body: bytes) -> tuple:
    """断言 {code,message,data} 形状（§5：misId 字段断言，fail-fast exit 4）。"""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise core.UpstreamChangedError(f"响应非 JSON: {body[:80]!r}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("code"), int):
        raise core.UpstreamChangedError(f"响应形状异常（无 int code）: {str(payload)[:80]}")
    data = payload.get("data")
    if payload["code"] == 0 and not (isinstance(data, dict) and data.get("misId")):
        raise core.UpstreamChangedError(f"code==0 但 data 无 misId: {str(data)[:80]}")
    return payload["code"], data, payload.get("message")


# ── 健康探测（§2.5：info_by_uuid 是唯一可用探测端点，零副作用）────────
def probe_token(token: str, timeout: int = 10) -> tuple:
    """GET /user/info_by_uuid?uuid=<token>。

    返回 (code, data, err)：code==0 有效；code!=0 失效；code=None 网络错。
    形状异常抛 core.UpstreamChangedError（exit 4）。
    """
    try:
        _, _, body = _fetch(_info_by_uuid_url(token), timeout=timeout)
    except McliNetError as e:
        return None, None, str(e)
    except (urllib.error.URLError, OSError) as e:
        # URLError.reason 链可能带完整 URL（含 token query）——统一剥 query
        return None, None, f"{type(e).__name__}: {_sanitize_url(str(e))}"
    code, data, _msg = _parse_code_body(body)
    return code, data, None


# ── 行式 KV 原语（§4.5：新原语留本文件，mtsso_core 不改）──────────────
def read_auth_line(path: str = None):
    """读配置的 AUTHORIZATION 值；文件缺失/无该行/空值 → None。"""
    path = os.path.expanduser(path or MCLI_CONFIG)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    for ln in raw.decode("utf-8", errors="replace").split("\n"):
        if ln.startswith(AUTH_PREFIX):
            val = ln[len(AUTH_PREFIX):].strip()
            return val or None
    return None


def _verify_writeback(path: str, expected: bytes, backup) -> None:
    """护栏 4：重读断言（写后内容 == 期望字节）；不一致 → 回滚（备份还原 /
    首铸无备份删除坏文件，core.rollback 同语义）后抛 IOError。"""
    with open(path, "rb") as f:
        after = f.read()
    if after == expected:
        return
    core.rollback(path, backup)
    raise IOError(f"回写验证失败（内容与期望不一致），已回滚；备份: {backup or '无(首铸)'}")


def rewrite_authorization_line(path: str, new_token: str, backup_dir: str = None) -> str:
    """四护栏行级重写 AUTHORIZATION 行（§4.3）。token 必须已过 verify-new。

    护栏 1：逐行处理原字节，仅替换/追加 AUTHORIZATION 行，其余行（含空行、
            行尾换行风格）原样字节级保留；
    护栏 2：schema 断言——可读、≤64 行、每行 ^[A-Z_]+: .*$ 或空行、
            AUTHORIZATION 行恰 0/1 个；不满足抛 core.UpstreamChangedError 拒写；
    护栏 3：copy2 备份落 backup_dir（0600，绝不留在消费目录）→ 写 .mint-tmp
            → os.replace 原子替换 → chmod 0600；
    护栏 4：_verify_writeback 重读断言，失败回滚。
    返回备份路径（首铸无旧文件 → None）。

    并发注意：避免 keeper 铸票窗口并行手跑 mc（writeYaml 全文件重写可能回滚
    新票，后果有界：下 tick 自动重铸）。
    """
    path = os.path.expanduser(path)
    backup_dir = backup_dir or BACKUP_DIR
    # 护栏 2：先断言再动手（拒写时原文件一个字节不动）
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise core.UpstreamChangedError(f"配置不可读，schema 拒写: {e}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise core.UpstreamChangedError("配置非 UTF-8，schema 拒写") from None
    lines = text.split("\n")  # 末元素为 '' ⇔ 文件以 \n 结尾（join 可无损还原）
    n_lines = len(lines) - (1 if lines and lines[-1] == "" else 0)
    if n_lines > MAX_CONFIG_LINES:
        raise core.UpstreamChangedError(f"配置 {n_lines} 行 > {MAX_CONFIG_LINES}，schema 拒写")
    auth_idx = [i for i, ln in enumerate(lines) if ln.startswith(AUTH_PREFIX)]
    if len(auth_idx) > 1:
        raise core.UpstreamChangedError(f"AUTHORIZATION 行有 {len(auth_idx)} 个，schema 拒写")
    for ln in lines:
        if ln and not LINE_RE.match(ln):
            raise core.UpstreamChangedError(f"未知行形态，schema 拒写: {ln[:40]!r}")
    # 护栏 1：计算期望字节（替换保留原行尾 \r，追加跟随文件尾换行）
    if auth_idx:
        i = auth_idx[0]
        trailing = "\r" if lines[i].endswith("\r") else ""
        lines[i] = AUTH_PREFIX + new_token + trailing
    elif lines and lines[-1] == "":
        lines = lines[:-1] + [AUTH_PREFIX + new_token, ""]
    elif not text:
        lines = [AUTH_PREFIX + new_token, ""]
    else:  # 末行无换行：补 \n 后追加（原末行字节不变）
        lines = lines + [AUTH_PREFIX + new_token, ""]
    expected = "\n".join(lines).encode("utf-8")
    # 护栏 3：备份 → 原子写
    backup = core.backup_file(path, backup_dir)
    tmp = path + ".mint-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(expected)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    # 护栏 4：回读验证 + 失败回滚
    _verify_writeback(path, expected, backup)
    return backup


# ── SSO session（§4.4：人工建立，不纳入 tick 自动维护）────────────────
def load_sso_session():
    """读 sso_session.json → (cookies_str, source)；缺失/坏/空 → None。"""
    try:
        with open(os.path.expanduser(SSO_SESSION_FILE), encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("cookies"), str) and obj["cookies"].strip():
        return obj["cookies"].strip(), str(obj.get("source", "?"))
    return None


def _inject_cookie_str(jar, cookie_str: str, domain: str = ".sankuai.com") -> None:
    """把 "k=v; k2=v2" 注入 cookie jar（.sankuai.com 匹配 ssosv/mcli 全域）。

    SSO cookie 不打日志（凭证纪律），只在此处进入请求头。
    """
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, val = pair.partition("=")
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=val,
            port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
            path="/", path_specified=True,
            secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))


# ── 激活链（§4.2）────────────────────────────────────────────────────
def activate_via_cookie(mcli_uuid: str, sso_cookie_str: str, verbose: bool = False,
                        timeout: int = HTTP_TIMEOUT) -> None:
    """b 路径执行器：程序化重放 302 激活链。

    S1 无凭证 GET login?uuid= → 302 sson/login（校验目标含 sson/login）
    S2 带 SSO session cookie GET sson/login → 302 成功 / 200 登录页 =
       SSO session 失效（exit 3）/ 其余网络错
    S3+ 手工逐跳（同 cookie jar）直到 mcli 域 200 = 激活完成；
       被 302 踢回 sson/login = session 失效/风控（exit 3）
    """
    jar = http.cookiejar.CookieJar()
    _inject_cookie_str(jar, sso_cookie_str)
    opener = urllib.request.build_opener(_NoRedirectHandler(),
                                         urllib.request.HTTPCookieProcessor(jar))
    # S1：无凭证拿 SSO 跳转目标
    status, hdrs, _ = _fetch(_login_url(mcli_uuid), timeout=timeout)
    if status != 302 or not hdrs.get("location"):
        raise core.UpstreamChangedError(
            f"S1 非 302（HTTP {status}）——登录入口契约变更，勿硬闯")
    sso_url = urllib.parse.urljoin(_login_url(mcli_uuid), hdrs["location"])
    if "sson/login" not in sso_url:
        raise core.UpstreamChangedError(
            f"S1 302 目标非 sson/login（{urllib.parse.urlsplit(sso_url).netloc}）——契约变更")
    # S2：带 SSO session cookie 过认证
    status, hdrs, _ = _fetch(sso_url, timeout=timeout, opener=opener)
    if status == 200:
        raise SsoSessionDeadError(
            "SSO session 失效（sson/login 返回 200 登录页/风控页，非 302 放行）")
    if status != 302 or not hdrs.get("location"):
        raise McliNetError(f"S2 HTTP {status}（非 302/200，网络/上游异常）")
    url = urllib.parse.urljoin(sso_url, hdrs["location"])
    # S3+：逐跳（S2 起服务端可能种 mcli 域 cookie，callback 需要，全程同 jar）
    for hop in range(ACTIVATE_MAX_HOPS):
        status, hdrs, _ = _fetch(url, timeout=timeout, opener=opener)
        if 300 <= status < 400:
            loc = hdrs.get("location")
            if not loc:
                raise core.UpstreamChangedError(f"激活链跳 {hop}: 30x 无 Location")
            nxt = urllib.parse.urljoin(url, loc)
            if "sson/login" in nxt:
                raise SsoSessionDeadError("激活链被踢回 sson/login（session 失效/风控）")
            url = nxt
            _v(verbose, f"激活链跳 {hop}: 302 → {urllib.parse.urlsplit(nxt).netloc}")
            continue
        if status == 200 and urllib.parse.urlsplit(url).netloc.endswith("mcli.sankuai.com"):
            _v(verbose, f"激活链完成（{hop + 3} 跳，uuid={core.fingerprint(mcli_uuid)}）")
            return
        raise McliNetError(f"激活链跳 {hop} HTTP {status}")
    raise core.UpstreamChangedError(
        f"激活链超 {ACTIVATE_MAX_HOPS} 跳——契约可能变更，勿盲目重试")


def activate_via_browser(mcli_uuid: str, verbose: bool = False) -> None:
    """c 路径执行器骨架：到"调 ego skill"边界即止。

    ego-browser 是 Claude 会话侧 skill，CLI 进程无法直接调用——本函数只
    打印激活 URL（mc v0.2.3 同款行为），由 ego/人工在浏览器完成激活，
    本进程继续 poll_info 轮询（与 b 共享同一状态机，仅执行器不同）。
    """
    print("请用浏览器（或让 ego-browser）打开以下 URL 完成 SSO 登录激活：")
    print(f"  {_login_url(mcli_uuid)}")
    print(f"（等待激活，最长 {POLL_TIMEOUT:.0f}s；本进程只轮询不写盘直到激活确认）")


# ── 轮询（§4.1：POST /user/info，1s 间隔 60s 超时，对齐 mc）───────────
def poll_info(mcli_uuid: str, interval: float = None, timeout: float = None,
              verbose: bool = False) -> dict:
    """轮询 /user/info 直到 code==0（返回含 misId 的 data）或超时。

    未激活（code=500 can't find user）与网络抖动均继续轮询（对齐 mc：
    resp==nil 继续 sleep）；形状断言失败 fail-fast（UpstreamChanged）。
    """
    interval = POLL_INTERVAL if interval is None else interval
    timeout = POLL_TIMEOUT if timeout is None else timeout
    body = json.dumps({"uuid": mcli_uuid, "cliVersion": CLI_VERSION,
                       "osInfo": OS_INFO}).encode("utf-8")
    deadline = time.monotonic() + timeout
    n = 0
    while True:
        n += 1
        try:
            _, _, raw = _fetch(f"{API_BASE}/user/info", data=body, method="POST")
            code, data, _msg = _parse_code_body(raw)
            if code == 0:
                _v(verbose, f"轮询第 {n} 次激活确认（misId={data.get('misId')}）")
                return data
        except core.UpstreamChangedError:
            raise
        except (McliNetError, urllib.error.URLError, OSError, ValueError):
            pass  # 网络抖动：继续轮询
        if time.monotonic() >= deadline:
            raise PollTimeoutError(f"轮询 {timeout:.0f}s（{n} 次）未激活")
        time.sleep(interval)


# ── 主流程 ───────────────────────────────────────────────────────────
def run_ensure(force: bool = False, verbose: bool = False, activator: str = "cookie",
               config_path: str = None, backup_dir: str = None,
               state_hook=None) -> int:
    """ensure 主流程（§4.1 状态机），返回退出码。

    state_hook：铸票成功回写配置后调用 `state_hook(new_token, old_code)`；
    keeper state 的写入由调用方注入（本模块不写 keeper state）。"""
    config_path = os.path.expanduser(config_path or MCLI_CONFIG)
    backup_dir = backup_dir or BACKUP_DIR

    # ensure 短路：现网 token 探活，code==0 秒退（零铸票）
    if not force:
        existing = read_auth_line(config_path)
        if existing:
            try:
                code, data, err = probe_token(existing)
            except core.UpstreamChangedError as e:
                print(f"[ERROR] 上游契约变更: {e}", file=sys.stderr)
                return 4
            if code == 0:
                print(f"[完成] mcli token 已有效（{core.fingerprint(existing)} "
                      f"misId={data.get('misId', '?')}），无需铸票")
                return 0
            _v(verbose, f"现有 token 探测无效（code={code} err={err}），铸票")

    # 铸票：b 超时换新 uuid 重试一次；c 超时直接 exit 3 不换 uuid（§5 订正）
    attempts = 2 if activator == "cookie" else 1
    minted_uuid, minted_data = None, None
    for _attempt in range(attempts):
        mcli_uuid = uuid_mod.uuid4().hex  # 与 mc 行为逐字一致：uuid4 去横线
        try:
            if activator == "cookie":
                sess = load_sso_session()
                if sess is None:
                    raise SsoSessionDeadError("~/.mtsso_keeper/sso_session.json 缺失/无效")
                _v(verbose, f"b 路径激活（uuid={core.fingerprint(mcli_uuid)}，"
                            f"SSO source={sess[1]}）")
                activate_via_cookie(mcli_uuid, sess[0], verbose)
            else:
                _v(verbose, f"c 路径激活（uuid={core.fingerprint(mcli_uuid)}）")
                activate_via_browser(mcli_uuid, verbose)
            minted_data = poll_info(mcli_uuid, verbose=verbose)
            minted_uuid = mcli_uuid
            break
        except SsoSessionDeadError as e:
            print(f"[ERROR] 需人工: {e}", file=sys.stderr)
            print(_SSO_DEAD_HINT, file=sys.stderr)
            return 3
        except PollTimeoutError as e:
            if activator == "cookie":
                _v(verbose, f"{e}；b 路径换新 uuid 重试一次")
                continue
            print(f"[ERROR] 需人工: 轮询超时，c 路径不换 uuid"
                  f"（避免浏览器侧移动目标）: {e}", file=sys.stderr)
            return 3
        except core.UpstreamChangedError as e:
            print(f"[ERROR] 上游契约变更: {e}", file=sys.stderr)
            return 4
        except McliNetError as e:
            print(f"[ERROR] 网络错误: {e}", file=sys.stderr)
            return 1
        except (urllib.error.URLError, OSError) as e:
            # P1 复核修复：激活链 _fetch 的 URLError/超时原样上抛，原此处穿透致
            # CLI 裸 traceback；消息剥 URL query（uuid 防进日志，同 probe_token）
            print(f"[ERROR] 网络错误: {_sanitize_url(str(e))}", file=sys.stderr)
            return 1
    if minted_uuid is None:
        print("[ERROR] 铸票失败：轮询超时（含一次换 uuid 重试）", file=sys.stderr)
        return 1

    # verify-new：新 token 必须 info_by_uuid code==0 才允许回写（§4.3 顺序约束）
    try:
        code, _data, err = probe_token(minted_uuid)
    except core.UpstreamChangedError as e:
        print(f"[ERROR] 上游契约变更: {e}", file=sys.stderr)
        return 4
    if code != 0:
        print(f"[ERROR] 新 token 验证失败（code={code} err={err}），不回写", file=sys.stderr)
        return 1

    # verify-old：互斥性观测（§4.1/§7，只记录不阻断）
    old_code = None
    old = read_auth_line(config_path)
    if old and old != minted_uuid:
        try:
            old_code, _, _ = probe_token(old)
        except (core.UpstreamChangedError, McliNetError):
            old_code = None

    # 回写（四护栏）
    try:
        backup = rewrite_authorization_line(config_path, minted_uuid, backup_dir)
    except core.UpstreamChangedError as e:
        print(f"[ERROR] 上游契约变更，拒写: {e}", file=sys.stderr)
        return 4
    except OSError as e:
        print(f"[ERROR] 回写 IO 失败: {e}", file=sys.stderr)
        return 1

    if state_hook:
        state_hook(minted_uuid, old_code)
    print(f"[完成] 已铸 mcli token 并回写 {config_path}")
    print(f"  新 token: {core.fingerprint(minted_uuid)}  misId={minted_data.get('misId', '?')}")
    if old_code is not None:
        verdict = "旧票仍有效（无互斥）" if old_code == 0 else "旧票已失效（可能互斥）"
        print(f"  互斥观测: 旧 token 铸后探测 code={old_code}（{verdict}）")
    if backup:
        _v(verbose, f"备份: {backup}")
    return 0


def run_check(config_path: str = None) -> int:
    """--check：只读健康检查（probe 现网 token，不铸票不写盘）。"""
    config_path = os.path.expanduser(config_path or MCLI_CONFIG)
    token = read_auth_line(config_path)
    if not token:
        print(f"mcli token: <无>（{config_path} 无 AUTHORIZATION 行，等首铸）")
        return 1
    try:
        code, data, err = probe_token(token)
    except core.UpstreamChangedError as e:
        print(f"mcli token: 探测形状异常（上游契约变更）: {e}")
        return 4
    if code == 0:
        print(f"mcli token 健康: {core.fingerprint(token)}  code=0  "
              f"misId={data.get('misId', '?')}")
        return 0
    print(f"mcli token 失效: {core.fingerprint(token)}  code={code}  err={err}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="铸 mcli (CatPaw) token：uuid 自生成 + 激活链 + 轮询 + 四护栏回写")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印过程细节")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--force", action="store_true",
                   help="无条件铸票（默认 ensure：先探活，有效则跳过）")
    g.add_argument("--check", action="store_true",
                   help="只读健康检查：探测现网 token，不铸票")
    ap.add_argument("--activator", choices=["cookie", "browser"], default="cookie",
                    help="激活执行器：cookie=程序化 SSO 重放（默认，b 路径）；"
                         "browser=打印激活 URL 等待 ego/人工浏览器（c 路径）")
    args = ap.parse_args()
    if args.check:
        return run_check()
    return run_ensure(force=args.force, verbose=args.verbose, activator=args.activator,
                      state_hook=keeper_common.apply_mcli_minted)


if __name__ == "__main__":
    sys.exit(main())
