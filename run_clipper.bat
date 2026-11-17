@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0skills\so2-clipper\scripts\clip.ps1" run %*
exit /b %ERRORLEVEL%
