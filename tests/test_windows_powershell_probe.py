"""临时探针：测量 CI 上 PowerShell/CIM 观测链路的真实耗时与失败位置。

只在原生 Windows 上跑；输出直接进 CI 日志，用来区分
「环境限制（WMI 冷启动/禁用）」还是「helper 契约缺陷」。
诊断完成后删除本文件。
"""
import ntpath
import os
import subprocess
import sys
import time
import unittest

from core.cli import windows_command_line as wcl


@unittest.skipUnless(os.name == "nt", "requires Windows Python")
class PowerShellProbe(unittest.TestCase):
    def test_probe(self):
        directory = wcl._system_directory()
        root = ntpath.dirname(directory)
        psdir = ntpath.join(directory, "WindowsPowerShell", "v1.0")
        exe = ntpath.join(psdir, "powershell.exe")
        env = {"SystemRoot": root, "PSModulePath": ntpath.join(psdir, "Modules")}
        print("probe system_directory=%r exe_exists=%r" % (directory, os.path.exists(exe)), flush=True)

        def run(args, stdin_data, budget):
            started = time.monotonic()
            try:
                proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, shell=False,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except BaseException as exc:
                print("probe spawn failed %r after %.2fs" % (exc, time.monotonic() - started), flush=True)
                return
            try:
                out, err = proc.communicate(stdin_data, timeout=budget)
                print("probe rc=%r elapsed=%.2fs out=%r err=%r"
                      % (proc.returncode, time.monotonic() - started,
                         out[:200], err[:400]), flush=True)
            except subprocess.TimeoutExpired:
                proc.kill()
                print("probe TIMEOUT after %.2fs" % (time.monotonic() - started), flush=True)

        run([exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "exit 0"], b"", 30)
        run([exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId = %d') | "
             "Select-Object -ExpandProperty CommandLine" % os.getpid()], b"", 60)
        run([exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             wcl._ENCODED_SCRIPT], ("%d\n" % os.getpid()).encode("ascii"), 60)
