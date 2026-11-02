@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ============================================================
REM  一键把 Windows 系统代理指向 ProxyScope（当前用户级）
REM  用法: set-proxy.bat          开启
REM        set-proxy.bat off      关闭（恢复直连）
REM ============================================================

set PROXY_PORT=18888
set REGKEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings

if /i "%~1"=="off" goto OFF

echo [*] 设置系统代理 -> 127.0.0.1:%PROXY_PORT%
reg add "%REGKEY%" /v ProxyEnable /t REG_DWORD /d 1 /f >nul
reg add "%REGKEY%" /v ProxyServer /t REG_SZ /d "127.0.0.1:%PROXY_PORT%" /f >nul
reg add "%REGKEY%" /v ProxyOverride /t REG_SZ /d "localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.2*;172.30.*;172.31.*;192.168.*" /f >nul
echo [*] 代理已开启。浏览器新开的连接会走 ProxyScope。
echo     注意：部分应用只在启动时读取系统代理，需要重启该应用。
goto NOTIFY

:OFF
echo [*] 关闭系统代理（恢复直连）
reg add "%REGKEY%" /v ProxyEnable /t REG_DWORD /d 0 /f >nul
echo [*] 已关闭。

:NOTIFY
REM 通知系统代理设置已变更
rundll32.exe wininet.dll,InternetSetOption 0 39 0 0 2>nul
echo.
pause
endlocal
