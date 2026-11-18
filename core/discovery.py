"""Read-only runtime path identity/conflict preflight.

It does not prove ACLs or prevent same-user replacement races. Windows
filesystem and reparse-point semantics require native testing.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable, Mapping

from core.runtime_paths import PATH_KEYS


class PreflightError(RuntimeError):
    """A path conflict, unstable path, or unprovable filesystem identity."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PathIdentity:
    """Internal evidence; paths and metadata must not be printed publicly."""

    requested: str
    canonical: str
    exists: bool
    device: int | None
    inode: int | None
    ancestor_device: int
    ancestor_inode: int
    unresolved: tuple[str, ...]
    trace: tuple[tuple, ...]
    final_was_link: bool
    mode: int | None
    uid: int | None
    gid: int | None
    link_count: int | None


@dataclass(frozen=True)
class PathPreflight:
    project_root: str
    identities: tuple[tuple[str, PathIdentity], ...]

    def as_dict(self) -> dict[str, PathIdentity]:
        return dict(self.identities)


@dataclass(frozen=True)
class WindowsPathPreflight(PathPreflight):
    """Immutable Windows observations, without any temporary HANDLEs.

    Evidence alone is not authority: Windows lock acquisition must reopen and
    revalidate all roles while holding live path leases.
    """
    windows_evidence: tuple[tuple[str, object], ...] = field(repr=False)

    def windows_dict(self) -> dict[str, object]:
        return dict(self.windows_evidence)


class _WindowsNativeBridge:
    """Cross-check path backend volume identity and persistent ACL support."""
    def __init__(self):
        from core.windows_paths import NativeWin32
        from core.platform_windows import _NativeAPI
        self._paths = NativeWin32()
        self._volume = _NativeAPI()
        self.release_unknown = False

    def open_existing(self, path, access, share, flags):
        return self._paths.open_existing(path, access, share, flags)

    def close(self, handle):
        try:
            return self._paths.close(handle)
        except BaseException:
            self.release_unknown = True
            raise

    def file_info(self, handle):
        return self._paths.file_info(handle)

    def volume_info(self, drive):
        # The platform adapter additionally requires FILE_PERSISTENT_ACLS.
        first = self._paths.volume_info(drive)
        second = self._volume.volume_info(drive + "\\")
        if first != second:
            raise OSError("volume evidence changed")
        return first


class _WindowsPathMonitor:
    """Record release failures even when a nested backend sanitizes them."""
    def __init__(self, backend):
        self._backend = backend
        self._release_unknown = False

    @property
    def release_unknown(self):
        native = getattr(self._backend, "_win32", None)
        return self._release_unknown or getattr(native, "release_unknown", False) is True

    def _call(self, name, *args):
        try:
            return getattr(self._backend, name)(*args)
        except BaseException as exc:
            if name == "close" or getattr(exc, "code", None) in (
                    "release_unknown", "close_failed", "resource_release_failed", "handle_close_failed"):
                self._release_unknown = True
            raise

    def open_root(self, drive): return self._call("open_root", drive)
    def open_child(self, parent, name): return self._call("open_child", parent, name)
    def observe(self, handle, policy, creation_kind=None):
        return self._call("observe", handle, policy, creation_kind)
    def close(self, handle): return self._call("close", handle)


def _windows_path_backend():
    """Native backend factory; tests may substitute an in-memory backend."""
    from core.windows_paths import WindowsPathBackend
    return WindowsPathBackend(win32=_WindowsNativeBridge())


def _windows_identity(requested: str, evidence) -> PathIdentity:
    """Expose the existing PathIdentity API without inventing POSIX modes."""
    last = evidence.components[-1].observation
    exists = evidence.exists
    trace = tuple((component.name, component.observation.volume,
                   component.observation.file_id, component.observation.kind,
                   component.observation.links, component.observation.reparse,
                   component.policy, component.creation_kind) for component in evidence.components)
    return PathIdentity(
        requested=requested, canonical=requested, exists=exists,
        device=last.volume if exists else None,
        inode=int.from_bytes(last.file_id, "big") if exists else None,
        ancestor_device=last.volume, ancestor_inode=int.from_bytes(last.file_id, "big"),
        unresolved=evidence.missing, trace=trace, final_was_link=False,
        mode=None, uid=None, gid=None, link_count=last.links if exists else None)


def _windows_close_leases(leases) -> None:
    """Each lease gets one close attempt, even when another close fails."""
    failed = False
    for lease in reversed(tuple(leases)):
        try:
            lease.close()
        except BaseException:
            failed = True
        if getattr(getattr(lease, "_backend", None), "release_unknown", False) is True:
            failed = True
    if failed:
        raise PreflightError("release_unknown", "Windows path evidence release is unknown")


def _windows_evidence_contains(parent, child) -> bool:
    """Conservative Windows containment on simultaneously held evidence.

    All leases are revalidated before and after the complete alias scan. This
    mirrors windows_paths' Unicode/case and 8.3 uncertainty, without reopening
    every lease quadratically. No frozen snapshot alone is an authority.
    """
    def prefix(left, right):
        if len(left) > len(right):
            return False
        return all(_component_may_alias(a, b) or "~" in a or "~" in b
                   for a, b in zip(left, right))
    for index, first in enumerate(parent.components):
        for other_index, second in enumerate(child.components):
            if ((first.observation.volume, first.observation.file_id)
                    != (second.observation.volume, second.observation.file_id)):
                continue
            left = tuple(x.name for x in parent.components[index + 1:]) + parent.missing
            right = tuple(x.name for x in child.components[other_index + 1:]) + child.missing
            if prefix(left, right):
                return True
    return False


def _windows_trusted_boundary(project_root, target, *, initial_missing_parents=()):
    """Select an existing path boundary, not an authorization to write."""
    import ntpath
    root = PureWindowsPath(os.fspath(project_root))
    path = PureWindowsPath(os.fspath(target))
    if (root.drive.upper() == path.drive.upper()
            and path.parts[1:len(root.parts)] == root.parts[1:]):
        return str(root)
    if root.drive.upper() == path.drive.upper():
        # A spelling mismatch does not establish that a path is outside the
        # project: Windows volumes may be case-sensitive, case-insensitive,
        # or expose short names.  The lower-level boundary API requires an
        # exact prefix and cannot prove these two spellings are one object.
        # Refuse the ambiguous classification instead of selecting the
        # weaker external-parent boundary.
        common = zip(root.parts[1:], path.parts[1:])
        if all(_component_may_alias(left, right) or "~" in left or "~" in right
               for left, right in common):
            raise PreflightError("windows_boundary_unknown", "Windows path boundary is ambiguous")
    parent = path.parent
    if ntpath.normcase(str(parent)) in initial_missing_parents:
        # The four explicitly recognized legacy mappings alone may create
        # their two known directories below an already selected LOCALAPPDATA.
        return str(parent.parent.parent)
    return str(parent)


