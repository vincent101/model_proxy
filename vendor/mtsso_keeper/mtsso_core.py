#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mtsso_core.py — CIBA 统一凭证源共享原语层（纯标准库，Python 3.9+）

被三条链共享：
  - mint_bi_token.py（动作 A：token-exchange 铸 BI/万象 app 票）
  - mint_dsx_token.py（动作 B：token-exchange 铸 dsx/万象分组票）
  - keeper.py（P1：refresh_token 前滚 CIBA 用户票 + 回写 oa-skills 缓存）

协议形状逐字核对自 oa-skills-shared/dist/esm/auth/{sso-oidc,sso-ciba-legacy}.js：
  - client_secret_jwt：HS256，payload {sub,iss,aud:[endpoint],exp:now+86400,iat:now-30,jti}
  - refresh:      grant_type=refresh_token + refresh_token
  - exchange:     grant_type=urn:ietf:params:oauth:grant-type:token-exchange
                  + subject_token + subject_token_type + audience（空格分隔）

凭证纪律（硬性）：
  client secret 与任何 token 不进 argv、不写 vault 内文件、不进日志。
  日志只允许 core.fingerprint() 的形态（前 6 字符 + 长度）。
"""

from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ── 常量 ─────────────────────────────────────────────────────────────
TOKEN_ENDPOINT = "https://ssosv.sankuai.com/sson/auth/oidc/v1/token"
AUTH_CACHE = "~/.cache/openclaw-auth/auth-cache.json"
CLAWDGW_CONF = "~/.config/clawdgw.json"  # defaultUserId → mis
OA_CONFIG_JS_GLOB = "@it/oa-skills*/**/oa-skills-shared/dist/esm/auth/config.js"

STATE_DIR = "~/.mtsso_keeper"           # vault 外（硬约束：含 RT，绝不进 vault）
STATE_FILE = STATE_DIR + "/state.json"
LOG_FILE = STATE_DIR + "/keeper.log"
LOCK_FILE = STATE_DIR + "/keeper.lock"

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
GRANT_TYPE_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

_HTTP_TIMEOUT = 15
_NETWORK_RETRIES = 2  # 初次之外的重试次数（指数退避）


# ── 异常 ─────────────────────────────────────────────────────────────
class SsoError(Exception):
    """SSO 服务端返回 error 体（原样携带，不吞 description）。"""

    def __init__(self, payload: dict):
        self.payload = payload if isinstance(payload, dict) else {"error": str(payload)}
        super().__init__(
            f"{self.payload.get('error')}: {self.payload.get('error_description', '')}"
        )

    @property
    def code(self) -> str:
        return str(self.payload.get("error", ""))


class NeedHumanError(Exception):
    """无法自动确定身份（MIS），需要人配置。"""


class UpstreamChangedError(Exception):
    """oa-skills 自升级改了目录布局/常量，取不到内置 client 凭证。"""


# ── 身份与凭证 ───────────────────────────────────────────────────────
def resolve_mis() -> str:
    """优先 env SSO_USER_ID → clawdgw.json:defaultUserId；都无 → NeedHumanError。"""
    mis = os.environ.get("SSO_USER_ID")
    if mis:
        return mis
    try:
        with open(os.path.expanduser(CLAWDGW_CONF), encoding="utf-8") as f:
            mis = json.load(f).get("defaultUserId")
    except (OSError, ValueError):
        mis = None
    if mis:
        return mis
    raise NeedHumanError(
        "无法确定 MIS：设 SSO_USER_ID 环境变量，或在 ~/.config/clawdgw.json 配 defaultUserId"
    )


def _deobfuscate(encoded: str, key: str) -> str:
    """XOR + base64 解混淆（照 config.js 的 _d 函数）。"""
    buf = bytearray(base64.b64decode(encoded))
    kb = key.encode("utf-8")
    for i in range(len(buf)):
        buf[i] ^= kb[i % len(kb)]
    return buf.decode("utf-8")


def _npm_global_roots() -> list:
    """npm 全局根候选：`npm root -g` 输出 + 常见安装位（launchd 环境 PATH 受限，须兜底）。"""
    roots = []
    npx = shutil.which("npm") or "/opt/homebrew/bin/npm"
    for npm in (shutil.which("npm"), npx, "/usr/local/bin/npm"):
        if not npm or not os.path.isfile(npm) or npm in roots:
            continue
        try:
            out = subprocess.run(
                [npm, "root", "-g"], capture_output=True, text=True, timeout=15
            )
            if out.returncode == 0 and out.stdout.strip():
                roots.append(out.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    roots += ["/opt/homebrew/lib/node_modules", "/usr/local/lib/node_modules"]
    seen: set = set()
    uniq = []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def load_client_creds() -> tuple:
    """从已安装的 oa-skills 包提取内置兜底 client (client_id, client_secret)。

    只依赖 config.js 里 _K/_DEFAULT_CID/_DEFAULT_CSE 三个字符串常量的存在；
    找不到/解不出 → UpstreamChangedError（附尝试过的路径）。
    """
    tried = []
    for root in _npm_global_roots():
        pattern = os.path.join(root, OA_CONFIG_JS_GLOB)
        for cfg in sorted(glob.glob(pattern, recursive=True)):
            tried.append(cfg)
            try:
                with open(cfg, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            m_k = re.search(r'export\s+const\s+_K\s*=\s*"([^"]+)"', text)
            m_cid = re.search(r'export\s+const\s+_DEFAULT_CID\s*=\s*"([^"]+)"', text)
            m_cse = re.search(r'export\s+const\s+_DEFAULT_CSE\s*=\s*"([^"]+)"', text)
            if not (m_k and m_cid and m_cse):
                continue
            try:
                cid = _deobfuscate(m_cid.group(1), m_k.group(1))
                cse = _deobfuscate(m_cse.group(1), m_k.group(1))
                if cid and cse:
                    return cid, cse
            except Exception:
                continue
    detail = "\n".join(tried) if tried else "(glob 无命中)"
    raise UpstreamChangedError(
        f"找不到/解不出 oa-skills 内置凭证（glob={OA_CONFIG_JS_GLOB}）。尝试过的候选：\n{detail}"
    )


# ── 协议原语 ─────────────────────────────────────────────────────────
def _b64url(data: bytes) -> str:
    """base64url 无 padding（照 sso-oidc.js 的 b64url）。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _client_assertion(client_id: str, secret: str, endpoint: str) -> str:
    """HS256 client_secret_jwt（照官方 makeClientAssertion 逐字段对齐）。"""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": client_id,
        "iss": client_id,
        "aud": [endpoint],
        "exp": now + 86400,
        "iat": now - 30,  # 官方 jwtClockSkewSeconds=30
        "jti": str(uuid.uuid4()),
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(
        hmac.new(secret.encode("utf-8"), f"{h}.{p}".encode("utf-8"), hashlib.sha256).digest()
    )
    return f"{h}.{p}.{sig}"


