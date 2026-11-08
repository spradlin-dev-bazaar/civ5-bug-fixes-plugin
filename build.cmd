@echo off
rem Builds the plugin DLL, then the mod folder in Build\ and its package in Packages\ with ModBuddy's build targets, and
rem checks the package. The Bazaar SDK's folder may be given as the argument; bazaar-build.ps1 lists where else it looks.
rem Windows runs no PowerShell script by default; Bypass allows these runs.
setlocal
set "BAZAAR_SDK_GIVEN=%~1"
set "BUILD_PROJECT="
for /f "tokens=1,* delims==" %%a in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bazaar-build.ps1" start -Plugin') do set "BUILD_%%a=%%b"
if not defined BUILD_PROJECT exit /b 1
if not exist "%VS90COMNTOOLS%vsvars32.bat" (echo Visual C++ 2008 was not found. & exit /b 1)
call "%VS90COMNTOOLS%vsvars32.bat" >nul
set "BAZAAR_SDK=%BUILD_SDK%"
vcbuild /nologo "%~dp0%BUILD_VCPROJ%" "Release|Win32" || exit /b 1
copy /y "%~dp0src\out\%BUILD_DLL%" "%~dp0mod\" >nul || exit /b 1
for /f "tokens=2,*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 8930" /v InstallLocation 2^>nul ^| findstr /c:"REG_SZ"') do set "CIV5_PATH=%%b"
if not defined CIV5_PATH (echo Civilization V was not found in the registry. & exit /b 1)
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('MyDocuments')"`) do set "DOCUMENTS=%%d"
if not defined DOCUMENTS (echo Your Documents folder was not found. & exit /b 1)
"%WINDIR%\Microsoft.NET\Framework\v4.0.30319\MSBuild.exe" "%~dp0%BUILD_PROJECT%" /t:Package /nologo /v:q ^
	"/p:Configuration=Package Only" "/p:SafeName=%BUILD_NAME%" "/p:Civ5Path=%CIV5_PATH%" ^
	"/p:Civ5UserPath=%DOCUMENTS%\My Games\Sid Meier's Civilization 5" || exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bazaar-build.ps1" check -Plugin
endlocal & exit /b %ERRORLEVEL%
