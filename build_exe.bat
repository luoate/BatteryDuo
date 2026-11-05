@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install PyInstaller
if errorlevel 1 goto :fail
python build_exe.py
if errorlevel 1 goto :fail
echo.
echo dist\ATK-AULA-DualBattery.exe
echo 1
pause
exit /b 0
:fail
echo.
echo fail
pause
exit /b 1
