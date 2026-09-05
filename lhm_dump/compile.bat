@echo off
:: 编译 lhm_dump.exe（需要 .NET Framework 4.x 的 csc.exe，Windows 自带）
:: 用法：将 LibreHardwareMonitorLib.dll 放在 LibreHardwareMonitor\ 目录后运行本脚本

setlocal
set "ROOT=%~dp0.."
set "LHM_DIR=%ROOT%\LibreHardwareMonitor"
cd /d "%~dp0"

set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe
if not exist "%CSC%" (
    echo [ERROR] csc.exe not found. Install .NET Framework 4.x developer tools.
    pause
    exit /b 1
)
if not exist "%LHM_DIR%\LibreHardwareMonitorLib.dll" (
    echo [ERROR] LibreHardwareMonitorLib.dll not found in "%LHM_DIR%".
    pause
    exit /b 1
)

"%CSC%" /nologo /target:exe /out:"%LHM_DIR%\lhm_dump.exe" /r:"%LHM_DIR%\LibreHardwareMonitorLib.dll" lhm_dump.cs
if errorlevel 1 (
    echo [ERROR] compile failed
    pause
    exit /b 1
)

echo [OK] "%LHM_DIR%\lhm_dump.exe" built.
pause
