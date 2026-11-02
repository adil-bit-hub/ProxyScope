@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ============================================================
REM  ProxyScope 启动脚本
REM  代理端口 18888 / Web 界面 18787（可按需修改下面的变量）
REM ============================================================

set PROXY_PORT=18888
set WEB_PORT=18787

REM ---- 定位 Python ----
set PY=
if exist "%~dp0.python-path" (
    set /p PY=<"%~dp0.python-path"
)
if not defined PY (
    where python >nul 2>nul && set PY=python
)
if not defined PY (
    if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" (
        set PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
    )
)
if not defined PY (
    echo [x] 未找到 Python，请先安装 Python 3.9+ 或修改本脚本中的 PY 变量。
    pause
    exit /b 1
)

set PYTHONPATH=%~dp0
set PYTHONIOENCODING=utf-8

echo.
echo  使用 Python: %PY%
echo.

"%PY%" -m proxyscope --port %PROXY_PORT% --web-port %WEB_PORT% --data-dir "%~dp0data" %*

endlocal
pause
