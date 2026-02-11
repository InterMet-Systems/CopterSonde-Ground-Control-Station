@echo off
REM CopterSonde GCS – Windows launcher (CMD)
REM Run from the repo root:  scripts\run_windows.bat

cd /d "%~dp0\.."

if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment …
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

python app\main.py
