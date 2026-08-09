@echo off
REM Builds a standalone AtmosTrackSplitter.exe with PyInstaller.
REM --windowed means no console window is attached, so closing the
REM PowerShell/cmd window you launched it from can no longer kill the app.
REM
REM Uses "python -m PyInstaller" rather than the bare "pyinstaller" command,
REM since pip can install the package without its Scripts folder ending up
REM on PATH - calling it as a module always works.
REM
REM NOTE: this bundles the Python app itself, but NOT mkvmerge/mkvextract/
REM ffmpeg/ffprobe - those still need to be installed separately and
REM reachable on PATH (or their paths set via settings.py).

python -m PyInstaller --noconfirm --onefile --windowed --icon=atmos_ripper.ico --name AtmosTrackSplitter main.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Build FAILED - see the error above.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo Build finished. Exe is in the "dist" folder.
pause
