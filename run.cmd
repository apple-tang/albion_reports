@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto fallback
py -3 -X utf8 "%~dp0collector.py"
goto done
:fallback
python -X utf8 "%~dp0collector.py"
:done
echo.
pause
