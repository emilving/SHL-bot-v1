@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SHL-boten - test

set PY=python
where python >nul 2>nul || set PY=py

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
