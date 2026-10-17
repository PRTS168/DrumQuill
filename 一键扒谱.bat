@echo off
chcp 936>nul
title DrumQuill - 一键扒谱
cd /d "%~dp0"
if "%~1"=="" (
    echo.
    echo   把音频文件（MP3 / WAV / FLAC / M4A）拖到本文件图标上，即可一键扒谱。
    echo   只想打开界面，请双击 start.bat。
    echo.
    pause
    exit /b
)
echo [1/2] 检查运行环境（首次运行会自动安装，约 10 分钟）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap\ensure.ps1"
if errorlevel 1 (
    echo.
    echo   环境检查或安装未成功，请查看上方提示，或打开界面在设置里单独安装组件。
    pause
    exit /b 1
)
echo.
echo [2/2] 开始处理：%~nx1
echo   有 GPU 通常 2-3 分钟；纯 CPU 约 5-8 分钟，请耐心等待。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_pipeline.ps1" -SongPath "%~1"
echo.
echo  ============ 完成 ============
echo  鼓谱 PDF 在 output\ 目录，文件名形如 歌曲名_鼓谱.pdf
echo.
pause