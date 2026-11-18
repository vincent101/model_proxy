"""Synthetic tests, a POSIX socket smoke test, and an opt-in Windows matrix."""
import contextlib
import io
import json
import os
import socket
import sys
import types
import unittest
from unittest.mock import patch

from core import windows_listener as listener


class FakeSocket:
    def __init__(self, net, family, kind, role):
        self.net, self.family, self.kind, self.role = net, family, kind, role
        self.index = len(net.sockets)
        self.options = {}
        self.address = None
        self.closed = False
        net.events.append((role, "socket", family, kind))

    def setsockopt(self, level, option, value):
        self.net.events.append((self.role, "setsockopt", level, option, value))
        if self.net.fail_setup_role == self.role:
            raise OSError(10022, "synthetic option failure")
        self.options[(level, option)] = value

    def getsockopt(self, level, option):
        self.net.events.append((self.role, "getsockopt", level, option))
        if self.net.option_mismatch:
            return 0 if self.options[(level, option)] else 1
        return self.options[(level, option)]

    def bind(self, address):
        host, port = address
        self.net.events.append((self.role, "bind", host, port))
        if self.role in ("control", "owner"):
            if port != 0:
                raise AssertionError("control and owner must use ephemeral port")
            self.address = (host, 42001 if self.role == "control" else 42002)
            if self.role == "control" and self.net.control_error:
                raise OSError(self.net.control_error, "synthetic control error")
        else:
            if port != 42002 or not self.net.owner_listening or self.net.owner.closed:
                raise AssertionError("contender must target the still-held owner")
            if self.net.contender_error is not None:
                raise OSError(self.net.contender_error, "synthetic contender error")
            self.address = (host, port)

    def getsockname(self):
        return self.address

    def listen(self, backlog):
        self.net.events.append((self.role, "listen", self.address, backlog))
        if self.role != "owner" or self.address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("only loopback owner may listen")
        self.net.owner_listening = True

    def close(self):
        self.net.events.append((self.role, "close"))
        if self.role in self.net.fail_close_roles or self.index in self.net.fail_close_indices:
            raise OSError(10022, "synthetic close failure")
        self.closed = True


class FakeNet:
    AF_INET, AF_INET6, SOCK_STREAM = 2, 23, 1
    SOL_SOCKET, SO_EXCLUSIVEADDRUSE, SO_REUSEADDR = 65535, -5, 4
    IPPROTO_IPV6, IPV6_V6ONLY = 41, 27

    def __init__(self, contender_error=10048, control_error=None,
                 fail_setup_role=None, option_mismatch=False, fail_close_roles=(),
                 fail_close_indices=()):
        self.events = []
        self.sockets = []
        self.owner = None
        self.owner_listening = False
        self.contender_error = contender_error
        self.control_error = control_error
        self.fail_setup_role = fail_setup_role
        self.option_mismatch = option_mismatch
        self.fail_close_roles = set(fail_close_roles)
        self.fail_close_indices = set(fail_close_indices)

    def socket(self, family, kind):
        role = ("control", "owner", "contender")[len(self.sockets) % 3]
        sock = FakeSocket(self, family, kind, role)
        self.sockets.append(sock)
        if role == "owner":
            self.owner = sock
            self.owner_listening = False
        return sock


