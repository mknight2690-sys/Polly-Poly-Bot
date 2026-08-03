@echo off
REM Desktop-friendly launcher — runs start_polly.ps1 next to this file
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_polly.ps1"
