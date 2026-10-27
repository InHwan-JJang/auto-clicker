@echo off
rem Builds AutoClicker.exe from autoclicker.py (requires Python 3.9+).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv 2>nul || python -m venv .venv || goto :error
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q pyinstaller || goto :error
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --noupx ^
    --name AutoClicker autoclicker.py || goto :error
copy /y "dist\AutoClicker.exe" "AutoClicker.exe" >nul || goto :error

echo.
echo Done: %~dp0AutoClicker.exe
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
