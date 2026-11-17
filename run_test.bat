@echo off
setlocal
cd /d "%~dp0"
py -3.12 -B -X utf8 test_so2_clipper.py
exit /b %ERRORLEVEL%
