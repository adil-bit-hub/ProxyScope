@echo off
setlocal
cd /d "%~dp0"

REM ===========================================================
REM  Point the Windows system proxy at ProxyScope
REM
REM  ASCII-only on purpose: cmd.exe parses .bat with the system
REM  ANSI codepage, so UTF-8 Chinese here would be mojibake.
REM
REM  usage: set-proxy.bat        enable
REM         set-proxy.bat off    restore direct connection
REM ===========================================================

set "PROXY_PORT=18888"
set "REGKEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

if /i "%~1"=="off" goto :off

echo [*] Setting system proxy to 127.0.0.1:%PROXY_PORT%
reg add "%REGKEY%" /v ProxyEnable /t REG_DWORD /d 1 /f >nul
reg add "%REGKEY%" /v ProxyServer /t REG_SZ /d "127.0.0.1:%PROXY_PORT%" /f >nul
reg add "%REGKEY%" /v ProxyOverride /t REG_SZ /d "localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.2*;172.30.*;172.31.*;192.168.*" /f >nul
echo [ok] Proxy enabled.
echo      Most apps read this at startup - restart them if needed.
goto :notify

:off
echo [*] Disabling system proxy (direct connection)
reg add "%REGKEY%" /v ProxyEnable /t REG_DWORD /d 0 /f >nul
echo [ok] Proxy disabled.

:notify
REM tell WinINet that the settings changed
rundll32.exe wininet.dll,InternetSetOption 0 39 0 0 >nul 2>nul
echo.
pause
exit /b 0
