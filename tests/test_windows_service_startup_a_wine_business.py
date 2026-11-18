"""Small Whisky bridge for the existing owned fake-config/business smoke test."""
import contextlib
import io
import json
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("requires -I -S -B and a fresh result path")
    result_path = Path(sys.argv[1])
    if result_path.exists():
        raise SystemExit("result exists")
    output = io.StringIO()
    script = Path(__file__).with_name("test_wine_proxy_integration.py")
    sys.argv = [str(script), "-v"]
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            runpy.run_path(str(script), run_name="__main__")
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    result_path.write_text(json.dumps({"exit_code": code, "output": output.getvalue()},
                                      ensure_ascii=False), encoding="utf-8")
    raise SystemExit(code)
