@echo off
rem Launch both tray icons silently (M=mouse, H=headset).
cd /d "%~dp0"
start "" pythonw "%~dp0atk_tray.py"
