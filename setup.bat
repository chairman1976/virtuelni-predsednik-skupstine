@echo off
setlocal
cd /d "%~dp0"
echo === Virtuelni predsednik skupstine - instalacija ===

set "PY="
py -3.11 -c "import sys" >nul 2>nul && set "PY=py -3.11"
if not defined PY py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY (
  echo Python 3.11 nije pronadjen - instaliram ga preko winget...
  winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
  set "PY=py -3.11"
)
%PY% -c "import sys" >nul 2>nul || (echo Python je instaliran - zatvori ovaj prozor i pokreni setup.bat ponovo. & pause & exit /b 1)

where ffmpeg >nul 2>nul || (
  echo Instaliram ffmpeg...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)
where node >nul 2>nul || (
  echo Instaliram Node.js ^(yt-dlp ga koristi za YouTube^)...
  winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
)

if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || goto :fail
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail

echo.
echo Gotovo. Pokreni start.bat i unesi svoj Claude API kljuc (dugme "API kljuc...").
echo Ako start.bat ne nadje ffmpeg ili node, odjavi se i ponovo prijavi u Windows (osvezava PATH).
pause
exit /b 0

:fail
echo.
echo Instalacija nije uspela - pogledaj poruku iznad.
pause
exit /b 1
