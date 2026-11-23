@echo off
cd /d "%~dp0"
rem 1) the .venv made by setup.bat  2) this PC's own Python 3.11  3) pythonw on PATH
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -W ignore app.py
) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" (
  start "" "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" -W ignore app.py
) else (
  start "" pythonw -W ignore app.py
)
