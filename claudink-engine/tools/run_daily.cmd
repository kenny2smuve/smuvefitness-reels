@echo off
setlocal
set PYTHONUTF8=1
cd /d D:\ClaudeInk\reel-engine
if not exist runs\cmd mkdir runs\cmd
rem %DATE% carries slashes and spaces on some locales; make it file-name safe.
set LOGDATE=%DATE:/=-%
set LOGDATE=%LOGDATE: =_%
set LOGFILE=runs\cmd\%LOGDATE%.log
echo ==== reel-daily start %DATE% %TIME% >> "%LOGFILE%"
rem Unattended run (verified against code.claude.com, 2026-10-18): auto mode lets the classifier
rem approve routine actions; --permission-prompts none (Claude Code 2.1.259+) denies anything that
rem would still prompt instead of waiting; the deny rules hold in every mode, and tools\safeio.py
rem refuses any write outside reel-engine and F:\My Drive\ClaudeInk Reels. A -p run never shows
rem the trust dialog, so the allow rules must live in %USERPROFILE%\.claude\settings.json (README).
rem On a build older than 2.1.259 drop --permission-prompts none and use --permission-mode dontAsk.
claude -p "Read CLAUDE.md, then run the daily program exactly as .claude/skills/daily-run/SKILL.md says." --permission-mode auto --permission-prompts none --max-turns 400 >> "%LOGFILE%" 2>&1
echo ==== reel-daily end %DATE% %TIME% exit %ERRORLEVEL% >> "%LOGFILE%"
endlocal
exit /b 0

rem Register with Task Scheduler (run once, elevated prompt). Every day at 06:40 local (PC clock on Eastern):
rem schtasks /create /tn "reel-daily" /tr "\"D:\ClaudeInk\reel-engine\tools\run_daily.cmd\"" /sc daily /st 06:40 /f
rem Check:  schtasks /query /tn "reel-daily" /v /fo list
rem Test:   schtasks /run /tn "reel-daily"
