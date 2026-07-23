@echo off
rem Запуск без окна консоли (pythonw). Для отладки используйте run_console.bat.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "main.py"
