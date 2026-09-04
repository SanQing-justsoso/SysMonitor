@echo off
:: 编译 lhm_dump.exe（需要 .NET Framework 4.x 的 csc.exe，Windows 自带）
:: 前提：LibreHardwareMonitorLib.dll 与本脚本同目录，且为 v4.7.2 兼容版本
:: 用法：把本脚本和 LibreHardwareMonitorLib.dll 一起放到 LibreHardwareMonitor 目录下双击运行

setlocal
cd /d "%~dp0"

set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe
if not exist "%CSC%" (
    echo [ERROR] csc.exe not found. Install .NET Framework 4.x developer tools.
    pause
    exit /b 1
)

"%CSC%" /nologo /target:exe /out:lhm_dump.exe /r:LibreHardwareMonitorLib.dll lhm_dump.cs
if errorlevel 1 (
    echo [ERROR] compile failed
    pause
    exit /b 1
)

echo [OK] lhm_dump.exe built.
pause