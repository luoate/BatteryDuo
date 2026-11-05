@echo off
rem Launch both mouse and headset tray icons silently.
cd /d "%~dp0"
start "" pythonw "%~dp0atk_tray.py"