def _auth_params(client_id: str, secret: str) -> dict:
    return {
        "client_id": client_id,
        "client_assertion_type": CLIENT_ASSERTION_TYPE,
        "client_assertion": _client_assertion(client_id, secret, TOKEN_ENDPOINT),
    }


def _post_token(params: dict, timeout: int = _HTTP_TIMEOUT, retries: int = _NETWORK_RETRIES) -> dict:
    """POST token 端点。网络失败/超时按指数退避重试；SSO error 体立即抛 SsoError（不重试）。"""
    body = urllib.parse.urlencode(params).encode("utf-8")
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                TOKEN_ENDPOINT,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and "error" in data:
                raise SsoError(data)
            return data
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read().decode("utf-8"))
            except Exception:
                data = None
            if isinstance(data, dict) and "error" in data:
                raise SsoError(data) from None
            last_exc = e
        except urllib.error.URLError as e:
            last_exc = e
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise SsoError(
        {"error": "network_error", "error_description": str(last_exc)}
    )


def refresh(refresh_token: str) -> dict:
    """grant_type=refresh_token 前滚 CIBA 用户票。返回含 access_token/refresh_token?/expires_in。"""
    cid, cse = load_client_creds()
    params = _auth_params(cid, cse)
    params["grant_type"] = "refresh_token"
    params["refresh_token"] = refresh_token
    return _post_token(params)


