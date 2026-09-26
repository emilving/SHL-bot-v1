@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SHL-boten

rem Hitta Python (python eller py)
set PY=python
where python >nul 2>nul || set PY=py
where %PY% >nul 2>nul || (
  echo.
  echo Hittar inte Python. Installera Python fran python.org och
  echo kryssa i "Add python.exe to PATH" under installationen.
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo.
  echo Filen .env saknas. Kopiera .env.example, doep om kopian till .env
  echo och fyll i DISCORD_TOKEN, DISCORD_CHANNEL_ID och DISCORD_GUILD_ID.
  echo.
  pause
  exit /b 1
)

echo Installerar det boten behover (tar en stund forsta gangen)...
%PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installationen misslyckades. Ta en skarmbild av fonstret och skicka den.
  pause
  exit /b 1
)

echo.
echo ==========================================================
echo  Boten startar. LAT DET HAR FONSTRET VARA OPPET.
echo  Stang fonstret for att stanga av boten.
echo ==========================================================
echo.
%PY% -m shlbot run
echo.
echo Boten har stannat. Ta en skarmbild av texten ovan om nagot gick fel.
pause
