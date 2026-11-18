@echo off
setlocal DisableDelayedExpansion
where py >nul 2>nul
if errorlevel 1 goto check_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto use_py
:check_python
where python >nul 2>nul
if errorlevel 1 goto no_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto use_python
:no_python
echo Error: no Python ^>= 3.10 interpreter found; actual platform support still requires validation. 1>&2
exit /b 1
:use_py
py -3 "%~dp0core\cli\main.py" %*
exit /b %errorlevel%
:use_python
python "%~dp0core\cli\main.py" %*
exit /b %errorlevel%