def _windows_open_preflight(project_root, runtime_paths, *, light=False, initial_missing_parents=()):
    """Open and retain all Windows role leases; caller must close them.

    All comparisons use simultaneously held native path leases. The returned
    snapshot contains only immutable evidence; leases are separate and local.
    Missing custom runtime parents are rejected; the fixed state directory may
    be absent during the initial read-only installation preflight.
    """
    from core import windows_paths
    if set(runtime_paths) != set(PATH_KEYS):
        raise PreflightError("invalid_paths", "Runtime path fields are incomplete")
    try:
        raw_root = os.fspath(project_root)
        raw_values = {name: os.fspath(runtime_paths[name]) for name in PATH_KEYS}
        if not isinstance(raw_root, str) or any(not isinstance(value, str) for value in raw_values.values()):
            raise ValueError("invalid path")
        root = str(PureWindowsPath(raw_root))
        values = {name: str(PureWindowsPath(raw_values[name])) for name in PATH_KEYS}
        if root != raw_root or any(values[name] != raw_values[name] for name in PATH_KEYS):
            raise ValueError("ambiguous path")
        backend = _WindowsPathMonitor(_windows_path_backend())
    except Exception:
        raise PreflightError("windows_evidence_unavailable", "Windows path evidence unavailable") from None
    state = str(PureWindowsPath(root) / ".runtime")
    paths = dict(values)
    paths["state_dir"] = state
    paths["tmp_dir"] = str(PureWindowsPath(state) / "tmp")
    paths["backups_dir"] = str(PureWindowsPath(state) / "backups")
    # A light observation skips optional records elsewhere, but every fixed
    # record name must still participate in role-collision checks. Omitting
    # install.json/last_good_paths.json here would let a runtime role masquerade
    # as a fixed record during first-start/status preflight.
    fixed = ("anchor.lock", "control.lock", "install.json", "active.json",
             "last_good_paths.json")
    for name in fixed:
        paths[name] = str(PureWindowsPath(state) / name)
    leases = {}
    try:
        leases["__root__"] = windows_paths.inspect(root, backend=backend, kind="directory", private=False,
                                                   trusted_boundary=root)
        if not leases["__root__"].evidence.exists:
            raise PreflightError("invalid_root", "Project root is unavailable")
        for name, path in paths.items():
            kind = "directory" if name in ("state_dir", "tmp_dir", "backups_dir") else "file"
            private = True
            fixed_single = name in ("anchor.lock", "control.lock", "lock", "install.json", "active.json", "last_good_paths.json")
            try:
                lease = windows_paths.inspect(
                    path, backend=backend, kind=kind, private=private, single_link=fixed_single,
                    trusted_boundary=_windows_trusted_boundary(
                        root, path, initial_missing_parents=initial_missing_parents))
            except windows_paths.PathEvidenceError as exc:
                if exc.code == "boundary_unavailable":
                    raise PreflightError("parent_missing", "A runtime target parent is unavailable") from None
                # Legacy start_lock may be a directory with an ancestor
                # policy rather than a private-file ACL. The fallback still
                # independently verifies type, identity and protection.
                if name != "start_lock" or exc.code not in ("invalid_type", "observation_unavailable"):
                    raise
                lease = windows_paths.inspect(
                    path, backend=backend, kind="directory", private=False,
                    trusted_boundary=_windows_trusted_boundary(
                        root, path, initial_missing_parents=initial_missing_parents))
            leases[name] = lease
        for name in PATH_KEYS:
            if not leases[name].evidence.direct_parent_observed:
                # Only the fixed, not-yet-created state container may be
                # missing at the read-only first-start preflight.
                import ntpath
                parent = ntpath.normcase(ntpath.dirname(paths[name]))
                fixed_missing = (not leases["state_dir"].evidence.exists
                                 and parent == ntpath.normcase(state))
                if not fixed_missing and parent not in initial_missing_parents:
                    raise PreflightError("parent_missing", "A runtime target parent is unavailable")
        # Hold all leases and bracket the pure alias scan with revalidation.
        for lease in leases.values():
            lease.revalidate()
        for name in PATH_KEYS:
            state_evidence, target_evidence = leases["state_dir"].evidence, leases[name].evidence
            if _windows_evidence_contains(target_evidence, state_evidence):
                raise PreflightError("state_conflict", "A runtime target overlaps the fixed state directory")
            if _windows_evidence_contains(state_evidence, target_evidence):
                state_parts = PureWindowsPath(state).parts
                target_parts = PureWindowsPath(paths[name]).parts
                exact_prefix = target_parts[:len(state_parts)] == state_parts
                observed_prefix = False
                if state_evidence.exists:
                    fixed = state_evidence.components[-1].observation
                    observed_prefix = any(
                        (item.observation.volume, item.observation.file_id)
                        == (fixed.volume, fixed.file_id) for item in target_evidence.components)
                if not (exact_prefix or observed_prefix):
                    raise PreflightError("state_conflict", "Runtime container identity is unknown")
        names = tuple(paths)
        for index, left in enumerate(names):
            for right in names[index + 1:]:
                first, second = leases[left].evidence, leases[right].evidence
                forward = _windows_evidence_contains(first, second)
                reverse = _windows_evidence_contains(second, first)
                if forward and reverse:
                    raise PreflightError("same_identity", "Two runtime roles refer to the same object")
                if (left != "state_dir" and right != "state_dir" and (forward or reverse)):
                    raise PreflightError("nested_targets", "Runtime targets overlap")
        for lease in leases.values():
            lease.revalidate()
        if backend.release_unknown:
            raise PreflightError("release_unknown", "Windows path evidence release is unknown")
        # Auxiliary directories participate in every conflict scan, but may
        # legitimately be created while a service is running.  Do not turn
        # their presence into a frozen lifetime lock snapshot.
        auxiliary = {"tmp_dir", "backups_dir"}
        if light:
            # They participate in the held collision scan above, but retain
            # the existing light-snapshot contract for locator state updates.
            auxiliary.update(("install.json", "last_good_paths.json"))
        identities = tuple((name, _windows_identity(path, leases[name].evidence))
                           for name, path in paths.items() if name not in auxiliary)
        evidence = tuple((name, lease.evidence) for name, lease in leases.items()
                         if name not in auxiliary)
        return WindowsPathPreflight(root, identities, evidence), leases
    except BaseException as exc:
        try:
            _windows_close_leases(leases.values())
        except PreflightError:
            raise
        if backend.release_unknown:
            raise PreflightError("release_unknown", "Windows path evidence release is unknown") from None
        if isinstance(exc, PreflightError):
            raise
        if isinstance(exc, Exception):
            raise PreflightError("windows_evidence_unavailable", "Windows path evidence unavailable") from None
        raise


def _verify_identity(info: os.stat_result) -> None:
    if (not isinstance(info.st_dev, int) or info.st_dev < 0
            or not isinstance(info.st_ino, int) or info.st_ino <= 0):
        raise PreflightError("unknown_path", "Filesystem identity is unavailable")


def _metadata(path: Path) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError:
        raise PreflightError("unknown_path", "A path or its parent cannot be verified") from None
    _verify_identity(info)
    return info


def _path_identity(path: Path, *, fixed: bool, kind: str) -> PathIdentity:
    """Resolve existing symlinks manually and record each observed parent.

    Missing ordinary suffixes are allowed.  A symlink whose target is missing,
    an inaccessible component, or an ambiguous missing/.. traversal is not.
    All operations are read-only; this is not a substitute for a later recheck.
    """
    if not path.is_absolute() or (os.name == "posix" and str(path).startswith("//")):
        raise PreflightError("invalid_path", "A runtime path is not an unambiguous absolute path")
    # Each item carries whether it came from a symlink target.  Such components
    # must exist; otherwise the link is dangling and identity is unknown.
    pending = deque((part, False) for part in path.parts[1:])
    current = Path(path.anchor)
    trace: list[tuple] = []
    links_seen = 0
    origin_final_link = False
    final_stat: os.stat_result | None = None
    ancestor_stat: os.stat_result | None = None
    unresolved: tuple[str, ...] = ()
    missing_suffix = False
    while pending:
        component, required = pending.popleft()
        if component in ("", "."):
            continue
        if component == "..":
            current = current.parent
            final_stat = None
            continue
        candidate = current / component
        is_original_final = not pending and not required
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            if required:
                raise PreflightError("dangling_link", "A symlink target is unavailable") from None
            # Once a parent is absent, no later child can be inspected.  A ..
            # following that missing component is ambiguous and is refused.
            suffix = [component] + [part for part, _ in pending]
            if any(part == ".." for part in suffix):
                raise PreflightError("unknown_path", "A missing parent has an ambiguous suffix") from None
            ancestor_stat = _metadata(current)
            if not stat.S_ISDIR(ancestor_stat.st_mode):
                raise PreflightError("unknown_path", "A missing target parent is not a directory")
            observed = next((item for item in reversed(trace) if item[0] == str(current)), None)
            if observed is not None and (observed[1], observed[2]) != (ancestor_stat.st_dev, ancestor_stat.st_ino):
                raise PreflightError("unknown_path", "A target parent changed during preflight")
            unresolved = tuple(suffix)
            current = current.joinpath(*suffix)
            missing_suffix = True
            final_stat = None
            pending.clear()
            break
        except OSError:
            raise PreflightError("unknown_path", "A path or its parent cannot be verified") from None
        _verify_identity(st)
        link = stat.S_ISLNK(st.st_mode)
        target = None
        if link:
            try:
                target = os.readlink(candidate)
            except OSError:
                raise PreflightError("unknown_path", "A symlink cannot be verified") from None
        # APFS directory link counts change when children are created; they
        # do not identify the directory. File link counts remain verified.
        trace.append((str(candidate), st.st_dev, st.st_ino, st.st_mode, st.st_uid,
                      st.st_gid, None if stat.S_ISDIR(st.st_mode) else st.st_nlink, target))
        if link:
            if is_original_final:
                origin_final_link = True
                if fixed:
                    raise PreflightError("fixed_link", "A fixed state target is a symlink")
            links_seen += 1
            if links_seen > 40 or not target:
                raise PreflightError("unknown_path", "A symlink chain cannot be verified")
            target_path = Path(target)
            target_parts = target_path.parts
            if target_path.is_absolute():
                current = Path(target_path.anchor)
                target_parts = target_parts[1:]
            for part in reversed(target_parts):
                pending.appendleft((part, True))
            final_stat = None
            continue
        if pending and not stat.S_ISDIR(st.st_mode):
            raise PreflightError("invalid_type", "A runtime path parent is not a directory")
        current = candidate
        final_stat = st

    # Handle a path ending in / or .. without guessing whether it is a file.
    if final_stat is None and not missing_suffix:
        final_stat = _metadata(current)
    if final_stat is not None:
        ancestor_stat = final_stat
        if kind == "directory" and not stat.S_ISDIR(final_stat.st_mode):
            raise PreflightError("invalid_type", "A fixed state directory is not a directory")
        if kind == "file" and not stat.S_ISREG(final_stat.st_mode):
            raise PreflightError("invalid_type", "A runtime target is not a regular file")
        if kind == "file_or_directory" and not (stat.S_ISREG(final_stat.st_mode)
                                                or stat.S_ISDIR(final_stat.st_mode)):
            raise PreflightError("invalid_type", "A legacy lock target has an unsupported type")
        if fixed and kind == "file" and final_stat.st_nlink != 1:
            raise PreflightError("fixed_hardlink", "A fixed state file has an unsafe link count")
    if ancestor_stat is None:
        raise PreflightError("unknown_path", "A target ancestor cannot be identified")
    return PathIdentity(
        requested=str(path), canonical=str(current), exists=final_stat is not None,
        device=final_stat.st_dev if final_stat else None,
        inode=final_stat.st_ino if final_stat else None,
        ancestor_device=ancestor_stat.st_dev, ancestor_inode=ancestor_stat.st_ino,
        unresolved=unresolved,
        trace=tuple(trace), final_was_link=origin_final_link,
        mode=final_stat.st_mode if final_stat else None,
        uid=final_stat.st_uid if final_stat else None,
        gid=final_stat.st_gid if final_stat else None,
        link_count=(final_stat.st_nlink if final_stat and not stat.S_ISDIR(final_stat.st_mode)
                    else None),
    )


def _component_may_alias(left: str, right: str) -> bool:
    """Do not assume a volume's case/Unicode rules for unresolved names.

    Equal NFD/casefold forms can alias.  Different pure-ASCII forms are
    distinguishable under case-sensitive and case-insensitive rules.  For
    other Unicode mismatches, conservatively treat equivalence as unknown;
    this can reject distinct paths until a platform adapter proves otherwise.
    """
    left_folded = unicodedata.normalize("NFD", unicodedata.normalize("NFD", left).casefold())
    right_folded = unicodedata.normalize("NFD", unicodedata.normalize("NFD", right).casefold())
    if left_folded == right_folded:
        return True
    return not (left.isascii() and right.isascii())


def _components_prefix(parent: tuple[str, ...], child: tuple[str, ...]) -> bool:
    return len(parent) <= len(child) and all(
        _component_may_alias(left, right) for left, right in zip(parent, child)
    )


def _contains(parent: str, child: str) -> bool:
    return _components_prefix(Path(parent).parts, Path(child).parts)


