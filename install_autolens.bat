@echo off
setlocal
title AutoLens NukeX v0.10.30 Installer

echo.
echo ================================================================
echo AutoLens NukeX v0.10.30 Installer
echo ================================================================
echo.
echo This window should remain open until you press Enter at the end.
echo The live log is written to:
echo   %USERPROFILE%\.nuke\AutoLens\AutoLens_Install_Live.log
echo.

where py >nul 2>nul
if errorlevel 1 goto try_python

py -3 "%~dp0install_autolens.py"
set "RESULT=%ERRORLEVEL%"
goto done

:try_python
where python >nul 2>nul
if errorlevel 1 goto no_python

python "%~dp0install_autolens.py"
set "RESULT=%ERRORLEVEL%"
goto done

:no_python
echo ERROR: Python 3 was not found.
echo Install Python 3 and run this installer again.
set "RESULT=2"

:done
echo.
echo Installer exit code: %RESULT%
echo.

if "%RESULT%"=="0" goto success

echo AutoLens installation failed or stopped before completion.
echo Open:
echo   %USERPROFILE%\.nuke\AutoLens\AutoLens_Install_Report.txt
echo   %USERPROFILE%\.nuke\AutoLens\AutoLens_Install_Live.log
goto finish

:success
echo AutoLens installation completed successfully.

:finish
echo.
pause
exit /b %RESULT%
