"""Descriptor-bound config snapshots with observable race detection.

Metadata checks cannot prove reliability of network/cloud filesystems or defeat
writers able to restore metadata; deployment should use a local filesystem.
Windows identity semantics require native testing.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from typing import Any

from .discovery import PreflightError, verify_local_filesystem_fd


class ConfigSnapshotError(RuntimeError):
    """A sanitized failure to read a stable configuration snapshot."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FileMarker:
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    size: int


@dataclass(frozen=True)
class ConfigSnapshot:
    config: dict[str, Any]
    marker: Any
    # Only Mac business configuration carries this extra path/protection proof.
    evidence: Any = None


def _platform_gate() -> None:
    if os.name not in ("posix", "nt"):
        raise ConfigSnapshotError("file_identity_unavailable")


def _require_reliable_filesystem() -> None:
    """Hook retained for deployments that enforce a local-filesystem policy.

    Portable Python cannot prove that a filesystem is local or coherent.
    """
    return None


def _marker(info: os.stat_result) -> FileMarker:
    if not stat.S_ISREG(info.st_mode):
        raise ConfigSnapshotError("config_read_error")
    values = tuple(getattr(info, attr, None) for attr in
                   ("st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns", "st_size"))
    if (any(not isinstance(value, int) for value in values)
            or values[0] < 0 or values[1] <= 0 or values[4] < 0):
        raise ConfigSnapshotError("file_identity_unavailable")
    return FileMarker(*values)


def _decode_json_object(data: bytes, *, reject_duplicate_keys: bool) -> dict[str, Any]:
    """Decode the already-read bytes without disclosing keys or contents."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    try:
        text = data.decode("utf-8")
        if reject_duplicate_keys:
            value = json.loads(text, object_pairs_hook=unique_object)
        else:
            value = json.loads(text)
    except (UnicodeError, ValueError, RecursionError):
        raise ConfigSnapshotError("config_parse_error") from None
    if not isinstance(value, dict):
        raise ConfigSnapshotError("config_parse_error")
    return value


def _windows_snapshot(path, *, with_data: bool, reject_duplicate_keys: bool = False):
    """Windows same-handle identity/ChangeTime and guarded parent evidence."""
    import ntpath
    from core.platform_windows import read_guarded_file, WindowsReadError
    try:
        target = ntpath.abspath(os.fspath(path))
        value = read_guarded_file(target, with_data=with_data)
        marker = FileMarker(*value.marker)
        if not with_data:
            return marker
        config = _decode_json_object(value.data, reject_duplicate_keys=reject_duplicate_keys)
        return ConfigSnapshot(config, marker)
    except WindowsReadError as exc:
        if exc.code == "file_changed":
            raise ConfigSnapshotError("config_unstable") from None
        if exc.code == "release_unknown":
            raise ConfigSnapshotError("filesystem_unverified") from None
        raise ConfigSnapshotError("config_read_error") from None
    except (OSError, TypeError, ValueError):
        raise ConfigSnapshotError("config_read_error") from None


def current_file_marker(path: str | os.PathLike[str]) -> FileMarker:
    """Read a descriptor-bound current marker on a verified local filesystem."""
    _platform_gate()
    if os.name == "nt":
        return _windows_snapshot(path, with_data=False)
    _require_reliable_filesystem()
    try:
        with open(path, "rb") as stream:
            verify_local_filesystem_fd(stream.fileno())
            marker = _marker(os.fstat(stream.fileno()))
            if _marker(os.stat(path)) != marker:
                raise ConfigSnapshotError("config_unstable")
            return marker
    except PreflightError:
        raise ConfigSnapshotError("filesystem_unverified") from None
    except OSError:
        raise ConfigSnapshotError("config_read_error") from None


def read_stable_config(path: str | os.PathLike[str], *, reject_duplicate_keys: bool = False) -> ConfigSnapshot:
    """Read one JSON object, rejecting observable changes before return.

    The same open descriptor is used for the pre/post-read identity.  The path
    is checked against that descriptor after reading and again immediately
    before return. Duplicate-key rejection is opt-in for ledger callers;
    ordinary configuration retains its existing parsing behavior.
    No file contents, paths or raw exceptions enter errors.
    """
    if type(reject_duplicate_keys) is not bool:
        raise TypeError("reject_duplicate_keys must be bool")
    _platform_gate()
    if os.name == "nt":
        return _windows_snapshot(path, with_data=True, reject_duplicate_keys=reject_duplicate_keys)
    _require_reliable_filesystem()
    try:
        with open(path, "rb") as stream:
            verify_local_filesystem_fd(stream.fileno())
            before = _marker(os.fstat(stream.fileno()))
            data = stream.read()
            after = _marker(os.fstat(stream.fileno()))
            current = _marker(os.stat(path))
            if before != after or before != current:
                raise ConfigSnapshotError("config_unstable")
            config = _decode_json_object(data, reject_duplicate_keys=reject_duplicate_keys)
            if _marker(os.stat(path)) != before:
                raise ConfigSnapshotError("config_unstable")
    except PreflightError:
        raise ConfigSnapshotError("filesystem_unverified") from None
    except OSError:
        raise ConfigSnapshotError("config_read_error") from None
    return ConfigSnapshot(config=config, marker=before)


# Only fixed internal categories are exposed to callers. Never return exception
# text, paths, OS messages, or unknown lower-layer codes across control APIs.
_MAC_READ_FAILURES = frozenset({
    "config_read_error", "file_not_found", "permission_denied",
    "invalid_path", "invalid_cwd", "invalid_link", "not_directory",
    "not_regular_file", "too_many_links", "file_permissions_unsafe",
    "parent_writable", "parent_owner_unverified", "sticky_entry_owner_unverified",
    "link_acl_present",
})
_MAC_CHANGED_FAILURES = frozenset({
    "config_unstable", "config_changed", "identity_changed", "directory_changed",
    "target_changed", "parent_changed", "link_changed", "link_protection_changed",
    "protection_changed",
})


def mac_config_error_code(exc) -> str:
    """Map known Mac proof failures to sanitized existing reload categories.

    Unknown protection, filesystem and close results fail closed as unverified;
    they must not be mislabeled as observable identity/content races.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _MAC_READ_FAILURES:
        return "config_read_error"
    if isinstance(code, str) and code in _MAC_CHANGED_FAILURES:
        return "config_unstable"
    return "filesystem_unverified"


