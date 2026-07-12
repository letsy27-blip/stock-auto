@echo off
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0main.py"
) else if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" "%~dp0main.py"
) else (
    py -3 "%~dp0main.py"
)

echo.
echo Finished. Press any key to close.
pause > nul
