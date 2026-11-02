@echo off
setlocal
cd /d "%~dp0"

REM ===========================================================
REM  Install / remove the ProxyScope root CA (current user only)
REM
REM  ASCII-only on purpose: cmd.exe parses .bat with the system
REM  ANSI codepage, so UTF-8 Chinese here would be mojibake.
REM
REM  usage: install-ca.bat          install
REM         install-ca.bat remove   uninstall
REM ===========================================================

set "CA=%~dp0data\certs\proxyscope-ca.crt"

if not exist "%CA%" (
    echo [x] CA certificate not found:
    echo     %CA%
    echo.
    echo     Run start.bat once first; the CA is generated on startup.
    echo.
    pause
    exit /b 1
)

if /i "%~1"=="remove" goto :remove

echo [*] Installing trusted root CA for the current user:
echo     %CA%
echo.
certutil -user -addstore -f Root "%CA%"
if errorlevel 1 (
    echo.
    echo [x] Installation failed.
    pause
    exit /b 1
)
echo.
echo [ok] Done. HTTPS traffic can now be decrypted by ProxyScope.
echo      To uninstall: install-ca.bat remove
echo.
pause
exit /b 0

:remove
echo [*] Removing ProxyScope Root CA from the current user store...
certutil -user -delstore Root "ProxyScope Root CA" >nul 2>&1
echo [ok] Removed.
echo.
pause
exit /b 0
