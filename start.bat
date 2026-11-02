@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ===========================================================
REM  ProxyScope launcher for Windows
REM
REM  NOTE: this file is intentionally ASCII-only.
REM  cmd.exe parses .bat files with the system ANSI codepage
REM  (936 on Chinese Windows), so UTF-8 Chinese literals placed
REM  here would print as mojibake. Localized text is printed by
REM  Python instead, which handles UTF-8 correctly.
REM ===========================================================

set "PROXY_PORT=18888"
set "WEB_PORT=18787"
set "PY="

REM ---- 1. locate a usable Python 3.9+ ------------------------

REM 1a. explicit override file beside this script
if exist "%~dp0.python-path" (
    set /p "PY="<"%~dp0.python-path"
    if defined PY set "PY=!PY:"=!"
)

REM 1b. py launcher (avoids the Microsoft Store placeholder)
if not defined PY call :try_py3

REM 1c. python on PATH, skipping the Microsoft Store alias
if not defined PY call :try_path_python

REM 1d. common per-user / system install locations
if not defined PY call :try_file "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY call :try_file "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :try_file "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY call :try_file "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY call :try_file "%ProgramFiles%\Python313\python.exe"
if not defined PY call :try_file "%ProgramFiles%\Python312\python.exe"
if not defined PY call :try_file "%ProgramFiles%\Python311\python.exe"
if not defined PY call :try_file "%ProgramFiles%\Python310\python.exe"
if not defined PY call :try_file "%ProgramFiles%\Python39\python.exe"

if not defined PY goto :no_python

echo   Python : !PY!
echo.

REM ---- 2. openssl notice (needed to decrypt HTTPS) ----------
where openssl >nul 2>nul
if errorlevel 1 (
    echo   [warn] openssl not found on PATH.
    echo          HTTP capture still works; HTTPS cannot be decrypted.
    echo          Install Git for Windows, or point PROXYSCOPE_OPENSSL
    echo          at openssl.exe.
    echo.
)

REM ---- 3. first-run notice ----------------------------------
if not exist "%~dp0data\certs\proxyscope-ca.crt" (
    echo   [info] First run: the root CA is generated automatically.
    echo          Run install-ca.bat afterwards to trust it, otherwise
    echo          HTTPS traffic cannot be decrypted.
    echo.
)

REM ---- 4. switch console to UTF-8 so Chinese displays right --
REM Two settings are required:
REM   chcp 65001    - makes the console interpret UTF-8 bytes
REM   PYTHONUTF8=1  - makes Python 3.7+ actually emit UTF-8
REM PYTHONIOENCODING alone is not enough: on Windows Python can
REM still select the ANSI codepage (936) for redirected output.
set "OLDCP="
for /f "tokens=2 delims=:" %%c in ('chcp') do set "OLDCP=%%c"
if defined OLDCP set "OLDCP=!OLDCP: =!"
chcp 65001 >nul
set "PYTHONPATH=%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

"!PY!" -m proxyscope --port %PROXY_PORT% --web-port %WEB_PORT% --data-dir "%~dp0data" %*
set "RC=!ERRORLEVEL!"

REM restore the original codepage so the prompt looks normal
if defined OLDCP chcp !OLDCP! >nul

echo.
if not "!RC!"=="0" (
    echo   [x] exited with code !RC!
    echo       If the port is already in use, pick another one:
    echo         set PROXY_PORT=18889 ^&^& start.bat
    echo.
)
pause
exit /b !RC!

REM ===========================================================
REM  helpers
REM ===========================================================

:no_python
echo [x] Python 3.9+ not found.
echo.
echo     Install from: https://www.python.org/downloads/
echo     During setup, tick "Add python.exe to PATH".
echo.
echo     Installed elsewhere? Create a file named .python-path
echo     next to this script containing the full path to python.exe.
echo.
pause
exit /b 1

:try_py3
set "TMPF=%TEMP%\ps_pyprobe_%RANDOM%.txt"
py -3 -c "import sys;sys.stdout.write(sys.executable)" >"%TMPF%" 2>nul
if not exist "%TMPF%" exit /b 0
set "PROBE="
set /p "PROBE="<"%TMPF%"
del "%TMPF%" >nul 2>nul
if not defined PROBE exit /b 0
call :accept "%PROBE%"
exit /b 0

:try_path_python
for /f "delims=" %%p in ('where python 2^>nul') do (
    echo %%p | find /i "WindowsApps" >nul
    if errorlevel 1 (
        if not defined PY call :try_file "%%p"
    )
)
exit /b 0

:try_file
REM %~1 = full path to python.exe ; verifies 3.9+ by exit code
if not exist "%~1" exit /b 0
"%~1" -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)" >nul 2>nul
if errorlevel 1 (
    echo   [!] skipping %~1  ^(requires Python 3.9+^)
    exit /b 0
)
set "PY=%~1"
exit /b 0

:accept
REM %~1 = candidate interpreter path reported by a probe
if not exist "%~1" exit /b 0
"%~1" -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "PY=%~1"
exit /b 0
