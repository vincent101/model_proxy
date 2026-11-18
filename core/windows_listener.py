"""Standalone Windows socket overlap observations, not a service security gate.

Run explicitly with ``python -m core.windows_listener`` on an authorized test
machine. Every listening socket is loopback-only and obtains its port from the
OS (port 0); an overlapping contender uses that port only while our listener
is held. The default matrix binds only loopback addresses, including mapped
IPv4 loopback. Wildcard contenders and their port-0 controls require the
separate ``--allow-wildcard-bind`` opt-in and are never listened on. Nothing
connects, consults process tables, or stops a process.

This is a finite empirical matrix, not proof of native Windows, ownership of a
production listener, behavior against hostile processes, restart/TIME_WAIT, or
readiness to start the service. Wine exposes the Windows API too, and is not a
substitute for an authorized native Windows run. The service remains gated.
"""

from contextlib import ExitStack
from dataclasses import asdict, dataclass
import ipaddress
import json
import os
import socket
import sys


@dataclass(frozen=True)
class _Endpoint:
    family: str
    host: str
    v6only: int | None = None


@dataclass(frozen=True)
class _Case:
    name: str
    owner: _Endpoint
    contender: _Endpoint
    owner_option: str
    contender_option: str


_V4 = _Endpoint("ipv4", "127.0.0.1")
_V6 = _Endpoint("ipv6", "::1", 1)
_OVERLAPS = (
    ("v4_same", _V4, _V4),
    ("v4_wildcard", _V4, _Endpoint("ipv4", "0.0.0.0")),
    ("v6_same", _V6, _V6),
    ("v6_wildcard_v6only", _V6, _Endpoint("ipv6", "::", 1)),
    ("v6_wildcard_dual", _V6, _Endpoint("ipv6", "::", 0)),
    ("v4_v6_wildcard_dual", _V4, _Endpoint("ipv6", "::", 0)),
    ("v4_v6_mapped", _V4, _Endpoint("ipv6", "::ffff:127.0.0.1", 0)),
)
# Test both an exclusive first owner and an existing nonexclusive first owner.
_OPTIONS = (("exclusive", "default"), ("exclusive", "reuse"),
            ("exclusive", "exclusive"), ("default", "exclusive"),
            ("reuse", "exclusive"))
_CASES = tuple(_Case(f"{name}__{owner_opt}_then_{contender_opt}", owner,
                     contender, owner_opt, contender_opt)
               for name, owner, contender in _OVERLAPS
               for owner_opt, contender_opt in _OPTIONS)
_CONFLICT_CODES = frozenset((10013, 10048))  # WSAEACCES / WSAEADDRINUSE


class MatrixUnavailable(RuntimeError):
    """The diagnostic cannot safely run on this runtime."""


def _configure(sock, endpoint, option, net):
    if endpoint.family == "ipv6":
        sock.setsockopt(net.IPPROTO_IPV6, net.IPV6_V6ONLY, endpoint.v6only)
        if sock.getsockopt(net.IPPROTO_IPV6, net.IPV6_V6ONLY) != endpoint.v6only:
            raise MatrixUnavailable("IPV6_V6ONLY was not retained")
    if option == "exclusive":
        sock.setsockopt(net.SOL_SOCKET, net.SO_EXCLUSIVEADDRUSE, 1)
        if not sock.getsockopt(net.SOL_SOCKET, net.SO_EXCLUSIVEADDRUSE):
            raise MatrixUnavailable("SO_EXCLUSIVEADDRUSE was not retained")
    elif option == "reuse":
        sock.setsockopt(net.SOL_SOCKET, net.SO_REUSEADDR, 1)
        if not sock.getsockopt(net.SOL_SOCKET, net.SO_REUSEADDR):
            raise MatrixUnavailable("SO_REUSEADDR was not retained")
    elif option != "default":
        raise MatrixUnavailable("unrecognized option")


def _release_socket(sock, role, failures):
    try:
        sock.close()
    except Exception as exc:
        failures.append({"socket": role, "error_type": type(exc).__name__,
                         "error_code": _code(exc)})


def _new_socket(endpoint, option, net, stack, role, failures):
    family = net.AF_INET if endpoint.family == "ipv4" else net.AF_INET6
    sock = net.socket(family, net.SOCK_STREAM)
    stack.callback(_release_socket, sock, role, failures)
    _configure(sock, endpoint, option, net)
    return sock


def _bound_port(sock, endpoint):
    address = sock.getsockname()
    if (ipaddress.ip_address(address[0]) != ipaddress.ip_address(endpoint.host)
            or not isinstance(address[1], int) or not 1 <= address[1] <= 65535):
        raise MatrixUnavailable("bound address or ephemeral port could not be verified")
    return address[1]


def _code(exc):
    return getattr(exc, "winerror", None) or getattr(exc, "errno", None)


def _is_loopback(endpoint):
    address = ipaddress.ip_address(endpoint.host)
    return address.is_loopback or (getattr(address, "ipv4_mapped", None) is not None
                                   and address.ipv4_mapped.is_loopback)


