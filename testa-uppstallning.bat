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
%PY% -m shlbot lineups
echo.
echo ==========================================================
echo  Ta en skarmbild av fonstret och skicka den.
echo ==========================================================
pause
