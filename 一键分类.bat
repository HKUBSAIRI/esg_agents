@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 goto failed
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist ".venv-b-audit\Scripts\python.exe" goto local_python
where py >nul 2>nul
if errorlevel 1 goto system_python
set "ESG_CLASSIFY_RUNNER=py -3"
goto run
:system_python
set "ESG_CLASSIFY_RUNNER=python"
goto run
:local_python
set ESG_CLASSIFY_RUNNER="%~dp0.venv-b-audit\Scripts\python.exe"
:run
%ESG_CLASSIFY_RUNNER% classify_esg.py %*
set "ESG_CLASSIFY_EXIT=%ERRORLEVEL%"
if not "%ESG_CLASSIFY_EXIT%"=="0" echo Classification failed. Keep the error above.
echo Press any key to close this window.
pause >nul
exit /b %ESG_CLASSIFY_EXIT%
:failed
echo Cannot open the project folder.
pause
exit /b 1
