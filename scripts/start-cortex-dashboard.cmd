@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-cortex-dashboard.ps1" %*
