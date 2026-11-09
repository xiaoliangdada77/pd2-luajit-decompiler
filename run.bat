@echo off
REM PAYDAY 2 LuaJIT decompiler -- one-click
REM
REM Usage:
REM   run.bat                        (defaults: ..\..\assets  ->  .\out)
REM   run.bat <assets_dir> <out_dir>
REM
REM Set PYTHON to a python.exe path if `python` is not on PATH.
setlocal
if "%PYTHON%"=="" set PYTHON=python

set ASSETS=%~1
if "%ASSETS%"=="" set ASSETS=%~dp0..\..\assets
set OUT=%~2
if "%OUT%"=="" set OUT=%~dp0out

echo Using PYTHON=%PYTHON%
"%PYTHON%" "%~dp0pd2decompile.py" --all "%ASSETS%" "%OUT%" --workers 8 --index
if errorlevel 1 echo [exit %errorlevel%]
endlocal
