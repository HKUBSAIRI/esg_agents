@echo off
setlocal
chcp 65001 >nul
title ESG Water Review
cd /d "%~dp0"
if errorlevel 1 goto bad_directory
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist "%~dp0.venv-b-audit\Scripts\python.exe" goto try_b_venv
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 -c "import sys; sys.exit(sys.version_info[:2] < (3, 10))" >nul 2>nul
if errorlevel 1 goto try_python
set "ESG_REVIEW_RUNNER=py -3"
goto run
:try_python
python -c "import sys; sys.exit(sys.version_info[:2] < (3, 10))" >nul 2>nul
if errorlevel 1 goto missing_python
set "ESG_REVIEW_RUNNER=python"
goto run
:try_b_venv
"%~dp0.venv-b-audit\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info[:2] < (3, 10))" >nul 2>nul
if errorlevel 1 goto try_python
set ESG_REVIEW_RUNNER="%~dp0.venv-b-audit\Scripts\python.exe"
:run
echo Starting ESG Water Review...
echo Please keep this window open while using the workbench.
echo Browser address: http://127.0.0.1:8765
echo.
%ESG_REVIEW_RUNNER% -m review_workbench --open %*
set "ESG_REVIEW_EXIT=%ERRORLEVEL%"
echo.
if not "%ESG_REVIEW_EXIT%"=="0" echo Startup failed. Please keep the error message above.
echo Press any key to close this window.
pause >nul
exit /b %ESG_REVIEW_EXIT%
:missing_python
echo Python 3.10 or newer was not found.
echo Please install Python and enable Add Python to PATH.
pause
exit /b 1
:bad_directory
echo Cannot open the project folder.
pause
exit /b 1
