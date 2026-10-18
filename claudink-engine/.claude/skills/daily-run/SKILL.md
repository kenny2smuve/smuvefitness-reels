---
name: daily-run
description: 'The 06:40 ET daily program: take the lock, import metrics, attribute and record results, plan two slots, source, build, gate, critic, deliver, refill HOLD, write the run log. Use for the scheduled daily task and for any catch-up run.'
allowed-tools: 'Bash(py *), Bash(python *), Bash(ffmpeg *), Bash(ffprobe *), Read, Write, Edit, Glob, Grep'
---
Full spec: START_HERE.md section 5. Format rules: knowledge/RULES.md wins.

Runs at 06:40 ET from D:\ClaudeInk\reel-engine, PYTHONUTF8=1. A late start still delivers, but only
for the next valid slots (section 11): never make a reel for a slot that has passed; if a relay time
(10:05 / 13:05 ET) passed, the Drive mirror F:\My Drive\ClaudeInk Reels\POST\<date>\ is the delivery
and the first line of POST_THIS says so.

0. Lock. Take state/engine.lock {owner, pid, start}. Another live session holds it: wait up to
   20 min, then deliver from HOLD only. Stale (older than 4 h or dead pid): take it over, note it.
   Idempotent: count reels already in post/<date>/, in ledgers/deliveries.csv and his own posts
   today (data/reels.csv posted_et); never exceed 2 per date. The burn-in pauses 06:30-10:30.
1. Read state/ (burn-in.md, preflight.md, blocked.txt, breakout.json, render_profile.json),
   knowledge/RULES.md and the ledgers: py -m tools.ledgers validate.
2. py -m tools.reels_analysis import (newest CSV). Unreadable or older than 26 h: one line in
   runs/<date>.md, continue on yesterday's data/reels.csv.
3. Fingerprint new posted reels you can obtain (learn skill, at most 6 downloads a day):
   py -m tools.panel plan --mode daily, py -m tools.panel download --mode daily --out cache/panel/posted --run,
   then py -m tools.fingerprint match "<video>" per reel (<= 12 CLAUDE, >= 20 CLAUDIN, else UNSURE).
4. Update ledgers/deliveries.csv with views_24h, views_72h, shares_72h, saves_72h, avg_watch_72h
   (analyst agent).
5. Update experiments.md: a log row per result; apply the decision rule in the file; never change
   a rule yourself (rules-keeper only).
6. Plan: py -m tools.deliver plan --now <now ISO> (slot_planner underneath). A = SEND session story
   11:00-11:30 ET, posted alone (X1 morning arm: delivered the day before for ~07:00). B = SAVE/TRUST
   >= 3 h later, 14:30-16:00 ET: healed reveal, talking head with the numbers, price/value, or a
   labelled-hours b-roll. He already posted today: the planner shifts to keep >= 3 h and <= 2 total.
   Never two reels of the same type or the same session on one day.
7. Source, in order: a new session folder; unposted footage in sessions.csv that is out of rest;
   the HOLD queue re-gated today (a hook burned since or a session spent since fails it); a b-roll
   with a fresh idea. Never zero: nothing passes -> deliver the best HOLD cut and log why. A reel
   failing G1 (frame 1) or G7 (chronology) is never delivered; soft-gate failures are listed in
   POST_THIS. Never deliver config.NEVER_REUPLOAD files.
8. Build -> gates -> critic -> fix, at most 4 passes: cut-session / cut-broll / cut-talkinghead
   skills; py -m tools.gates "<reel>" --type story|broll|th; the critic agent on the anonymised
   packet when a calibrated panel exists, else the gates decide; then py -m tools.deliver (writes
   post/<date>/A_<HHMM>_<name>.mp4 + _chat.mp4, .edl.json, .gates.json, POST_THIS.txt, the Drive
   mirror, deliveries.csv). Refill HOLD to >= 4 with the runners-up: py -m tools.deliver hold,
   named HOLD_<date>_<name>; never deliver two HOLD cuts together.
9. Breakout (>= 5K views by 24 h): write state/breakout.json {shortcode, session, at}; POST_THIS
   opens with "share it to the grid"; tomorrow's slot A is the new-angle sequel from that session
   (healed, day 2, the reaction), never a re-cut; then rest that session 7 days in sessions.csv.
10. Time left: build tomorrow's pair into hold/next/ so the morning only re-gates it (deliver
    refuses a second copy by frame-1 hash). Then py -m tools.clean_cache (cache/ only).
11. Stock check: less than 3 days of slot-A footage left -> write "Upload your last session's
    glasses clips to Drive" to state/blocked.txt; it goes to the top of POST_THIS.
12. Re-check the newest CSV right before writing POST_THIS; if he posted since the run started,
    re-plan the slots. Write runs/<date>.md (<= 12 lines), update the ledgers and
    register/weak-points.md (one OPEN W-item attacked in spare time). Release the lock.

13. Sunday only, after delivery and HOLD refill: run the weekly block (.claude/skills/weekly/SKILL.md),
    60 minutes at most, still under the same engine.lock. One scheduled task runs the whole day: the
    Desktop app skips a scheduled run while another is still running, so there is no separate 05:30 task.

Never: post, log in, pay, delete/move footage, burst, re-upload.
