@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SHL-boten - testa uppstallning

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python ar inte installerat. Se instruktionerna i README.
  pause
  exit /b 1
)

%PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
echo.
echo Skriv ett datum, t.ex. 2026-09-26, eller tryck bara Enter for idag.
set "DATUM="
set /p DATUM=Datum: 
%PY% -m shlbot lineups %DATUM%
echo.
echo ==========================================================
echo  Ta en skarmbild av fonstret och skicka den.
echo ==========================================================
pause
