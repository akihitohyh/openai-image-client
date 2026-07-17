@echo off
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

if not exist ".venv\Scripts\python.exe" (
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error

".venv\Scripts\python.exe" -m pip install -r requirements.txt "pyinstaller>=6,<7"
if errorlevel 1 goto :error

".venv\Scripts\pyinstaller.exe" --clean --noconfirm OpenAIImageClient.spec
if errorlevel 1 goto :error

echo.
echo Build complete:
echo %CD%\dist\OpenAIImageClient.exe
echo.
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
