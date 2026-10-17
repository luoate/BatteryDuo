@echo off
chcp 65001 >nul
cd /d "%~dp0"
python atk_tray.py --debug
if errorlevel 1 (
  echo.
  echo 无法启动，请检查依赖或退出旧版单图标托盘。
)
pause
