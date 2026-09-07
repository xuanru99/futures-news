@echo off
REM Remove the two scheduled tasks created by register_tasks.bat

schtasks /delete /tn "FuturesNewsMorning" /f
schtasks /delete /tn "FuturesNewsEvening" /f

echo.
echo Tasks removed.
pause
