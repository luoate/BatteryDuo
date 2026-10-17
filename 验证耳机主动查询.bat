@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [注意] 本脚本会向 A7 Pro 的 FF90 接口发送一次 HID Output Report。
echo 请确认已退出旧的电量监控程序和 AULA 控制面板。
set /p confirm=输入 YES 进行一次主动查询测试，其他内容退出：
if /I not "%confirm%"=="YES" exit /b 0
python aula_a7pro_battery.py --active-query --seconds 10 --debug
pause
