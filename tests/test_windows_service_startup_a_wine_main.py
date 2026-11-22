"""Whisky bridge for a fake-project main/startup smoke, not native ACL proof."""
import contextlib
import io
import json
import runpy
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

if __name__ == "__main__":
    if len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("requires -I -S -B and a fresh result path")
    result_path = Path(sys.argv[1])
    if result_path.exists():
        raise SystemExit("result exists")
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root / "tests")]
    import wine_service_fixture
    original_install = wine_service_fixture.install

    def install_light(stack, server, temp, project, config, pathsfile, paths):
        from core import discovery
        original_install(stack, server, temp, project, config, pathsfile, paths)
        if not Path(paths["log"]).is_relative_to(Path(temp)):
            raise AssertionError("test log escaped owned fixture")
        paths["log"].write_bytes(b"")
        inspector = server.inspect_runtime_identities
        stack.enter_context(patch.object(
            server, "inspect_runtime_identities",
            side_effect=lambda project_root, values, *, windows_light=False:
                inspector(project_root, values)))
        original_main = server._windows_main
        def traced_main(config_path):
            try:
                return original_main(config_path)
            except Exception:
                traceback.print_exc()
                raise
        stack.enter_context(patch.object(server, "_windows_main", side_effect=traced_main))
        state = project / ".runtime"
        def read_active(directory):
            if Path(directory) != state:
                raise AssertionError("test record escaped owned fixture")
            path = state / "active.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        stack.enter_context(patch.object(discovery, "read_windows_active", side_effect=read_active))
        stack.enter_context(patch.object(discovery, "prepare_windows_runtime_directories"))
        stack.enter_context(patch.object(discovery, "prepare_windows_log",
                                        side_effect=lambda root, paths, snapshot: snapshot))
        stack.enter_context(patch.object(server, "_require_windows_service_launch"))
        print("TEST ONLY: light active reads and already-created fake log; no ACL proof.", flush=True)

    output = io.StringIO()
    script = root / "tests" / "test_wine_service_startup.py"
    sys.argv = [str(script), "--injected"]
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
         patch.object(wine_service_fixture, "install", side_effect=install_light):
        try:
            runpy.run_path(str(script), run_name="__main__")
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except BaseException:
            traceback.print_exc()
            code = 1
    result_path.write_text(json.dumps({"exit_code": code, "output": output.getvalue()},
                                      ensure_ascii=False), encoding="utf-8")
    raise SystemExit(code)
