@echo off
rem Запуск с консолью: журнал виден прямо в окне.
cd /d "%~dp0"
".venv\Scripts\python.exe" "main.py"
pause
