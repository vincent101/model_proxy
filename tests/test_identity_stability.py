"""Stable installation identity (POSIX schema 4) regression tests.

Covers the matrix in ``docs/designs/2026-11-22-macos重启安装身份稳定性.md``:
identity collection, the read/judge split, schema 2/4 dispatch, back-up and
resumable migration, interactive rebinding, stale-lock comparison and the
stop-first entry policy. Every fixture is a private temporary project root;
the production ``.runtime`` is never read or written.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import pty
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import discovery, platform_process
from core.cli import authority, control
from core.discovery import (
    IDENTITY_INODE_CHANGED,
    IDENTITY_INVALID,
    IDENTITY_LEGACY_OTHER_BOOT,
    IDENTITY_LEGACY_SAME_BOOT,
    IDENTITY_SAME,
    IDENTITY_VOLUME_CHANGED,
    INSTANCE_SCHEMA,
    POSIX_RECORD_SCHEMA,
    VOLUME_IDENTITY_KIND,
    PreflightError,
    RootEvidence,
    collect_root_identity,
    inspect_runtime_identities,
    judge_installation_identity,
    lock_identity_matches,
    migrate_installation,
    read_instance_set,
    rebind_installation,
    require_installation_identity,
    stable_lock_record,
    verify_record_directory,
    write_instance_record,
)
from core.platform_lock import acquire_lock, verify_local_access
from core.runtime_paths import PATH_KEYS


UUID_A = "5ac2a76a-745b-493a-a82f-e1df050eae75"
UUID_B = "ad11b164-0d5d-405d-8ae5-fc5fc1f284ea"


@unittest.skipUnless(sys.platform == "darwin", "native Darwin ACL, volume UUID and locks required")
class StableIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir(mode=0o700)
        self.state = self.root / ".runtime"
        self.state.mkdir(mode=0o700)
        (self.root / "runtime").mkdir(mode=0o700)
        (self.root / "config").mkdir(mode=0o700)
        self.paths = {key: str(self.root / "runtime" / key) for key in PATH_KEYS}
        self.install_id = "b" * 32
        self.nonce = "a" * 32

    # --- fixture helpers -------------------------------------------------
    @contextlib.contextmanager
    def held_locks(self):
        snapshot = inspect_runtime_identities(self.root, self.paths)
        with acquire_lock(snapshot, "anchor.lock", create=True,
                          access_verifier=verify_local_access) as anchor:
            with acquire_lock(anchor.snapshot, "lock", create=True,
                              access_verifier=verify_local_access, anchor=anchor) as legacy:
                yield anchor, legacy

    def exited_process(self):
        """A genuinely exited Darwin process identity owned by this test."""
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        start = discovery.process_start_identity(child.pid)
        executable = discovery.process_executable(child.pid)
        child.wait(timeout=10)
        self.assertIsNotNone(start)
        self.assertIsNotNone(executable)
        return child.pid, start, executable

    def make_records(self, schema, *, anchor, legacy, started=True, root_identity=None,
                     listen=None, process=None):
        if schema == POSIX_RECORD_SCHEMA:
            evidence = collect_root_identity(self.state)
            identity = evidence.record()
        else:
            device, inode = verify_record_directory(self.state)
            evidence = None
            identity = {"device": device, "inode": inode}
        install = {"schema": schema, "record": "install", "project_root": str(self.root),
                   "install_id": self.install_id,
                   "root_identity": root_identity if root_identity is not None else identity,
                   "initialization_source": "isolated unit fixture",
                   "ever_started": started,
                   "last_instance_nonce": self.nonce if started else None}
        if not started:
            return {"install": install, "active": None, "last_good_paths": None}
        common = {"schema": schema, "project_root": str(self.root),
                  "install_id": self.install_id, "instance_nonce": self.nonce,
                  "listen": listen or {"host": "127.0.0.1", "port": 29491},
                  "runtime_paths": dict(self.paths)}
        locks = {}
        for name, held in (("anchor", anchor), ("legacy", legacy)):
            info = os.fstat(held.fileno())
            identity = held.snapshot.as_dict()[held.role]
            locks[name] = (stable_lock_record(identity.canonical, device=info.st_dev,
                                              inode=info.st_ino)
                           if schema == POSIX_RECORD_SCHEMA
                           else {"device": info.st_dev, "inode": info.st_ino})
        pid, process_start, executable = process or (
            54321, {"kind": "darwin_proc_bsdinfo", "seconds": 1, "microseconds": 0},
            "/fake/python3")
        active = dict(common, record="active", state="READY", pid=pid,
                      process_start=process_start, executable=executable, locks=locks)
        good = dict(common, record="last_good_paths")
        return {"install": install, "active": active, "last_good_paths": good}

    def write_records(self, schema, *, anchor, legacy, **kwargs):
        records = self.make_records(schema, anchor=anchor, legacy=legacy, **kwargs)
        for name in ("last_good_paths", "active", "install"):
            if records[name] is not None:
                write_instance_record(self.state, name + ".json", records[name],
                                      anchor=anchor, legacy=legacy)
        return read_instance_set(self.state)

    @staticmethod
    def drift_legacy_locks(active):
        """A schema-2 lock record whose boot-local device changed, inode equal."""
        return {name: {"device": value["device"] + 1, "inode": value["inode"]}
                for name, value in active["locks"].items()}

    def lock_ids(self, anchor, legacy):
        ids = {}
        for name, held in (("anchor", anchor), ("legacy", legacy)):
            info = os.fstat(held.fileno())
            identity = held.snapshot.as_dict()[held.role]
            ids[name] = stable_lock_record(identity.canonical, device=info.st_dev,
                                           inode=info.st_ino)
        return ids

    def backup_dirs(self):
        backups = self.state / "backups"
        return sorted(p for p in backups.iterdir()) if backups.is_dir() else []

    def write_launch_config(self):
        """Write the protected business/runtime config a start preflight needs."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        runtime = self.root / "config" / "runtime_paths.json"
        runtime.write_text(json.dumps(self.paths), encoding="utf-8")
        runtime.chmod(0o600)
        config = self.root / "config" / "model_proxy_config.json"
        config.write_text(json.dumps({
            "admin_token": "fake-only-test-token",
            "listen": {"host": "127.0.0.1", "port": port},
            "supplies": [], "routes": [], "strategies": [],
        }), encoding="utf-8")
        config.chmod(0o600)
        return config

    def pty_pair(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        instream = os.fdopen(os.dup(slave), "r", buffering=1)
        outstream = os.fdopen(os.dup(slave), "w", buffering=1)
        self.addCleanup(instream.close)
        self.addCleanup(outstream.close)
        return master, instream, outstream

    def write_other_boot_install(self, records, *, anchor, legacy, schema=INSTANCE_SCHEMA):
        """Rewrite the install record as if it came from another boot/volume."""
        identity = dict(records["install"]["root_identity"])
        if schema == POSIX_RECORD_SCHEMA:
            identity["inode"] = identity["inode"] + 1
        else:
            identity["device"] = identity["device"] + 1
        written = dict(records["install"], root_identity=identity)
        write_instance_record(self.state, "install.json", written,
                              anchor=anchor, legacy=legacy)
        return read_instance_set(self.state)

    def status_cli(self):
        return control.MacControl(
            self.root, config_path=self.root / "config/model_proxy_config.json",
            out=io.StringIO(), err=io.StringIO())

    # --- identity collection --------------------------------------------
    def test_collect_root_identity_reports_volume_uuid_and_directory_inode(self):
        evidence = collect_root_identity(self.state)
        info = self.root.lstat()
        self.assertEqual(evidence.kind, VOLUME_IDENTITY_KIND)
        self.assertEqual(evidence.uuid, discovery._darwin_volume_uuid(str(self.root)))
        self.assertEqual((evidence.inode, evidence.device), (info.st_ino, info.st_dev))
        self.assertEqual(evidence.record(),
                         {"kind": VOLUME_IDENTITY_KIND, "uuid": evidence.uuid,
                          "inode": info.st_ino})

    # --- per-lock volume evidence ---------------------------------------
    def test_stable_lock_record_uses_each_lock_own_volume(self):
        with self.held_locks() as (anchor, legacy):
            root_uuid = collect_root_identity(self.state).uuid
            anchor_info = os.fstat(anchor.fileno())
            self.assertEqual(
                stable_lock_record(self.state / "anchor.lock", device=anchor_info.st_dev,
                                   inode=anchor_info.st_ino),
                {"kind": VOLUME_IDENTITY_KIND, "uuid": root_uuid,
                 "inode": anchor_info.st_ino})
            legacy_path = Path(self.paths["lock"])
            legacy_info = os.fstat(legacy.fileno())

            def volume(path):
                return UUID_B if Path(path) == legacy_path else root_uuid

            with mock.patch.object(discovery, "_darwin_volume_uuid", side_effect=volume):
                current = stable_lock_record(legacy_path, device=legacy_info.st_dev,
                                             inode=legacy_info.st_ino)
            self.assertEqual(current, {"kind": VOLUME_IDENTITY_KIND, "uuid": UUID_B,
                                       "inode": legacy_info.st_ino})

    @staticmethod
    def _listening_process(program):
        child = subprocess.Popen([sys.executable, '-B', '-u', '-c', program],
                                 stdout=subprocess.PIPE, text=True)
        def stop():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
            child.stdout.close()
        try:
            port = int(child.stdout.readline().strip())
            start = discovery.process_start_identity(child.pid)
            executable = discovery.process_executable(child.pid)
        except BaseException:
            stop()
            raise
        return child, port, start, executable, stop

    def test_listening_owner_evidence_uses_the_real_fd_table(self):
        child, port, start, executable, stop = self._listening_process(
            'import socket,time; s=socket.socket(); '
            's.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); '
            's.bind(("127.0.0.1",0)); s.listen(4); '
            'print(s.getsockname()[1],flush=True); time.sleep(30)')
        self.addCleanup(stop)
        self.assertTrue(platform_process.verify_listening_owner(
            child.pid, start, executable, socket.AF_INET, '127.0.0.1', port))
        self.assertFalse(platform_process.verify_listening_owner(
            child.pid, start, executable, socket.AF_INET, '127.0.0.1', port + 1))
        self.assertFalse(platform_process.verify_listening_owner(
            child.pid, start, executable, socket.AF_INET6, '::1', port))
        stop()
        # An exited generation is UNKNOWN, never absence.
        with self.assertRaises(platform_process.ProcessEvidenceError):
            platform_process.verify_listening_owner(
                child.pid, start, executable, socket.AF_INET, '127.0.0.1', port)

    def test_listening_owner_wildcard_never_reports_a_false_absence(self):
        wild4, port, start, executable, stop = self._listening_process(
            'import socket,time; s=socket.socket(); '
            's.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); '
            's.bind(("0.0.0.0",0)); s.listen(4); '
            'print(s.getsockname()[1],flush=True); time.sleep(30)')
        self.addCleanup(stop)
        for address in ('127.0.0.1', '192.0.2.50'):
            self.assertTrue(platform_process.verify_listening_owner(
                wild4.pid, start, executable, socket.AF_INET, address, port), address)
        # An IPv4 wildcard may be the reported view of an IPv6 dual-stack
        # listener, so an IPv6 expectation must be owned or ambiguous, never
        # a false "not held".
        with self.assertRaises(platform_process.ProcessEvidenceError) as caught:
            platform_process.verify_listening_owner(
                wild4.pid, start, executable, socket.AF_INET6, '::1', port)
        self.assertEqual(str(caught.exception), 'listening_owner_ambiguous')
        stop()
        dual, port, start, executable, stop = self._listening_process(
            'import socket,time; s=socket.socket(socket.AF_INET6); '
            's.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,0); '
            's.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); '
            's.bind(("::",0)); s.listen(4); '
            'print(s.getsockname()[1],flush=True); time.sleep(30)')
        self.addCleanup(stop)
        self.assertTrue(platform_process.verify_listening_owner(
            dual.pid, start, executable, socket.AF_INET, '127.0.0.1', port))
        # A dual-stack wildcard must be owned or explicitly ambiguous for an
        # IPv6 expectation, never a false "not held".
        try:
            covered = platform_process.verify_listening_owner(
                dual.pid, start, executable, socket.AF_INET6, '::1', port)
        except platform_process.ProcessEvidenceError as exc:
            self.assertEqual(str(exc), 'listening_owner_ambiguous')
        else:
            self.assertTrue(covered)

    def test_cli_validation_accepts_a_configured_lock_on_another_volume(self):
        with self.held_locks() as (anchor, legacy):
            records = self.make_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            root_uuid = collect_root_identity(self.state).uuid
            legacy_path = Path(self.paths["lock"])
            legacy_info = os.fstat(legacy.fileno())
            cross = dict(records["active"], locks={
                "anchor": records["active"]["locks"]["anchor"],
                "legacy": {"kind": VOLUME_IDENTITY_KIND, "uuid": UUID_B,
                           "inode": legacy_info.st_ino}})

            def volume(path):
                return UUID_B if Path(path) == legacy_path else root_uuid

            with mock.patch.object(discovery, "_darwin_volume_uuid", side_effect=volume):
                instance = control._valid_record(self.root, cross,
                                                 records["last_good_paths"])
                self.assertEqual(instance.active, cross)
                wrong = dict(cross, locks=dict(cross["locks"], legacy={
                    "kind": VOLUME_IDENTITY_KIND, "uuid": root_uuid,
                    "inode": legacy_info.st_ino}))
                with self.assertRaises(control.ControlError) as caught:
                    control._valid_record(self.root, wrong, records["last_good_paths"])
            self.assertEqual(caught.exception.code, "lock_identity_changed")

    def test_replaced_lock_path_fails_lock_identity_evidence(self):
        with self.held_locks() as (anchor, legacy):
            target = Path(self.paths["lock"])
            info = os.fstat(legacy.fileno())
            target.rename(target.with_name(target.name + ".old"))
            target.write_text("replacement", encoding="utf-8")
            target.chmod(0o600)
            with self.assertRaises(PreflightError) as caught:
                stable_lock_record(target, device=info.st_dev, inode=info.st_ino)
        self.assertEqual(caught.exception.code, "identity_evidence_changed")

    def test_lock_volume_evidence_unavailable_fails_closed(self):
        with self.held_locks() as (anchor, legacy):
            info = os.fstat(legacy.fileno())
            for value in (None, "0" * 32, "not-a-uuid"):
                with self.subTest(value=value), mock.patch.object(
                        discovery, "_darwin_volume_uuid", return_value=value):
                    with self.assertRaises(PreflightError) as caught:
                        stable_lock_record(Path(self.paths["lock"]),
                                           device=info.st_dev, inode=info.st_ino)
                    self.assertEqual(caught.exception.code, "volume_identity_unavailable")

    def test_lock_identity_change_during_volume_probe_fails_closed(self):
        with self.held_locks() as (anchor, legacy):
            target = Path(self.paths["lock"])
            info = os.fstat(legacy.fileno())

            def replace(_path):
                target.rename(target.with_name(target.name + ".old"))
                target.write_text("replacement", encoding="utf-8")
                target.chmod(0o600)
                return UUID_A

            with mock.patch.object(discovery, "_darwin_volume_uuid", side_effect=replace):
                with self.assertRaises(PreflightError) as caught:
                    stable_lock_record(target, device=info.st_dev, inode=info.st_ino)
        self.assertEqual(caught.exception.code, "identity_evidence_changed")

    def test_missing_or_zero_volume_uuid_is_unavailable_not_downgraded(self):
        for value in (None, "0" * 32, "not-a-uuid"):
            with self.subTest(value=value), \
                 mock.patch.object(discovery, "_darwin_volume_uuid", return_value=value):
                with self.assertRaises(PreflightError) as caught:
                    collect_root_identity(self.state)
                self.assertEqual(caught.exception.code, "volume_identity_unavailable")

    def test_changed_directory_evidence_during_probe_is_rejected(self):
        real = discovery.verify_record_directory
        calls = []

        def changing(state_dir, **kwargs):
            calls.append(state_dir)
            return (999999, 999999) if len(calls) >= 2 else real(state_dir, **kwargs)

        with mock.patch.object(discovery, "verify_record_directory", side_effect=changing):
            with self.assertRaises(PreflightError) as caught:
                collect_root_identity(self.state)
        self.assertEqual(caught.exception.code, "identity_evidence_changed")

    # --- verdict matrix ---------------------------------------------------
    def test_install_identity_verdict_matrix(self):
        info = self.root.lstat()
        stable = {"kind": VOLUME_IDENTITY_KIND, "uuid": UUID_A, "inode": info.st_ino}
        evidence = RootEvidence(VOLUME_IDENTITY_KIND, UUID_A, info.st_ino, info.st_dev)
        cases = (
            ({"schema": 4, "root_identity": stable}, IDENTITY_SAME),
            ({"schema": 4, "root_identity": dict(stable, uuid=UUID_B)}, IDENTITY_VOLUME_CHANGED),
            ({"schema": 4, "root_identity": dict(stable, inode=info.st_ino + 1)}, IDENTITY_INODE_CHANGED),
            ({"schema": 4, "root_identity": dict(stable, kind="other")}, IDENTITY_INVALID),
            ({"schema": 4, "root_identity": dict(stable, device=info.st_dev)}, IDENTITY_INVALID),
            ({"schema": 4, "root_identity": {"device": info.st_dev, "inode": info.st_ino}}, IDENTITY_INVALID),
            ({"schema": 4}, IDENTITY_INVALID),
            ({"schema": 2, "root_identity": {"device": info.st_dev, "inode": info.st_ino}},
             IDENTITY_LEGACY_SAME_BOOT),
            ({"schema": 2, "root_identity": {"device": info.st_dev + 1, "inode": info.st_ino}},
             IDENTITY_LEGACY_OTHER_BOOT),
            ({"schema": 2, "root_identity": {"device": info.st_dev, "inode": info.st_ino + 1}},
             IDENTITY_INODE_CHANGED),
            ({"schema": 2, "root_identity": dict(stable)}, IDENTITY_INVALID),
            ({"schema": 3, "root_identity": {"device": info.st_dev, "inode": info.st_ino}},
             IDENTITY_INVALID),
            ({"schema": 4, "root_identity": dict(stable, uuid=UUID_A.upper())}, IDENTITY_INVALID),
        )
        for install, expected in cases:
            with self.subTest(install=install):
                self.assertEqual(judge_installation_identity(install, evidence), expected)
        self.assertEqual(judge_installation_identity(None, evidence), IDENTITY_INVALID)

    def test_lock_identity_comparison_separates_stable_and_boot_local_forms(self):
        current = {"kind": VOLUME_IDENTITY_KIND, "uuid": UUID_A, "inode": 42}
        self.assertTrue(lock_identity_matches(current, current, require_same_boot=True))
        self.assertFalse(lock_identity_matches(dict(current, uuid=UUID_B), current,
                                               require_same_boot=True))
        self.assertFalse(lock_identity_matches(dict(current, inode=43), current,
                                               require_same_boot=True))
        legacy = {"device": 7, "inode": 42}
        self.assertTrue(lock_identity_matches(legacy, {"kind": "legacy", "device": 7, "inode": 42},
                                              require_same_boot=True))
        self.assertFalse(lock_identity_matches(legacy, {"kind": "legacy", "device": 8, "inode": 42},
                                               require_same_boot=True))
        self.assertTrue(lock_identity_matches(legacy, {"kind": "legacy", "device": 8, "inode": 42},
                                              require_same_boot=False))
        self.assertFalse(lock_identity_matches({"device": 7, "inode": 43},
                                               {"kind": "legacy", "device": 7, "inode": 42},
                                               require_same_boot=False))
        self.assertFalse(lock_identity_matches(dict(legacy, uuid=UUID_A), current,
                                               require_same_boot=True))
        self.assertTrue(lock_identity_matches(legacy, current, require_same_boot=True,
                                              current_device=7))
        self.assertFalse(lock_identity_matches(legacy, current, require_same_boot=True,
                                               current_device=8))
        self.assertTrue(lock_identity_matches(legacy, current, require_same_boot=False,
                                              current_device=8))

    def test_require_installation_identity_fails_closed(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            install = records["install"]
            self.assertEqual(require_installation_identity(self.state, install).uuid,
                             collect_root_identity(self.state).uuid)
            with mock.patch.object(discovery, "_darwin_volume_uuid", return_value=None):
                with self.assertRaises(PreflightError) as caught:
                    require_installation_identity(self.state, install)
            self.assertEqual(caught.exception.code, "volume_identity_unavailable")
            other_boot = dict(install, root_identity=dict(install["root_identity"],
                                                          device=install["root_identity"]["device"] + 1))
            with self.assertRaises(PreflightError) as caught:
                require_installation_identity(self.state, other_boot)
            self.assertEqual(caught.exception.code, "installation_identity_unverified")

    # --- schema dispatch --------------------------------------------------
    def test_schema_dispatch_accepts_mixed_2_and_4_posix_records(self):
        with self.held_locks() as (anchor, legacy):
            for schema in (INSTANCE_SCHEMA, POSIX_RECORD_SCHEMA):
                records = self.make_records(schema, anchor=anchor, legacy=legacy)
                for name, record in records.items():
                    with self.subTest(schema=schema, name=name):
                        self.assertTrue(discovery._valid_record(record, name, self.state))

    def test_invalid_schema_identity_shape_and_history_are_rejected(self):
        history_entry = {"time": "2026-11-22T00:00:00Z", "reason": "legacy_other_boot",
                         "old": {"device": 1, "inode": 2},
                         "new": {"kind": VOLUME_IDENTITY_KIND, "uuid": UUID_A, "inode": 2},
                         "source": "unit_fixture"}
        with self.held_locks() as (anchor, legacy):
            records = self.make_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            install = records["install"]
            self.assertTrue(discovery._valid_record(
                dict(install, identity_history=[history_entry]), "install", self.state))
            self.assertFalse(discovery._valid_record(
                dict(install, identity_history=[history_entry] * 9), "install", self.state))
            self.assertFalse(discovery._valid_record(
                dict(install, identity_history=[dict(history_entry, source="   ")]),
                "install", self.state))
            self.assertTrue(discovery._valid_record(
                dict(install, identity_history=[dict(history_entry, source="Operator note")]),
                "install", self.state))
            self.assertFalse(discovery._valid_record(
                dict(install, identity_history=[dict(history_entry, extra=1)]),
                "install", self.state))
            self.assertFalse(discovery._valid_record(dict(install, schema=5), "install", self.state))
            self.assertFalse(discovery._valid_record(
                dict(install, root_identity=dict(install["root_identity"], device=1)),
                "install", self.state))
            self.assertFalse(discovery._valid_record(
                dict(install, root_identity={"device": 1, "inode": 2}), "install", self.state))
            active = records["active"]
            self.assertFalse(discovery._valid_record(
                dict(active, locks={"anchor": {"device": 1, "inode": 2},
                                    "legacy": {"device": 1, "inode": 3}}),
                "active", self.state))
            self.assertFalse(discovery._valid_record(dict(active, schema=3), "active", self.state))

    def test_windows_schema_dispatch_is_unchanged(self):
        with mock.patch.object(discovery.os, "name", "nt"):
            self.assertTrue(discovery._record_schema_ok({"schema": INSTANCE_SCHEMA}, "install"))
            self.assertTrue(discovery._record_schema_ok({"schema": INSTANCE_SCHEMA}, "active"))
            self.assertTrue(discovery._record_schema_ok({"schema": 3}, "active"))
            self.assertFalse(discovery._record_schema_ok({"schema": 3}, "last_good_paths"))
            self.assertFalse(discovery._record_schema_ok({"schema": POSIX_RECORD_SCHEMA}, "install"))
        self.assertTrue(discovery._record_schema_ok({"schema": POSIX_RECORD_SCHEMA}, "install"))

    def test_read_instance_set_does_not_judge_posix_identity(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            moved = dict(records["install"],
                         root_identity=dict(records["install"]["root_identity"], device=1))
            write_instance_record(self.state, "install.json", moved, anchor=anchor, legacy=legacy)
            read = read_instance_set(self.state)
        self.assertEqual(read["install"]["root_identity"]["device"], 1)
        self.assertEqual(judge_installation_identity(read["install"],
                                                     collect_root_identity(self.state)),
                         IDENTITY_LEGACY_OTHER_BOOT)

    # --- migration and backups -------------------------------------------
    def test_migration_upgrades_all_records_and_backs_up_originals(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            evidence = collect_root_identity(self.state)
            ids = self.lock_ids(anchor, legacy)
            migrate_installation(self.state, records, evidence, ids, anchor=anchor, legacy=legacy)
            after = read_instance_set(self.state)
        for name in ("install", "active", "last_good_paths"):
            self.assertEqual(after[name]["schema"], POSIX_RECORD_SCHEMA)
            self.assertEqual(after[name]["install_id"], self.install_id)
        self.assertEqual(after["install"]["root_identity"], evidence.record())
        self.assertEqual(after["active"]["locks"], ids)
        self.assertEqual(after["install"]["last_instance_nonce"],
                         records["install"]["last_instance_nonce"])
        self.assertEqual(after["install"]["initialization_source"],
                         records["install"]["initialization_source"])
        backups = self.backup_dirs()
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].name.startswith("identity-v2-"))
        self.assertEqual(oct(backups[0].stat().st_mode & 0o777), "0o700")
        for file_name, key in (("install.json", "install"), ("active.json", "active"),
                               ("last_good_paths.json", "last_good_paths")):
            backup = backups[0] / file_name
            self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o600")
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), records[key])

    def test_migration_failure_keeps_mixed_set_and_can_resume(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            evidence = collect_root_identity(self.state)
            ids = self.lock_ids(anchor, legacy)
            real_write = discovery.write_instance_record
            calls = []

            def failing(state_dir, name, record, **kwargs):
                calls.append(name)
                if len(calls) == 2:
                    raise PreflightError("record_unavailable", "injected write failure")
                return real_write(state_dir, name, record, **kwargs)

            with mock.patch.object(discovery, "write_instance_record", side_effect=failing):
                with self.assertRaises(PreflightError):
                    migrate_installation(self.state, records, evidence, ids,
                                         anchor=anchor, legacy=legacy)
            self.assertEqual(calls, ["install.json", "active.json"])
            first_backup = self.backup_dirs()[0]
            self.assertEqual(len(self.backup_dirs()), 1)
            mixed = read_instance_set(self.state)
            self.assertEqual(mixed["install"]["schema"], POSIX_RECORD_SCHEMA)
            self.assertEqual(mixed["active"]["schema"], INSTANCE_SCHEMA)
            self.assertEqual(mixed["last_good_paths"]["schema"], INSTANCE_SCHEMA)
            resume_evidence = collect_root_identity(self.state)
            resume_ids = self.lock_ids(anchor, legacy)
            migrate_installation(self.state, mixed, resume_evidence, resume_ids,
                                 anchor=anchor, legacy=legacy)
            resumed = read_instance_set(self.state)
            backups = self.backup_dirs()
        for name in ("install", "active", "last_good_paths"):
            self.assertEqual(resumed[name]["schema"], POSIX_RECORD_SCHEMA)
        self.assertEqual(len(backups), 2)
        self.assertFalse(backups[0].name == backups[1].name)
        self.assertEqual(json.loads((first_backup / "install.json").read_text(encoding="utf-8")),
                         records["install"])

    def write_mixed_records(self, *, anchor, legacy,
                            active_schema=INSTANCE_SCHEMA,
                            last_schema=INSTANCE_SCHEMA, **kwargs):
        stable = self.make_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy,
                                   **kwargs)
        legacy_set = self.make_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy,
                                       **kwargs)
        records = {
            "install": stable["install"],
            "active": (stable if active_schema == POSIX_RECORD_SCHEMA else legacy_set)["active"],
            "last_good_paths": (stable if last_schema == POSIX_RECORD_SCHEMA
                                else legacy_set)["last_good_paths"],
        }
        for name in ("last_good_paths", "active", "install"):
            write_instance_record(self.state, name + ".json", records[name],
                                  anchor=anchor, legacy=legacy)
        return records

    def test_cli_accepts_mixed_schema_set_and_reaches_resumable_migration(self):
        for active_schema, last_schema in (
                (POSIX_RECORD_SCHEMA, INSTANCE_SCHEMA),
                (INSTANCE_SCHEMA, INSTANCE_SCHEMA)):
            with self.subTest(active=active_schema, last=last_schema):
                with self.held_locks() as (anchor, legacy):
                    self.write_mixed_records(active_schema=active_schema,
                                             last_schema=last_schema,
                                             anchor=anchor, legacy=legacy)
                    read = read_instance_set(self.state)
                    self.assertEqual(read["install"]["schema"], POSIX_RECORD_SCHEMA)
                    instance = control._valid_record(self.root, read["active"],
                                                     read["last_good_paths"])
                    self.assertEqual(instance.active, read["active"])
                    evidence = collect_root_identity(self.state)
                    ids = self.lock_ids(anchor, legacy)
                    migrate_installation(self.state, read, evidence, ids,
                                         anchor=anchor, legacy=legacy)
                migrated = read_instance_set(self.state)
                for name in ("install", "active", "last_good_paths"):
                    self.assertEqual(migrated[name]["schema"], POSIX_RECORD_SCHEMA)

    def test_cli_still_rejects_genuinely_inconsistent_records(self):
        with self.held_locks() as (anchor, legacy):
            records = self.make_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            for key, value in (
                    ("install_id", "c" * 32),
                    ("instance_nonce", "d" * 32),
                    ("runtime_paths", {key: str(Path(path).with_name("other-" + key))
                                       for key, path in self.paths.items()})):
                with self.subTest(key=key):
                    with self.assertRaises(control.ControlError) as caught:
                        control._valid_record(
                            self.root, records["active"],
                            dict(records["last_good_paths"], **{key: value}))
                    self.assertEqual(caught.exception.code, "records_mismatch")
            # Schema is judged per record: an unsupported value is a record
            # error even when both records carry it.
            with self.assertRaises(control.ControlError) as caught:
                control._valid_record(self.root, dict(records["active"], schema=5),
                                      dict(records["last_good_paths"], schema=5))
            self.assertEqual(caught.exception.code, "record_invalid")

    def test_migration_judges_stale_active_before_writing_it(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            evidence = collect_root_identity(self.state)
            ids = self.lock_ids(anchor, legacy)
            order = []

            def before_active():
                order.append(("before_active", read_instance_set(self.state)["active"]["schema"]))

            real_write = discovery.write_instance_record

            def traced(state_dir, name, record, **kwargs):
                order.append(("write", name))
                return real_write(state_dir, name, record, **kwargs)

            with mock.patch.object(discovery, "write_instance_record", side_effect=traced):
                migrate_installation(self.state, records, evidence, ids, anchor=anchor,
                                     legacy=legacy, before_active=before_active)
        self.assertEqual(order, [("write", "install.json"),
                                 ("before_active", INSTANCE_SCHEMA),
                                 ("write", "active.json"),
                                 ("write", "last_good_paths.json")])

    def test_backup_failure_stops_before_any_record_is_rewritten(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            evidence = collect_root_identity(self.state)
            ids = self.lock_ids(anchor, legacy)
            with mock.patch.object(discovery, "_write_backup_record",
                                   side_effect=PreflightError("record_backup_failed", "injected")):
                with self.assertRaises(PreflightError) as caught:
                    migrate_installation(self.state, records, evidence, ids,
                                         anchor=anchor, legacy=legacy)
            self.assertEqual(caught.exception.code, "record_backup_failed")
            unchanged = read_instance_set(self.state)
        self.assertEqual(unchanged, records)

    def test_upgrade_is_idempotent_for_records_already_at_schema_4(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            evidence = collect_root_identity(self.state)
            ids = self.lock_ids(anchor, legacy)
            upgraded = discovery.upgraded_installation_records(records, evidence, ids)
        self.assertEqual(upgraded, records)

    # --- interactive rebinding -------------------------------------------
    def test_rebinding_preserves_install_id_and_records_bounded_history(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            evidence = collect_root_identity(self.state)
            ids = self.lock_ids(anchor, legacy)
            with mock.patch.object(platform_process, "process_gone", return_value=True):
                rebound = rebind_installation(self.state, records, evidence, ids,
                                              anchor=anchor, legacy=legacy,
                                              source="unit_fixture")
            after = read_instance_set(self.state)
        self.assertEqual(rebound["install"], after["install"])
        self.assertEqual(after["install"]["schema"], POSIX_RECORD_SCHEMA)
        self.assertEqual(after["install"]["install_id"], self.install_id)
        self.assertEqual(after["install"]["root_identity"], evidence.record())
        history = after["install"]["identity_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(set(history[0]), {"time", "reason", "old", "new", "source"})
        self.assertEqual(history[0]["old"], records["install"]["root_identity"])
        self.assertEqual(history[0]["new"], evidence.record())
        self.assertEqual(history[0]["reason"], "legacy_other_boot")
        self.assertEqual(history[0]["source"], "unit_fixture")

    def test_rebinding_rejects_stable_records_and_empty_source(self):
        with self.held_locks() as (anchor, legacy):
            stable = self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            evidence = collect_root_identity(self.state)
            ids = self.lock_ids(anchor, legacy)
            with self.assertRaises(PreflightError) as caught:
                rebind_installation(self.state, stable, evidence, ids,
                                    anchor=anchor, legacy=legacy, source="unit_fixture")
            self.assertEqual(caught.exception.code, "installation_identity_unverified")
            legacy_records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            for source in ("", "   "):
                with self.assertRaises(PreflightError) as caught:
                    rebind_installation(self.state, legacy_records, evidence, ids,
                                        anchor=anchor, legacy=legacy, source=source)
                self.assertEqual(caught.exception.code, "initialization_unconfirmed")

    def test_rebinding_requires_a_matching_terminal(self):
        observation = SimpleNamespace()
        with self.assertRaises(PreflightError) as caught:
            authority.InstallationObservation.rebaseline_interactively(
                observation, io.StringIO(), io.StringIO())
        self.assertEqual(caught.exception.code, "initialization_required")

    # --- entry policy -----------------------------------------------------
    def test_stale_instance_judgement_uses_stable_lock_identities(self):
        from core import server, platform_process
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            ids = self.lock_ids(anchor, legacy)
            devices = {name: os.fstat(held.fileno()).st_dev
                       for name, held in (("anchor", anchor), ("legacy", legacy))}
            active = records["active"]
            with mock.patch.object(server.os, "kill", side_effect=ProcessLookupError):
                server._ensure_stale_instance(records, active["runtime_paths"],
                                              active["listen"], ids, lock_devices=devices)
            replaced = dict(records, active=dict(
                active, locks={name: dict(value, inode=value["inode"] + 1)
                               for name, value in ids.items()}))
            with self.assertRaises(PreflightError) as caught:
                server._ensure_stale_instance(replaced, active["runtime_paths"],
                                              active["listen"], ids, lock_devices=devices)
            self.assertEqual(caught.exception.code, "lock_identity_changed")
            with mock.patch.object(server.os, "kill", return_value=None), \
                 mock.patch.object(server, "process_start_identity",
                                   return_value=active["process_start"]), \
                 mock.patch.object(platform_process, "darwin_recorded_state",
                                   return_value="RUNNING"):
                with self.assertRaises(PreflightError) as caught:
                    server._ensure_stale_instance(records, active["runtime_paths"],
                                                  active["listen"], ids, lock_devices=devices)
            self.assertEqual(caught.exception.code, "process_still_active")

    def test_stale_judgement_tolerates_only_boot_drift_with_held_locks(self):
        from core import server
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            ids = self.lock_ids(anchor, legacy)
            devices = {name: os.fstat(held.fileno()).st_dev
                       for name, held in (("anchor", anchor), ("legacy", legacy))}
            active = records["active"]
            drifted = dict(records, active=dict(
                active, locks={name: {"device": devices[name] + 1,
                                      "inode": value["inode"]}
                               for name, value in ids.items()}))
            with mock.patch.object(server.os, "kill", side_effect=ProcessLookupError):
                with self.assertRaises(PreflightError) as caught:
                    server._ensure_stale_instance(drifted, active["runtime_paths"],
                                                  active["listen"], ids, lock_devices=devices)
                self.assertEqual(caught.exception.code, "lock_identity_changed")
                server._ensure_stale_instance(drifted, active["runtime_paths"],
                                              active["listen"], ids,
                                              allow_boot_drift=True, lock_devices=devices)

    def test_unknown_record_schema_is_not_read(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            unknown = dict(records["install"], schema=5)
            target = self.state / "install.json"
            target.chmod(0o600)
            data = json.dumps(unknown, sort_keys=True, separators=(",", ":")).encode("utf-8")
            target.write_bytes(data)
            target.chmod(0o600)
            with self.assertRaises(PreflightError) as caught:
                read_instance_set(self.state)
        self.assertIn(caught.exception.code, ("record_invalid", "record_unavailable"))

    def test_stop_locator_never_judges_install_identity(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            other_boot = dict(records["install"],
                              root_identity=dict(records["install"]["root_identity"], device=1))
            write_instance_record(self.state, "install.json", other_boot,
                                  anchor=anchor, legacy=legacy)
        with mock.patch.object(authority, "installation_identity",
                               side_effect=AssertionError("off must not judge identity")):
            with authority.MacInstallationAuthority().locked_for_stop(
                    self.root, str(self.root / "config/model_proxy_config.json")) as lease:
                self.assertIsInstance(lease, authority.MacAuthorityLease)
                self.assertIsNotNone(lease.records())

    class _LiveCheckReached(Exception):
        """Sentinel: status passed the record stage and entered live checks."""

    def _drive_status(self, cli, lease):
        with mock.patch.object(type(lease), "verify_active",
                               side_effect=self._LiveCheckReached):
            with self.assertRaises(self._LiveCheckReached):
                cli._request(lease, "status")

    def test_status_never_judges_installation_identity(self):
        # An install record from another boot stays irrelevant to status: it
        # locates the recorded instance structurally and proves it live. Any
        # identity verdict or volume-UUID probe would trip the guards below.
        with self.held_locks() as (anchor, legacy):
            self.write_other_boot_install(
                self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy),
                anchor=anchor, legacy=legacy)
        cli = self.status_cli()
        forbidden = AssertionError("status must not judge installation identity")
        with mock.patch.object(authority, "installation_identity", side_effect=forbidden), \
             mock.patch.object(discovery, "_darwin_volume_uuid", side_effect=forbidden):
            with authority.locked_observation(self.root, self.paths) as observation:
                self._drive_status(cli, authority.MacAuthorityLease(observation))
        self.assertEqual(cli.out.getvalue(), "")
        self.assertEqual(cli.err.getvalue(), "")

    def test_status_ignores_a_changed_directory_identity(self):
        with self.held_locks() as (anchor, legacy):
            self.write_other_boot_install(
                self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy),
                anchor=anchor, legacy=legacy, schema=POSIX_RECORD_SCHEMA)
        with authority.locked_observation(self.root, self.paths) as observation:
            lease = authority.MacAuthorityLease(observation)
            # The strict gate still rejects a real change for credential paths.
            with self.assertRaises(PreflightError) as caught:
                lease.installation_identity()
        self.assertEqual(caught.exception.code, "installation_changed")
        cli = self.status_cli()
        with mock.patch.object(discovery, "_darwin_volume_uuid",
                               side_effect=AssertionError("status must not collect a UUID")):
            with authority.locked_observation(self.root, self.paths) as observation:
                self._drive_status(cli, authority.MacAuthorityLease(observation))
        self.assertEqual(cli.err.getvalue(), "")

    def test_status_lease_admin_token_reuses_the_relaxed_evidence_standard(self):
        # status still reads the admin token, so the lease must repeat its last
        # verification with the same evidence standard instead of judging
        # installation identity again (which would probe the volume UUID).
        token = "t" * 16
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            active = records["active"]
            instance = control.Instance(active["pid"], active["process_start"],
                                        active["executable"], active["instance_nonce"],
                                        active["listen"]["host"], active["listen"]["port"],
                                        active, records["last_good_paths"])
            snapshot = inspect_runtime_identities(self.root, self.paths)
            config_path = self.root / "config/model_proxy_config.json"
            pair = SimpleNamespace(exists=True,
                                   contents=json.dumps({"admin_token": token}).encode(),
                                   evidence=SimpleNamespace(source_path=str(config_path)))
            listen = dict(active["listen"])
            observation = SimpleNamespace(
                _root=self.root, _config_path=config_path,
                _paths=self.paths, record_set=lambda: records, _check=lambda: None)
            lease = authority.MacAuthorityLease(observation)
            lease._launch_config_pair = (pair, pair)
            with mock.patch.object(lease, "_launch", return_value={}), \
                 mock.patch.object(lease, "_configuration",
                                   return_value=(pair, pair, listen, snapshot)), \
                 mock.patch.object(authority, "_same_business"), \
                 mock.patch.object(authority, "_check_restart_targets"), \
                 mock.patch.object(authority, "_capture_business", return_value=pair), \
                 mock.patch.object(authority, "_capture", return_value=pair), \
                 mock.patch.object(authority.platform_process, "check_identity"), \
                 mock.patch.object(discovery, "_darwin_volume_uuid",
                                   side_effect=AssertionError("must not probe")):
                self.assertTrue(lease.verify_active(instance, require_identity_evidence=False))
                self.assertEqual(lease.admin_token(), token)

    def test_status_and_off_never_collect_a_volume_uuid(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("status/off must not probe the volume UUID")

        for schema in (INSTANCE_SCHEMA, POSIX_RECORD_SCHEMA):
            with self.subTest(schema=schema):
                with self.held_locks() as (anchor, legacy):
                    records = self.write_records(schema, anchor=anchor, legacy=legacy)
                cli = self.status_cli()
                with mock.patch.object(discovery, "_darwin_volume_uuid",
                                       side_effect=forbidden):
                    instance = control._valid_record(
                        self.root, records["active"], records["last_good_paths"],
                        allow_unavailable_host=True, require_identity_evidence=False)
                    self.assertEqual(instance.active, records["active"])
                    with mock.patch.object(authority.platform_process, "process_gone",
                                           return_value=True):
                        with authority.locked_observation(self.root, self.paths) as observation:
                            lease = authority.MacAuthorityLease(observation)
                            lease.stop_lock_evidence(instance)
                            self.assertTrue(lease.assert_stopped(
                                instance, require_identity_evidence=False))
                    with authority.MacInstallationAuthority().locked_for_stop(
                            self.root,
                            str(self.root / "config/model_proxy_config.json")) as lease:
                        self.assertIsNotNone(lease.records()[0])
                    with authority.locked_observation(self.root, self.paths) as observation:
                        self._drive_status(cli, authority.MacAuthorityLease(observation))
                self.assertEqual(cli.err.getvalue(), "")

    def test_reload_requires_a_verified_identity(self):
        # Verdicts are produced by the production judge, not fed as bare enums.
        with self.held_locks() as (anchor, legacy):
            self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
        with authority.locked_observation(self.root, self.paths) as observation:
            self.assertEqual(observation.installation_identity(), IDENTITY_SAME)
            control.MacControl._require_verified_identity(observation.installation_identity())
        with self.held_locks() as (anchor, legacy):
            self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
        with authority.locked_observation(self.root, self.paths) as observation:
            self.assertEqual(observation.installation_identity(), IDENTITY_LEGACY_SAME_BOOT)
            control.MacControl._require_verified_identity(observation.installation_identity())
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            other_boot = dict(records["install"], root_identity=dict(
                records["install"]["root_identity"],
                device=records["install"]["root_identity"]["device"] + 1))
            write_instance_record(self.state, "install.json", other_boot,
                                  anchor=anchor, legacy=legacy)
        with authority.locked_observation(self.root, self.paths) as observation:
            verdict = observation.installation_identity()
        self.assertEqual(verdict, IDENTITY_LEGACY_OTHER_BOOT)
        with self.assertRaises(control.ControlError) as caught:
            control.MacControl._require_verified_identity(verdict)
        self.assertEqual(caught.exception.code, "installation_identity_unverified")
        # No records means no verdict at all; a missing identity is not verified.
        empty_root = self.base / "empty-project"
        empty_root.mkdir(mode=0o700)
        (empty_root / ".runtime").mkdir(mode=0o700)
        (empty_root / "runtime").mkdir(mode=0o700)
        empty_paths = {key: str(empty_root / "runtime" / key) for key in PATH_KEYS}
        with authority.locked_observation(empty_root, empty_paths) as observation:
            self.assertIsNone(observation.installation_identity())
            with self.assertRaises(control.ControlError) as caught:
                control.MacControl._require_verified_identity(observation.installation_identity())
        self.assertEqual(caught.exception.code, "installation_identity_unverified")

    def test_reload_request_fails_closed_without_an_identity_verdict(self):
        # Non-status requests are credential-bearing; a missing verdict must
        # stop before any record read, socket or token acquisition.
        cli = control.MacControl(
            self.root, config_path=self.root / "config/model_proxy_config.json",
            out=io.StringIO(), err=io.StringIO())
        lease = SimpleNamespace(installation_identity=lambda: None)
        with self.assertRaises(control.ControlError) as caught:
            cli._request(lease, "reload")
        self.assertEqual(caught.exception.code, "installation_identity_unverified")

    def test_boot_identity_requirement_is_per_owning_record(self):
        # Only schema 2 stores a boot-local st_dev. Anything unrecognized keeps
        # the requirement so a malformed record cannot opt out of the check.
        self.assertTrue(discovery.lock_identity_requires_same_boot(
            {"schema": INSTANCE_SCHEMA}))
        self.assertFalse(discovery.lock_identity_requires_same_boot(
            {"schema": POSIX_RECORD_SCHEMA}))
        for value in (None, {}, {"schema": 3}, {"schema": 1}, "schema-2"):
            with self.subTest(value=value):
                self.assertTrue(discovery.lock_identity_requires_same_boot(value))

    def test_live_lock_gates_are_exact_for_schema_2(self):
        # Design §3 live rule: a schema-2 lock record additionally requires
        # this boot's device. The default validator (startup and reload) keeps
        # that exact check; plain status/off do not compare recorded locks.
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            drift = self.drift_legacy_locks(records["active"])
            drifted = dict(records["active"], locks=drift)
            write_instance_record(self.state, "active.json", drifted,
                                  anchor=anchor, legacy=legacy)
            with self.assertRaises(control.ControlError) as caught:
                control._valid_record(self.root, drifted, records["last_good_paths"])
            self.assertEqual(caught.exception.code, "lock_identity_changed")
            instance = control._valid_record(self.root, drifted,
                                             records["last_good_paths"],
                                             require_identity_evidence=False)
            with authority.locked_observation(self.root, self.paths) as observation:
                lease = authority.MacAuthorityLease(observation)
                lease._assert_running_anchor(instance, require_identity_evidence=False)
                with self.assertRaises(control.ControlError) as caught:
                    lease._assert_stop_locks(instance)
            self.assertEqual(caught.exception.code, "legacy_lock_unverified")

    def test_reload_chain_rejects_a_drifted_schema_2_lock_record(self):
        # The credential-bearing reload chain must not inherit the status/off
        # relaxation: its live lock comparison still requires this boot's device.
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            drift = self.drift_legacy_locks(records["active"])
        cli = self.status_cli()
        lease = SimpleNamespace(
            installation_identity=lambda: IDENTITY_LEGACY_SAME_BOOT,
            records=lambda: (dict(records["active"], locks=drift),
                             records["last_good_paths"]))
        with self.assertRaises(control.ControlError) as caught:
            cli._request(lease, "reload")
        self.assertEqual(caught.exception.code, "lock_identity_changed")

    def test_schema_4_lock_identity_is_never_completed_from_an_inode(self):
        # status/off skip the comparison entirely; nothing degrades a schema-4
        # uuid+inode match to an inode-only one on the strict paths.
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            ids = self.lock_ids(anchor, legacy)
            inode_changed = dict(records["active"], locks={
                name: dict(value, inode=value["inode"] + 1)
                for name, value in ids.items()})
            volume_changed = dict(records["active"], locks={
                name: dict(value, uuid=UUID_B) for name, value in ids.items()})
            for label, record in (("inode", inode_changed), ("volume", volume_changed)):
                with self.subTest(label):
                    with self.assertRaises(control.ControlError) as caught:
                        control._valid_record(self.root, record,
                                              records["last_good_paths"])
                    self.assertEqual(caught.exception.code, "lock_identity_changed")
            with mock.patch.object(discovery, "_darwin_volume_uuid",
                                   side_effect=AssertionError("must not probe")):
                instance = control._valid_record(self.root, inode_changed,
                                                 records["last_good_paths"],
                                                 require_identity_evidence=False)
            self.assertEqual(instance.active, inode_changed)

    def test_stop_locator_uses_structure_without_identity(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            drift = self.drift_legacy_locks(records["active"])
            drifted = dict(records["active"], locks=drift)
            write_instance_record(self.state, "active.json", drifted,
                                  anchor=anchor, legacy=legacy)
            instance = control._valid_record(self.root, drifted,
                                             records["last_good_paths"],
                                             allow_unavailable_host=True,
                                             require_identity_evidence=False)
            with mock.patch.object(discovery, "_darwin_volume_uuid",
                                   side_effect=AssertionError("off must not collect a UUID")):
                with authority.locked_observation(self.root, self.paths) as observation:
                    # Liveness on both fixed locks still passes without any
                    # recorded identity comparison.
                    authority.MacAuthorityLease(observation)._assert_stop_locks(
                        instance, require_identity_evidence=False)
        with authority.MacInstallationAuthority().locked_for_stop(
                self.root, str(self.root / "config/model_proxy_config.json")) as lease:
            self.assertEqual(lease.records()[0]["locks"], drift)
        replaced = dict(drifted, locks={
            name: dict(value, inode=value["inode"] + 1)
            for name, value in drift.items()})
        with self.assertRaises(control.ControlError) as caught:
            control._valid_record(self.root, replaced, records["last_good_paths"],
                                  allow_unavailable_host=True)
        self.assertEqual(caught.exception.code, "lock_identity_changed")

    def test_identity_unavailable_fails_closed_on_strict_paths(self):
        def unavailable(_path):
            raise PreflightError("volume_identity_unavailable", "Volume UUID unavailable")

        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
        active = records["active"]
        instance = control.Instance(active["pid"], active["process_start"],
                                    active["executable"], active["instance_nonce"],
                                    active["listen"]["host"], active["listen"]["port"],
                                    active, records["last_good_paths"])
        with mock.patch.object(discovery, "_darwin_volume_uuid", side_effect=unavailable):
            # Startup and the strict validator refuse an unavailable identity.
            with self.assertRaises(PreflightError) as caught:
                control._valid_record(self.root, active, records["last_good_paths"])
            self.assertEqual(caught.exception.code, "volume_identity_unavailable")
            with authority.locked_observation(self.root, self.paths) as observation:
                with self.assertRaises(PreflightError) as caught:
                    observation.installation_state()
            self.assertEqual(caught.exception.code, "volume_identity_unavailable")
        with mock.patch.object(discovery, "_darwin_volume_uuid", side_effect=unavailable), \
             mock.patch.object(authority.platform_process, "process_gone", return_value=True):
            with authority.locked_observation(self.root, self.paths) as observation:
                lease = authority.MacAuthorityLease(observation)
                lease.stop_lock_evidence(instance)
                with self.assertRaises(PreflightError) as caught:
                    lease.assert_stopped(instance)
        self.assertEqual(caught.exception.code, "volume_identity_unavailable")

    def test_stop_release_proof_does_not_judge_recorded_identity(self):
        # off confirms release from process exit, the unchanged record set and
        # re-acquired fixed locks; a recorded identity from another boot/volume
        # must not block stopping.
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            drift = self.drift_legacy_locks(records["active"])
            active = dict(records["active"], locks=drift)
            write_instance_record(self.state, "active.json", active,
                                  anchor=anchor, legacy=legacy)
            instance = control._valid_record(self.root, active,
                                             records["last_good_paths"],
                                             require_identity_evidence=False)
        with mock.patch.object(authority.platform_process, "process_gone", return_value=True):
            with authority.locked_observation(self.root, self.paths) as observation:
                lease = authority.MacAuthorityLease(observation)
                lease.stop_lock_evidence(instance)
                self.assertTrue(lease.assert_stopped(
                    instance, require_identity_evidence=False))
            # A caller that still asks for identity evidence keeps the check.
            with authority.locked_observation(self.root, self.paths) as observation:
                lease = authority.MacAuthorityLease(observation)
                lease.stop_lock_evidence(instance)
                with self.assertRaises(control.ControlError) as caught:
                    lease.assert_stopped(instance)
        self.assertEqual(caught.exception.code, "lock_identity_changed")

    def test_stop_release_proof_rejects_replaced_original_lock_objects(self):
        # Review finding: holding the original double locks, replacing both
        # fixed lock paths and having the recorded process exited used to
        # return True because only the replacement files were re-acquired.
        pid, start, executable = self.exited_process()
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy,
                                         process=(pid, start, executable))
            instance = control._valid_record(
                self.root, records["active"], records["last_good_paths"],
                allow_unavailable_host=True, require_identity_evidence=False)
            with authority.locked_observation(self.root, self.paths) as observation:
                lease = authority.MacAuthorityLease(observation)
                evidence = lease.stop_lock_evidence(instance)
                self.assertEqual(evidence, {
                    name: (os.fstat(held.fileno()).st_dev, os.fstat(held.fileno()).st_ino)
                    for name, held in (("anchor", anchor), ("legacy", legacy))})
                replaced = []
                for path in (self.state / "anchor.lock", Path(self.paths["lock"])):
                    moved = path.with_name(path.name + ".original")
                    path.rename(moved)
                    path.write_text("replacement", encoding="utf-8")
                    path.chmod(0o600)
                    replaced.append(moved)
                with self.assertRaises(control.ControlError) as caught:
                    lease.assert_stopped(instance, require_identity_evidence=False)
            self.assertEqual(caught.exception.code, "release_unverified")
            # The original objects are still exclusively held, so reporting a
            # release at the replaced paths would have been false.
            for moved in replaced:
                with open(moved, "r+") as held:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_stop_release_proof_requires_pre_signal_lock_evidence(self):
        pid, start, executable = self.exited_process()
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy,
                                         process=(pid, start, executable))
        instance = control._valid_record(self.root, records["active"],
                                         records["last_good_paths"],
                                         allow_unavailable_host=True,
                                         require_identity_evidence=False)
        with authority.locked_observation(self.root, self.paths) as observation:
            with self.assertRaises(control.ControlError) as caught:
                authority.MacAuthorityLease(observation).assert_stopped(
                    instance, require_identity_evidence=False)
        self.assertEqual(caught.exception.code, "release_unverified")

    def test_start_gate_tolerates_boot_drift_only_under_a_schema_4_install(self):
        config = self.write_launch_config()
        with self.held_locks() as (anchor, legacy):
            records = self.write_mixed_records(anchor=anchor, legacy=legacy)
            active = dict(records["active"],
                          locks=self.drift_legacy_locks(records["active"]))
            write_instance_record(self.state, "active.json", active,
                                  anchor=anchor, legacy=legacy)
            records = read_instance_set(self.state)
        with authority.locked_observation(self.root, self.paths, config) as observation:
            lease = authority.MacAuthorityLease(observation)
            # install is schema 4 and verified SAME: the interrupted mixed
            # migration stays resumable (design §2/§5).
            lease._locks_free(records)

        # A pure schema-2 install has no schema-4-verified evidence, so the
        # same drift must not be tolerated (design §3).
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            active = dict(records["active"],
                          locks=self.drift_legacy_locks(records["active"]))
            write_instance_record(self.state, "active.json", active,
                                  anchor=anchor, legacy=legacy)
            records = read_instance_set(self.state)
        with authority.locked_observation(self.root, self.paths, config) as observation:
            lease = authority.MacAuthorityLease(observation)
            with self.assertRaises(control.ControlError) as caught:
                lease._locks_free(records)
        self.assertEqual(caught.exception.code, "lock_identity_changed")

        with self.held_locks() as (anchor, legacy):
            records = self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            ids = self.lock_ids(anchor, legacy)
            changed = dict(records["active"], locks={
                name: dict(value, inode=value["inode"] + 1)
                for name, value in ids.items()})
            write_instance_record(self.state, "active.json", changed,
                                  anchor=anchor, legacy=legacy)
            records = read_instance_set(self.state)
        with authority.locked_observation(self.root, self.paths, config) as observation:
            lease = authority.MacAuthorityLease(observation)
            with self.assertRaises(control.ControlError) as caught:
                lease._locks_free(records)
        self.assertEqual(caught.exception.code, "lock_identity_changed")

    def test_interrupted_mixed_migration_resumes_through_the_cli_gates(self):
        config = self.write_launch_config()
        listen = json.loads(config.read_text(encoding="utf-8"))["listen"]
        entry = self.root / "core" / "cli" / "model_proxy.py"
        entry.parent.mkdir(parents=True, mode=0o700)
        entry.write_text("# isolated start-gate fixture\n", encoding="utf-8")
        with self.held_locks() as (anchor, legacy):
            records = self.write_mixed_records(anchor=anchor, legacy=legacy,
                                               listen=listen)
            active = dict(records["active"],
                          locks=self.drift_legacy_locks(records["active"]))
            write_instance_record(self.state, "active.json", active,
                                  anchor=anchor, legacy=legacy)
        with mock.patch.object(authority.platform_process, "process_gone",
                               return_value=True):
            with authority.MacInstallationAuthority().locked(self.root, str(config)) as lease:
                self.assertEqual(lease.installation_state(),
                                 control.INSTALL_INITIALIZED)
                self.assertTrue(lease.assert_startable())

    def test_server_boot_drift_tolerance_requires_a_schema_4_install(self):
        from core import server
        with self.held_locks() as (anchor, legacy):
            stable = self.make_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            legacy_records = self.make_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
        mixed = {"install": stable["install"], "active": legacy_records["active"],
                 "last_good_paths": legacy_records["last_good_paths"]}
        self.assertTrue(server._upgrade_allows_boot_drift(mixed))
        self.assertTrue(server._upgrade_allows_boot_drift(
            dict(mixed, last_good_paths=stable["last_good_paths"])))
        self.assertFalse(server._upgrade_allows_boot_drift(legacy_records))
        self.assertFalse(server._upgrade_allows_boot_drift(
            {"install": stable["install"], "active": stable["active"],
             "last_good_paths": stable["last_good_paths"]}))
        self.assertFalse(server._upgrade_allows_boot_drift(
            {"install": legacy_records["install"], "active": stable["active"],
             "last_good_paths": legacy_records["last_good_paths"]}))
        self.assertFalse(server._upgrade_allows_boot_drift(None))

    def test_server_startup_forwards_the_narrow_boot_drift_criterion(self):
        # Drive the production ``_run_server`` up to its first stale-record
        # judgement and check the flag it derived from the record set. The
        # guard stops the start before any listener/record is created.
        from core import server

        class StopStartup(Exception):
            pass

        records = {"install": {"schema": POSIX_RECORD_SCHEMA},
                   "active": {"schema": INSTANCE_SCHEMA},
                   "last_good_paths": {"schema": INSTANCE_SCHEMA}}
        legacy_install = {"install": {"schema": INSTANCE_SCHEMA},
                          "active": {"schema": INSTANCE_SCHEMA},
                          "last_good_paths": {"schema": INSTANCE_SCHEMA}}
        entries = {"state_dir": SimpleNamespace(canonical=str(self.state)),
                   "anchor.lock": SimpleNamespace(canonical=str(self.root / "runtime/anchor.lock"),
                                                  device=1),
                   "lock": SimpleNamespace(canonical=str(self.root / "runtime/lock"), device=1)}
        entries.update({key: SimpleNamespace(canonical=str(self.root / "runtime" / key),
                                             device=1) for key in PATH_KEYS})
        snapshot = SimpleNamespace(as_dict=lambda: entries, project_root=str(self.root))
        legacy = SimpleNamespace(snapshot=snapshot)
        for record_set, expected in ((records, True), (legacy_install, False)):
            with self.subTest(install=record_set["install"]["schema"]):
                seen = []

                def stale(*args, **kwargs):
                    seen.append(kwargs.get("allow_boot_drift"))
                    raise StopStartup

                with mock.patch.object(server, "ConfigStore"), \
                     mock.patch.object(server, "resolve_listen",
                                       return_value={"host": "127.0.0.1", "port": 29491}), \
                     mock.patch.object(server, "resolve_runtime_paths",
                                       return_value=server._runtime_paths), \
                     mock.patch.object(server, "inspect_runtime_identities",
                                       return_value=snapshot), \
                     mock.patch.object(server, "_check_config_targets"), \
                     mock.patch.object(server, "read_instance_set",
                                       return_value=record_set), \
                     mock.patch.object(
                         server, "installation_identity",
                         return_value=SimpleNamespace(verdict=IDENTITY_SAME,
                                                      evidence=object())), \
                     mock.patch.object(server, "_lock_identities", return_value={}), \
                     mock.patch.object(server, "_ensure_stale_instance", side_effect=stale):
                    with self.assertRaises(StopStartup):
                        server._run_server(self.root / "config/model_proxy_config.json",
                                           {"host": "127.0.0.1", "port": 29491}, None, legacy)
                self.assertEqual(seen, [expected])

    def test_rebinding_accepts_schema_2_boot_drift_with_equal_inodes(self):
        config = self.write_launch_config()
        listen = json.loads(config.read_text(encoding="utf-8"))["listen"]
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy,
                                         listen=listen)
            install = dict(records["install"], root_identity=dict(
                records["install"]["root_identity"],
                device=records["install"]["root_identity"]["device"] + 1))
            write_instance_record(self.state, "install.json", install,
                                  anchor=anchor, legacy=legacy)
            active = dict(records["active"],
                          locks=self.drift_legacy_locks(records["active"]))
            write_instance_record(self.state, "active.json", active,
                                  anchor=anchor, legacy=legacy)
            expected_locks = self.drift_legacy_locks(records["active"])
        master, instream, outstream = self.pty_pair()
        os.write(master, b"unit fixture\nYES\n")
        with mock.patch.object(platform_process, "process_gone", return_value=True):
            with authority.locked_observation(self.root, self.paths, config) as observation:
                lease = authority._InitializationLease(observation)
                self.assertTrue(lease.rebaseline_interactively(instream, outstream))
        after = read_instance_set(self.state)
        self.assertEqual(after["install"]["schema"], POSIX_RECORD_SCHEMA)
        self.assertEqual(after["install"]["install_id"], self.install_id)
        self.assertEqual(after["active"]["schema"], POSIX_RECORD_SCHEMA)
        for name, role in (("anchor", "anchor.lock"), ("legacy", "lock")):
            recorded = after["active"]["locks"][name]
            self.assertEqual(recorded["kind"], VOLUME_IDENTITY_KIND)
            self.assertEqual(recorded["inode"], expected_locks[name]["inode"])


    def test_rebinding_refuses_a_recorded_lock_that_is_not_held(self):
        config = self.write_launch_config()
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            other_boot = dict(records["install"], root_identity=dict(
                records["install"]["root_identity"],
                device=records["install"]["root_identity"]["device"] + 1))
            write_instance_record(self.state, "install.json", other_boot,
                                  anchor=anchor, legacy=legacy)
            wrong = dict(records["active"], locks={
                "anchor": records["active"]["locks"]["anchor"],
                "legacy": dict(records["active"]["locks"]["legacy"],
                               inode=records["active"]["locks"]["legacy"]["inode"] + 1)})
            write_instance_record(self.state, "active.json", wrong,
                                  anchor=anchor, legacy=legacy)
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        instream = os.fdopen(os.dup(slave), "r", buffering=1)
        outstream = os.fdopen(os.dup(slave), "w", buffering=1)
        self.addCleanup(instream.close)
        self.addCleanup(outstream.close)
        with authority.locked_observation(self.root, self.paths, config) as observation:
            lease = authority._InitializationLease(observation)
            with self.assertRaises(PreflightError) as caught:
                lease.rebaseline_interactively(instream, outstream)
        self.assertEqual(caught.exception.code, "lock_identity_changed")

    def rebindable_fixture(self):
        """Schema-2 records from another boot: install and lock devices drift."""
        config = self.write_launch_config()
        listen = json.loads(config.read_text(encoding="utf-8"))["listen"]
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy,
                                         listen=listen)
            install = dict(records["install"], root_identity=dict(
                records["install"]["root_identity"],
                device=records["install"]["root_identity"]["device"] + 1))
            write_instance_record(self.state, "install.json", install,
                                  anchor=anchor, legacy=legacy)
            active = dict(records["active"],
                          locks=self.drift_legacy_locks(records["active"]))
            write_instance_record(self.state, "active.json", active,
                                  anchor=anchor, legacy=legacy)
        return config

    def test_rebinding_refuses_a_recorded_process_that_is_still_alive(self):
        config = self.rebindable_fixture()
        master, instream, outstream = self.pty_pair()
        os.write(master, b"unit fixture\nYES\n")
        with mock.patch.object(platform_process, "process_gone", return_value=False):
            with authority.locked_observation(self.root, self.paths, config) as observation:
                lease = authority._InitializationLease(observation)
                with self.assertRaises(PreflightError) as caught:
                    lease.rebaseline_interactively(instream, outstream)
        self.assertEqual(caught.exception.code, "process_still_active")
        after = read_instance_set(self.state)
        self.assertEqual(after["install"]["schema"], INSTANCE_SCHEMA)
        self.assertEqual(after["active"]["schema"], INSTANCE_SCHEMA)

    def start_owned_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait()
        self.addCleanup(cleanup)
        return child

    def test_rebinding_refuses_a_live_process_whose_image_changed(self):
        # ``process_identity_changed`` with an unchanged creation time means a
        # live process that exec'd elsewhere; it is not exit evidence. The real
        # child below is still running while the record claims another image.
        config = self.rebindable_fixture()
        child = self.start_owned_process()
        start = discovery.process_start_identity(child.pid)
        with self.held_locks() as (anchor, legacy):
            current = read_instance_set(self.state)
            active = dict(current["active"], pid=child.pid,
                          process_start=dict(start),
                          executable="/not/the/child/image")
            write_instance_record(self.state, "active.json", active,
                                  anchor=anchor, legacy=legacy)
        master, instream, outstream = self.pty_pair()
        os.write(master, b"unit fixture\nYES\n")
        with authority.locked_observation(self.root, self.paths, config) as observation:
            lease = authority._InitializationLease(observation)
            with self.assertRaises(PreflightError) as caught:
                lease.rebaseline_interactively(instream, outstream)
        self.assertEqual(caught.exception.code, "process_still_active")
        after = read_instance_set(self.state)
        self.assertEqual(after["install"]["schema"], INSTANCE_SCHEMA)
        self.assertEqual(after["active"]["schema"], INSTANCE_SCHEMA)
        self.assertIsNone(child.poll())

    def test_rebinding_accepts_a_reused_pid_with_a_different_creation_time(self):
        config = self.rebindable_fixture()
        child = self.start_owned_process()
        start = discovery.process_start_identity(child.pid)
        image = discovery.process_executable(child.pid)
        reused = dict(start, seconds=start["seconds"] - 1)
        with self.held_locks() as (anchor, legacy):
            current = read_instance_set(self.state)
            active = dict(current["active"], pid=child.pid,
                          process_start=reused, executable=image)
            write_instance_record(self.state, "active.json", active,
                                  anchor=anchor, legacy=legacy)
        master, instream, outstream = self.pty_pair()
        os.write(master, b"unit fixture\nYES\n")
        with authority.locked_observation(self.root, self.paths, config) as observation:
            lease = authority._InitializationLease(observation)
            self.assertTrue(lease.rebaseline_interactively(instream, outstream))
        after = read_instance_set(self.state)
        self.assertEqual(after["install"]["schema"], POSIX_RECORD_SCHEMA)
        self.assertEqual(after["install"]["install_id"], self.install_id)

    def test_rebinding_accepts_a_whole_volume_replacement_with_equal_inodes(self):
        # A restored/cloned volume can keep the directory and lock inodes while
        # changing the volume UUID. Schema-2 records carry only a boot-local
        # device, so that evidence cannot distinguish this from another boot;
        # the explicit operator confirmation is what accepts it.
        config = self.rebindable_fixture()
        new_volume = UUID_B
        root_inode = verify_record_directory(self.state)[1]
        master, instream, outstream = self.pty_pair()
        os.write(master, b"unit fixture\nYES\n")
        with mock.patch.object(platform_process, "process_gone", return_value=True), \
             mock.patch.object(discovery, "_darwin_volume_uuid", return_value=new_volume):
            with authority.locked_observation(self.root, self.paths, config) as observation:
                lease = authority._InitializationLease(observation)
                self.assertTrue(lease.rebaseline_interactively(instream, outstream))
        after = read_instance_set(self.state)
        self.assertEqual(after["install"]["schema"], POSIX_RECORD_SCHEMA)
        self.assertEqual(after["install"]["install_id"], self.install_id)
        self.assertEqual(after["install"]["root_identity"],
                         {"kind": VOLUME_IDENTITY_KIND, "uuid": new_volume,
                          "inode": root_inode})
        for name in ("anchor", "legacy"):
            self.assertEqual(after["active"]["locks"][name]["uuid"], new_volume)

    def test_rebinding_refuses_a_held_lock_on_another_volume(self):
        config = self.rebindable_fixture()
        root_uuid = collect_root_identity(self.state).uuid
        lock_path = Path(self.paths["lock"])

        def volume(path):
            return UUID_B if Path(path) == lock_path else root_uuid

        master, instream, outstream = self.pty_pair()
        os.write(master, b"unit fixture\nYES\n")
        with mock.patch.object(platform_process, "process_gone", return_value=True), \
             mock.patch.object(discovery, "_darwin_volume_uuid", side_effect=volume):
            with authority.locked_observation(self.root, self.paths, config) as observation:
                lease = authority._InitializationLease(observation)
                with self.assertRaises(PreflightError) as caught:
                    lease.rebaseline_interactively(instream, outstream)
        # The recorded inode matches, but the held lock is not on the project
        # root's current volume; rebinding refuses that inconsistent layout.
        self.assertEqual(caught.exception.code, "lock_identity_changed")
        after = read_instance_set(self.state)
        self.assertEqual(after["install"]["schema"], INSTANCE_SCHEMA)

    def test_rebinding_refuses_locks_without_volume_ownership_evidence(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            install = dict(records["install"], root_identity=dict(
                records["install"]["root_identity"],
                device=records["install"]["root_identity"]["device"] + 1))
            write_instance_record(self.state, "install.json", install,
                                  anchor=anchor, legacy=legacy)
            current = read_instance_set(self.state)
            evidence = collect_root_identity(self.state)
            ids = self.lock_ids(anchor, legacy)
            with mock.patch.object(platform_process, "process_gone", return_value=True):
                with self.assertRaises(PreflightError) as caught:
                    rebind_installation(self.state, current, evidence, ids,
                                        anchor=anchor, legacy=legacy,
                                        source="unit_fixture")
        self.assertEqual(caught.exception.code, "lock_ownership_unverified")
        after = read_instance_set(self.state)
        self.assertEqual(after["install"]["schema"], INSTANCE_SCHEMA)

    def _initialization_lease(self, state, rebaseline=None):
        lease = SimpleNamespace(
            installation_state=lambda: state,
            initialize_interactively=lambda stdin, stdout: True)
        if rebaseline is not None:
            lease.rebaseline_interactively = rebaseline
        return lease

    def test_initialization_gate_reports_a_changed_directory_from_records(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(POSIX_RECORD_SCHEMA, anchor=anchor, legacy=legacy)
            changed = dict(records["install"], root_identity=dict(
                records["install"]["root_identity"],
                inode=records["install"]["root_identity"]["inode"] + 1))
            write_instance_record(self.state, "install.json", changed,
                                  anchor=anchor, legacy=legacy)
        cli = control.MacControl(self.root, config_path=self.root / "config/model_proxy_config.json",
                                 out=io.StringIO(), err=io.StringIO(),
                                 terminal_in=io.StringIO(), terminal_out=io.StringIO())
        with authority.locked_observation(self.root, self.paths) as observation:
            self.assertEqual(observation.installation_state(), control.INSTALL_CHANGED)
            with self.assertRaises(PreflightError) as caught:
                observation.installation_identity()
            self.assertEqual(caught.exception.code, "installation_changed")
            with self.assertRaises(control.ControlError) as caught:
                cli._prepare_initialization(authority._InitializationLease(observation),
                                            allow_initialize=True)
        self.assertEqual(caught.exception.code, "installation_changed")

    def test_initialization_gate_handles_identity_states(self):
        with self.held_locks() as (anchor, legacy):
            records = self.write_records(INSTANCE_SCHEMA, anchor=anchor, legacy=legacy)
            other_boot = dict(records["install"], root_identity=dict(
                records["install"]["root_identity"],
                device=records["install"]["root_identity"]["device"] + 1))
            write_instance_record(self.state, "install.json", other_boot,
                                  anchor=anchor, legacy=legacy)
        cli = control.MacControl(self.root, config_path=self.root / "config/model_proxy_config.json",
                                 out=io.StringIO(), err=io.StringIO(),
                                 terminal_in=io.StringIO(), terminal_out=io.StringIO())
        called = []
        with authority.locked_observation(self.root, self.paths) as observation:
            self.assertEqual(observation.installation_state(),
                             control.INSTALL_IDENTITY_UNVERIFIED)
            lease = authority._InitializationLease(observation)
            lease.rebaseline_interactively = lambda stdin, stdout: called.append((stdin, stdout))
            with self.assertRaises(control.ControlError) as caught:
                cli._prepare_initialization(lease, allow_initialize=True)
        self.assertEqual(caught.exception.code, "initialization_interactive_required")
        self.assertEqual(called, [])

    def test_unverified_identity_needs_confirmed_rebinding_on_a_tty(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        instream = os.fdopen(os.dup(slave), "r", buffering=1)
        outstream = os.fdopen(os.dup(slave), "w", buffering=1)
        self.addCleanup(instream.close)
        self.addCleanup(outstream.close)
        cli = control.MacControl(self.root, config_path=self.root / "config/model_proxy_config.json",
                                 out=io.StringIO(), err=io.StringIO(),
                                 terminal_in=instream, terminal_out=outstream)
        answered = self._initialization_lease(control.INSTALL_IDENTITY_UNVERIFIED,
                                              lambda stdin, stdout: False)
        with self.assertRaises(control.ControlError) as caught:
            cli._prepare_initialization(answered, allow_initialize=True)
        self.assertEqual(caught.exception.code, "initialization_unconfirmed")
        state = {"value": control.INSTALL_IDENTITY_UNVERIFIED}

        def rebind(stdin, stdout):
            state["value"] = control.INSTALL_INITIALIZED
            return True

        confirmed = SimpleNamespace(
            installation_state=lambda: state["value"],
            initialize_interactively=lambda stdin, stdout: True,
            rebaseline_interactively=rebind)
        cli._prepare_initialization(confirmed, allow_initialize=True)
        self.assertEqual(state["value"], control.INSTALL_INITIALIZED)


@unittest.skipUnless(sys.platform == "darwin", "native Darwin process evidence required")
class RebindProcessEvidenceTests(unittest.TestCase):
    """``process_identity_changed`` is exit evidence only for a reused PID.

    Uses a real owned child and the production ``process_gone`` /
    ``process_start_identity`` chain; nothing about the process evidence is
    mocked here.
    """

    def setUp(self):
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap)
        self.start = discovery.process_start_identity(self.child.pid)
        self.image = discovery.process_executable(self.child.pid)
        self.assertIsInstance(self.start, dict)
        self.assertIsInstance(self.image, str)

    def reap(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait()

    def test_same_start_with_a_changed_image_is_not_exit_evidence(self):
        with self.assertRaises(discovery.PreflightError) as caught:
            discovery._rebind_stale_process_evidence({
                "pid": self.child.pid, "process_start": dict(self.start),
                "executable": "/not/the/child/image"})
        self.assertEqual(caught.exception.code, "process_still_active")
        self.assertIsNone(self.child.poll())

    def test_a_different_creation_time_is_exit_evidence(self):
        # The recorded creation time no longer exists: the PID was reused.
        recorded = dict(self.start, seconds=self.start["seconds"] - 1)
        discovery._rebind_stale_process_evidence({
            "pid": self.child.pid, "process_start": recorded,
            "executable": self.image})

    def test_an_unobservable_identity_is_refused(self):
        recorded = dict(self.start, seconds=self.start["seconds"] - 1)
        with mock.patch.object(discovery, "process_start_identity", return_value=None):
            with self.assertRaises(discovery.PreflightError) as caught:
                discovery._rebind_stale_process_evidence({
                    "pid": self.child.pid, "process_start": recorded,
                    "executable": self.image})
        self.assertEqual(caught.exception.code, "process_unverified")

    def test_an_exited_process_is_accepted(self):
        self.child.kill()
        self.child.wait()
        discovery._rebind_stale_process_evidence({
            "pid": self.child.pid, "process_start": dict(self.start),
            "executable": self.image})


if __name__ == "__main__":
    unittest.main()
