@echo off
chcp 65001 >nul 2>&1
title Oneservice Pipeline
echo === Oneservice Pipeline ===
echo Запуск всех процессов конвейера...

cd /d "C:\Projects\PromptPilot"

echo [1/4] PP server...
start /min "PP-Server" cmd /c "py -3.11 -m promptpilot server"

timeout /t 3 >nul 2>&1

echo [2/4] PP worker...
start /min "PP-Worker" cmd /c "py -3.11 -m promptpilot worker"

timeout /t 3 >nul 2>&1

echo [3/4] Email intake (os_intake)...
start /min "OS-Email" cmd /c "py -3.11 scripts\oneservice\os_intake.py email"

timeout /t 2 >nul 2>&1

echo [4/4] Oneservice TG bot...
start /min "OS-TGBot" cmd /c "py -3.11 scripts\oneservice\os_intake.py tg"

echo.
echo === Всё запущено. Окна свёрнуты в панели задач. ===
echo Панель: http://127.0.0.1:8420
echo Дашборд: scripts\oneservice\os_iceberg.html
echo Для остановки: taskkill /f /im python.exe /fi "WINDOWTITLE eq PP-*"
pause