class MatrixContractTests(unittest.TestCase):
    def test_matrix_covers_address_and_bind_order_combinations(self):
        self.assertEqual(len(listener._CASES), 35)
        self.assertEqual(len({case.name for case in listener._CASES}), 35)
        self.assertEqual({case.owner_option for case in listener._CASES},
                         {"default", "reuse", "exclusive"})
        self.assertEqual({case.contender_option for case in listener._CASES},
                         {"default", "reuse", "exclusive"})
        self.assertEqual({case.contender.host for case in listener._CASES},
                         {"127.0.0.1", "0.0.0.0", "::1", "::", "::ffff:127.0.0.1"})
        for case in listener._CASES:
            with self.subTest(case=case.name):
                net = FakeNet()
                result = listener._run_case(case, net, allow_wildcard=True)
                self.assertEqual(result["observation"], "bind_rejected")
                self.assertEqual(result["error_code"], 10048)
                self.assertTrue(all(s.closed for s in net.sockets))
                binds = [e for e in net.events if e[1] == "bind"]
                self.assertEqual([b[3] for b in binds], [0, 0, 42002])
                listen_events = [e for e in net.events if e[1] == "listen"]
                self.assertEqual(len(listen_events), 1)
                self.assertEqual(listen_events[0][0], "owner")
                self.assertLess(net.events.index(("control", "close")),
                                net.events.index(next(e for e in binds if e[0] == "owner")))
                self.assertLess(net.events.index(("contender", "close")),
                                net.events.index(("owner", "close")))
                for role, endpoint, option in (("control", case.contender, case.contender_option),
                                               ("owner", case.owner, case.owner_option),
                                               ("contender", case.contender, case.contender_option)):
                    bind_index = net.events.index(next(e for e in binds if e[0] == role))
                    if endpoint.family == "ipv6":
                        self.assertIn((role, "setsockopt", net.IPPROTO_IPV6,
                                       net.IPV6_V6ONLY, endpoint.v6only), net.events[:bind_index])
                    if option == "exclusive":
                        self.assertIn((role, "setsockopt", net.SOL_SOCKET,
                                       net.SO_EXCLUSIVEADDRUSE, 1), net.events[:bind_index])
                        self.assertNotIn((role, "setsockopt", net.SOL_SOCKET,
                                          net.SO_REUSEADDR, 1), net.events)

    def test_succeeded_overlap_is_reported_and_closed(self):
        net = FakeNet(contender_error=None)
        result = listener._run_case(listener._CASES[0], net)
        self.assertEqual(result["observation"], "overlapping_bind_succeeded")
        self.assertTrue(all(s.closed for s in net.sockets))
        self.assertEqual([e[0] for e in net.events if e[1] == "listen"], ["owner"])

    def test_access_denied_recorded_as_observation_not_proof(self):
        net = FakeNet(contender_error=10013)
        result = listener._run_case(listener._CASES[0], net)
        self.assertEqual((result["observation"], result["error_code"]), ("bind_rejected", 10013))

    def test_other_bind_error_is_inconclusive(self):
        net = FakeNet(contender_error=10022)
        result = listener._run_case(listener._CASES[0], net)
        self.assertEqual((result["observation"], result["stage"], result["error_code"]),
                         ("inconclusive", "contender_bind", 10022))
        self.assertTrue(all(s.closed for s in net.sockets))

    def test_control_error_does_not_claim_conflict(self):
        net = FakeNet(control_error=10013)
        result = listener._run_case(listener._CASES[0], net)
        self.assertEqual((result["observation"], result["stage"]), ("inconclusive", "control"))
        self.assertEqual(len(net.sockets), 1)
        self.assertTrue(net.sockets[0].closed)

    def test_setup_failure_and_option_mismatch_are_inconclusive(self):
        for net in (FakeNet(fail_setup_role="owner"), FakeNet(option_mismatch=True)):
            with self.subTest(net=net):
                result = listener._run_case(listener._CASES[0], net)
                self.assertEqual(result["observation"], "inconclusive")
                self.assertTrue(all(s.closed for s in net.sockets))

    def test_arbitrary_endpoints_are_rejected_without_socket(self):
        net = FakeNet()
        bad = listener._Case("external", listener._Endpoint("ipv4", "8.8.8.8"),
                             listener._Endpoint("ipv4", "0.0.0.0"), "exclusive", "reuse")
        with self.assertRaises(ValueError):
            listener._run_case(bad, net)
        self.assertEqual(net.events, [])

    def test_wildcard_case_refused_without_opt_in_before_any_socket(self):
        net = FakeNet()
        wildcard = next(case for case in listener._CASES
                        if case.contender.host == "0.0.0.0")
        with self.assertRaisesRegex(ValueError, "explicit opt-in"):
            listener._run_case(wildcard, net)
        self.assertEqual(net.events, [])

    def test_default_matrix_is_loopback_only_even_with_fake_sockets(self):
        net = FakeNet()
        with patch.object(listener, "os", types.SimpleNamespace(name="nt")), \
             patch.object(listener, "sys", types.SimpleNamespace(platform="win32")), \
             patch.object(listener, "socket", net):
            report = listener.run_matrix()
        self.assertEqual(report["scope"], "loopback_only")
        self.assertEqual((report["cases_planned"], report["cases_attempted"]), (15, 15))
        self.assertTrue(report["matrix_completed"])
        self.assertTrue(report["all_contender_binds_rejected"])
        self.assertEqual(len(net.sockets), 45)
        for case in listener._CASES:
            if any(item["case"] == case.name for item in report["observations"]):
                self.assertTrue(listener._is_loopback(case.owner))
                self.assertTrue(listener._is_loopback(case.contender))
        self.assertEqual({e[2] for e in net.events if e[1] == "bind"},
                         {"127.0.0.1", "::1", "::ffff:127.0.0.1"})

    def test_explicit_wildcard_opt_in_with_fake_sockets_only(self):
        net = FakeNet()
        with patch.object(listener, "os", types.SimpleNamespace(name="nt")), \
             patch.object(listener, "sys", types.SimpleNamespace(platform="win32")), \
             patch.object(listener, "socket", net):
            report = listener.run_matrix(allow_wildcard=True)
        self.assertEqual(report["scope"], "loopback_and_wildcard")
        self.assertEqual((report["cases_planned"], report["cases_attempted"]), (35, 35))
        self.assertTrue(report["matrix_completed"])
        hosts = [e[2] for e in net.events if e[1] == "bind"]
        self.assertIn("0.0.0.0", hosts)
        self.assertIn("::", hosts)

    def test_control_close_failure_stops_before_owner_or_later_cases(self):
        net = FakeNet(fail_close_roles=("control",))
        with patch.object(listener, "os", types.SimpleNamespace(name="nt")), \
             patch.object(listener, "sys", types.SimpleNamespace(platform="win32")), \
             patch.object(listener, "socket", net):
            report = listener.run_matrix()
        self.assertEqual(len(net.sockets), 1)
        self.assertEqual((report["cases_planned"], report["cases_attempted"]), (15, 1))
        self.assertFalse(report["matrix_completed"])
        self.assertFalse(report["all_contender_binds_rejected"])
        result = report["observations"][0]
        self.assertEqual(result["observation"], "resource_release_failure")
        self.assertEqual(result["release_failures"],
                         [{"socket": "control", "error_type": "OSError", "error_code": 10022}])
        self.assertEqual(net.events[-1], ("control", "close"))

    def test_owner_and_contender_close_failures_stop_matrix_after_cleanup_attempts(self):
        net = FakeNet(fail_close_roles=("owner", "contender"))
        with patch.object(listener, "os", types.SimpleNamespace(name="nt")), \
             patch.object(listener, "sys", types.SimpleNamespace(platform="win32")), \
             patch.object(listener, "socket", net):
            report = listener.run_matrix()
        self.assertEqual(len(net.sockets), 3)
        self.assertEqual(report["cases_attempted"], 1)
        self.assertFalse(report["matrix_completed"])
        self.assertFalse(report["all_contender_binds_rejected"])
        self.assertEqual(net.events[-2:], [("contender", "close"), ("owner", "close")])
        result = report["observations"][0]
        self.assertEqual(result["observation"], "resource_release_failure")
        self.assertEqual([failure["socket"] for failure in result["release_failures"]],
                         ["contender", "owner"])

    def test_later_case_close_failure_stops_before_following_case(self):
        # First case uses sockets 0..2; the next case's control is socket 3.
        net = FakeNet(fail_close_indices=(3,))
        with patch.object(listener, "os", types.SimpleNamespace(name="nt")), \
             patch.object(listener, "sys", types.SimpleNamespace(platform="win32")), \
             patch.object(listener, "socket", net):
            report = listener.run_matrix()
        self.assertEqual(len(net.sockets), 4)
        self.assertEqual(report["cases_attempted"], 2)
        self.assertFalse(report["matrix_completed"])
        self.assertFalse(report["all_contender_binds_rejected"])
        self.assertEqual([item["observation"] for item in report["observations"]],
                         ["bind_rejected", "resource_release_failure"])
        self.assertEqual(net.events[-1], ("control", "close"))

    def test_close_failure_takes_precedence_over_bind_error(self):
        net = FakeNet(control_error=10013, fail_close_roles=("control",))
        result = listener._run_case(listener._CASES[0], net)
        self.assertEqual(result["observation"], "resource_release_failure")
        self.assertEqual(len(net.sockets), 1)

    def test_wildcard_opt_in_must_be_boolean(self):
        with self.assertRaises(ValueError):
            listener.run_matrix(allow_wildcard=1)

    def test_nonnative_platform_fails_before_opening_socket(self):
        net = FakeNet()
        with patch.object(listener, "os", types.SimpleNamespace(name="posix")), \
             patch.object(listener, "sys", types.SimpleNamespace(platform="darwin")), \
             patch.object(listener, "socket", net):
            with self.assertRaises(listener.MatrixUnavailable):
                listener.run_matrix()
        self.assertEqual(net.events, [])

    def test_report_requires_every_case_to_be_rejected(self):
        fake_socket_module = types.SimpleNamespace(SO_EXCLUSIVEADDRUSE=-5)
        outcomes = [{"observation": "bind_rejected"}] * 14 + [{"observation": "inconclusive"}]
        with patch.object(listener, "os", types.SimpleNamespace(name="nt")), \
             patch.object(listener, "sys", types.SimpleNamespace(platform="win32")), \
             patch.object(listener, "socket", fake_socket_module), \
             patch.object(listener, "_run_case", side_effect=outcomes):
            report = listener.run_matrix()
        self.assertFalse(report["all_contender_binds_rejected"])
        self.assertIn("native_not_attested", report["runtime"])
        self.assertIn("not_service_gate", report["kind"])
        self.assertTrue(any("Wine" in s for s in report["limitations"]))

    def test_cli_refuses_arguments_and_inconclusive_returns_nonzero(self):
        with patch.object(listener, "sys", types.SimpleNamespace(argv=["module", "123"], stderr=io.StringIO())), \
             patch.object(listener, "run_matrix") as run:
            self.assertEqual(listener.main(), 2)
            run.assert_not_called()
        report = {"all_contender_binds_rejected": False}
        out = io.StringIO()
        with patch.object(listener, "sys", types.SimpleNamespace(argv=["module"])), \
             patch.object(listener, "run_matrix", return_value=report), contextlib.redirect_stdout(out):
            self.assertEqual(listener.main(), 1)
        self.assertEqual(json.loads(out.getvalue()), report)

    def test_cli_wildcard_flag_is_explicit_and_only_tested_with_mock(self):
        report = {"all_contender_binds_rejected": False}
        with patch.object(listener, "sys", types.SimpleNamespace(
                argv=["module", "--allow-wildcard-bind"])), \
             patch.object(listener, "run_matrix", return_value=report) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(listener.main(), 1)
        run.assert_called_once_with(allow_wildcard=True)


