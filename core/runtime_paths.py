"""Parse the single runtime-path configuration without touching its targets.

This module is deliberately not a lifecycle or ownership check.  Callers must
still perform the read-only identity/anchor preflight before opening, creating,
locking, or writing any returned path. Legacy POSIX values are compatibility
mappings, not permission to inspect targets.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

PATH_KEYS = ("log", "totals", "lock", "pid", "ensure_log", "start_lock")
DEFAULTS: Mapping[str, str] = {
    "log": ".runtime/model_proxy.log",
    "totals": ".runtime/model_proxy_totals.json",
    "lock": ".runtime/model_proxy.lock",
    "pid": ".runtime/model_proxy.pid",
    "ensure_log": ".runtime/model_proxy_ensure.log",
    "start_lock": ".runtime/model_proxy_start.lock",
}
LEGACY_DEFAULTS: Mapping[str, str] = {
    "log": ".model_proxy.log",
    "totals": ".model_proxy_totals.json",
    "lock": "/tmp/model_proxy.lock",
    "pid": "/tmp/model_proxy.pid",
    "ensure_log": "/tmp/model_proxy_ensure.log",
    "start_lock": "/tmp/model_proxy_start.lock",
}
_WINDOWS_LEGACY_NAMES = {
    "lock": "model_proxy.lock",
    "pid": "model_proxy.pid",
    "ensure_log": "model_proxy_ensure.log",
    "start_lock": "model_proxy_start.lock",
}
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


class RuntimePathsError(ValueError):
    """A runtime path file/value is unavailable or cannot be interpreted safely."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ResolvedRuntimePaths(dict):
    """Paths plus the exact legacy values observed while parsing.

    This provenance only permits consideration of the existing legacy parent
    creation protocol; it is not write authorization or filesystem evidence.
    A plain dictionary deliberately carries no such provenance.
    """

    def __init__(self, values=(), *, windows_legacy_keys=()):
        super().__init__(values)
        self.windows_legacy_keys = frozenset(windows_legacy_keys)

    def map_values(self, convert):
        return ResolvedRuntimePaths(
            ((key, convert(value)) for key, value in self.items()),
            windows_legacy_keys=self.windows_legacy_keys)


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RuntimePathsError("invalid_value", f"Invalid runtime path field: {field}")
    return value


def _windows_path(value: str, *, field: str) -> PureWindowsPath:
    # Device paths and drive-relative paths depend on OS state not represented
    # by the installation root.  Do not guess their interpretation.
    path = PureWindowsPath(value)
    if value.startswith(("\\\\?\\", "\\\\.\\")):
        raise RuntimePathsError("unsupported_path", f"Unsupported runtime path field: {field}")
    if (path.drive or path.root) and not path.is_absolute():
        raise RuntimePathsError("unsupported_path", f"Ambiguous runtime path field: {field}")
    for part in path.parts[1 if path.anchor else 0:]:
        if part in (".", ".."):
            continue
        if (any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or part.endswith((" ", "."))
                or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED):
            raise RuntimePathsError("unsupported_path", f"Unsupported runtime path field: {field}")
    return path


def parse_runtime_paths(
    raw: Any,
    *,
    project_root: str | PurePath,
    platform: str | None = None,
    local_appdata: str | None = None,
    defaults: Mapping[str, str] | None = None,
) -> dict[str, PurePath]:
    """Resolve the six configured values without stat/open/mkdir on any target.

    A valid object missing exactly one known key uses that key's default.
    More missing keys are rejected until their compatibility is decided.
    Unknown keys remain ignored.  ``defaults`` is injectable so OS tests can
    use isolated paths instead of ever opening the production legacy defaults.
    """
    if not isinstance(raw, dict):
        raise RuntimePathsError("invalid_object", "Runtime paths must be a JSON object")
    values = DEFAULTS if defaults is None else defaults
    if set(values) != set(PATH_KEYS):
        raise RuntimePathsError("invalid_defaults", "Invalid runtime path defaults")
    missing = [key for key in PATH_KEYS if key not in raw]
    if len(missing) > 1:
        raise RuntimePathsError("multiple_missing", "Multiple runtime path fields are missing")

    platform = platform or ("windows" if os.name == "nt" else "posix")
    if platform not in ("posix", "windows"):
        raise RuntimePathsError("unsupported_platform", "Unsupported path platform")
    if platform == "windows":
        root = _windows_path(str(project_root), field="project_root")
        if not root.is_absolute():
            raise RuntimePathsError("invalid_root", "Project root must be absolute")
    else:
        root = PurePosixPath(str(project_root))
        if not root.is_absolute() or "\x00" in str(root):
            raise RuntimePathsError("invalid_root", "Project root must be absolute")

    result: dict[str, PurePath] = {}
    legacy_keys = set()
    for key in PATH_KEYS:
        value = _text(raw[key] if key in raw else values[key], field=key)
        if platform == "windows":
            if key in _WINDOWS_LEGACY_NAMES and value == LEGACY_DEFAULTS[key]:
                legacy_keys.add(key)
                appdata = _windows_path(_text(local_appdata, field="LOCALAPPDATA"),
                                        field="LOCALAPPDATA")
                if not appdata.is_absolute():
                    raise RuntimePathsError("invalid_local_appdata", "LOCALAPPDATA must be absolute")
                path = appdata / "model_proxy" / "runtime" / _WINDOWS_LEGACY_NAMES[key]
            else:
                # Only the four exact legacy values are mapped.  An arbitrary
                # POSIX absolute path is not a Windows-root-relative path.
                if value.startswith("/"):
                    raise RuntimePathsError("unsupported_path", f"Unsupported runtime path field: {key}")
                path = _windows_path(value, field=key)
        else:
            path = PurePosixPath(value)
        result[key] = path if path.is_absolute() else root / path
    return ResolvedRuntimePaths(result, windows_legacy_keys=legacy_keys)


def read_runtime_paths(
    paths_file: str | os.PathLike[str],
    *,
    project_root: str | PurePath,
    platform: str | None = None,
    local_appdata: str | None = None,
    defaults: Mapping[str, str] | None = None,
) -> dict[str, PurePath]:
    """Read the configured file; failure never triggers whole-file defaults."""
    try:
        with Path(paths_file).open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise RuntimePathsError("unreadable_file", "Runtime paths file is unavailable or invalid") from None
    return parse_runtime_paths(raw, project_root=project_root, platform=platform,
                               local_appdata=local_appdata, defaults=defaults)
