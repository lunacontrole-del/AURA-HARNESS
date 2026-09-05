@echo off
setlocal
title AURA Quant-X - Diagnostico Definitivo Read-Only
cd /d "%~dp0.."
python engine\diagnostico_aura_definitivo.py
set "RC=%ERRORLEVEL%"
echo.
echo Diagnostico encerrado com codigo %RC%.
echo Nenhuma configuracao ou banco foi alterado.
exit /b %RC%
