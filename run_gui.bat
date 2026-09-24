@echo off
rem Starts the desktop interface from this folder's virtual environment.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m idcard_extractor.gui
) else (
    echo The virtual environment .venv was not found. See README: Installation.
    pause
)
