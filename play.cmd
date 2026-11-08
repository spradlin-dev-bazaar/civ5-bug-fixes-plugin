@echo off
rem Builds the mod, then starts Civilization V with the mod switched on so that you can play. Closing the game puts your
rem files back as they were. Saves that you make by hand stay. bazaar_check\README.md has more details.
rem %% delays expanding this folder's path and the arguments until after call has doubled every ^ on this line, so a ^
rem in either reaches check.cmd unchanged.
call "%%~dp0check.cmd" --play %%* || (pause & exit /b 1)
