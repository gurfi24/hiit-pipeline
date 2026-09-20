@echo off
rem Task Scheduler launcher: absolute paths, UTF-8 output, project cwd, one log per script.
rem Task Scheduler does not inherit the PATH of an interactive shell.
rem Usage: run_task.cmd <script.py> [args]
set "PYTHONIOENCODING=utf-8"
set "PATH=C:\Users\gurfi\AppData\Local\Programs\Python\Python310;C:\Program Files\Git\cmd;C:\Users\gurfi\.local\bin;%PATH%"
cd /d "%~dp0.." || exit /b 1
if not exist logs mkdir logs
echo [%date% %time%] start %* >> "logs\%~n1.log"
"C:\Users\gurfi\AppData\Local\Programs\Python\Python310\python.exe" %* >> "logs\%~n1.log" 2>&1
set "RC=%errorlevel%"
echo [%date% %time%] exit %RC% >> "logs\%~n1.log"
exit /b %RC%
