---
name: learn
description: 'The learning loop: obtain and fingerprint posted reels to attribute them to deliveries, record 24 h / 72 h results, keep experiments.md by its decision rule, route rule changes through rules-keeper, update the shoot card. Use in daily-run steps 3-5 and in weekly.'
---
Full spec: START_HERE.md section 9. Format rules: knowledge/RULES.md wins.

1. Obtain new posted reels (section 8 sources, in order): a legacy render in D:\ClaudeInk\out\
   that fingerprint-matches; else py -m tools.panel plan --mode daily then
   py -m tools.panel download --mode daily --out cache/panel/posted --run (yt-dlp, his own public
   reels only, <= 6 a day, 3-5 s apart, one attempt per shortcode, stop the whole batch for 24 h
   on the first 429 or login wall; never --cookies, never --cookies-from-browser); else nothing.
   Unobtainable reels are "unverifiable" in state/panel.md.
2. Attribute: py -m tools.fingerprint match "<video>" (9 frames x crop scales 1.0, 1.15, 1.33, the
   text bands masked because he zooms into the letterbox and adds text) against deliveries.csv.
   Median distance <= 12 = CLAUDE (with the delivery id), >= 20 = CLAUDIN, between = UNSURE.
   Validate on the known posted Claude cuts (to Oct 8) before trusting a label. Two deliveries
   matching: the smaller median wins, the other is noted. Write posted_shortcode and status into
   ledgers/deliveries.csv; sign new posted reels: py -m tools.fingerprint sign "<video>" --key
   <shortcode> --kind posted.
3. Results: py -m tools.reels_analysis import, then views_24h, views_72h, shares_72h, saves_72h,
   avg_watch_72h into deliveries.csv (last snapshot at or before +24 h / +72 h; younger than 72 h
   = unknown, never a miss). Hit = views_72h >= 5000.
4. Follows: day-matched only, labelled "estimate"; no follower-count file -> "follows: n/a".
   Track daily reach too; both go next to the hit rate in the weekly numbers.
5. Every result answers three questions, one line each in runs/<date>.md: did it beat the 14-day
   median; which gate or rule did it test; which W-item or experiment does it inform.
6. experiments.md: at most 2 running (X1 slot A and X2 slot B first; X3-X6 queued). One log row per
   result (date, experiment, reel id, arm, result at 24h/72h, note). Decide on median views at
   72 h and shares/1K, never on the hit rate; a winner needs 1.5x the median and at least as many
   5K+ hits; else extend once by 6 per arm; then close "no difference". Start the next queued one
   when one closes.
7. Rule changes only through the rules-keeper agent and RULES.md section 6, with the experiment id
   and the numbers as evidence. An observation never becomes a rule on its own.
8. Shoot card: after every session update post/SHOOT_CARD.txt from what was missing in the footage
   (numbers not spoken, no greeting in motion, no finished hold, ...). deliver pastes it at the
   bottom of every POST_THIS.
9. Breakout (>= 5K views by 24 h, RULES D8): state/breakout.json {shortcode, session, at};
   tomorrow's slot A is the new-angle sequel (healed, day 2, the reaction), never a re-cut; rest the
   session 7 days (sessions.csv rest_until).

Never: post, log in, pay, delete/move footage, burst, re-upload.
