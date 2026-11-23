@echo off
chcp 65001 >nul
title Hauskern einrichten
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  ===============================================
echo    Hauskern einrichten
echo  ===============================================
echo.

rem --- Aus dem ZIP heraus gestartet? Dann landet alles im Temp-Ordner und ist spaeter weg.
echo "%~dp0" | findstr /i /r "\\Temp[0-9][0-9]*_.*\.zip\\" >nul && (
  echo  Bitte zuerst die ZIP-Datei entpacken:
  echo  Rechtsklick auf Hauskern.zip - "Alle extrahieren ..." - z. B. nach "Dokumente".
  echo  Danach im entpackten Ordner Hauskern-Setup.cmd starten.
  echo.
  pause
  exit /b 1
)
if not exist "%~dp0Hauskern.pyw" (
  echo  Hauskern.pyw fehlt in diesem Ordner. Bitte die ZIP-Datei vollstaendig entpacken.
  pause
  exit /b 1
)

call :findpy
if defined PYW goto :ready

echo  Python ist noch nicht installiert - das erledige ich jetzt (einmalig, ca. 2 Minuten).
echo.

rem 1) Python-Installer liegt schon im Ordner (python-3.x.x-amd64.exe)?
set "LOCALPY="
for %%f in ("%~dp0python-3*.exe" "%~dp0Python\python-3*.exe") do if exist "%%~f" set "LOCALPY=%%~f"
if defined LOCALPY (
  echo  - Installiere Python aus "%LOCALPY%" ...
  "%LOCALPY%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
  call :findpy
)

rem 2) winget (bei Windows 10/11 meist dabei)
if not defined PYW (
  where winget >nul 2>&1 && (
    echo  - Installiere Python ueber winget ...
    winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
    call :findpy
  )
)

rem 3) direkt von python.org laden (Signatur wird geprueft)
if not defined PYW (
  echo  - Lade Python von python.org ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-python.ps1"
  call :findpy
)

if not defined PYW (
  echo.
  echo  Das hat leider nicht geklappt. Bitte Python von Hand installieren:
  echo  https://www.python.org/downloads/  -  dabei den Haken "Add python.exe to PATH" setzen.
  echo  Danach diese Datei noch einmal starten.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)
echo  - Python ist installiert.

:ready
echo  - Python gefunden: %PYW%
echo  - Lege eine Verknuepfung auf dem Desktop an ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Hauskern.lnk'));" ^
  "$s.TargetPath='%PYW%'; $s.Arguments='\"%~dp0Hauskern.pyw\"'; $s.WorkingDirectory='%~dp0';" ^
  "if (Test-Path '%~dp0assets\app.ico') { $s.IconLocation='%~dp0assets\app.ico' }; $s.Description='Hauskern'; $s.Save()" >nul 2>&1
echo  - Starte Hauskern ...
start "" "%PYW%" "%~dp0Hauskern.pyw"
echo.
echo  Fertig! Ab jetzt startest du Hauskern ueber "Hauskern" auf dem Desktop.
timeout /t 6 >nul
exit /b 0


:findpy
rem Sucht ein richtiges Python (nicht die Microsoft-Store-Version) mit tkinter.
set "PYW="
for %%V in (313 314 315 312 311 310) do (
  if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" call :checkpy "%LOCALAPPDATA%\Programs\Python\Python%%V"
  if not defined PYW if exist "%ProgramFiles%\Python%%V\pythonw.exe" call :checkpy "%ProgramFiles%\Python%%V"
)
if defined PYW exit /b 0
for %%L in ("%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" "%WINDIR%\py.exe") do (
  if not defined PYW if exist "%%~L" (
    for /f "delims=" %%P in ('""%%~L" -3 -c "import sys,os;print(os.path.dirname(sys.executable))"" 2^>nul') do call :checkpy "%%P"
  )
)
if defined PYW exit /b 0
for /f "delims=" %%P in ('where python.exe 2^>nul ^| findstr /v /i "WindowsApps"') do (
  if not defined PYW call :checkpy "%%~dpP."
)
exit /b 0

:checkpy
rem %1 = Python-Ordner. Ok, wenn python.exe tkinter laden kann und Version >= 3.10 ist.
if not exist "%~1\pythonw.exe" exit /b 0
echo %~1 | findstr /i "WindowsApps" >nul && exit /b 0
"%~1\python.exe" -c "import sys,tkinter;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PYW=%~f1\pythonw.exe"
exit /b 0
