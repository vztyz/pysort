@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запускаю скан от администратора (MFT-режим WizTree, в разы быстрее)...
powershell -NoProfile -Command "Start-Process -FilePath 'python' -ArgumentList 'main.py','all','--open' -WorkingDirectory '%~dp0' -Verb RunAs"
