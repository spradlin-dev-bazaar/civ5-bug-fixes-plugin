@echo off
rem Builds the mod, then runs checks.toml's automated tests in Civilization V. bazaar_check\README.md describes what the
rem tests require and do. Arguments that start with -- go to the checker. Any other argument goes to build.cmd.
setlocal
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHON="
for %%p in ("py -3" python) do if not defined PYTHON call %%~p -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PYTHON=%%~p"
if not defined PYTHON (echo The checks need Python 3.11 or newer: install it from python.org. & exit /b 1)
set "OPTIONS="
set "SDK_GIVEN="
:next_argument
if "%~1"=="" goto build
set "ARGUMENT=%~1"
if "%ARGUMENT:~0,2%"=="--" (set "OPTIONS=%OPTIONS% %ARGUMENT%") else set "SDK_GIVEN=%ARGUMENT%"
shift /1
goto next_argument
:build
rem %% delays expanding this folder's path and SDK_GIVEN until after call has doubled every ^ on this line, so a ^ in
rem either path reaches build.cmd unchanged.
call "%%~dp0build.cmd" "%%SDK_GIVEN%%" || exit /b 1
cd /d "%~dp0"
call %PYTHON% -m bazaar_check%OPTIONS% checks.toml
endlocal & exit /b %ERRORLEVEL%
