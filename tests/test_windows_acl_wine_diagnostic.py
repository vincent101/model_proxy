"""Read-only ACL diagnostic against Wine/Windows and a fresh temporary directory.

Run explicitly with Windows Python -I -S -B, followed by a new result path.
It reports only well-known trustee categories, access masks, flags, and
sanitized error codes; never paths, account names, SIDs, or descriptors.
Native Windows acceptance cannot be inferred from a Wine run.
"""
from __future__ import annotations

import ctypes
import json
import ntpath
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import windows_acl as acl
from core import windows_paths as paths


WELL_KNOWN = {
    acl._SYSTEM: "SYSTEM",
    acl._ADMINISTRATORS: "Administrators",
    acl._BUILTIN_USERS: "Builtin Users",
    acl._AUTHENTICATED_USERS: "Authenticated Users",
    acl._EVERYONE: "Everyone",
    bytes.fromhex("010100000000000300000000"): "Creator Owner",
    bytes.fromhex("010100000000000304000000"): "Owner Rights",
    bytes.fromhex("010100000000000504000000"): "Interactive",
}


def role(sid, observation):
    name = WELL_KNOWN.get(sid)
    if name:
        return name
    if sid == observation.user:
        return "effective user"
    if sid == observation.owner:
        return "owner"
    if sid == observation.group:
        return "primary group"
    return "other"


def observe_directory(path, label, win, backend):
    result = {"component": label}
    handle = None
    try:
        handle = win.open_existing(path, paths._READ_ACCESS,
                                   paths._SHARE_READ_WRITE, paths._OPEN_FLAGS)
        obs = backend.read_observation(handle, "directory", paths._DIR_CHECK_ACCESS)
        result.update({
            "owner": role(obs.owner, obs),
            "effective_token": obs.token_source,
            "dacl_protected": bool(obs.control & 0x1000),
            "access_granted": obs.access_granted,
            "privilege_count": obs.privilege_count,
            "aces": [{"principal": role(ace.sid, obs), "type": ace.ace_type,
                      "flags": hex(ace.flags), "mask": hex(ace.mask),
                      "mapped": hex(acl._map_mask(ace.mask)),
                      "outside_public_read": hex(acl._map_mask(ace.mask)
                                                 & ~acl._ANCESTOR_PUBLIC_READ)}
                     for ace in obs.aces],
        })
        for label, policy, required in (
                ("ancestor", "ancestor", paths._DIR_CHECK_ACCESS),
                ("private", "private", paths._DIR_CHECK_ACCESS),
                ("creation_parent_file", "ancestor", paths._DIR_CHECK_ACCESS | 0x2),
                ("creation_parent_directory", "ancestor", paths._DIR_CHECK_ACCESS | 0x4)):
            try:
                acl.read_stable_evidence(handle, kind="directory",
                                         required_access=required,
                                         policy=policy, backend=backend)
                result[label] = "accepted"
            except acl.ACLVerificationError as exc:
                result[label] = exc.code
    except acl.ACLVerificationError as exc:
        result["error"] = exc.code
    except OSError:
        result["error"] = "open_or_close_unavailable"
    finally:
        if handle is not None:
            try:
                win.close(handle)
            except OSError:
                result["release"] = "unknown"
    return result


