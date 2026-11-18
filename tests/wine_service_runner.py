"""Detached Whisky result bridge for the test-only startup experiment.

Arguments: test-script, mode, fresh result-file in a caller-owned temp folder.
The supervisor owns exactly one Windows Python child, captures its output and
waits for exit. On timeout it terminates only that Popen child and reaps it;
that is a failed/inconclusive test, not a successful cleanup or Windows proof.
No process-name, PID, port lookup or product stop command is used.

Example (replace paths with the bottle's Python and the worktree/test result):
  whisky run py-test 'C:\path\python.exe' -- -I -S -B \
      'Z:\path\tests\wine_service_runner.py' \
      'Z:\path\tests\test_wine_service_startup.py' --injected \
      'Z:\path\owned-empty-directory\result.json'
Whisky can return before the detached process finishes: wait for result.json,
then read its exit_code, completion and output. --native intentionally reports
the unmodified startup failure if a production guard rejects the fixture.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
    raise SystemExit('requires -I -S -B')
if len(sys.argv) != 4 or sys.argv[2] not in ('--native', '--injected'):
    raise SystemExit('invalid test runner arguments')
script, mode, result = sys.argv[1:]
p = Path(result)
if p.exists():
    raise SystemExit('result file already exists')
child = subprocess.Popen([sys.executable, '-I', '-S', '-B', script, mode],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
try:
    output, _ = child.communicate(timeout=60)
    status = child.returncode
    completion = 'owned child exited and was reaped'
except subprocess.TimeoutExpired:
    child.kill()  # only the process created above; no PID lookup
    try:
        output, _ = child.communicate(timeout=15)
        status = 1
        completion = 'TIMEOUT: owned child killed and reaped; test failed, fixture may remain'
    except subprocess.TimeoutExpired:
        output = b''
        status = 1
        completion = 'TIMEOUT: owned child termination/reaping UNCONFIRMED; fixture may remain'
tmp = p.with_name(p.name + '.tmp')
with tmp.open('x', encoding='utf-8') as f:
    json.dump({'exit_code': status, 'completion': completion,
               'output': output.decode('utf-8', errors='replace')}, f)
os.replace(tmp, p)
