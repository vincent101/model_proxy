"""Native primitives for the finite Windows protected-write path.

These observations are not a CAS; native Windows behavior is not yet tested.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core import platform_windows as win, windows_paths
from core.windows_secure_files import (
    TargetSnapshot, _MISSING, _safe_exception_field, _write_exception_info,
)


class CandidateError(RuntimeError):
    def __init__(self, code: str, *, created: bool | None = None,
                 candidate=None, content_may_exist: bool = False,
                 release_unknown: bool = False):
        super().__init__(code)
        self.code = code
        self.created = created
        self.candidate = candidate
        self.content_may_exist = content_may_exist
        self.release_unknown = release_unknown


def _release_is_unknown(exc):
    # Metadata is read statically: arbitrary exception properties may block.
    _, unknown, _ = _write_exception_info(exc)
    owned, unreadable = _safe_exception_field(exc, "owned_handle")
    return unknown or unreadable or (owned is not _MISSING and owned is not None)


def _raise_candidate_error(error):
    try:
        raise error from None
    except CandidateError as sanitized:
        sanitized.__context__ = None
        raise


class CommitOutcomeUnknown(RuntimeError):
    """An invoked native commit call may have made partial changes."""


@dataclass(frozen=True, repr=False)
class _CommitEvidence:
    """Internal handoff of observations, not an independent authority."""
    target: str = field(repr=False)
    source: str = field(repr=False)
    volume: tuple = field(repr=False)
    exists: bool


class _CommitCalls:
    """Native commit calls used by the finite writer.

    The last path observation is not a CAS against concurrent local writers.
    """
    def __init__(self, *, api):
        if api is None:
            raise CandidateError("commit_api_unverified")
        self.api = api

    @staticmethod
    def _check(target, source, evidence, exists):
        try:
            target_drive, target_parts = windows_paths._parse(target)
            source_drive, source_parts = windows_paths._parse(source)
            if (not isinstance(evidence, _CommitEvidence) or evidence.target != target or
                    evidence.source != source or evidence.exists is not exists or
                    type(evidence.volume) is not tuple or len(evidence.volume) != 4 or
                    type(evidence.volume[0]) is not int or evidence.volume[0] != 3 or
                    evidence.volume[1] != "NTFS" or
                    type(evidence.volume[2]) is not int or not 0 <= evidence.volume[2] <= 0xFFFFFFFF or
                    not isinstance(evidence.volume[3], str) or
                    not re.fullmatch(r"\\Device\\HarddiskVolume[0-9]+", evidence.volume[3], re.I) or
                    (target_drive == source_drive and
                     tuple(p.casefold() for p in target_parts) ==
                     tuple(p.casefold() for p in source_parts))):
                raise CandidateError("commit_path_unverified")
        except (windows_paths.PathEvidenceError, TypeError, IndexError):
            raise CandidateError("commit_path_unverified") from None

    def replace(self, target, source, evidence=None):
        self._check(target, source, evidence, True)
        try:
            self.api.replace_existing(target, source)
        except BaseException:
            # ReplaceFileW failures can leave partial effects. Do not map an
            # arbitrary error to a definitely-not-replaced result.
            raise CommitOutcomeUnknown("replace_outcome_unknown") from None

    def move_new(self, target, source, evidence=None):
        self._check(target, source, evidence, False)
        try:
            self.api.move_new(source, target)
        except BaseException:
            raise CommitOutcomeUnknown("move_outcome_unknown") from None


class ProductWriteBackend:
    """Finite native transaction; checks are observations, not a CAS.

    All artifacts are CREATE_NEW with explicit security descriptors. Existing
    descriptors must read back exactly; new files must read back private and
    protected. No native Windows result is implied by use of this adapter.
    """
    def __init__(self, snapshot, *, api=None, acl_backend=None,
                 stage_directory=None, backup_directory=None,
                 stage_boundary=None, backup_boundary=None,
                 stage_at_target_parent=False):
        from core import windows_acl
        if not isinstance(snapshot, TargetSnapshot):
            raise CandidateError("snapshot_invalid", created=False)
        self.snapshot = snapshot
        self.api = win._api(api)
        self.acl_backend = acl_backend
        self._owned = None
        self._released = False
        self._candidate_ids = set()
        self._directories = {}
        self._role_boundaries = {}
        self._reservations = {}
        self._stage_read = None
        self._stage_lease = None
        self._target_parent_lease = None
        self._commit_parent_proof = None
        self._commit_evidence = None
        self._stage_directory = stage_directory
        self._backup_directory = backup_directory
        self._stage_boundary = stage_boundary
        self._backup_boundary = backup_boundary
        if (type(stage_at_target_parent) is not bool or
                (stage_at_target_parent and stage_directory is None) or
                (stage_directory is None and stage_boundary is not None) or
                (backup_directory is None and backup_boundary is not None)):
            raise CandidateError("candidate_boundary_unverified", created=False)
        self._stage_at_target_parent = stage_at_target_parent
        snapshot.revalidate()
        handles = snapshot._target_lease._handles
        parent = handles[-2] if snapshot.exists else handles[-1]
        parent_proof = windows_acl.read_stable_evidence(
            parent.raw, kind="directory", policy="ancestor",
            required_access=windows_paths._DIR_CHECK_ACCESS | 0x2,
            backend=acl_backend)
        self.user = parent_proof.observation.user
        self._target_parent_proof = parent_proof
        self._target_parent_raw = parent.raw
        self._target_proof = None
        if snapshot.exists:
            self._target_proof = windows_acl.read_stable_evidence(
                handles[-1].raw, kind="file", policy="private",
                required_access=windows_acl.FILE_GENERIC_READ | windows_acl.FILE_GENERIC_WRITE | 0x10000,
                backend=acl_backend)
        self.original_handle = handles[-1].raw if snapshot.exists else None
        self.original_protection = snapshot._read.protection if snapshot.exists else None

    def reserve(self, role):
        import secrets
        from core import windows_acl
        if role not in ("stage", "backup") or role in self._directories:
            raise CandidateError("candidate_role_unverified", created=False)
        explicit = self._stage_directory if role == "stage" else self._backup_directory
        boundary = ((self._stage_boundary if role == "stage" else self._backup_boundary)
                    if explicit is not None else self.snapshot._trusted_boundary)
        target_parent_path = self.snapshot.path.rsplit("\\", 1)[0]
        parent = explicit if explicit is not None else target_parent_path
        target_parent_stage = role == "stage" and self._stage_at_target_parent
        lease = None
        try:
            if target_parent_stage and windows_paths._parse(parent) != windows_paths._parse(target_parent_path):
                raise CandidateError("candidate_parent_unverified", created=False)
            private_directory = explicit is not None and not target_parent_stage
            lease = windows_paths.inspect(parent, backend=self.snapshot._monitor,
                                          kind="directory", private=private_directory,
                                          trusted_boundary=boundary)
            if not lease.evidence.exists:
                raise CandidateError("candidate_parent_missing", created=False)
            handle = lease._handles[-1]
            proof = windows_acl.read_stable_evidence(
                handle.raw, kind="directory", policy="private" if private_directory else "ancestor",
                required_access=windows_paths._DIR_CHECK_ACCESS | 0x2,
                backend=self.acl_backend)
            lease.revalidate()
            self.snapshot.revalidate()
            target_index = -2 if self.snapshot.exists else -1
            target_parent = self.snapshot._target_lease._handles[target_index]
            if (target_parent_stage and
                    (lease.evidence.components[-1].observation !=
                     self.snapshot._target_lease.evidence.components[target_index].observation or
                     handle.volume != target_parent.volume)):
                raise CandidateError("candidate_parent_unverified", created=False)
            if role == "stage" and handle.volume != target_parent.volume:
                raise CandidateError("commit_volume_or_identity_unverified", created=False)
            path = parent.rstrip("\\") + "\\.model-proxy-" + role + "-" + secrets.token_hex(12)
            self._directories[role] = (lease, proof)
            self._role_boundaries[role] = boundary
            self._reservations[path] = role
            return path
        except BaseException:
            if lease is not None:
                try:
                    lease.close()
                except BaseException:
                    raise CandidateError("release_unknown", created=False, release_unknown=True) from None
            raise

    def _verify_candidate_path(self, path, directory, directory_proof, boundary):
        from core import windows_acl
        snap = self.snapshot
        directory.revalidate()
        windows_acl.verify_unchanged(directory._handles[-1].raw, directory_proof,
                                     backend=self.acl_backend)
        snap.revalidate()
        # A generated name may still collide with an absent target (including
        # an unresolved 8.3/Unicode alias). Reject before CREATE_NEW, not after
        # accidentally creating the target as a candidate.
        proposed = windows_paths.inspect(path, backend=snap._monitor, kind="file",
                                         private=True, single_link=True, trusted_boundary=boundary)
        try:
            if (proposed.evidence.exists or not proposed.evidence.direct_parent_observed or
                    proposed.evidence.components[-1].observation !=
                    directory.evidence.components[-1].observation or
                    proposed._handles[-1].volume != directory._handles[-1].volume or
                    windows_paths.may_alias(proposed, snap._target_lease)):
                raise CandidateError("candidate_path_unverified", created=False)
        finally:
            try:
                proposed.close()
            except BaseException:
                raise CandidateError("release_unknown", created=False, release_unknown=True) from None
        directory.revalidate()
        snap.revalidate()

    def create_and_write(self, path, data):
        from core import windows_acl
        snap = self.snapshot
        role = self._reservations.get(path)
        if role is None:
            raise CandidateError("candidate_not_authorized", created=False)
        directory, directory_proof = self._directories[role]
        boundary = self._role_boundaries[role]
        try:
            self._verify_candidate_path(path, directory, directory_proof, boundary)
        except BaseException as exc:
            code, unknown, _ = _write_exception_info(exc)
            unknown = unknown or _release_is_unknown(exc) or snap._monitor.release_unknown
            _raise_candidate_error(CandidateError(
                "release_unknown" if unknown else (code or "candidate_path_unverified"),
                created=False, release_unknown=unknown))
        owned = None
        handle = None
        close_started = False
        created = False
        pending = None
        close_unknown = False
        try:
            handle = self.api.open_private_candidate(path, original_handle=self.original_handle,
                                                     user_sid=self.user)
            created = True
            owned = win.WindowsFileHandle(handle, self.api, created=True)
            self._owned = owned
            ident = win.file_identity(handle, backend=self.api)
            identity_key = (ident.volume_serial, ident.file_index)
            original = snap._read.identity if snap.exists else None
            if (identity_key in self._candidate_ids or
                    original is not None and identity_key == (original.volume_serial, original.file_index)):
                raise CandidateError("candidate_identity_unverified", created=True)
            self._candidate_ids.add(identity_key)
            proof = windows_acl.read_stable_evidence(
                handle, kind="file", policy="private",
                required_access=windows_acl.FILE_GENERIC_READ | windows_acl.FILE_GENERIC_WRITE,
                backend=self.acl_backend)
            protection = win.acl_protection(proof)
            if proof.observation.identity != (ident.volume_serial, ident.file_index,
                                               ident.link_count, ident.attributes):
                raise CandidateError("candidate_identity_unverified", created=True)
            # Existing objects preserve the actual descriptor, including its
            # inheritance/protection flag. Unprotected is not itself unsafe:
            # the ACL policy and AccessCheck above still have to succeed.
            # For a new target we requested a protected descriptor and must
            # reject an OS which loses that flag during creation.
            if snap.exists:
                if protection != self.original_protection:
                    raise CandidateError("candidate_protection_unverified", created=True)
            elif (proof.observation.owner != self.user or
                  not proof.observation.control & 0x1000):
                raise CandidateError("candidate_protection_unverified", created=True)
            marker = self.api.file_marker(handle)
            if marker[:2] != (ident.volume_serial, ident.file_index) or marker[4] != 0:
                raise CandidateError("candidate_identity_unverified", created=True)
            # Verify the actual pathname and creation parent before writing any bytes.
            lease = windows_paths.inspect(path, backend=snap._monitor, kind="file",
                                          private=True, single_link=True, trusted_boundary=boundary)
            lease_error = None
            try:
                observed = lease.evidence.components[-1].observation
                if (not lease.evidence.exists or observed.volume != ident.volume_serial or
                        int.from_bytes(observed.file_id, "big") != ident.file_index):
                    raise CandidateError("candidate_identity_unverified", created=True)
                if (lease.evidence.components[-2].observation !=
                        directory.evidence.components[-1].observation or
                        lease._handles[-2].volume != directory._handles[-1].volume):
                    raise CandidateError("candidate_parent_changed", created=True)
                lease.revalidate()
                directory.revalidate()
                windows_acl.verify_unchanged(directory._handles[-1].raw, directory_proof,
                                             backend=self.acl_backend)
                snap.revalidate()
                windows_acl.verify_unchanged(handle, proof, backend=self.acl_backend)
                self.api.write_all_and_flush(handle, data)
            except BaseException as exc:
                lease_error = exc
            try:
                lease.close()
            except BaseException:
                # A failed lease close is unknown regardless of the earlier
                # error; do not inspect or lose it before closing the handle.
                close_unknown = True
            if lease_error is not None:
                raise lease_error
            if close_unknown:
                raise CandidateError("release_unknown", created=True, release_unknown=True)
            # Close exactly once before the pathname readback/rename.
            self._owned = None
            try:
                close_started = True
                owned.close()
            except BaseException:
                raise CandidateError("release_unknown", created=True, release_unknown=True) from None
            owned = None
            result = self.inspect(path)
            if result.identity != ident or result.protection != protection or result.data != data:
                raise CandidateError("candidate_readback_unverified", created=True)
            snap.revalidate()
            if role == "stage":
                self._stage_read = result
        except BaseException as exc:
            # Keep the original untouched until after the candidate handle
            # has had exactly one close attempt.
            pending = exc
        finally:
            if handle is not None and not close_started:
                try:
                    self._owned = None
                    close_started = True
                    if owned is None:
                        self.api.close(handle)
                    else:
                        owned.close()
                except BaseException:
                    close_unknown = True
        if pending is not None or close_unknown:
            code = None
            unknown = close_unknown
            known_created = True if created else None
            candidate = None
            content_may_exist = False
            if pending is not None:
                code, metadata_unknown, reported_created = _write_exception_info(
                    pending, include_created=True)
                unknown = unknown or metadata_unknown or _release_is_unknown(pending)
                if type(pending) is CandidateError:
                    observation, bad_observation = _safe_exception_field(pending, "candidate")
                    content, bad_content = _safe_exception_field(pending, "content_may_exist")
                    if not bad_observation and observation is not _MISSING:
                        candidate = observation
                    content_may_exist = (content if type(content) is bool and not bad_content else True)
                if not created:
                    known_created = reported_created
                    release, unreadable = _safe_exception_field(pending, "release_unknown")
                    unknown = unknown or unreadable
                    # No handle was transferred: only an explicit failure
                    # observation can rule out an uncertain native creation.
                    if release is not False and reported_created is not False:
                        unknown = True
            _raise_candidate_error(CandidateError(
                "release_unknown" if unknown else (code or "candidate_unverified"),
                created=known_created, candidate=candidate,
                content_may_exist=content_may_exist, release_unknown=unknown))
        return result

    def inspect(self, path):
        from core import windows_acl
        role = self._reservations.get(path)
        directory, proof = self._directories[role] if role is not None else (None, None)
        if directory is not None:
            directory.revalidate()
            windows_acl.verify_unchanged(directory._handles[-1].raw, proof,
                                         backend=self.acl_backend)
        result = win.read_guarded_file(path, with_data=True, max_bytes=self.snapshot._max_bytes,
                                       backend=self.api, acl_backend=self.acl_backend,
                                       trusted_boundary=(self._role_boundaries[role] if role is not None
                                                         else self.snapshot._trusted_boundary))
        if directory is not None:
            directory.revalidate()
            windows_acl.verify_unchanged(directory._handles[-1].raw, proof,
                                         backend=self.acl_backend)
        return result

    def revalidate(self):
        from core import windows_acl
        self.snapshot.revalidate()
        windows_acl.verify_unchanged(self._target_parent_raw, self._target_parent_proof,
                                     backend=self.acl_backend)
        if self._target_proof is not None:
            windows_acl.verify_unchanged(self.original_handle, self._target_proof,
                                         backend=self.acl_backend)
        for lease, proof in self._directories.values():
            lease.revalidate()
            windows_acl.verify_unchanged(lease._handles[-1].raw, proof, backend=self.acl_backend)

    def release_precommit(self):
        from core import windows_acl
        snap = self.snapshot
        stages = [path for path, role in self._reservations.items() if role == "stage"]
        if len(stages) != 1 or self._stage_read is None:
            raise CandidateError("stage_unverified")
        stage = stages[0]
        directory, _ = self._directories["stage"]
        self._stage_lease = windows_paths.inspect(stage, backend=snap._monitor, kind="file",
                                                 private=True, single_link=True,
                                                 trusted_boundary=self._role_boundaries["stage"])
        source = self._stage_lease
        source_obs = source.evidence.components[-1].observation
        stage_ident = self._stage_read.identity
        parent = snap._target_lease._handles[-2 if snap.exists else -1]
        source_parent = source._handles[-2]
        self._target_parent_lease = windows_paths.inspect(
            snap.path.rsplit("\\", 1)[0], backend=snap._monitor,
            kind="directory", private=False, trusted_boundary=snap._trusted_boundary)
        target_parent_lease = self._target_parent_lease
        expected_parent = snap._target_lease.evidence.components[-2 if snap.exists else -1].observation
        if (not target_parent_lease.evidence.exists or
                target_parent_lease.evidence.components[-1].observation != expected_parent or
                target_parent_lease._handles[-1].volume != parent.volume):
            raise CandidateError("target_parent_changed")
        target_parent_proof = windows_acl.read_stable_evidence(
            target_parent_lease._handles[-1].raw, kind="directory", policy="ancestor",
            required_access=windows_paths._DIR_CHECK_ACCESS | 0x2,
            backend=self.acl_backend)
        # Volume tuple contains verified NTFS type, serial and actual device,
        # all re-observed by the path backend; drive spelling is not evidence.
        volume = parent.volume
        if (not source.evidence.exists or source_obs.volume != stage_ident.volume_serial or
                int.from_bytes(source_obs.file_id, "big") != stage_ident.file_index or
                source.evidence.components[-2].observation != directory.evidence.components[-1].observation or
                source_parent.volume != directory._handles[-1].volume or
                source._handles[-1].volume != volume or source_parent.volume != volume):
            raise CandidateError("commit_volume_or_identity_unverified")
        if windows_paths.may_alias(source, snap._target_lease):
            raise CandidateError("commit_alias_unverified")
        source_proof = windows_acl.read_stable_evidence(
            source._handles[-1].raw, kind="file", policy="private",
            required_access=windows_acl.FILE_GENERIC_READ | windows_acl.FILE_GENERIC_WRITE | 0x10000,
            backend=self.acl_backend)
        if self.inspect(stage) != self._stage_read:
            raise CandidateError("stage_changed")
        self.revalidate()
        source.revalidate()
        target_parent_lease.revalidate()
        windows_acl.verify_unchanged(target_parent_lease._handles[-1].raw, target_parent_proof,
                                     backend=self.acl_backend)
        windows_acl.verify_unchanged(source._handles[-1].raw, source_proof,
                                     backend=self.acl_backend)
        self._commit_evidence = _CommitEvidence(snap.path, stage, volume, snap.exists)
        self._commit_parent_proof = target_parent_proof
        # These no-delete-share file handles must be released for the native
        # rename. The separately held directory leases survive through commit.
        try:
            source.close()
            self._stage_lease = None
            snap.close()
            self._released = True
        except BaseException:
            raise CandidateError("release_unknown", release_unknown=True) from None

    def commit(self, stage):
        from core import windows_acl
        calls = _CommitCalls(api=self.api)
        evidence = self._commit_evidence
        calls._check(self.snapshot.path, stage, evidence, self.snapshot.exists)
        # The source/target file leases had to close before rename. Keep and
        # revalidate both parent leases, then re-observe the source after that
        # handoff. These observations are not a CAS and do not prevent ABA.
        target_parent = self._target_parent_lease
        directory, proof = self._directories["stage"]
        if target_parent is None or self._commit_parent_proof is None:
            raise CandidateError("commit_volume_or_identity_unverified")
        target_parent.revalidate()
        directory.revalidate()
        windows_acl.verify_unchanged(target_parent._handles[-1].raw, self._commit_parent_proof,
                                     backend=self.acl_backend)
        windows_acl.verify_unchanged(directory._handles[-1].raw, proof,
                                     backend=self.acl_backend)
        if (target_parent._handles[-1].volume != evidence.volume or
                directory._handles[-1].volume != evidence.volume or
                self.inspect(stage) != self._stage_read):
            raise CandidateError("commit_volume_or_identity_unverified")
        target_parent.revalidate()
        directory.revalidate()
        if self.snapshot.exists:
            calls.replace(self.snapshot.path, stage, evidence)
        else:
            calls.move_new(self.snapshot.path, stage, evidence)

    def postcheck(self):
        return self.inspect(self.snapshot.path)

    def finish(self):
        failed = False
        if self._owned is not None:
            owned, self._owned = self._owned, None
            try:
                owned.close()
            except BaseException:
                failed = True
        if self._stage_lease is not None:
            lease, self._stage_lease = self._stage_lease, None
            try:
                lease.close()
            except BaseException:
                failed = True
        if self._target_parent_lease is not None:
            lease, self._target_parent_lease = self._target_parent_lease, None
            try:
                lease.close()
            except BaseException:
                failed = True
        for lease, _ in self._directories.values():
            try:
                lease.close()
            except BaseException:
                failed = True
        self._directories.clear()
        self._role_boundaries.clear()
        if not self._released:
            try:
                self.snapshot.close()
            except BaseException:
                failed = True
            self._released = True
        if failed or self.snapshot._monitor.release_unknown:
            raise CandidateError("release_unknown", release_unknown=True)
