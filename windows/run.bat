@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "vpn-proxy.exe" goto runexe
echo vpn-proxy.exe not found, falling back to "go run ."
go run . %*
goto end

:runexe
".\vpn-proxy.exe" %*

:end
echo.
echo === exit code: %ERRORLEVEL% ===
pause