def _identity_contains(parent: PathIdentity, child: PathIdentity) -> bool:
    if _contains(parent.canonical, child.canonical):
        return True
    # A shared existing ancestor can have multiple names (e.g. mount aliases).
    # Compare only the unresolved suffix after that verified physical anchor.
    if ((parent.ancestor_device, parent.ancestor_inode)
            == (child.ancestor_device, child.ancestor_inode)):
        return _components_prefix(parent.unresolved, child.unresolved)
    return False


def _identity_same(left: PathIdentity, right: PathIdentity) -> bool:
    if len(Path(left.canonical).parts) == len(Path(right.canonical).parts) and _contains(left.canonical, right.canonical):
        return True
    if ((left.ancestor_device, left.ancestor_inode) == (right.ancestor_device, right.ancestor_inode)
            and len(left.unresolved) == len(right.unresolved)):
        return _components_prefix(left.unresolved, right.unresolved)
    return False


def inspect_runtime_identities(
    project_root: str | os.PathLike[str],
    runtime_paths: Mapping[str, str | os.PathLike[str]],
    *,
    windows_light: bool = False,
) -> PathPreflight:
    """Read-only same-file and same-directory preflight.

    Success only records the observed snapshot.  It does not verify ACLs,
    acquire anchor/legacy locks, prevent races, or authorize data writes.
    """
    if os.name not in ("posix", "nt"):
        raise PreflightError("unsupported_platform", "Native file identity is not available on this platform")
    if os.name == "nt":
        if windows_light:
            snapshot, leases = _windows_open_preflight(project_root, runtime_paths, light=True)
        else:
            snapshot, leases = _windows_open_preflight(project_root, runtime_paths)
        _windows_close_leases(leases.values())
        return snapshot
    if set(runtime_paths) != set(PATH_KEYS):
        raise PreflightError("invalid_paths", "Runtime path fields are incomplete")
    try:
        root = Path(project_root)
        if not root.is_absolute():
            raise PreflightError("invalid_root", "Project root is not absolute")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise PreflightError("invalid_root", "Project root is not a directory")
    except (OSError, RuntimeError, ValueError):
        raise PreflightError("unknown_root", "Project root cannot be verified") from None
    state_dir = root / ".runtime"
    candidates: dict[str, tuple[Path, bool, str]] = {
        key: (Path(runtime_paths[key]), False,
              "file_or_directory" if key == "start_lock" else "file")
        for key in PATH_KEYS
    }
    candidates["state_dir"] = (state_dir, True, "directory")
    candidates["tmp_dir"] = (state_dir / "tmp", True, "directory")
    candidates["backups_dir"] = (state_dir / "backups", True, "directory")
    for name in ("anchor.lock", "control.lock", "install.json", "active.json", "last_good_paths.json"):
        candidates[name] = (state_dir / name, True, "file")
    identities = {name: _path_identity(path, fixed=fixed, kind=kind)
                  for name, (path, fixed, kind) in candidates.items()}

    for name in PATH_KEYS:
        target = identities[name]
        state = identities["state_dir"]
        if _identity_contains(target, state):
            raise PreflightError("state_conflict", "A runtime target overlaps the fixed state directory")
        if _identity_contains(state, target):
            state_parts, target_parts = Path(state.canonical).parts, Path(target.canonical).parts
            exact_prefix = target_parts[:len(state_parts)] == state_parts
            observed_prefix = (state.exists and any(
                (entry[1], entry[2]) == (state.device, state.inode) for entry in target.trace))
            if not (exact_prefix or observed_prefix):
                raise PreflightError("state_conflict", "Runtime container identity is unknown")

    names = list(identities)
    for index, left_name in enumerate(names):
        left = identities[left_name]
        for right_name in names[index + 1:]:
            right = identities[right_name]
            if _identity_same(left, right) or (
                left.exists and right.exists
                and (left.device, left.inode) == (right.device, right.inode)
            ):
                raise PreflightError("same_identity", "Two runtime roles refer to the same object")
            # Two runtime roles cannot require a file/legacy-lock directory
            # inside each other.  The fixed records intentionally live in the
            # fixed state directory and are checked separately above.
            if left_name != "state_dir" and right_name != "state_dir" and (
                _identity_contains(left, right)
                or _identity_contains(right, left)
            ):
                raise PreflightError("nested_targets", "Runtime targets overlap")
    for name in ("log", "totals"):
        if identities[name].final_was_link:
            raise PreflightError("atomic_target_symlink", "An atomic-write runtime target is a symlink")
    return PathPreflight(str(root), tuple((name, identity) for name, identity in identities.items()
                                          if name not in ("tmp_dir", "backups_dir")))


def windows_initial_missing_parents(runtime_paths, local_appdata):
    """Only the existing four exact legacy mappings may provision their parent."""
    import ntpath
    from core.runtime_paths import ResolvedRuntimePaths, _WINDOWS_LEGACY_NAMES
    if not local_appdata or not ntpath.isabs(os.fspath(local_appdata)):
        return ()
    if not isinstance(runtime_paths, ResolvedRuntimePaths):
        return ()
    runtime = ntpath.join(ntpath.normpath(os.fspath(local_appdata)), "model_proxy", "runtime")
    for key, name in _WINDOWS_LEGACY_NAMES.items():
        if (key in runtime_paths.windows_legacy_keys
                and ntpath.normcase(os.fspath(runtime_paths[key])) == ntpath.normcase(ntpath.join(runtime, name))):
            return (ntpath.normcase(runtime),)
    return ()


def prepare_windows_runtime_directories(project_root, runtime_paths, local_appdata, *,
                                        ensure_directory=None):
    """Create/verify only the fixed state and mapped default runtime directories.

    All custom runtime parents must already exist. Native creation is injected
    for tests; the production primitive verifies private DACLs and never
    repairs an existing directory.
    """
    import ntpath
    if ensure_directory is None:
        try:
            from core.platform_windows import ensure_private_directory as ensure_directory
        except ImportError:
            raise PreflightError("windows_private_creation_unavailable", "Directory creation unavailable") from None
    if not callable(ensure_directory) or set(runtime_paths) != set(PATH_KEYS):
        raise PreflightError("windows_private_creation_unavailable", "Directory creation unavailable")
    root = ntpath.normpath(os.fspath(project_root))
    if not ntpath.isabs(root):
        raise PreflightError("invalid_root", "Project root is unavailable")
    selected = [(ntpath.join(root, ".runtime"), root)]
    allowed = windows_initial_missing_parents(runtime_paths, local_appdata)
    if local_appdata:
        appdata = ntpath.normpath(os.fspath(local_appdata))
        # The runtime parser already rejects invalid LOCALAPPDATA when an
        # actual legacy default needs it; custom paths do not depend on it.
        if ntpath.isabs(appdata):
            base = ntpath.join(appdata, "model_proxy")
            runtime = ntpath.join(base, "runtime")
            if allowed:
                boundary = _windows_trusted_boundary(
                    root, ntpath.join(runtime, "boundary-probe"),
                    initial_missing_parents=allowed)
                selected.extend(((base, boundary), (runtime, boundary)))
    # Before any creation, observe all roles, including unresolved suffixes.
    # The only additional missing parents are the exact legacy mappings above.
    _, leases = _windows_open_preflight(root, runtime_paths, initial_missing_parents=allowed)
    _windows_close_leases(leases.values())
    for directory, boundary in selected:
        try:
            result = ensure_directory(directory, trusted_boundary=boundary)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "release_unknown" or getattr(exc, "release_unknown", False):
                code = "windows_creation_release_unknown"
            elif getattr(exc, "created", False) is None:
                code = "windows_creation_unknown"
            elif getattr(exc, "created", False) is True:
                code = "windows_directory_created_unverified"
            else:
                code = "windows_private_directory_unverified"
            raise PreflightError(code, "Private runtime directory unavailable") from None
        created = getattr(result, "created", None)
        identity = getattr(result, "identity", None)
        protection = getattr(result, "protection", None)
        if created is None:
            raise PreflightError("windows_creation_unknown", "Private runtime directory unavailable")
        if (type(created) is not bool or not isinstance(identity, tuple) or len(identity) != 4
                or any(type(value) is not int or value < 0 for value in identity)
                or identity[1] <= 0 or not isinstance(protection, tuple) or not protection):
            code = "windows_directory_created_unverified" if created is True else "windows_private_directory_unverified"
            raise PreflightError(code, "Private runtime directory unavailable")
    # Creation is not evidence that targets/parents are still the same.
    _, leases = _windows_open_preflight(root, runtime_paths)
    _windows_close_leases(leases.values())


def prepare_windows_log(project_root, runtime_paths, snapshot, *, create_empty_file=None,
                        inspect=None):
    """Create a missing empty log only after config/role conflict preflight."""
    if snapshot.as_dict()["log"].exists:
        return snapshot
    if create_empty_file is None:
        try:
            from core.platform_windows import create_private_empty_file as create_empty_file
        except ImportError:
            raise PreflightError("windows_private_creation_unavailable", "Log creation unavailable") from None
    if inspect is None:
        inspect = inspect_runtime_identities
    expected_identity = None
    try:
        target = snapshot.as_dict()["log"].canonical
        boundary = _windows_trusted_boundary(project_root, target)
        result = create_empty_file(target, trusted_boundary=boundary)
        expected_identity = getattr(result, "identity", None)
        if (getattr(result, "created", None) is not True
                or not isinstance(expected_identity, tuple) or len(expected_identity) != 4
                or any(type(value) is not int or value < 0 for value in expected_identity)
                or expected_identity[1] <= 0 or not isinstance(getattr(result, "protection", None), tuple)
                or not result.protection):
            code = ("windows_log_created_unverified" if getattr(result, "created", None) is True
                    else "windows_creation_unknown")
            raise PreflightError(code, "Log creation not verified")
    except PreflightError:
        raise
    except Exception as exc:
        # A racer may have created the path; only a definite already-existing
        # result can be re-inspected. Unknown creation effects are not retried.
        if getattr(exc, "code", None) != "already_exists" or getattr(exc, "created", None) is not False:
            if getattr(exc, "release_unknown", False):
                code = "windows_creation_release_unknown"
            elif getattr(exc, "created", False) is None:
                code = "windows_creation_unknown"
            elif getattr(exc, "created", False) is True:
                code = "windows_log_created_unverified"
            else:
                code = "windows_log_create_unverified"
            raise PreflightError(code, "Private log creation unavailable") from None
    fresh = inspect(project_root, runtime_paths, windows_light=True)
    log = fresh.as_dict()["log"]
    if (not log.exists or expected_identity is not None
            and (log.device, log.inode) != expected_identity[:2]):
        raise PreflightError("windows_log_create_unverified", "Log creation not verified")
    return fresh


