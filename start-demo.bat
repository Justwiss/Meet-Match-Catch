@echo off
rem StudiRadar-Demo unter Windows starten (Doppelklick)
cd /d "%~dp0"
set PY=python
where py >nul 2>nul
if %errorlevel%==0 set PY=py -3

if not exist ".venv\Scripts\python.exe" (
  echo Richte StudiRadar einmalig ein, das dauert ca. 1 Minute ...
  %PY% -m venv .venv
  if errorlevel 1 goto nopython
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m studiradar demo
pause
exit /b 0

:nopython
echo.
echo Python wurde nicht gefunden.
echo Bitte Python 3.11 oder neuer installieren: https://www.python.org/downloads/
echo Wichtig: beim Installieren den Haken bei "Add python.exe to PATH" setzen.
pause
exit /b 1

:fail
echo.
echo Installation der Pakete fehlgeschlagen. Internetverbindung pruefen und erneut starten.
pause
exit /b 1
