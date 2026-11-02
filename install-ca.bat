@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ============================================================
REM  安装/卸载 ProxyScope 根证书到「当前用户」受信任根存储
REM  用法: install-ca.bat          安装
REM        install-ca.bat remove   卸载
REM ============================================================

set CA=%~dp0data\certs\proxyscope-ca.crt

if not exist "%CA%" (
    echo [x] 未找到 CA 证书: %CA%
    echo     请先运行一次 start.bat，代理会自动生成 CA。
    pause
    exit /b 1
)

if /i "%~1"=="remove" (
    echo [*] 从「受信任的根证书颁发机构」移除 ProxyScope Root CA ...
    certutil -user -delstore Root "ProxyScope Root CA" >nul 2>&1
    echo [*] 已移除。
    goto END
)

echo [*] 安装证书到当前用户受信任根存储：
echo     %CA%
certutil -user -addstore -f Root "%CA%"
if errorlevel 1 (
    echo [x] 安装失败。
) else (
    echo.
    echo [√] 安装完成。现在 HTTPS 流量可被 ProxyScope 解密查看。
    echo     卸载命令: install-ca.bat remove
)

:END
echo.
pause
endlocal