# Instance records are observations, not proof of ownership or authority to
# signal a process/send credentials. The consumer must independently verify
# process identity, held locks, installation trust and the connected peer.
INSTANCE_SCHEMA = 2


def process_start_identity(pid: int) -> dict | None:
    """Best-effort OS creation identity, or None when unavailable.

    Darwin layout comes from the system proc_bsdinfo header. Windows uses
    GetProcessTimes. Both must be independently re-read by a consumer; OS
    behavior/permissions, especially Windows, need native validation.
    """
    import ctypes
    import sys
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            class ProcBsdInfo(ctypes.Structure):
                _fields_ = [(f"field_{i}", ctypes.c_uint32) for i in range(12)] + [
                    ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
                    (f"tail_{i}", ctypes.c_uint32) for i in range(6)] + [
                    ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            call = lib.proc_pidinfo
            call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                             ctypes.c_void_p, ctypes.c_int]
            call.restype = ctypes.c_int
            info = ProcBsdInfo()
            if call(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
                return None
            if info.field_3 != pid or not info.start_sec:
                return None
            return {"kind": "darwin_proc_bsdinfo", "seconds": info.start_sec,
                    "microseconds": info.start_usec}
        if sys.platform == "win32":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.GetProcessTimes.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
                if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                              ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    return None
                ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return {"kind": "windows_filetime", "ticks_100ns": ticks} if ticks else None
            finally:
                kernel.CloseHandle(handle)
    except (OSError, AttributeError, ValueError, OverflowError):
        return None
    return None


def process_executable(pid: int) -> str | None:
    """Best-effort observed image path; not a same-handle authorization.

    Callers must separately guard PID reuse; Windows termination in particular
    requires identity checks and the operation on the same verified handle.
    """
    import ctypes
    import sys
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            call = lib.proc_pidpath
            call.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            call.restype = ctypes.c_int
            output = ctypes.create_string_buffer(4096)
            if call(pid, output, len(output)) <= 0:
                return None
            return os.fsdecode(output.value)
        if sys.platform == "win32":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                output = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(output))
                if not kernel.QueryFullProcessImageNameW(handle, 0, output, ctypes.byref(size)):
                    return None
                return output.value
            finally:
                kernel.CloseHandle(handle)
    except (OSError, AttributeError, ValueError, OverflowError):
        return None
    return None


def verify_local_filesystem_fd(fd: int) -> None:
    """Require a locally mounted APFS/HFS filesystem on a Darwin descriptor.

    fstatfs cannot detect cloud-sync agents sharing a locally mounted tree;
    that remains an explicit operator attestation and unsupported deployment.
    """
    import ctypes
    import sys
    if sys.platform != "darwin" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise PreflightError("filesystem_unverified", "Filesystem cannot be verified")
    class StatFS(ctypes.Structure):
        _fields_ = [("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32)] + [
            (name, ctypes.c_uint64) for name in ("blocks", "bfree", "bavail", "files", "ffree")
        ] + [("fsid", ctypes.c_int32 * 2), ("owner", ctypes.c_uint32),
             ("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
             ("subtype", ctypes.c_uint32), ("fstype", ctypes.c_char * 16),
             ("mnton", ctypes.c_char * 1024), ("mntfrom", ctypes.c_char * 1024),
             ("flags_ext", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 7)]
    try:
        lib = ctypes.CDLL(None, use_errno=True)
        call = lib.fstatfs64
        call.argtypes = [ctypes.c_int, ctypes.POINTER(StatFS)]
        call.restype = ctypes.c_int
        info = StatFS()
        if (call(fd, ctypes.byref(info)) != 0 or not (info.flags & 0x1000)
                or info.flags & 0x00200000 or info.fstype not in (b"apfs", b"hfs")):
            raise PreflightError("filesystem_unverified", "Filesystem cannot be verified")
    except PreflightError:
        raise
    except (OSError, AttributeError, ValueError):
        raise PreflightError("filesystem_unverified", "Filesystem cannot be verified") from None


def verify_no_extended_acl(fd: int) -> bytes | None:
    """Strict for files; directories may have a validated deny-only ACL.

    The complete external ACL bytes are read twice on the same descriptor and
    compared. Kept under the historical name for existing lock/record callers.
    """
    if os.name == "nt":
        from core import platform_windows, windows_acl
        try:
            handle = platform_windows._NativeFDAPI().handle(fd)
            info = platform_windows._api().file_info(handle)
            is_directory = bool(info.attributes & platform_windows.FILE_ATTRIBUTE_DIRECTORY)
            kind = "directory" if is_directory else "file"
            required = (0x000200A1 if is_directory else
                        windows_acl.FILE_GENERIC_READ | windows_acl.FILE_GENERIC_WRITE)
            evidence = windows_acl.read_stable_evidence(handle, kind=kind,
                                                        required_access=required, policy="private")
            observation = evidence.observation
            # Comparable evidence, not a chmod shortcut and not a path proof.
            return repr((observation.owner, observation.group, observation.user,
                         observation.token_source, observation.token_statistics,
                         observation.control, observation.revision, observation.dacl_present,
                         observation.dacl_null, observation.acl_header,
                         tuple((ace.ace_type, ace.flags, ace.mask, ace.sid, ace.raw)
                               for ace in observation.aces))).encode()
        except windows_acl.ACLVerificationError as exc:
            if exc.code in ("resource_release_failed", "handle_close_failed"):
                raise PreflightError("release_unknown", "Windows ACL release is unknown") from None
            raise PreflightError("protection_unavailable", "ACL verification unavailable") from None
        except Exception:
            raise PreflightError("protection_unavailable", "ACL verification unavailable") from None
    from core.darwin_acl import ACLVerificationError, read_acl_snapshot
    try:
        return read_acl_snapshot(fd)
    except ACLVerificationError:
        raise PreflightError("protection_unavailable", "ACL verification unavailable") from None


def _verify_directory(path: Path, *, private: bool, _capture=None) -> os.stat_result:
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        before = path.lstat()
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISDIR(before.st_mode) or not stat.S_ISDIR(opened.st_mode)
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    or opened.st_uid not in (os.geteuid(), 0)):
                raise PreflightError("protection_unavailable", "Directory identity unavailable")
            if private:
                if opened.st_uid != os.geteuid() or opened.st_mode & 0o077:
                    raise PreflightError("protection_unavailable", "Private directory is not protected")
            elif opened.st_mode & 0o022:
                raise PreflightError("protection_unavailable", "Directory is writable by others")
            acl_before = verify_no_extended_acl(fd)
            verify_local_filesystem_fd(fd)
            acl_after = verify_no_extended_acl(fd)
            if acl_before != acl_after:
                raise PreflightError("protection_changed", "Directory ACL changed")
            after = os.fstat(fd)
            current = path.lstat()
            def marker(st):
                if not stat.S_ISDIR(st.st_mode) or not hasattr(st, "st_flags") or st.st_nlink < 1:
                    raise PreflightError("protection_unavailable", "Directory identity unavailable")
                return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_flags)
            if (marker(before) != marker(opened) or marker(opened) != marker(after)
                    or marker(after) != marker(current)):
                raise PreflightError("protection_changed", "Directory protection changed")
            if _capture is not None:
                _capture.append((str(path), marker(after), acl_after))
            return opened
        finally:
            os.close(fd)
    except PreflightError:
        raise
    except (OSError, AttributeError):
        raise PreflightError("protection_unavailable", "Directory protection unavailable") from None


def _windows_record_directory(state_dir, *, capture=None):
    """Read-only fixed-state directory identity and full held-chain evidence."""
    from core import windows_paths
    state = PureWindowsPath(os.fspath(state_dir))
    if not state.is_absolute() or state.name != ".runtime":
        raise PreflightError("protection_unavailable", "Invalid fixed directory")
    lease = None
    monitor = None
    result = None
    error = None
    try:
        monitor = _WindowsPathMonitor(_windows_path_backend())
        lease = windows_paths.inspect(str(state), backend=monitor, kind="directory", private=True,
                                      trusted_boundary=str(state.parent))
        if not lease.evidence.exists or len(lease.evidence.components) < 2:
            raise PreflightError("protection_unavailable", "Fixed directory unavailable")
        lease.revalidate()
        parent = lease.evidence.components[-2].observation
        if parent.kind != "directory" or parent.reparse:
            raise PreflightError("protection_unavailable", "Project identity unavailable")
        result = (parent.volume, int.from_bytes(parent.file_id, "big"), lease.evidence)
    except BaseException as exc:
        error = exc
    failed = False
    if lease is not None:
        try:
            lease.close()
        except BaseException:
            failed = True
    if failed or (monitor is not None and monitor.release_unknown):
        raise PreflightError("release_unknown", "Windows path evidence release is unknown") from None
    if error is not None:
        if isinstance(error, PreflightError):
            raise error
        if isinstance(error, Exception):
            raise PreflightError("protection_unavailable", "Directory protection unavailable") from None
        raise error
    if capture is not None:
        capture.append(("windows", result[2]))
    return result[0], result[1]


def _windows_read_instance_record(state_dir, name):
    import json
    from core.platform_windows import WindowsReadError, read_guarded_file
    if name not in ("install.json", "active.json", "last_good_paths.json"):
        raise PreflightError("record_name_invalid", "Invalid instance record name")
    state = PureWindowsPath(os.fspath(state_dir))
    before = []
    verify_record_directory(state, _capture=before)
    try:
        value = read_guarded_file(str(state / name), max_bytes=65536,
                                  trusted_boundary=str(state.parent))
    except WindowsReadError as exc:
        if exc.code == "file_missing":
            value = None
        elif exc.code == "release_unknown":
            raise PreflightError("release_unknown", "Windows record release is unknown") from None
        else:
            raise PreflightError("record_unavailable", "Instance record unavailable") from None
    after = []
    verify_record_directory(state, _capture=after)
    if before != after:
        raise PreflightError("protection_changed", "Directory protection changed during record read")
    if value is None:
        return None
    try:
        record = json.loads(value.data.decode("utf-8"))
        if not _valid_record(record, PureWindowsPath(name).stem, state):
            raise ValueError("invalid record")
        return record
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise PreflightError("record_unavailable", "Instance record invalid") from None


