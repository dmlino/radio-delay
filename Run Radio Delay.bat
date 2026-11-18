@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "radio_delay.py"
    exit /b 0
)
echo First run: installing Radio Delay dependencies, one moment...
python -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install numpy sounddevice imageio-ffmpeg
if errorlevel 1 (
    echo.
    echo Setup failed. Make sure Python 3 is installed and available as "python".
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "radio_delay.py"
exit /b 0
