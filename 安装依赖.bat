@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install --upgrade hidapi PySide6
if errorlevel 1 (
  echo.
  echo 安装未完成。请确认 Windows 已安装 Python，并且已加入 PATH。
) else (
  echo.
  echo 依赖安装完成。请双击 启动托盘.bat。
)
pause
