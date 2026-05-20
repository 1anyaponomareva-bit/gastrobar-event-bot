@echo off

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (

  echo Oshibka: net .venv — snachala: python -m venv .venv

  echo            zatem: .venv\Scripts\pip install -r requirements.txt

  pause

  exit /b 1

)

call .venv\Scripts\activate

python -B scripts\check_telegram_conflict.py

pause