def read_mac_business_config(path: str | os.PathLike[str]) -> ConfigSnapshot:
    """Read/parse only a Mac business config, with the original chain proof.

    This is deliberately separate from read_stable_config: ledger and runtime
    callers retain their existing duplicate-key and protection policies.
    """
    from .mac_config_path import MacConfigError, capture_config, verify_config
    try:
        evidence = capture_config(path)
        config = _decode_json_object(evidence.data, reject_duplicate_keys=False)
        verify_config(evidence)
        return ConfigSnapshot(config, evidence.marker, evidence)
    except MacConfigError as exc:
        raise ConfigSnapshotError(mac_config_error_code(exc)) from None


def mac_business_marker(path: str | os.PathLike[str]):
    """Probe complete path/ACL/content evidence without advancing a marker."""
    from .mac_config_path import MacConfigError, capture_config
    try:
        return capture_config(path).marker
    except MacConfigError as exc:
        raise ConfigSnapshotError(mac_config_error_code(exc)) from None


def verify_mac_business_config(snapshot: ConfigSnapshot) -> None:
    """Revalidate the exact proof and bytes attached to a parsed snapshot."""
    from .mac_config_path import MacConfigError, verify_config
    try:
        if snapshot.evidence is None or snapshot.marker != snapshot.evidence.marker:
            raise ConfigSnapshotError("config_unstable")
        verify_config(snapshot.evidence)
    except MacConfigError as exc:
        raise ConfigSnapshotError(mac_config_error_code(exc)) from None
