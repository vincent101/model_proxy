"""Old/current endpoint regressions using fake records and owned local sockets."""
import contextlib
import copy
import io
import json
import os
import socket
import tempfile
import unittest
from unittest.mock import patch

from core import windows_process
from core.cli import windows_control as wc
from tests.test_windows_control_a_contract import RECORD


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from tests.test_windows_control_a_unstarted import UnstartedTests
        self.fixture = UnstartedTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.control = self.fixture.control
        self.paths = self.fixture.paths
        self.fs = self.fixture.fs
        self.state = self.fixture.STATE
        self.fs.objects[self.state] = self.fs.obj(60, directory=True)
        self.fs.objects[self.state + r"\active.json"] = dict(self.fs.obj(63), data=b"fixture")
        self.old = {"host": "127.0.0.1", "port": 19431}
        self.current = {"host": "127.0.0.1", "port": 19432}
        self.active = dict(RECORD, project_root=self.fixture.ROOT, runtime_paths=self.paths,
                           config=self.control.config_path,
                           entry=self.fixture.ROOT + r"\core\cli\model_proxy.py", listen=self.old)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for item in (
            patch.object(self.control, "_locator_hint", return_value=True),
            patch.object(self.control, "_active", return_value=self.active),
            patch.object(self.control, "_configuration", return_value=(self.paths, self.current)),
            patch.object(windows_process, "recorded_process_state", return_value="gone"),
        ):
            self.stack.enter_context(item)

    def assert_unchanged(self, before):
        self.assertEqual(self.fs.objects, before)
        self.assertFalse(self.fs.handles)
        self.assertFalse(self.fs.locked)
        self.assertFalse(self.fixture.crt.mapping)

    def test_stale_status_rejects_occupied_current_port_after_checking_old(self):
        before = copy.deepcopy(self.fs.objects)
        probes = []
        def probe(endpoint):
            probes.append(endpoint["port"])
            if endpoint["port"] == 19432:
                raise wc.WindowsControlError("port_busy")
        with patch.object(self.control, "_probe_port", side_effect=probe), \
             patch.object(self.control, "_request") as request, \
             patch.object(windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.status(), 1)
        self.assertEqual(probes, [19431, 19432], self.fixture.err.getvalue())
        self.assertIn("current_port_busy", self.fixture.err.getvalue())
        self.assertNotIn("verified free", self.fixture.out.getvalue())
        request.assert_not_called()
        process.assert_not_called()
        self.assert_unchanged(before)

    def test_stale_off_rejects_occupied_current_port_without_stopping_process(self):
        before = copy.deepcopy(self.fs.objects)
        probes = []
        def probe(endpoint):
            probes.append(endpoint["port"])
            if endpoint["port"] == 19432:
                raise wc.WindowsControlError("port_busy")
        with patch.object(os.path, "lexists", return_value=False), \
             patch.object(self.control, "_current_listen_for_off", return_value=self.current), \
             patch.object(self.control, "_probe_port", side_effect=probe), \
             patch.object(self.control, "_locked") as locked, \
             patch.object(windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.off(), 1)
        self.assertEqual(probes, [19431, 19432])
        self.assertIn("current_port_busy", self.fixture.err.getvalue())
        self.assertNotIn("verified free", self.fixture.out.getvalue())
        locked.assert_not_called()
        process.assert_not_called()
        self.assert_unchanged(before)

    def test_off_with_existing_control_lock_still_rejects_occupied_current_port(self):
        self.fs.objects[self.fixture.APPDATA + r"\model_proxy"] = self.fs.obj(61, directory=True)
        self.fs.objects[self.fixture.DEFAULT] = self.fs.obj(62, directory=True)
        self.fs.objects[self.state + r"\control.lock"] = dict(
            self.fs.obj(64), data=b"fake-control-canary")
        before = copy.deepcopy(self.fs.objects)
        probes = []
        def probe(endpoint):
            probes.append(endpoint["port"])
            if endpoint["port"] == 19432:
                raise wc.WindowsControlError("port_busy")
        with patch.object(os.path, "lexists", return_value=True), \
             patch.object(self.control, "_current_listen_for_off", return_value=self.current), \
             patch.object(self.control, "_probe_port", side_effect=probe), \
             patch.object(windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.off(), 1)
        self.assertEqual(probes, [19431, 19432], self.fixture.err.getvalue())
        self.assertIn("current_port_busy", self.fixture.err.getvalue())
        process.assert_not_called()
        self.assert_unchanged(before)

    def test_verified_stop_reports_current_port_busy_without_claiming_full_success(self):
        from tests.test_windows_process_a_stop import StopBackend
        backend = StopBackend()
        backend.exe = self.active["executable"]
        original = windows_process.open_verified_process
        probes = []
        def open_process(pid, start, image, **kwargs):
            return original(pid, start, image, backend=backend, **kwargs)
        def probe(endpoint):
            probes.append(endpoint["port"])
            if endpoint["port"] == 19432:
                raise wc.WindowsControlError("port_busy")
        with patch.object(os.path, "lexists", return_value=True), \
             patch.object(self.control, "_current_listen_for_off", return_value=self.current), \
             patch.object(windows_process, "recorded_process_state", return_value="running"), \
             patch.object(windows_process, "open_verified_process", side_effect=open_process), \
             patch.object(self.control, "_verify_entry", side_effect=lambda _a, check: check()), \
             patch.object(self.control, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(self.control, "_probe_port", side_effect=probe):
            self.assertEqual(self.control.off(), 1)
        self.assertEqual(backend.stops, [(100, self.control.stop_timeout)])
        self.assertEqual(backend.closed, [100])
        self.assertEqual(probes, [19431, 19432])
        self.assertIn("Recorded model_proxy process exited", self.fixture.err.getvalue())
        self.assertIn("current_port_busy", self.fixture.err.getvalue())
        self.assertNotIn("verified free", self.fixture.out.getvalue())
        self.assertFalse(self.fs.handles)

    def test_verified_stop_reports_config_change_without_claiming_ports_free(self):
        from tests.test_windows_process_a_stop import StopBackend
        backend = StopBackend()
        backend.exe = self.active["executable"]
        original = windows_process.open_verified_process
        changed = {"host": "127.0.0.1", "port": 19433}
        def open_process(pid, start, image, **kwargs):
            return original(pid, start, image, backend=backend, **kwargs)
        with patch.object(os.path, "lexists", return_value=True), \
             patch.object(self.control, "_current_listen_for_off", side_effect=[self.current, changed]), \
             patch.object(windows_process, "recorded_process_state", return_value="running"), \
             patch.object(windows_process, "open_verified_process", side_effect=open_process), \
             patch.object(self.control, "_verify_entry", side_effect=lambda _a, check: check()), \
             patch.object(self.control, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(self.control, "_probe_port"):
            self.assertEqual(self.control.off(), 1)
        self.assertEqual(backend.stops, [(100, self.control.stop_timeout)])
        self.assertIn("Recorded model_proxy process exited", self.fixture.err.getvalue())
        self.assertIn("current_configuration_changed", self.fixture.err.getvalue())
        self.assertNotIn("verified free", self.fixture.out.getvalue())

    def test_both_free_are_checked_before_status_success(self):
        before = copy.deepcopy(self.fs.objects)
        probes = []
        with patch.object(self.control, "_probe_port",
                          side_effect=lambda endpoint: probes.append(endpoint["port"])), \
             patch.object(windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.status(), 0)
        self.assertEqual(probes, [19431, 19432])
        self.assertIn("recorded endpoint and current configuration endpoint verified free",
                      self.fixture.out.getvalue())
        process.assert_not_called()
        self.assert_unchanged(before)

    def test_both_free_are_checked_before_off_success(self):
        before = copy.deepcopy(self.fs.objects)
        probes = []
        with patch.object(os.path, "lexists", return_value=False), \
             patch.object(self.control, "_current_listen_for_off", return_value=self.current), \
             patch.object(self.control, "_probe_port",
                          side_effect=lambda endpoint: probes.append(endpoint["port"])), \
             patch.object(windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.off(), 0)
        self.assertEqual(probes, [19431, 19432])
        self.assertIn("recorded endpoint and current configuration endpoint verified free",
                      self.fixture.out.getvalue())
        process.assert_not_called()
        self.assert_unchanged(before)

    def test_same_bind_endpoint_is_checked_once(self):
        same = {"host": "localhost", "port": 19431}
        before = copy.deepcopy(self.fs.objects)
        probes = []
        with patch.object(self.control, "_configuration", return_value=(self.paths, same)), \
             patch.object(self.control, "_probe_port",
                          side_effect=lambda endpoint: probes.append(endpoint["port"])):
            self.assertEqual(self.control.status(), 0)
        self.assertEqual(probes, [19431])
        self.assertIn("current configuration endpoint verified free", self.fixture.out.getvalue())
        self.assert_unchanged(before)

    def test_running_instance_with_busy_changed_current_port_is_unknown_without_credentials(self):
        before = copy.deepcopy(self.fs.objects)
        with patch.object(windows_process, "recorded_process_state", return_value="running"), \
             patch.object(self.control, "_probe_port", side_effect=wc.WindowsControlError("port_busy")), \
             patch.object(self.control, "_request") as request:
            self.assertEqual(self.control.status(), 1)
        self.assertIn("current_port_busy", self.fixture.err.getvalue())
        request.assert_not_called()
        self.assert_unchanged(before)

    def test_running_instance_with_free_changed_current_port_reports_both(self):
        before = copy.deepcopy(self.fs.objects)
        probes = []
        with patch.object(windows_process, "recorded_process_state", return_value="running"), \
             patch.object(self.control, "_probe_port",
                          side_effect=lambda endpoint: probes.append(endpoint["port"])), \
             patch.object(self.control, "_request", return_value=({"supplies": [], "routes": [], "strategies": [], "cooldown": {}}, "fake-admin-credential")) as request:
            self.assertEqual(self.control.status(), 0)
        self.assertEqual(probes, [19432])
        request.assert_called_once_with(self.active, self.paths, "status")
        self.assertIn("current configuration endpoint differs and was verified free",
                      self.fixture.out.getvalue())
        self.assert_unchanged(before)

    def test_running_status_config_change_produces_no_running_output(self):
        changed = {'host': '127.0.0.1', 'port': 19433}
        result = ({'supplies': [], 'routes': [], 'strategies': [], 'cooldown': {}}, 'fake-admin-token')
        with patch.object(self.control, '_configuration', side_effect=[(self.paths, self.current), (self.paths, changed)]), \
             patch.object(windows_process, 'recorded_process_state', return_value='running'), \
             patch.object(self.control, '_probe_port'), \
             patch.object(self.control, '_request', return_value=result):
            self.assertEqual(self.control.status(), 1)
        self.assertEqual(self.fixture.out.getvalue(), '')
        self.assertIn('current_configuration_changed', self.fixture.err.getvalue())
        self.assertIn('unknown', self.fixture.err.getvalue())

    def test_configuration_change_after_probe_returns_unknown(self):
        changed = {"host": "127.0.0.1", "port": 19433}
        before = copy.deepcopy(self.fs.objects)
        with patch.object(self.control, "_configuration",
                          side_effect=[(self.paths, self.current), (self.paths, changed)]), \
             patch.object(self.control, "_probe_port"), \
             patch.object(windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.status(), 1)
        self.assertIn("current_configuration_changed", self.fixture.err.getvalue())
        self.assertNotIn("verified free", self.fixture.out.getvalue())
        process.assert_not_called()
        self.assert_unchanged(before)

    def test_off_configuration_change_after_probe_returns_unknown(self):
        changed = {"host": "127.0.0.1", "port": 19433}
        before = copy.deepcopy(self.fs.objects)
        with patch.object(os.path, "lexists", return_value=False), \
             patch.object(self.control, "_current_listen_for_off",
                          side_effect=[self.current, changed]), \
             patch.object(self.control, "_probe_port"), \
             patch.object(windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.off(), 1)
        self.assertIn("current_configuration_changed", self.fixture.err.getvalue())
        self.assertNotIn("verified free", self.fixture.out.getvalue())
        process.assert_not_called()
        self.assert_unchanged(before)

    def test_off_configuration_becomes_unreadable_after_probe_returns_unknown(self):
        before = copy.deepcopy(self.fs.objects)
        with patch.object(os.path, "lexists", return_value=False), \
             patch.object(self.control, "_current_listen_for_off",
                          side_effect=[self.current, None]), \
             patch.object(self.control, "_probe_port"), \
             patch.object(windows_process, "open_verified_process") as process:
            self.assertEqual(self.control.off(), 1)
        self.assertIn("current_configuration_changed", self.fixture.err.getvalue())
        self.assertNotIn("verified free", self.fixture.out.getvalue())
        process.assert_not_called()
        self.assert_unchanged(before)

    def test_off_bad_configuration_still_stops_verified_same_handle(self):
        from tests.test_windows_process_a_stop import StopBackend
        backend = StopBackend()
        backend.exe = self.active["executable"]
        original = windows_process.open_verified_process
        probes = []
        def open_process(pid, start, image, **kwargs):
            return original(pid, start, image, backend=backend, **kwargs)
        def verify(_active, revalidate):
            revalidate()
        with patch.object(os.path, "lexists", return_value=True), \
             patch.object(self.control, "_current_listen_for_off", return_value=None), \
             patch.object(windows_process, "recorded_process_state", return_value="running"), \
             patch.object(windows_process, "open_verified_process", side_effect=open_process), \
             patch.object(self.control, "_verify_entry", side_effect=verify), \
             patch.object(self.control, "_locked", return_value=contextlib.nullcontext()), \
             patch.object(self.control, "_probe_port",
                          side_effect=lambda endpoint: probes.append(endpoint["port"])):
            self.assertEqual(self.control.off(), 0, self.fixture.err.getvalue())
        self.assertEqual(backend.stops, [(100, self.control.stop_timeout)])
        self.assertEqual(backend.closed, [100])
        self.assertEqual(probes, [19431])
        self.assertIn("Current configuration endpoint UNKNOWN (not checked)", self.fixture.out.getvalue())
        self.assertNotIn("current configuration endpoint verified free", self.fixture.out.getvalue())
        self.assertFalse(self.fs.handles)

    def test_local_owned_listener_on_current_port_causes_unknown_for_status_and_off(self):
        before = copy.deepcopy(self.fs.objects)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as foreign:
            foreign.bind(("127.0.0.1", 0))
            foreign.listen(1)
            current = {"host": "127.0.0.1", "port": foreign.getsockname()[1]}
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                old = {"host": "127.0.0.1", "port": reservation.getsockname()[1]}
            self.active["listen"] = old
            def local_probe(endpoint):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    try:
                        probe.bind((endpoint["host"], endpoint["port"]))
                        probe.listen(1)
                    except OSError:
                        raise wc.WindowsControlError("port_busy") from None
            with patch.object(self.control, "_configuration", return_value=(self.paths, current)), \
                 patch.object(self.control, "_current_listen_for_off", return_value=current), \
                 patch.object(os.path, "lexists", return_value=False), \
                 patch.object(self.control, "_probe_port", side_effect=local_probe), \
                 patch.object(windows_process, "open_verified_process") as process:
                self.assertEqual(self.control.status(), 1)
                self.assertEqual(self.control.off(), 1)
            process.assert_not_called()
            foreign.settimeout(.1)
            with self.assertRaises(socket.timeout):
                foreign.accept()  # product probes bind only; no token or connection
        self.assertEqual(self.fixture.err.getvalue().count("current_port_busy"), 2)
        self.assertNotIn("verified free", self.fixture.out.getvalue())
        self.assert_unchanged(before)


class CurrentConfigTests(unittest.TestCase):
    def test_real_fake_file_invalid_or_missing_is_unavailable_without_defaulting(self):
        control = wc.WindowsControl(r"C:\fixture\project", out=io.StringIO(), err=io.StringIO())
        with tempfile.TemporaryDirectory(prefix="mp-current-endpoint-") as directory:
            path = os.path.join(directory, "fake.json")
            control.config_path = path  # test-only local fake file
            self.assertIsNone(control._current_listen_for_off())
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"listen": {"host": "127.0.0.1", "port": 19432}}, stream)
            self.assertEqual(control._current_listen_for_off()["port"], 19432)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{broken")
            self.assertIsNone(control._current_listen_for_off())

    def test_different_consecutive_endpoint_reads_are_not_accepted(self):
        control = wc.WindowsControl(r"C:\fixture\project", out=io.StringIO(), err=io.StringIO())
        with tempfile.TemporaryDirectory(prefix="mp-current-endpoint-") as directory:
            path = os.path.join(directory, "fake.json")
            control.config_path = path
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{}")
            first = {"host": "127.0.0.1", "port": 19431}
            second = {"host": "127.0.0.1", "port": 19432}
            with patch("core.listen_config.resolve_listen", side_effect=[first, second]):
                self.assertIsNone(control._current_listen_for_off())

    def test_off_reads_fake_current_config_and_propagates_busy_as_unknown(self):
        control = wc.WindowsControl(RECORD["project_root"], config_path=RECORD["config"],
                                    out=io.StringIO(), err=io.StringIO())
        with tempfile.TemporaryDirectory(prefix="mp-current-endpoint-") as directory:
            path = os.path.join(directory, "fake.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"listen": {"host": "127.0.0.1", "port": 19432}}, stream)
            control.config_path = path  # test-only local fake file
            def idle(_paths, old, *, expected_active, current_listen):
                self.assertEqual(old["port"], 12345)
                self.assertEqual(current_listen["port"], 19432)
                self.assertEqual(expected_active, RECORD)
                raise wc.WindowsControlError("current_port_busy")
            with patch.object(os, "name", "nt"), \
                 patch.object(control, "_locator_hint", return_value=True), \
                 patch.object(os.path, "lexists", return_value=False), \
                 patch.object(control, "_active", return_value=RECORD), \
                 patch.object(windows_process, "recorded_process_state", return_value="gone"), \
                 patch.object(windows_process, "open_verified_process") as process, \
                 patch.object(control, "_idle_readonly", side_effect=idle):
                self.assertEqual(control.off(), 1)
            self.assertIn("current_port_busy", control.err.getvalue())
            self.assertNotIn("verified free", control.out.getvalue())
            process.assert_not_called()

    def test_off_bad_fake_json_still_stops_verified_target_and_reports_unknown(self):
        from tests.test_windows_process_a_stop import StopBackend
        control = wc.WindowsControl(RECORD["project_root"], config_path=RECORD["config"],
                                    out=io.StringIO(), err=io.StringIO())
        backend = StopBackend()
        backend.exe = RECORD["executable"]
        original = windows_process.open_verified_process
        with tempfile.TemporaryDirectory(prefix="mp-current-endpoint-") as directory:
            path = os.path.join(directory, "fake.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{broken")
            control.config_path = path
            def open_process(pid, start, image, **kwargs):
                return original(pid, start, image, backend=backend, **kwargs)
            with patch.object(os, "name", "nt"), \
                 patch.object(control, "_locator_hint", return_value=True), \
                 patch.object(os.path, "lexists", return_value=True), \
                 patch.object(control, "_active", return_value=RECORD), \
                 patch.object(control, "_locked", return_value=contextlib.nullcontext()), \
                 patch.object(windows_process, "recorded_process_state", return_value="running"), \
                 patch.object(windows_process, "open_verified_process", side_effect=open_process), \
                 patch.object(control, "_verify_entry", side_effect=lambda _a, check: check()), \
                 patch.object(control, "_idle_readonly") as idle:
                self.assertEqual(control.off(), 0)
            idle.assert_called_once_with(RECORD["runtime_paths"], RECORD["listen"],
                                         expected_active=RECORD, held_control=None, current_listen=None)
        self.assertEqual(backend.stops, [(100, control.stop_timeout)])
        self.assertIn("Current configuration endpoint UNKNOWN (not checked)", control.out.getvalue())


if __name__ == "__main__":
    unittest.main()