def verify_record_directory(state_dir: str | os.PathLike[str], *, _capture=None) -> tuple[int, int]:
    """Verify the fixed directory and its canonical project/ancestor chain.

    This validates POSIX ownership/modes and accepts only absent or deny-only
    directory ACLs on macOS; it is not proof against a same-user process or later
    replacement. Returns the canonical project directory identity.
    """
    import sys
    if os.name == "nt":
        return _windows_record_directory(state_dir, capture=_capture)
    if sys.platform != "darwin":
        raise PreflightError("protection_unavailable", "ACL verification unavailable")
    state = Path(state_dir)
    if not state.is_absolute() or state.name != ".runtime":
        raise PreflightError("protection_unavailable", "Invalid fixed directory")
    try:
        root = state.parent.resolve(strict=True)
        if state != root / state.name:
            raise PreflightError("protection_unavailable", "Noncanonical fixed directory")
        first = []
        root_info = _verify_directory(root, private=False, _capture=first)
        _verify_directory(state, private=True, _capture=first)
        second = []
        _verify_directory(root, private=False, _capture=second)
        _verify_directory(state, private=True, _capture=second)
        if first != second:
            raise PreflightError("protection_changed", "Directory protection changed")
        if _capture is not None:
            _capture.extend(first)
        return root_info.st_dev, root_info.st_ino
    except PreflightError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise PreflightError("protection_unavailable", "Directory protection unavailable") from None


def _record_target(state_dir: Path, name: str) -> Path:
    if name not in ("install.json", "active.json", "last_good_paths.json"):
        raise PreflightError("record_name_invalid", "Invalid instance record name")
    return state_dir / name


def _darwin_unlinked_record_replaced(fd: int, path: Path, opened: os.stat_result) -> bool:
    """Recognize only an unlinked trusted fd replaced by a protected file.

    This is retry evidence, never an ACL verdict or permission to return data.
    Both the fd and current path are checked twice; any uncertainty is false.
    """
    import sys
    if os.name != "posix" or sys.platform != "darwin":
        return False
    try:
        uid = os.geteuid()
        def protected_regular(value):
            return (stat.S_ISREG(value.st_mode) and value.st_nlink == 1
                    and value.st_uid == uid and not value.st_mode & 0o077
                    and isinstance(value.st_dev, int) and value.st_dev >= 0
                    and isinstance(value.st_ino, int) and value.st_ino > 0)
        def fd_marker(value):
            return (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                    value.st_gid, value.st_nlink, value.st_ctime_ns)
        def path_marker(value):
            return (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                    value.st_gid, value.st_nlink, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns)
        if not protected_regular(opened):
            return False
        first_fd = os.fstat(fd)
        if (not stat.S_ISREG(first_fd.st_mode) or first_fd.st_nlink != 0
                or (first_fd.st_dev, first_fd.st_ino, first_fd.st_mode,
                    first_fd.st_uid, first_fd.st_gid) !=
                   (opened.st_dev, opened.st_ino, opened.st_mode,
                    opened.st_uid, opened.st_gid)):
            return False
        first_path = path.lstat()
        if (not protected_regular(first_path)
                or (first_path.st_dev, first_path.st_ino) == (opened.st_dev, opened.st_ino)):
            return False
        second_fd = os.fstat(fd)
        second_path = path.lstat()
        return (fd_marker(first_fd) == fd_marker(second_fd)
                and protected_regular(second_path)
                and path_marker(first_path) == path_marker(second_path))
    except (OSError, AttributeError, TypeError, ValueError, OverflowError):
        return False


