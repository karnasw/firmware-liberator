@echo off
rem Start Autoflash, the conversion tool, on Windows: double-click this file, or
rem run it from a terminal (.\start.cmd run --verbose: the tool's own commands
rem and options pass through). It runs tools\windows-start.ps1 as text, not as a
rem script file, so no policy against PowerShell scripts stops it - not even
rem one set for the whole computer, which -ExecutionPolicy cannot lift. The
rem arguments go after a "#", where PowerShell takes them for a comment rather
rem than code: windows-start.ps1 reads them from the command line itself.
setlocal
set "AUTOFLASH_START=%~dp0tools\windows-start.ps1"
rem How it was started, for the run's report (report_data.LAUNCHERS).
set "AUTOFLASH_LAUNCHER=start.cmd"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create([IO.File]::ReadAllText($env:AUTOFLASH_START))) #" %*
exit /b %errorlevel%
