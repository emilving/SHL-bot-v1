@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SHL-boten - test

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

%PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt

echo.
echo Dagens matcher:
echo.
%PY% -m shlbot games
echo.
echo Kopiera ID:t (den langa koden langst till hoger) for en match som pagar.
echo Hogerklicka i fonstret for att klistra in.
set /p GAMEID=Klistra in ID och tryck Enter: 
echo.
%PY% -m shlbot probe %GAMEID%
echo.
echo ==========================================================
echo  Kopiera ALL text i fonstret och klistra in den i chatten.
echo  (Markera med musen och tryck Enter for att kopiera.)
echo ==========================================================
pause
