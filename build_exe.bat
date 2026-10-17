@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install PyInstaller
if errorlevel 1 goto :fail
python -m PyInstaller --noconfirm --clean --onefile --windowed --name ATK-AULA-DualBattery --hidden-import hid atk_tray.py
if errorlevel 1 goto :fail
echo.
echo 已生成：dist\ATK-AULA-DualBattery.exe
echo 若此前已启用开机自启，请用新版本托盘菜单先关闭再重新启用。
pause
exit /b 0
:fail
echo.
echo 打包失败。可以先用 托盘调试.bat 查看错误。
pause
exit /b 1
