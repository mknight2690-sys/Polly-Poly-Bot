@echo off
REM Desktop-friendly stopper — runs stop_polly.ps1 next to this file
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_polly.ps1"
