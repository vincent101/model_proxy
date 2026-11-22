"""Compare listener setup with the baseline using fake sockets only.

No native Windows calls, DNS requests, network binds or production data.
Windows socket-option checks are candidate mechanics, not exclusivity proof.
"""
from __future__ import annotations

import errno
import os
import socket
import sys
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import core.server as server


class FakeSocket:
    def __init__(self, family, kind):
        self.family = family
        self.kind = kind
        self.events = [("socket", family, kind)]
        self.address = None

    def setsockopt(self, level, option, value):
        self.events.append(("setsockopt", level, option, value))

    def bind(self, address):
        self.events.append(("bind", address))
        if self.family == socket.AF_INET and address[0] in ("::", "::1"):
            raise OSError(errno.EAFNOSUPPORT, "fixture IPv6 on IPv4 socket")
        self.address = address

    def getsockname(self):
        return self.address

    def listen(self, backlog):
        self.events.append(("listen", backlog))

    def close(self):
        self.events.append(("close",))


class ListenerBaselineComparisonTests(unittest.TestCase):
    def _fake_start(self, constructor, host):
        sockets = []

        def new_socket(family, kind):
            fake = FakeSocket(family, kind)
            sockets.append(fake)
            return fake

        result = None
        # allow_reuse_address 是类体在 import 时求值的常量：Windows 宿主上是
        # False。模拟 darwin 时必须一并还原，否则比较的是宿主常量而非代码路径。
        with patch.object(os, "name", "posix"), \
             patch.object(sys, "platform", "darwin"), \
             patch.object(server.ProxyHTTPServer, "allow_reuse_address", True), \
             patch.object(socket, "socket", side_effect=new_socket), \
             patch.object(socket, "getfqdn", return_value="fixture"), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            try:
                http = constructor((host, 19431), server.ModelProxyHandler)
            except OSError as exc:
                result = ("error", type(exc), exc.errno)
            else:
                result = ("bound", http.address_family, http.server_address)
                http.server_close()
        self.assertEqual(len(sockets), 1)
        return result, sockets[0].events

    def test_darwin_matches_threading_http_server_for_ipv4_and_ipv6(self):
        # This compares the original constructor, rather than assuming that
        # merely refusing IPv6 proves the other listener behavior unchanged.
        for host in ("127.0.0.1", "localhost", "::1", "::"):
            with self.subTest(host=host):
                baseline, baseline_events = self._fake_start(ThreadingHTTPServer, host)
                actual, actual_events = self._fake_start(server.ProxyHTTPServer, host)
                self.assertEqual(actual, baseline)
                self.assertEqual(actual_events, baseline_events)
                self.assertEqual(actual_events[0][1], socket.AF_INET)
                if host in ("::1", "::"):
                    self.assertEqual(actual, ("error", OSError, errno.EAFNOSUPPORT))
                else:
                    self.assertEqual(actual, ("bound", socket.AF_INET, (host, 19431)))

    def test_windows_candidate_sets_exclusive_option_before_fake_bind(self):
        exclusive = 0x7F01  # synthetic constant, not a native Windows result
        for host, family, v6only in (("127.0.0.1", socket.AF_INET, None),
                                     ("::1", socket.AF_INET6, 1),
                                     ("::", socket.AF_INET6, 0)):
            with self.subTest(host=host):
                sockets = []

                def new_socket(actual_family, kind):
                    fake = FakeSocket(actual_family, kind)
                    sockets.append(fake)
                    return fake

                with patch.object(os, "name", "nt"), \
                     patch.object(sys, "platform", "win32"), \
                     patch.object(server.ProxyHTTPServer, "allow_reuse_address", False), \
                     patch.object(socket, "SO_EXCLUSIVEADDRUSE", exclusive, create=True), \
                     patch.object(socket, "socket", side_effect=new_socket), \
                     patch.object(socket, "getfqdn", return_value="fixture"), \
                     patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")):
                    http = server.ProxyHTTPServer((host, 19431), server.ModelProxyHandler)
                    self.assertEqual(http.address_family, family)
                    http.server_close()
                self.assertEqual(len(sockets), 1)
                events = sockets[0].events
                bind_index = next(i for i, event in enumerate(events) if event[0] == "bind")
                exclusive_event = ("setsockopt", socket.SOL_SOCKET, exclusive, 1)
                self.assertIn(exclusive_event, events[:bind_index])
                self.assertNotIn(("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1), events)
                if v6only is not None:
                    self.assertIn(("setsockopt", socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only),
                                  events[:bind_index])
                else:
                    self.assertFalse(any(event[0] == "setsockopt" and event[1] == socket.IPPROTO_IPV6
                                         for event in events))


if __name__ == "__main__":
    unittest.main()