@unittest.skipUnless(os.name == "posix", "POSIX socket smoke test only")
class PosixLoopbackSmokeTests(unittest.TestCase):
    def test_real_ephemeral_loopback_contender(self):
        # Real local bind/listen/close exercise, NOT Windows exclusivity: this
        # adapter implements the Windows-only option as an inert test marker.
        # POSIX errors are deliberately not translated into Windows error codes.
        class WrappedSocket:
            def __init__(self, raw, net):
                self.raw, self.net, self.marker = raw, net, 0

            def setsockopt(self, level, option, value):
                if (level, option) == (self.net.SOL_SOCKET, self.net.SO_EXCLUSIVEADDRUSE):
                    self.marker = value
                else:
                    self.raw.setsockopt(level, option, value)

            def getsockopt(self, level, option):
                if (level, option) == (self.net.SOL_SOCKET, self.net.SO_EXCLUSIVEADDRUSE):
                    return self.marker
                return self.raw.getsockopt(level, option)

            def bind(self, address):
                if address[0] not in ("127.0.0.1", "::1"):
                    raise AssertionError("real smoke test forbids non-loopback bind")
                self.net.binds.append(address)
                return self.raw.bind(address)

            def getsockname(self):
                return self.raw.getsockname()

            def listen(self, backlog):
                self.net.listens.append(self.raw.getsockname())
                return self.raw.listen(backlog)

            def close(self):
                self.raw.close()
                self.net.closed += 1

        class PosixNet:
            AF_INET, AF_INET6, SOCK_STREAM = socket.AF_INET, socket.AF_INET6, socket.SOCK_STREAM
            SOL_SOCKET, SO_REUSEADDR = socket.SOL_SOCKET, socket.SO_REUSEADDR
            IPPROTO_IPV6, IPV6_V6ONLY = socket.IPPROTO_IPV6, socket.IPV6_V6ONLY
            SO_EXCLUSIVEADDRUSE = -9999  # synthetic; never passed to the OS

            def __init__(self):
                self.binds, self.listens, self.closed = [], [], 0

            def socket(self, family, kind):
                return WrappedSocket(socket.socket(family, kind), self)

        for case in (listener._CASES[0],):
            with self.subTest(case=case.name):
                net = PosixNet()
                result = listener._run_case(case, net)
                self.assertIn(result["observation"], ("overlapping_bind_succeeded", "inconclusive"))
                if result["observation"] == "inconclusive":
                    self.assertEqual(result["stage"], "contender_bind")
                self.assertEqual(net.binds[0][1], 0)
                self.assertEqual(net.binds[1], ("127.0.0.1", 0))
                self.assertEqual(len(net.listens), 1)
                self.assertEqual(net.listens[0][0], "127.0.0.1")
                self.assertEqual(net.binds[2][1], net.listens[0][1])
                self.assertEqual(net.closed, 3)


@unittest.skipUnless(os.name == "nt" and sys.platform == "win32" and
                     os.environ.get("MODEL_PROXY_RUN_WINDOWS_LISTENER_MATRIX") == "1",
                     "requires explicit authorization on a Windows API runtime")
class ExplicitWindowsMatrixTests(unittest.TestCase):
    def test_observed_conflict_matrix(self):
        # This test cannot distinguish Wine from native Windows. Its opt-in is
        # not evidence authorizing startup or bypassing the existing gate.
        report = listener.run_matrix()
        self.assertEqual(report["scope"], "loopback_only")
        self.assertEqual(len(report["observations"]), 15)
        self.assertTrue(report["all_contender_binds_rejected"], json.dumps(report, indent=2))


if __name__ == "__main__":
    unittest.main()
