@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SHL-boten

rem Hitta en riktig Python (inte Windows genvag till Microsoft Store)
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
  echo.
  echo ==========================================================
  echo  Python ar inte installerat.
  echo.
  echo  1. Ga till python.org/downloads och klicka Download Python
  echo  2. Oppna filen och KRYSSA I "Add python.exe to PATH"
  echo  3. Klicka Install Now
  echo  4. Dubbelklicka pa den har filen igen
  echo ==========================================================
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