def create_private_fixture(temp, win, backend):
    """Try a private descriptor on a new fixture directory, not any ancestor."""
    handle = win.open_existing(temp, paths._READ_ACCESS, paths._SHARE_READ_WRITE,
                               paths._OPEN_FLAGS)
    try:
        user = backend.read_observation(handle, "directory", paths._DIR_CHECK_ACCESS).user
    finally:
        win.close(handle)
    if not acl._valid_sid(user):
        return {"creation": "token_sid_invalid"}
    authority = int.from_bytes(user[2:8], "big")
    subauth = [int.from_bytes(user[i:i + 4], "little") for i in range(8, len(user), 4)]
    user_sddl = "-".join(("S", str(user[0]), str(authority), *(str(n) for n in subauth)))
    descriptor_text = (f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
                       f"(A;OICI;FA;;;{user_sddl})")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle_t, dword = ctypes.c_void_p, ctypes.c_uint32

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", dword), ("descriptor", handle_t), ("inherit", ctypes.c_int32)]

    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [ctypes.c_wchar_p, dword, ctypes.POINTER(handle_t), ctypes.POINTER(dword)]
    convert.restype = ctypes.c_int32
    kernel.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(SecurityAttributes)]
    kernel.CreateDirectoryW.restype = ctypes.c_int32
    kernel.LocalFree.argtypes = [handle_t]
    kernel.LocalFree.restype = handle_t
    descriptor, length = handle_t(), dword()
    if not convert(descriptor_text, 1, ctypes.byref(descriptor), ctypes.byref(length)):
        result = {"creation": "descriptor_unavailable", "winerror": ctypes.get_last_error()}
        if descriptor.value and kernel.LocalFree(descriptor):
            result["descriptor_release"] = "unknown"
        return result
    result = {}
    try:
        present, defaulted, dacl = acl.BOOL(), acl.BOOL(), handle_t()
        control, revision = acl.WORD(), acl.DWORD()
        sd_length = backend.a.GetSecurityDescriptorLength(descriptor)
        if (not backend.a.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                    ctypes.byref(dacl), ctypes.byref(defaulted))
                or not backend.a.GetSecurityDescriptorControl(descriptor, ctypes.byref(control),
                                                               ctypes.byref(revision))
                or not present.value or not dacl.value or not sd_length):
            raise acl.ACLVerificationError("descriptor_unavailable")
        _, requested = backend._aces(dacl, descriptor.value, descriptor.value + sd_length)
        def requested_role(sid):
            if sid == acl._SYSTEM:
                return "SYSTEM"
            if sid == acl._ADMINISTRATORS:
                return "Administrators"
            if sid == user:
                return "effective user"
            return "other"
        request_summary = [{"principal": requested_role(a.sid), "flags": hex(a.flags),
                            "mask": hex(a.mask)} for a in requested]
        target = ntpath.join(temp, "explicit-private-fixture")
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, 0)
        if not kernel.CreateDirectoryW(target, ctypes.byref(attributes)):
            result = {"creation": "create_unavailable", "winerror": ctypes.get_last_error(),
                      "requested_aces": request_summary,
                      "requested_dacl_protected": bool(control.value & 0x1000)}
        else:
            result = {"creation": "created", "requested_aces": request_summary,
                      "requested_dacl_protected": bool(control.value & 0x1000),
                      "observed": observe_directory(target, "explicit private fixture", win, backend)}
    except acl.ACLVerificationError as exc:
        result = {"creation": "descriptor_inspection_unavailable", "error": exc.code}
    finally:
        if kernel.LocalFree(descriptor):
            result["descriptor_release"] = "unknown"
    return result


def diagnostic():
    if os.name != "nt" or not (sys.flags.isolated and sys.flags.no_site
                                and sys.dont_write_bytecode):
        raise SystemExit("requires Windows Python -I -S -B")
    try:
        getattr(ctypes.WinDLL("ntdll"), "wine_get_version")
        environment = "Wine"
    except (OSError, AttributeError):
        environment = "Windows-compatible; native Windows not established"
    win, backend = paths.NativeWin32(), acl._NativeBackend()
    with tempfile.TemporaryDirectory(prefix="mp-acl-probe-") as temp:
        # Simulate only directory creation inside this newly created fixture.
        state = ntpath.join(temp, ".runtime")
        os.mkdir(state)
        drive, tail = ntpath.splitdrive(ntpath.abspath(temp))
        result = {"environment": environment, "temp_drive": drive.upper(),
                  "directories": []}
        try:
            win.volume_info(drive)
            result["volume_policy"] = "accepted"
        except OSError:
            result["volume_policy"] = "rejected"
        current = drive + "\\"
        result["directories"].append(observe_directory(current, "drive root", win, backend))
        parts = [part for part in tail.split("\\") if part]
        for index, part in enumerate(parts):
            current = ntpath.join(current, part)
            label = "temporary directory" if index == len(parts) - 1 else f"ancestor {index + 1}"
            result["directories"].append(observe_directory(current, label, win, backend))
        result["directories"].append(observe_directory(state, "new state directory", win, backend))
        try:
            with paths.inspect(state, backend=paths.WindowsPathBackend(win32=win, acl_backend=backend),
                               kind="directory", private=True):
                result["state_path_policy"] = "accepted"
        except paths.PathEvidenceError as exc:
            result["state_path_policy"] = exc.code
        result["explicit_private_creation"] = create_private_fixture(temp, win, backend)
        return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("provide a fresh result-file path")
    data = diagnostic()
    with open(sys.argv[1], "x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=True, indent=2)
