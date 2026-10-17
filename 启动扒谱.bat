@echo off
chcp 65001>nul
cd /d "%~dp0"
rem 静默启动：wscript 无窗口运行 silent-launch.vbs（py/python -> launcher -> pythonw 服务）
start "" wscript.exe "%~dp0bootstrap\silent-launch.vbs"
exit /b
