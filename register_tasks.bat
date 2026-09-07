@echo off
REM Register daily scheduled tasks for the futures news bot (08:00 and 17:00)
REM Run this file by double-clicking. No admin rights required for current-user tasks.

set PY=C:\Users\10702\.workbuddy\binaries\python\versions\3.13.12\python.exe
set BOT=C:\Users\10702\WorkBuddy\2026-09-07-21-44-09\futures-news\news_bot.py

schtasks /create /tn "FuturesNewsMorning" /tr "\"%PY%\" \"%BOT%\" morning" /sc daily /st 08:00 /f
schtasks /create /tn "FuturesNewsEvening" /tr "\"%PY%\" \"%BOT%\" evening" /sc daily /st 17:00 /f

echo.
echo Done. Tasks "FuturesNewsMorning" (08:00) and "FuturesNewsEvening" (17:00) registered.
echo NOTE: The computer must be ON at those times for the tasks to run.
pause
