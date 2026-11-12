@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
set "LOG=%CD%\logs\scanner.out.log"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PY=%CD%\.venv\Scripts\python.exe"
if defined FN_PYTHON set "PY=%FN_PYTHON%"
if not "%~1"=="" set "PY=%~1"
if not exist "%PY%" (
  echo [%date% %time%] Python ni najden: %PY%>> "%LOG%"
  exit /b 1
)
if exist "%LOG%" (
  for %%F in ("%LOG%") do (
    if %%~zF GTR 5242880 move /Y "%LOG%" "%CD%\logs\scanner.out.prev.log" >nul
  )
)
"%PY%" -u "%CD%\scanner.py" >> "%LOG%" 2>&1
exit /b