def exchange(subject_token: str, audiences: list) -> dict:
    """token-exchange：用 CIBA 用户票铸 app 票。audiences 空格分隔，上限 5。"""
    if len(audiences) > 5:
        raise ValueError(f"audiences 上限 5，收到 {len(audiences)}")
    cid, cse = load_client_creds()
    params = _auth_params(cid, cse)
    params["grant_type"] = GRANT_TYPE_TOKEN_EXCHANGE
    params["subject_token"] = subject_token
    params["subject_token_type"] = SUBJECT_TOKEN_TYPE
    params["audience"] = " ".join(audiences)
    return _post_token(params)


# ── oa-skills 缓存读写（D3 四护栏）────────────────────────────────────
def _ciba_key(mis: str) -> str:
    return f"sso-ciba:{mis}:ciba"


def read_ciba_entry(mis: str):
    """读 auth-cache.json 的 tokens["sso-ciba:<mis>:ciba"]；无/坏 → None。"""
    try:
        with open(os.path.expanduser(AUTH_CACHE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    entry = (data.get("tokens") or {}).get(_ciba_key(mis))
    return entry if isinstance(entry, dict) else None


def write_ciba_entry(mis: str, entry: dict) -> tuple:
    """回写 ciba 用户票条目，四护栏缺一不可。返回 (成功?, 失败原因)。

    失败原因：'ok' / 'schema_changed'（json 解析失败或 schema/形状断言不过，
    上游契约变更）/ 'io_error'（读/写 OSError：文件不可读、磁盘满、权限等，
    不是上游的锅，勿误报 schema_changed）。

    护栏 1 read-modify-write 只碰一个键（cookies/其它 token 条目/未知字段原样保留）；
    护栏 2 schema 断言（version=="1.0"、旧值形状合法），不满足不写；
    护栏 3 备份 .keeper-bak（只留最近一份）+ 写 .keeper-tmp + os.replace（与官方 .tmp 互不踩）；
    护栏 4 由护栏 1 保证：绝不动 app 票条目。
    """
    path = os.path.expanduser(AUTH_CACHE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return False, "io_error"
    except ValueError:
        return False, "schema_changed"
    if data.get("version") != "1.0" or not isinstance(data.get("tokens"), dict):
        return False, "schema_changed"
    old = data["tokens"].get(_ciba_key(mis))
    if old is not None and not (
        isinstance(old, dict) and "token" in old and "expiresAt" in old
    ):
        return False, "schema_changed"
    data["tokens"][_ciba_key(mis)] = entry
    data["updatedAt"] = int(time.time() * 1000)
    try:
        shutil.copy2(path, path + ".keeper-bak")
        tmp = path + ".keeper-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)  # 官方 saveToDisk 同为 2 空格缩进
        os.replace(tmp, path)
    except OSError:
        return False, "io_error"
    return True, "ok"


# ── 通用 ─────────────────────────────────────────────────────────────
def atomic_write_json(path: str, obj, mode: int = 0o600) -> None:
    """原子写 JSON：tmp → chmod → os.replace；父目录 0700。

    tmp 文件名按 pid + 纳秒时间戳 + 随机后缀唯一：固定名会让并发写者
    互相截断对方的 tmp，写出半份 JSON。写完（或失败）清理自己的 tmp。
    """
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{time.time_ns()}.{secrets.token_hex(4)}"
    created = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            if created and os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def token_id(tok: str) -> str:
    """身份判据（等值比较），不可与仅供日志展示的 fingerprint 混用。

    fingerprint 只含前 6 字符与长度，JWT header 相同必然撞车。
    """
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()[:16]


def fingerprint(tok: str) -> str:
    """日志/输出唯一允许的 token 形态：前 6 字符 + 长度。"""
    return f"{tok[:6]}…({len(tok)}B)"


# ── cookie 文件铸票通用件（mint_bi_token / mint_dsx_token 共享）──────
def _read_keeper_state() -> dict:
    try:
        with open(os.path.expanduser(STATE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def pick_subject_token(verbose: bool = False) -> tuple:
    """铸票 subject：keeper state 的 at（优先，最新）→ auth-cache 的 ciba 条目 token。

    返回 (token, source)，无可用票时 (None, None)。
    """
    now = time.time() * 1000
    st = _read_keeper_state()
    if st.get("at") and st.get("expiresAt", 0) > now:
        _v(verbose, f"subject 取自 keeper state（剩余 {round((st['expiresAt'] - now) / 3600000, 1)}h）")
        return st["at"], "keeper-state"
    entry = read_ciba_entry(resolve_mis())
    if entry and entry.get("token") and entry.get("expiresAt", 0) > now:
        rem = round((entry["expiresAt"] - now) / 3600000, 1)
        _v(verbose, f"subject 取自 auth-cache ciba 条目（剩余 {rem}h）")
        return entry["token"], "auth-cache"
    return None, None


def backup_file(path: str, backup_dir: str | None = None) -> str | None:
    """备份现有 cookie 文件，返回备份路径（无旧文件 → None）。

    backup_dir=None：备份落原文件同目录（{path}.bak.{ts}），适用于
    ~/.meituan_sso/ 这类无目录扫描风险的场景。
    backup_dir 非空：备份落指定目录（{dir}/{basename}.bak.{ts}）——dsx 的
    token 目录会被消费端字典序扫描取第一个有效 .json，备份绝不能留在
    ~/.dsx/product/ 里（实测结论 #9，见 dsx 回写方案）。
    """
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return None
    ts = int(time.time())
    if backup_dir:
        backup_dir = os.path.expanduser(backup_dir)
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
        backup = os.path.join(backup_dir, f"{os.path.basename(path)}.bak.{ts}")
    else:
        backup = f"{path}.bak.{ts}"
    shutil.copy2(path, backup)
    try:
        os.chmod(backup, 0o600)  # 备份含 token 明文，copy2 继承原权限可能过宽，统一收紧
    except OSError:
        pass
    return backup


def write_cookie_file(path: str, cookie_str: str, mode: int = 0o600) -> None:
    """写 cookie 文件：JSON 顶层是 **str**（"n1=v1;n2=v2"），非 dict；
    tmp → chmod → os.replace 原子写（消费端 writeFileSync 非原子，撕裂读
    风险由这一侧消除）；父目录 0700。"""
    path = os.path.expanduser(path)
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = path + ".mint-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cookie_str, f, ensure_ascii=False)  # 顶层是 str，勿写成 dict
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def rollback(path: str, backup: str | None) -> None:
    """还原 cookie 文件：有备份则 copy 回；无备份（首铸）则删除刚写的坏文件。"""
    path = os.path.expanduser(path)
    if backup:
        if os.path.isfile(backup):
            shutil.copy2(backup, path)
            print(f"[core] 已回滚到备份 {backup}", file=sys.stderr)
    elif os.path.isfile(path):
        # 首铸失败（无旧文件）：坏 cookie 不能留盘，删除刚写的文件
        os.remove(path)
        print(f"[core] 无旧文件可回滚，已删除刚写的坏 cookie: {path}", file=sys.stderr)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """redirect_request 返回 None → 30x 由 urllib 以 HTTPError(30x) 上抛。

    等价于 requests allow_redirects=False。必要性：垃圾 cookie 下 SSO URL 会
    302 → 登录页 → urllib 默认跟随后拿到 200，误判健康通过（0820 复核修复）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HEALTH_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def health_probe(cookie_str: str, urls: list) -> tuple:
    """逐个 URL 探测 cookie 有效性，返回 (全通过?, 失败明细列表)。

    301/302/303/307/308/超时/非 200 均算失败（不跟随重定向，8s 硬超时）。
    """
    failures = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"Cookie": cookie_str})
            with _HEALTH_OPENER.open(req, timeout=8) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            failures.append(f"{url} → {type(e).__name__}: {e}")
            continue
        if code != 200:
            failures.append(f"{url} → HTTP {code}")
    return (not failures), failures


def _v(verbose: bool, msg: str) -> None:
    if verbose:
        print(f"[core] {msg}", file=sys.stderr)
