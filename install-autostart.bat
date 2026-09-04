@echo off
:: 创建开机自启计划任务（以最高权限运行 SysMonitor）
:: 用法：右键 -> 以管理员身份运行

net session >nul 2>&1
if not %errorlevel%==0 goto elevate
goto run

:elevate
powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:run
schtasks /Create /TN "SysMonitor" /TR "%~dp0SysMonitor.exe" /SC ONLOGON /RL HIGHEST /F
echo.
echo ==========================================
echo  Autostart task created successfully: SysMonitor
echo  The monitor will start elevated at every logon.
echo  To remove it later: schtasks /Delete /TN "SysMonitor" /F
echo ==========================================
echo.
pause