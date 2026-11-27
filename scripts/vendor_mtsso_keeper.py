#!/usr/bin/env python3
"""把 mtsso_keeper 的认证脚本白名单快照进 vendor/mtsso_keeper/。

只读源目录、只写本方白名单文件；不打包目录、不复制运行态凭据。

用法：
  python3 scripts/vendor_mtsso_keeper.py --sync    # 重新冻结快照
  python3 scripts/vendor_mtsso_keeper.py --check   # 校验快照与 SHA256SUMS/SOURCE.json 自洽
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "mtsso_keeper"
DEFAULT_SOURCE = Path("/Users/vincentwang/Documents/NoteVault/tools/mtsso_keeper")

# 组件 → 该组件运行所需的最小源码闭包。
# catpaw-ide：凭据产出/刷新；catpaw-cli-refresh：mcli 令牌保鲜（可选）。
COMPONENTS = {
    "catpaw-ide": ["catpaw_ide_session.py", "file_lock.py", "mtsso_core.py"],
    "catpaw-cli-refresh": ["mcli_token.py", "keeper_common.py", "mtsso_core.py",
                           "file_lock.py"],
}

# 绝不允许出现在快照里的名字（运行态/凭据/守护体系/缓存）。
FORBIDDEN_NAMES = {
    "keeper.py", "install.sh", "uninstall.sh",
    "sso_session.json", "catpaw_ide_session.json", "catpaw_ide_pending.json",
    ".config.yaml", "state.json", "auth.json",
}
FORBIDDEN_SUFFIXES = (".plist", ".tmpl", ".lock", ".log", ".bak", ".pyc")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _git(src: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(src), *args],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def planned_files() -> list[str]:
    return sorted({f for fs in COMPONENTS.values() for f in fs})


def sync(src: Path) -> int:
    missing = [f for f in planned_files() if not (src / f).is_file()]
    if missing:
        print(f"源目录缺少文件: {missing}", file=sys.stderr)
        return 1
    bad = [f for f in planned_files()
           if f in FORBIDDEN_NAMES or f.endswith(FORBIDDEN_SUFFIXES)]
    if bad:
        print(f"白名单命中禁止项: {bad}", file=sys.stderr)
        return 1

    VENDOR.mkdir(parents=True, exist_ok=True)
    entries = {}
    for name in planned_files():
        s, d = src / name, VENDOR / name
        d.write_bytes(s.read_bytes())
        os.chmod(d, 0o644)
        entries[name] = {
            "sha256": _sha256(d),
            "bytes": d.stat().st_size,
            "source_mtime": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(s.stat().st_mtime)),
        }

    dirty = bool(_git(src, "status", "--porcelain", "--", "."))
    source = {
        "schema": 1,
        "component": "mtsso_keeper",
        "source_repo": "NoteVault",
        "source_path": "tools/mtsso_keeper",
        "source_commit": _git(src, "log", "-1", "--format=%H", "--", "."),
        "source_commit_date": _git(src, "log", "-1", "--format=%cI", "--", "."),
        "source_dirty": dirty,
        "snapshot_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "vendored_by": "tools/model_proxy/scripts/vendor_mtsso_keeper.py --sync",
        "components": {k: list(v) for k, v in COMPONENTS.items()},
        "files": entries,
        "notes": (["快照取自 keeper 工作区（含未提交改动）；keeper 冻结后应重跑 --sync 重新冻结。"]
                  if dirty else []),
    }
    (VENDOR / "SOURCE.json").write_text(
        json.dumps(source, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (VENDOR / "SHA256SUMS").write_text(
        "\n".join(f"{entries[f]['sha256']}  {f}" for f in planned_files()) + "\n",
        encoding="utf-8")
    print(f"已冻结 {len(entries)} 个文件 → {VENDOR}")
    if dirty:
        print("注意：源为脏工作区，SOURCE.json 已标记 source_dirty=true")
    return 0


def check() -> int:
    problems = []
    sums_path = VENDOR / "SHA256SUMS"
    src_path = VENDOR / "SOURCE.json"
    if not sums_path.is_file() or not src_path.is_file():
        print(f"快照不完整：缺少 SHA256SUMS 或 SOURCE.json（{VENDOR}）", file=sys.stderr)
        return 1

    source = json.loads(src_path.read_text(encoding="utf-8"))
    recorded = source.get("files", {})

    expected = planned_files()
    for name in expected:
        p = VENDOR / name
        if not p.is_file():
            problems.append(f"缺失文件: {name}")
            continue
        if name not in recorded:
            problems.append(f"SOURCE.json 未登记: {name}")
            continue
        if _sha256(p) != recorded[name]["sha256"]:
            problems.append(f"内容与 SOURCE.json 不符: {name}")

    on_disk = sorted(p.name for p in VENDOR.iterdir()
                     if p.is_file() and p.name not in ("SOURCE.json", "SHA256SUMS", "README.md", "NOTICE.md"))
    for extra in on_disk:
        if extra not in expected:
            problems.append(f"未登记的多余文件: {extra}")
    for name in on_disk:
        if name in FORBIDDEN_NAMES or name.endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"禁止分发的文件: {name}")

    listed = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        listed[name] = digest
    for name in expected:
        if listed.get(name) != recorded.get(name, {}).get("sha256"):
            problems.append(f"SHA256SUMS 与 SOURCE.json 不一致: {name}")

    if problems:
        print("快照校验失败：", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    print(f"快照校验通过：{len(expected)} 个文件，来源 commit "
          f"{source.get('source_commit', '')[:12]}"
          f"{'（脏工作区快照）' if source.get('source_dirty') else ''}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="vendor mtsso_keeper 认证脚本快照")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sync", action="store_true", help="从源目录重新冻结快照")
    g.add_argument("--check", action="store_true", help="校验快照自洽")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE), help="mtsso_keeper 源目录")
    args = ap.parse_args()
    if args.sync:
        return sync(Path(args.source).expanduser())
    return check()


if __name__ == "__main__":
    sys.exit(main())
