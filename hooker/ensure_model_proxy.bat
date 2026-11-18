@echo off
rem SessionStart hook: lifecycle, initialization and locking belong to the CLI.
setlocal DisableDelayedExpansion
call "%~dp0..\model_proxy_cli.bat" on
exit /b %errorlevel%
