@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo CGV Structured POC - Local Residential Test
echo ==========================================
echo.
echo This test does NOT send Discord alerts and does NOT modify production state.
echo It only observes structured responses from the official CGV booking page.
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  set "PYCMD=py -3"
) else (
  set "PYCMD=python"
)

%PYCMD% --version >nul 2>nul
if not %errorlevel%==0 (
  echo [ERROR] Python 3 is not installed or not in PATH.
  echo Install Python 3 first, then run this file again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating local virtual environment...
  %PYCMD% -m venv .venv
  if not %errorlevel%==0 goto :fail
)

call ".venv\Scripts\activate.bat"

echo [2/4] Installing required packages...
python -m pip install -q --disable-pip-version-check -r requirements.txt
if not %errorlevel%==0 goto :fail

echo [3/4] Running structured-response POC...
python poc_structured.py
if not %errorlevel%==0 goto :fail

echo.
echo [4/4] Result
echo ------------------------------------------
if exist "poc_structured_report.json" (
  python -c "import json; d=json.load(open('poc_structured_report.json',encoding='utf-8')); print(json.dumps({'summary':d.get('summary'),'discovered_structured_endpoints':d.get('discovered_structured_endpoints')},ensure_ascii=False,indent=2))"
) else (
  echo Report file was not created.
)

echo.
echo Finished. Send poc_structured_report.json to ChatGPT for comparison.
pause
exit /b 0

:fail
echo.
echo [ERROR] Local POC failed before completion.
pause
exit /b 1