def _run_case(case, net, *, allow_wildcard=False):
    # Refuse arbitrary endpoints and ports even when this private function is
    # called directly. The public entry point accepts no caller-supplied case.
    if case not in _CASES:
        raise ValueError("case is not in the fixed overlap matrix")
    if not all(_is_loopback(endpoint) for endpoint in (case.owner, case.contender)):
        if allow_wildcard is not True:
            raise ValueError("wildcard bind requires explicit opt-in")
    result = {"case": case.name, "owner": asdict(case.owner),
              "contender": asdict(case.contender), "owner_option": case.owner_option,
              "contender_option": case.contender_option}
    stage = "control"
    release_failures = []

    def release_failure():
        result.pop("error_code", None)
        result.update(observation="resource_release_failure",
                      stage="resource_release", release_failures=release_failures)
        return result

    try:
        # Control tests whether the contender configuration can bind at all;
        # it does not prove that every failure on the owner's port is a conflict.
        with ExitStack() as control:
            probe = _new_socket(case.contender, case.contender_option, net, control,
                                "control", release_failures)
            probe.bind((case.contender.host, 0))
            _bound_port(probe, case.contender)
        if release_failures:
            return release_failure()
        with ExitStack() as held:
            stage = "owner_setup"
            owner = _new_socket(case.owner, case.owner_option, net, held,
                                "owner", release_failures)
            owner.bind((case.owner.host, 0))
            port = _bound_port(owner, case.owner)
            owner.listen(1)  # loopback only; no accept/connect or request
            stage = "contender_setup"
            challenger = _new_socket(case.contender, case.contender_option, net, held,
                                     "contender", release_failures)
            stage = "contender_bind"
            try:
                challenger.bind((case.contender.host, port))
            except OSError as exc:
                code = _code(exc)
                if code not in _CONFLICT_CODES:
                    raise
                result.update(observation="bind_rejected", error_code=code)
            else:
                if _bound_port(challenger, case.contender) != port:
                    raise MatrixUnavailable("contender bound an unexpected port")
                result.update(observation="overlapping_bind_succeeded", error_code=None)
            # ExitStack closes the challenger before the held loopback owner.
        if release_failures:
            return release_failure()
        return result
    except Exception as exc:
        if release_failures:
            return release_failure()
        result.update(observation="inconclusive", stage=stage,
                      error_type=type(exc).__name__, error_code=_code(exc))
        return result


def run_matrix(*, allow_wildcard=False):
    """Observe the fixed matrix on a Windows API runtime, without service use.

    Default binds are loopback-only. ``allow_wildcard=True`` explicitly
    authorizes wildcard binds, never wildcard listeners. Any unsupported
    option/address and any unrecognized error is inconclusive. A close failure
    is reported and prevents subsequent cases. The runtime check does not
    distinguish Wine from native Windows.
    """
    if type(allow_wildcard) is not bool:
        raise ValueError("allow_wildcard must be a boolean")
    if os.name != "nt" or sys.platform != "win32" or not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        raise MatrixUnavailable("Windows socket API with SO_EXCLUSIVEADDRUSE is required")
    cases = [case for case in _CASES if allow_wildcard or
             all(_is_loopback(endpoint) for endpoint in (case.owner, case.contender))]
    observations = []
    for case in cases:
        observation = _run_case(case, socket, allow_wildcard=allow_wildcard)
        observations.append(observation)
        if observation["observation"] == "resource_release_failure":
            break
    completed = len(observations) == len(cases) and not any(
        item["observation"] == "resource_release_failure" for item in observations)
    return {
        "kind": "isolated_socket_observation_not_service_gate",
        "runtime": "win32_api_native_not_attested",
        "scope": "loopback_and_wildcard" if allow_wildcard else "loopback_only",
        "cases_planned": len(cases),
        "cases_attempted": len(observations),
        "matrix_completed": completed,
        "all_contender_binds_rejected": completed and all(
            item["observation"] == "bind_rejected" for item in observations),
        "limitations": [
            "Wine is not native Windows; native execution requires separate verification.",
            "The default matrix excludes wildcard binds and cannot verify wildcard overlap.",
            "Wildcard sockets are only bound, never listened on; wildcard-first is not tested.",
            "A rejected bind, including WSAEACCES, is an observation, not proof of its cause.",
            "No cross-process race, earlier wildcard owner, restart, TIME_WAIT, or ledger test.",
            "The service startup safety gate is unchanged.",
        ],
        "observations": observations,
    }


def main():
    arguments = sys.argv[1:]
    if arguments not in ([], ["--allow-wildcard-bind"]):
        print("Usage: python -m core.windows_listener [--allow-wildcard-bind]", file=sys.stderr)
        return 2
    try:
        report = run_matrix(allow_wildcard=bool(arguments))
    except MatrixUnavailable as exc:
        print(json.dumps({"error": str(exc), "kind": "diagnostic_unavailable"}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["all_contender_binds_rejected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
