@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 goto failed
set "PYTHONUTF8=1"
where py >nul 2>nul
if errorlevel 1 goto python
set "ESG_PIPELINE_SETUP=py -3"
goto install
:python
set "ESG_PIPELINE_SETUP=python"
:install
if exist ".venv-b-audit\Scripts\python.exe" goto dependencies
%ESG_PIPELINE_SETUP% -m venv .venv-b-audit
if errorlevel 1 goto failed
:dependencies
".venv-b-audit\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Pipeline environment installed.
pause
exit /b 0
:failed
echo Installation failed. Keep the message above for diagnosis.
pause
exit /b 1