def _check_record_target(path: Path) -> dict | None:
    """Read/validate an existing observation; absence is not proof of stop."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise PreflightError("record_unavailable", "Instance record unavailable") from None
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
            (os.name == "posix" and (st.st_uid != os.geteuid() or st.st_mode & 0o077))):
        raise PreflightError("record_unavailable", "Instance record protection unavailable")
    # An existing unknown/corrupt file is never silently overwritten.
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            # Only this open, after the protected lstat, proves a changed
            # observation. Retrying must repeat the entire record read.
            import errno
            if os.name == "posix" and exc.errno == errno.ENOENT:
                raise PreflightError("record_changed", "Instance record changed") from None
            raise
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) != (st.st_dev, st.st_ino):
                raise PreflightError("record_changed", "Instance record changed")
            try:
                verify_no_extended_acl(stream.fileno())
            except PreflightError as exc:
                # identity_unverified alone also describes pipes and closed or
                # otherwise invalid fds. Only a demonstrated replacement is
                # transient; retry must re-run the entire ACL/identity check.
                from core.darwin_acl import ACLVerificationError
                cause = exc.__context__
                if (exc.code == "protection_unavailable"
                        and type(cause) is ACLVerificationError
                        and cause.code == "identity_unverified"
                        and info.st_nlink in (0, 1)
                        and (info.st_mode, info.st_uid, info.st_gid,
                             getattr(info, "st_flags", None)) ==
                            (st.st_mode, st.st_uid, st.st_gid,
                             getattr(st, "st_flags", None))
                        # The opened fd may already be unlinked at its first
                        # fstat. `st` is the protected, linked pre-open path;
                        # its device/inode was just matched to this fd.
                        and _darwin_unlinked_record_replaced(stream.fileno(), path, st)):
                    raise PreflightError("record_changed", "Instance record changed") from None
                raise
            verify_local_filesystem_fd(stream.fileno())
            raw = stream.read(65537)
            after = os.fstat(stream.fileno())
            current = path.lstat()
            def marker(value):
                return (value.st_dev, value.st_ino, value.st_size,
                        value.st_mtime_ns, value.st_ctime_ns, value.st_mode, value.st_nlink)
            if marker(info) != marker(after) or marker(info) != marker(current):
                raise PreflightError("record_changed", "Instance record changed")
        import json
        if len(raw) > 65536:
            raise ValueError("record too large")
        data = json.loads(raw.decode("utf-8"))
        if (not isinstance(data, dict) or data.get("schema") != INSTANCE_SCHEMA
                or data.get("record") != path.stem
                or not _valid_record(data, path.stem, path.parent)):
            raise ValueError("unknown record")
        return data
    except PreflightError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise PreflightError("record_unavailable", "Instance record invalid") from None


def read_instance_record(state_dir: str | os.PathLike[str], name: str) -> dict | None:
    """Read one protected record, or None if absent; never proves stop/ownership."""
    if os.name == "nt":
        return _windows_read_instance_record(state_dir, name)
    state = Path(state_dir)
    before = []
    verify_record_directory(state, _capture=before)
    result = _check_record_target(_record_target(state, name))
    after = []
    verify_record_directory(state, _capture=after)
    if before != after:
        raise PreflightError("protection_changed", "Directory protection changed during record read")
    return result


def _same_record_directory_protection(before, after, state: Path) -> bool:
    """Compare directory identity, permissions, flags and complete ACL evidence."""
    return before == after


@dataclass(frozen=True)
class WindowsRecordRecovery:
    """Observed names only; no inference that a failed API was atomic."""
    target: str
    source: str
    old_other_name: str


class WindowsRecordCommitError(PreflightError):
    def __init__(self, code: str, *, stage: str, winerror=None, recovery=None,
                 temporary_created: bool | None = False, temporary_may_contain_data: bool | None = None):
        super().__init__(code, "Windows record commit requires review")
        self.stage = stage
        self.winerror = winerror
        self.recovery = recovery
        self.temporary_created = temporary_created
        self.temporary_may_contain_data = temporary_may_contain_data


def _windows_read_raw_record(path):
    from core.platform_windows import read_guarded_file, WindowsReadError
    try:
        return read_guarded_file(path, max_bytes=65536,
                                 trusted_boundary=str(PureWindowsPath(path).parent.parent))
    except WindowsReadError as exc:
        if exc.code == "file_missing":
            return None
        if exc.code == "release_unknown":
            raise PreflightError("release_unknown", "Windows record release is unknown") from None
        raise PreflightError("record_unavailable", "Instance record unavailable") from None


def _windows_record_observation(path, original, replacement):
    try:
        current = _windows_read_raw_record(path)
    except Exception:
        return "unknown"
    if current is None:
        return "missing"
    for label, expected in (("original", original), ("replacement", replacement)):
        if expected is not None and current.marker[:2] == expected.marker[:2]:
            return (label if current.marker == expected.marker and current.identity == expected.identity
                    and current.data == expected.data and current.protection == expected.protection
                    else label + "_changed")
    return "other"


def _windows_record_recovery(target, source, original, replacement, winerror):
    # Probe the two known names independently. 1177 may move the old file to
    # an unspecified third name; never scan/delete or invent that location.
    return WindowsRecordRecovery(
        _windows_record_observation(target, original, replacement),
        _windows_record_observation(source, original, replacement),
        "unknown" if winerror in (1176, 1177) else "not_assumed")


def _windows_record_snapshot_changed(before, after, name, replacement):
    from core.platform_lock import _windows_same_components
    old, new = before.windows_dict(), after.windows_dict()
    if old.keys() != new.keys():
        return False
    for role in old:
        if role != name and old[role] != new[role]:
            return False
    first, second = old[name], new[name]
    if not second.exists or not second.components or second.expected_kind != "file":
        return False
    result = second.components[-1].observation
    if (result.volume, int.from_bytes(result.file_id, "big")) != replacement.marker[:2]:
        return False
    if result.kind != "file" or result.reparse or result.links != 1:
        return False
    if not first.exists:
        return _windows_same_components(first, second)
    if (first.drive != second.drive or first.private != second.private
            or first.single_link != second.single_link
            or len(first.components) != len(second.components)
            or first.components[:-1] != second.components[:-1]
            or first.components[-1].name != second.components[-1].name):
        return False
    # Path backend protection excludes file ID and includes owner/DACL/token.
    return first.components[-1].observation.protection == result.protection


def _windows_write_instance_record(state_dir, name, record, *, anchor, legacy) -> None:
    import json
    import secrets
    from core import platform_lock as locks, platform_windows as win, windows_paths
    if name not in ("install.json", "active.json", "last_good_paths.json"):
        raise PreflightError("record_name_invalid", "Invalid instance record name")
    state = PureWindowsPath(os.fspath(state_dir))
    target = str(state / name)
    if (not isinstance(anchor, locks.HeldLock) or not isinstance(legacy, locks.HeldLock)
            or anchor is legacy):
        raise PreflightError("locks_unverified", "Instance locks are unavailable")
    with locks._fork_guard, anchor._state_lock, legacy._state_lock:
        scope = None
        post_scope = None
        temp_lease = None
        temp_owner = None
        source = None
        original = None
        temp = None
        temporary_created = False
        temporary_may_contain_data = None
        native_attempted = False
        native_success = False
        winerror = None
        snapshot = legacy.snapshot
        error = None
        try:
            verify_held_instance_locks(anchor, legacy)
            if (not isinstance(snapshot, WindowsPathPreflight)
                    or str(state) != snapshot.as_dict()["state_dir"].canonical):
                raise PreflightError("locks_unverified", "Lock installation does not match record")
            if (not isinstance(record, dict)
                    or (record.get("schema") != INSTANCE_SCHEMA
                        and not (name == "active.json" and record.get("schema") == 3))
                    or record.get("record") != PureWindowsPath(name).stem
                    or not _valid_record(record, PureWindowsPath(name).stem, state)):
                raise PreflightError("record_invalid", "Invalid instance record")
            data = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(data) > 65536:
                raise PreflightError("record_invalid", "Instance record is too large")
            identities = snapshot.as_dict()
            paths = {key: identities[key].requested for key in PATH_KEYS}
            light = "install.json" not in snapshot.as_dict()
            before, scope = _windows_open_preflight(snapshot.project_root, paths, light=True) if light else (
                _windows_open_preflight(snapshot.project_root, paths))
            if before != snapshot:
                raise PreflightError("preflight_changed", "Runtime paths changed")
            original = _windows_read_raw_record(target)
            if (original is None) == identities[name].exists:
                raise PreflightError("record_changed", "Instance record changed")
            if original is not None:
                if original.marker[:2] != (identities[name].device, identities[name].inode):
                    raise PreflightError("record_changed", "Instance record changed")
                try:
                    existing = json.loads(original.data.decode("utf-8"))
                    if not _valid_record(existing, PureWindowsPath(name).stem, state):
                        raise ValueError("invalid record")
                except (UnicodeError, ValueError, TypeError, RecursionError):
                    raise PreflightError("record_unavailable", "Instance record invalid") from None
            temp = str(state / (".record-" + secrets.token_hex(16) + ".tmp"))
            backend = scope["__root__"]._backend
            temp_lease = windows_paths.inspect(
                temp, backend=backend, kind="file", private=True, single_link=True,
                trusted_boundary=snapshot.project_root)
            if temp_lease.evidence.exists or not temp_lease.evidence.direct_parent_observed:
                raise PreflightError("record_temp_unavailable", "Temporary record unavailable")
            for role, lease in scope.items():
                if role in ("__root__", "state_dir"):
                    continue
                if (_windows_evidence_contains(temp_lease.evidence, lease.evidence)
                        or _windows_evidence_contains(lease.evidence, temp_lease.evidence)):
                    raise PreflightError("record_temp_conflict", "Temporary record conflicts with another role")
            for lease in (*scope.values(), temp_lease):
                lease.revalidate()
            _windows_close_leases((temp_lease,))
            temp_lease = None
            try:
                if record.get("schema") == 3:
                    # A locator may be the first child of a new directory; do
                    # not rely on inheritable ACEs to protect its contents.
                    from core import windows_acl
                    api = win._api()
                    with win.open_directory(str(state), backend=api) as parent:
                        ident = win.directory_identity(parent.handle, backend=api)
                        if (ident.volume_serial, ident.file_index) != (
                                identities["state_dir"].device, identities["state_dir"].inode):
                            raise PreflightError("record_directory_changed", "Record directory changed")
                        evidence = windows_acl.read_stable_evidence(
                            parent.handle, kind="directory", policy="private",
                            required_access=windows_acl.FILE_GENERIC_READ)
                        scope["state_dir"].revalidate()
                        temporary_created = None  # native create may fail after a side effect
                        handle = api.open_private_candidate(temp, user_sid=evidence.observation.user)
                        temp_owner = win.WindowsFileHandle(handle, api, created=True)
                        temporary_created = True
                else:
                    temp_owner = win.open_file(temp, create=True)
            except OSError as exc:
                created = getattr(exc, "created", None)
                if type(created) is bool:
                    temporary_created = created
                raise
            temporary_created = True
            proof = win.write_private_new_file(temp_owner, data)
            temporary_may_contain_data = True
            try:
                temp_owner.close()
            except BaseException:
                temp_owner = None  # never retry a possibly completed CloseHandle
                raise PreflightError("release_unknown", "Temporary record release is unknown") from None
            temp_owner = None
            source = _windows_read_raw_record(temp)
            if (source is None or source.marker[:2] != (proof.identity.volume_serial, proof.identity.file_index)
                    or source.data != data or source.protection != win.acl_protection(proof.acl)):
                raise PreflightError("record_temp_changed", "Temporary record changed")
            if original is not None and original.marker[0] != source.marker[0]:
                raise PreflightError("record_volume_changed", "Record volume changed")
            if source.marker[0] != identities["state_dir"].device:
                raise PreflightError("record_volume_changed", "Record volume changed")
            # Recheck all roles and the two locks, then close the destination
            # lease: ReplaceFileW cannot coexist with its no-DELETE sharing.
            for lease in scope.values():
                lease.revalidate()
            verify_held_instance_locks(anchor, legacy)
            target_lease = scope.pop(name)
            _windows_close_leases((target_lease,))
            if _windows_read_raw_record(target) != original or _windows_read_raw_record(temp) != source:
                raise PreflightError("record_changed", "Record changed before commit")
            for lease in scope.values():
                lease.revalidate()
            locks._windows_verify_guard(anchor._windows_guard, anchor._windows_lease)
            locks._windows_verify_guard(legacy._windows_guard, legacy._windows_lease)
            api = win._api()
            native_attempted = True
            try:
                if original is None:
                    api.move_new(temp, target)
                else:
                    api.replace_existing(target, temp)
                native_success = True
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                raise
            after_read = _windows_read_raw_record(target)
            expected_protection = original.protection if original is not None else source.protection
            expected_attributes = original.identity.attributes if original is not None else source.identity.attributes
            if (after_read is None or after_read.marker[:2] != source.marker[:2]
                    or after_read.data != data or after_read.protection != expected_protection
                    or after_read.identity.attributes != expected_attributes
                    or _windows_read_raw_record(temp) is not None):
                raise PreflightError("record_postcheck_failed", "Record committed but verification failed")
            for lease in scope.values():
                lease.revalidate()
            fresh, post_scope = _windows_open_preflight(snapshot.project_root, paths, light=True) if light else (
                _windows_open_preflight(snapshot.project_root, paths))
            if not _windows_record_snapshot_changed(snapshot, fresh, name, source):
                raise PreflightError("record_postcheck_failed", "Record committed but verification failed")
            locks._windows_verify_guard(anchor._windows_guard, post_scope["anchor.lock"])
            locks._windows_verify_guard(legacy._windows_guard, post_scope["lock"])
            for lease in post_scope.values():
                lease.revalidate()
            old_scope, post_scope = post_scope, None
            _windows_close_leases(old_scope.values())
            old_scope, scope = scope, None
            _windows_close_leases(old_scope.values())
            # Snapshot update is deliberately limited to this proven record
            # replacement and happens under both held-lock mutexes.
            anchor.snapshot = fresh
            legacy.snapshot = fresh
            verify_held_instance_locks(anchor, legacy)
        except BaseException as exc:
            error = exc
            if hasattr(exc, "content_may_exist"):
                temporary_may_contain_data = bool(exc.content_may_exist)
        release_failed = False
        if temp_owner is not None and not temp_owner.closed:
            try:
                temp_owner.close()
            except BaseException:
                release_failed = True
        for lease in (temp_lease,):
            if lease is not None:
                try:
                    _windows_close_leases((lease,))
                except BaseException:
                    release_failed = True
        for value in (post_scope, scope):
            if value is not None:
                try:
                    _windows_close_leases(value.values())
                except BaseException:
                    release_failed = True
        if release_failed:
            recovery = (_windows_record_recovery(target, temp, original, source, winerror)
                        if native_attempted else None)
            locks._windows_release_unknown = True
            anchor._windows_guard.invalidate()
            legacy._windows_guard.invalidate()
            raise WindowsRecordCommitError("release_unknown", stage="unknown", winerror=winerror,
                    recovery=recovery, temporary_created=temporary_created,
                    temporary_may_contain_data=temporary_may_contain_data) from None
        if error is None:
            return
        if native_attempted:
            # The API can report failure after changing names or attributes;
            # inspect only known names, never delete or automatically roll back.
            recovery = _windows_record_recovery(target, temp, original, source, winerror)
            locks._windows_release_unknown = True
            anchor._windows_guard.invalidate()
            legacy._windows_guard.invalidate()
            raise WindowsRecordCommitError("record_postcheck_failed" if native_success else "record_commit_unknown",
                    stage="reported_success" if native_success else "api_failed",
                    winerror=winerror, recovery=recovery, temporary_created=temporary_created,
                    temporary_may_contain_data=temporary_may_contain_data) from None
        if (getattr(error, "code", None) in ("release_unknown", "lock_release_unknown")
                or getattr(error, "owned_handle", None) is not None):
            locks._windows_release_unknown = True
            anchor._windows_guard.invalidate()
            legacy._windows_guard.invalidate()
            raise WindowsRecordCommitError("release_unknown", stage="precommit", temporary_created=temporary_created,
                                            temporary_may_contain_data=temporary_may_contain_data) from None
        if isinstance(error, PreflightError) and temporary_created is False:
            raise error
        if isinstance(error, Exception):
            raise WindowsRecordCommitError("record_precommit_failed", stage="precommit",
                    temporary_created=temporary_created, temporary_may_contain_data=temporary_may_contain_data) from None
        raise error


def write_instance_record(state_dir: str | os.PathLike[str], name: str, record: dict, *, anchor, legacy) -> None:
    """Replace a non-authoritative observation in the fixed state directory.

    Caller holds anchor+legacy locks. Windows uses same-volume Win32 operations
    and postchecks; native atomicity/durability still require real testing.
    """
    if os.name == "nt":
        return _windows_write_instance_record(state_dir, name, record, anchor=anchor, legacy=legacy)
    import json
    import tempfile
    state = Path(state_dir)
    target = _record_target(state, name)
    try:
        verify_held_instance_locks(anchor, legacy)
        if str(state) != legacy.snapshot.as_dict()["state_dir"].canonical:
            raise PreflightError("locks_unverified", "Lock installation does not match record")
        protection_before = []
        verify_record_directory(state, _capture=protection_before)
        if (not isinstance(record, dict) or record.get("schema") != INSTANCE_SCHEMA
                or record.get("record") != target.stem
                or not _valid_record(record, target.stem, state)):
            raise PreflightError("record_invalid", "Invalid instance record")
        _check_record_target(target)
        data = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(data) > 65536:
            raise PreflightError("record_invalid", "Instance record is too large")
        fd, tmp = tempfile.mkstemp(prefix=".record-", dir=state)
        try:
            with os.fdopen(fd, "wb") as stream:
                if os.name == "posix":
                    os.fchmod(stream.fileno(), 0o600)
                verify_no_extended_acl(stream.fileno())
                verify_local_filesystem_fd(stream.fileno())
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            protection_precommit = []
            verify_record_directory(state, _capture=protection_precommit)
            if not _same_record_directory_protection(protection_before, protection_precommit, state):
                raise PreflightError("protection_changed", "Directory protection changed before record replace")
            _check_record_target(target)
            # Rechecking then replacing is not a CAS: non-cooperating writers
            # can still race between this check and the filesystem operation.
            os.replace(tmp, target)
            tmp = None
            try:
                _check_record_target(target)
                protection_after = []
                verify_record_directory(state, _capture=protection_after)
                if not _same_record_directory_protection(protection_before, protection_after, state):
                    raise PreflightError("protection_changed", "Directory protection changed after record replace")
            except PreflightError:
                # Replacement already happened; never imply no side effect.
                raise PreflightError("record_postcheck_failed",
                                     "Instance record replaced but postcheck failed") from None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
    except PreflightError:
        raise
    except (OSError, TypeError, ValueError, RecursionError):
        raise PreflightError("record_unavailable", "Instance record write failed") from None


def _hex_id(value) -> bool:
    import re
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _valid_record(record: dict, name: str, state_dir: Path) -> bool:
    """Validate public on-disk schema; never grant trust based on a field."""
    if os.name == "nt" and name == "active" and isinstance(record, dict) and record.get("schema") == 3:
        return _valid_windows_active(record, state_dir)
    if (not isinstance(record, dict) or record.get("schema") != INSTANCE_SCHEMA
            or record.get("record") != name or not _hex_id(record.get("install_id"))
            or record.get("project_root") != str(state_dir.parent)):
        return False
    if name == "install":
        ident = record.get("root_identity")
        started = record.get("ever_started")
        nonce = record.get("last_instance_nonce")
        return (isinstance(ident, dict) and type(ident.get("device")) is int
                and type(ident.get("inode")) is int and ident["inode"] > 0
                and type(started) is bool and (not started and nonce is None or started and _hex_id(nonce))
                and isinstance(record.get("initialization_source"), str)
                and bool(record["initialization_source"].strip()))
    if name not in ("active", "last_good_paths") or not _hex_id(record.get("instance_nonce")):
        return False
    paths, listen = record.get("runtime_paths"), record.get("listen")
    if os.name == "nt":
        import ntpath
        is_absolute = ntpath.isabs
    else:
        is_absolute = os.path.isabs
    if (not isinstance(paths, dict) or set(paths) != set(PATH_KEYS)
            or any(not isinstance(v, str) or not is_absolute(v) for v in paths.values())
            or not isinstance(listen, dict) or not isinstance(listen.get("host"), str)
            or not listen["host"] or type(listen.get("port")) is not int
            or not 1 <= listen["port"] <= 65535):
        return False
    if name == "last_good_paths":
        return True
    locks, start = record.get("locks"), record.get("process_start")
    if (record.get("state") not in ("STARTING", "READY")
            or type(record.get("pid")) is not int or record["pid"] <= 0
            or not isinstance(record.get("executable"), str)
            or not is_absolute(record["executable"])
            or not isinstance(start, dict) or start.get("kind") not in ("darwin_proc_bsdinfo", "windows_filetime")
            or not isinstance(locks, dict) or set(locks) != {"anchor", "legacy"}):
        return False
    if start["kind"] == "darwin_proc_bsdinfo":
        if (type(start.get("seconds")) is not int or start["seconds"] <= 0
                or type(start.get("microseconds")) is not int or not 0 <= start["microseconds"] < 1000000):
            return False
    elif type(start.get("ticks_100ns")) is not int or start["ticks_100ns"] <= 0:
        return False
    return all(isinstance(locks[k], dict) and type(locks[k].get("device")) is int
               and type(locks[k].get("inode")) is int and locks[k]["inode"] > 0 for k in locks)


def _valid_windows_active(record, state_dir) -> bool:
    """Lightweight locating record; neither a process nor a credential authority."""
    import ntpath
    state = PureWindowsPath(os.fspath(state_dir))
    root = str(state.parent)
    start = record.get("process_start")
    listen, paths = record.get("listen"), record.get("runtime_paths")
    expected_entry = ntpath.join(root, "core", "cli", "model_proxy.py")
    def absolute(value):
        if not isinstance(value, str) or not value or "\0" in value:
            return False
        drive, tail = ntpath.splitdrive(value)
        return (len(drive) == 2 and drive[1] == ":" and drive[0].isascii()
                and drive[0].isalpha() and tail.startswith("\\")
                and ntpath.normpath(value) == value)
    return (
        record.get("schema") == 3 and record.get("record") == "active"
        and record.get("project_root") == root and record.get("state") in ("STARTING", "READY")
        and _hex_id(record.get("instance_nonce"))
        and type(record.get("pid")) is int and 1 < record["pid"] <= 0xffffffff
        and isinstance(start, dict) and set(start) == {"kind", "ticks_100ns"}
        and start.get("kind") == "windows_filetime"
        and type(start.get("ticks_100ns")) is int and 0 < start["ticks_100ns"] <= 0xffffffffffffffff
        and absolute(record.get("executable")) and absolute(record.get("entry"))
        and ntpath.normcase(record["entry"]) == ntpath.normcase(expected_entry)
        and absolute(record.get("config"))
        and isinstance(listen, dict) and isinstance(listen.get("host"), str) and bool(listen["host"])
        and type(listen.get("port")) is int and 1 <= listen["port"] <= 65535
        and isinstance(paths, dict) and set(paths) == set(PATH_KEYS)
        and all(absolute(value) for value in paths.values())
    )


def read_windows_active(state_dir):
    """Read one stable protected record; old/invalid formats stay UNKNOWN."""
    first = read_instance_record(state_dir, "active.json")
    second = read_instance_record(state_dir, "active.json")
    if first != second:
        raise PreflightError("records_changed", "Active record changed")
    if first is not None and not _valid_windows_active(first, state_dir):
        raise PreflightError("record_unavailable", "Active record is not a supported locator")
    return first


def read_instance_set(state_dir: str | os.PathLike[str]) -> dict | None:
    """Read a consistent protected three-record set, or None if uninitialized.

    This does not prove the recorded process or connected peer owns a socket.
    A transient/missing/mismatched record is UNKNOWN (PreflightError).
    """
    state = PureWindowsPath(os.fspath(state_dir)) if os.name == "nt" else Path(state_dir)
    names = ("install.json", "active.json", "last_good_paths.json")
    first = [read_instance_record(state, name) for name in names]
    second = [read_instance_record(state, name) for name in names]
    if first != second:
        raise PreflightError("records_changed", "Instance records changed")
    install, active, last = first
    if install is None:
        if active is not None or last is not None:
            raise PreflightError("records_inconsistent", "Instance initialization is unknown")
        return None
    identity = verify_record_directory(state)
    if install["root_identity"] != {"device": identity[0], "inode": identity[1]}:
        raise PreflightError("installation_changed", "Installation identity changed")
    if not install["ever_started"]:
        if active is not None or last is not None:
            raise PreflightError("records_inconsistent", "Instance records are inconsistent")
        return {"install": install, "active": None, "last_good_paths": None}
    if active is None or last is None:
        raise PreflightError("records_inconsistent", "Instance records are incomplete")
    for key in ("install_id", "project_root", "instance_nonce", "listen", "runtime_paths"):
        if active[key] != last[key] or (key in ("install_id", "project_root") and active[key] != install[key]):
            raise PreflightError("records_inconsistent", "Instance records are inconsistent")
    if active["instance_nonce"] != install["last_instance_nonce"]:
        raise PreflightError("records_inconsistent", "Instance generation is inconsistent")
    return {"install": install, "active": active, "last_good_paths": last}


def initialize_installation(anchor, legacy, listener, *, listen: dict, source: str, confirmed: bool,
                            precommit_check: Callable[[], None] | None = None) -> dict:
    """Initialize once with explicit operator attestation, locks and bound listener.

    Caller must confirm old autostart is disabled, no other legacy paths or
    shared ledger exist, the installation and data are not cloud-synchronized,
    and provide the source. It must hold the exact anchor
    and configured legacy lock and keep the configured listening socket bound.
    No missing record is silently treated as a new installation.
    """
    import ipaddress
    import socket
    import uuid
    from core.platform_lock import HeldLock
    if confirmed is not True or not isinstance(source, str) or not source.strip() or len(source) > 256:
        raise PreflightError("initialization_unconfirmed", "Operator confirmation and source required")
    if (not isinstance(anchor, HeldLock) or not isinstance(legacy, HeldLock)
            or anchor.closed or legacy.closed or anchor.role != "anchor.lock"
            or legacy.role != "lock" or legacy._parent is not anchor):
        raise PreflightError("locks_unverified", "Anchor and legacy locks are required")
    snapshot = legacy.snapshot
    ids = snapshot.as_dict()
    try:
        if (not isinstance(listener, socket.socket) or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                or not isinstance(listen, dict) or not isinstance(listen.get("host"), str)
                or type(listen.get("port")) is not int or not 1 <= listen["port"] <= 65535):
            raise PreflightError("listen_unverified", "A configured bound listener is required")
        address = listener.getsockname()
        if not address or address[1] != listen["port"]:
            raise PreflightError("listen_unverified", "Listener port does not match")
        try:
            if listener.family not in (socket.AF_INET, socket.AF_INET6):
                raise ValueError("unsupported address family")
            bound = ipaddress.ip_address(address[0])
            candidates = socket.getaddrinfo(listen["host"], listen["port"], listener.family,
                                            socket.SOCK_STREAM, socket.IPPROTO_TCP)
            matches = False
            for family, socktype, _, _, candidate in candidates:
                if family != listener.family or socktype != socket.SOCK_STREAM or candidate[1] != address[1]:
                    continue
                if ipaddress.ip_address(candidate[0]).packed != bound.packed:
                    continue
                if family == socket.AF_INET6 and (len(candidate) < 4 or len(address) < 4 or candidate[3] != address[3]):
                    continue
                matches = True
                break
        except (OSError, ValueError, UnicodeError, TypeError, IndexError):
            raise PreflightError("listen_unverified", "Listener address cannot be verified") from None
        if not matches:
            raise PreflightError("listen_unverified", "Listener address does not match")
        # Darwin does not support querying SO_ACCEPTCONN. Calling listen on
        # this already-bound socket confirms it can actually listen.
        listener.listen(5)
        verify_held_instance_locks(anchor, legacy)
        current = inspect_runtime_identities(snapshot.project_root,
                                             {key: ids[key].requested for key in PATH_KEYS})
        if current != snapshot:
            raise PreflightError("preflight_changed", "Runtime paths changed")
        state = Path(ids["state_dir"].canonical)
        root_dev, root_ino = verify_record_directory(state)
        if read_instance_set(state) is not None:
            raise PreflightError("already_initialized", "Installation is already initialized")
        if precommit_check is not None:
            precommit_check()
        record = {"schema": INSTANCE_SCHEMA, "record": "install", "install_id": uuid.uuid4().hex,
                  "project_root": snapshot.project_root,
                  "root_identity": {"device": root_dev, "inode": root_ino},
                  "initialization_source": source.strip(), "ever_started": False,
                  "last_instance_nonce": None}
        try:
            write_instance_record(state, "install.json", record, anchor=anchor, legacy=legacy)
            initialized = read_instance_set(state)
            if initialized is None or initialized["install"] != record:
                raise PreflightError("initialization_unverified", "Initialization did not verify")
        except Exception as exc:
            if os.name == "nt":
                if isinstance(exc, WindowsRecordCommitError):
                    raise
                # Windows cleanup by pathname has no approved delete contract;
                # a committed or unknown record is retained for manual review.
                raise PreflightError("initialization_cleanup_failed", "Initialization state requires review") from None
            try:
                delete_instance_record_if_matches(state, "install.json", record, anchor=anchor, legacy=legacy)
            except Exception:
                raise PreflightError("initialization_cleanup_failed", "Initialization cleanup unknown") from None
            raise
        return record
    except PreflightError:
        raise
    except (OSError, AttributeError, ValueError, KeyError, TypeError):
        raise PreflightError("initialization_unverified", "Initialization could not be verified") from None


def _verify_windows_held_instance_locks(anchor, legacy) -> None:
    """Revalidate both guards and a fresh simultaneous Windows role snapshot."""
    from core import platform_lock as locks
    scope = None
    with locks._fork_guard:
        if (not isinstance(anchor, locks.HeldLock) or not isinstance(legacy, locks.HeldLock)
                or anchor is legacy):
            raise PreflightError("locks_unverified", "Instance locks are unavailable")
        with anchor._state_lock, legacy._state_lock:
            if (locks._windows_release_unknown or anchor._closed or legacy._closed
                    or legacy._parent is not anchor or anchor.role != "anchor.lock"
                    or legacy.role != "lock" or anchor._children < 1
                    or not isinstance(legacy.snapshot, WindowsPathPreflight)
                    or not isinstance(anchor.snapshot, WindowsPathPreflight)
                    or anchor.snapshot.project_root != legacy.snapshot.project_root
                    or not hasattr(anchor, "_windows_guard")
                    or not hasattr(legacy, "_windows_guard")):
                raise PreflightError("locks_unverified", "Instance locks are unavailable")
            try:
                locks._windows_verify_guard(anchor._windows_guard, anchor._windows_lease)
                locks._windows_verify_guard(legacy._windows_guard, legacy._windows_lease)
                identities = legacy.snapshot.as_dict()
                paths = {key: identities[key].requested for key in PATH_KEYS}
                if "install.json" not in identities:
                    fresh, scope = _windows_open_preflight(legacy.snapshot.project_root, paths, light=True)
                else:
                    fresh, scope = _windows_open_preflight(legacy.snapshot.project_root, paths)
                if fresh != legacy.snapshot:
                    raise PreflightError("preflight_changed", "Runtime paths changed")
                locks._windows_verify_guard(anchor._windows_guard, scope["anchor.lock"])
                locks._windows_verify_guard(legacy._windows_guard, scope["lock"])
                for lease in scope.values():
                    lease.revalidate()
                locks._windows_verify_guard(anchor._windows_guard, scope["anchor.lock"])
                locks._windows_verify_guard(legacy._windows_guard, scope["lock"])
            except PreflightError as exc:
                anchor._windows_guard.invalidate()
                legacy._windows_guard.invalidate()
                if exc.code == "release_unknown":
                    locks._windows_release_unknown = True
                raise
            except BaseException as exc:
                anchor._windows_guard.invalidate()
                legacy._windows_guard.invalidate()
                if locks._windows_release_unknown:
                    raise PreflightError("release_unknown", "Windows path evidence release is unknown") from None
                if isinstance(exc, Exception):
                    raise PreflightError("locks_unverified", "Windows lock evidence is unavailable") from None
                raise
            finally:
                if scope is not None:
                    try:
                        _windows_close_leases(scope.values())
                    except PreflightError:
                        anchor._windows_guard.invalidate()
                        legacy._windows_guard.invalidate()
                        locks._windows_release_unknown = True
                        raise


def verify_held_instance_locks(anchor, legacy) -> None:
    """Check held lock descriptors and the actual legacy parent protection.

    This verifies local ownership/mode/ACL observations, not process ownership
    of a lock held by a different process.
    """
    if os.name == "nt":
        return _verify_windows_held_instance_locks(anchor, legacy)
    from core.platform_lock import HeldLock
    if (not isinstance(anchor, HeldLock) or not isinstance(legacy, HeldLock)
            or anchor.closed or legacy.closed or legacy._parent is not anchor
            or anchor.role != "anchor.lock" or legacy.role != "lock"):
        raise PreflightError("locks_unverified", "Instance locks are unavailable")
    ids = legacy.snapshot.as_dict()
    state = Path(ids["state_dir"].canonical)
    verify_record_directory(state)
    try:
        parent = Path(ids["lock"].canonical).parent
        if parent.resolve(strict=True) != parent:
            raise PreflightError("locks_unverified", "Legacy lock parent is not canonical")
        _verify_directory(parent, private=False)
        def marker(info):
            # Lock files, unlike directories, must retain their ctime as well
            # as their identity and protection metadata throughout this check.
            return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                    info.st_nlink, info.st_ctime_ns, getattr(info, "st_flags", None))

        observations = []
        for role, held in (("anchor.lock", anchor), ("lock", legacy)):
            fd = held.fileno()
            path = Path(ids[role].canonical)
            info = os.fstat(fd)
            current = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or (info.st_mode & 0o600) != 0o600
                    or (role == "anchor.lock" and info.st_mode & 0o077)
                    or marker(info) != marker(current)
                    or (info.st_dev, info.st_ino) != (ids[role].device, ids[role].inode)):
                raise PreflightError("locks_unverified", "Lock identity is unavailable")
            observations.append((fd, path, marker(info)))
        for fd, _, _ in observations:
            verify_no_extended_acl(fd)
            verify_local_filesystem_fd(fd)
        for fd, path, before in observations:
            if before != marker(os.fstat(fd)) or before != marker(path.lstat()):
                raise PreflightError("locks_unverified", "Lock protection changed")
    except PreflightError:
        raise
    except (OSError, AttributeError, ValueError, KeyError):
        raise PreflightError("locks_unverified", "Lock identity is unavailable") from None


def delete_instance_record_if_matches(state_dir, name, expected, *, anchor, legacy):
    """Remove only an exact protected record; unknown identity means no deletion."""
    state = Path(state_dir)
    verify_held_instance_locks(anchor, legacy)
    if str(state) != legacy.snapshot.as_dict()["state_dir"].canonical:
        raise PreflightError("locks_unverified", "Lock installation does not match record")
    current = read_instance_record(state, name)
    if current is None:
        return
    if current != expected:
        raise PreflightError("record_cleanup_failed", "Record changed before cleanup")
    try:
        verify_record_directory(state)
        target = _record_target(state, name)
        target.unlink()
        if read_instance_record(state, name) is not None:
            raise PreflightError("record_cleanup_failed", "Record cleanup could not be verified")
    except PreflightError:
        raise
    except OSError:
        raise PreflightError("record_cleanup_failed", "Record cleanup could not be verified") from None
