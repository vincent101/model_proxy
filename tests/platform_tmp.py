"""跨平台临时目录：Windows 上必须落在「ACL 可判私有」的位置。

**为什么不能用 `tempfile.TemporaryDirectory()` 直接测 Windows 分支**

产品的 Windows 配置/账本读取要求目标文件的 DACL 只含当前用户、SYSTEM、
Administrators（`core/windows_acl.py` 的 `private` 策略）。GitHub Windows
runner 的 `%TEMP%` 目录（`C:\\Users\\RUNNER~1\\AppData\\Local\\Temp`）ACL 是

    BUILTIN\\Administrators:(OI)(CI)(F)
    OWNER RIGHTS:(OI)(CI)(F)

没有显式用户 SID，于是所有继承它的临时文件都被判 `principal_untrusted`，
`read_guarded_file` 转成 `read_unavailable`，测试看到的是 `config_read_error`。
实测（2026-11-23，windows-latest）同一台机器上：

    %TEMP%          → 失败
    %LOCALAPPDATA%  → 成功（Administrators + <machine>\\<user>）
    %USERPROFILE%   → 成功
    D:\\a\\... workspace → 失败（含 NT AUTHORITY\\Authenticated Users / BUILTIN\\Users）

所以这里的「私有临时目录」在 Windows 走 `%LOCALAPPDATA%`，POSIX 保持系统临时
目录不变。这**不是**放宽产品校验：产品对 ACL 的要求没有改，只是让测试夹具站
在与生产配置文件相同的位置（用户目录之下）。
"""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from pathlib import Path

_BASE_ENV = ("LOCALAPPDATA", "USERPROFILE")


def base_dir() -> str | None:
    """Windows 上返回一个 ACL 含当前用户 SID 的父目录；POSIX 返回 None。"""
    if os.name != "nt":
        return None
    for name in _BASE_ENV:
        value = os.environ.get(name)
        if value and os.path.isdir(value):
            return value
    return None


class TemporaryDirectory:
    """`tempfile.TemporaryDirectory` 的跨平台替代（同名 API）。

    Windows 上建在 `%LOCALAPPDATA%`（见模块说明），POSIX 上行为不变。

    **为什么不是 `tempfile.mkdtemp`**：实测（windows-latest，2026-11-23）
    `mkdtemp` 与 `Path.mkdir()` 在同一父目录下产出的 DACL 不同——

        mkdtemp  → BUILTIN\\Administrators:(OI)(CI)(F) + OWNER RIGHTS:(OI)(CI)(F)
        mkdir    → BUILTIN\\Administrators:(OI)(CI)(F) + <machine>\\<user>:(OI)(CI)(F)

    （上面为反斜杠原样展示，判断 ACL 时看的是显式用户 SID。）

    前者没有显式用户 SID（`OWNER RIGHTS` 是 S-1-3-4，产品不认），后者可以
    通过产品的 `private` 判定。所以这里用 mkdir 自己拼随机名。
    """

    def __init__(self, suffix: str = "", prefix: str = "mp-test-", dir: str | None = None):
        base = dir if dir is not None else (base_dir() or tempfile.gettempdir())
        while True:
            candidate = Path(base) / f"{prefix}{secrets.token_hex(6)}{suffix}"
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            break
        # mkdir(mode=...) 在 Windows 上会得到 OWNER RIGHTS 而非显式用户 ACE
        # （实测：helper 版 mkdir(mode=0o700) → Administrators + OWNER RIGHTS，
        # 同一父目录下 mkdir() → Administrators + <machine><user>）。所以先不传
        # mode，再在 POSIX 上补 chmod（Windows 的 chmod 只切只读位，无副作用）。
        if os.name != "nt":
            try:
                candidate.chmod(0o700)
            except OSError:
                pass
        self.name = str(candidate)

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *exc) -> None:
        self.cleanup()


def path(prefix: str = "mp-test-") -> Path:
    """建一个私有临时目录并返回 Path（调用方负责清理）。"""
    return Path(TemporaryDirectory(prefix=prefix).name)
