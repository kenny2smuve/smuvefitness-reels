# @claudink tattoo reels: weak points audit
Date: 2026-10-18. Scope: Claudin's tattoo reels (IG @claudink, TikTok @claudn_p) and the system that makes them.
Goal these are judged against: top-of-funnel reach that turns into follows, then bookings.

Evidence base:
- 275 reels posted Aug 10 to Oct 15, from `Desktop\igserver\reels_history.csv` (hourly Porter pulls). The latest snapshot per reel was used, and the stats were computed with `analysis/reels_analysis.py`.
- Dated account logs, Oct 1 to 18 (`knowledge/HISTORY.md`).
- 60+ reels watched frame by frame (`knowledge/reel-video-learnings.md`).
- The 6 scheduled tasks and 50+ project docs.

"Hit" means 5,000 or more views. His trial bar is 3–5K, aiming for 5K.

---

## The five that matter most

**W1. Most of October's output was a format that almost never breaks out.**
- In October, 57% of reels (50 of 87) had under 10s average watch. Only 1 of those 50 hit 5K (DeDkdJTA_cd, the price debate).
- October reels with 17s or more of average watch hit 17% of the time (4 of 24).
- The monthly hit rate fell from 20% in Aug (22/109) to 15% in Sep (12/79) to 7% in Oct (6/87). September was 68% long-watch reels.
- Across all 275 reels, under 8s of average watch had a median of 577 views and a 6% hit rate. 25s or more had a median of 2,392 and a 25% hit rate.
- Short reels can still win, but only when they carry an idea:
  - The August short hits were opinion or education pieces ("Was this helpful?" 26K, "Choose wisely…" 9.3K, "horror stories" 8.4K) and one price debate.
  - October's station and B-roll montages with generic captions ("Did I cook?", "Be honest", "Which one?") all sat at the floor.
- **Fix:** every day's first post is a session story (36–48s single session, ≤75s multi-day) built to hold attention for 17s or more (a target to test). Shorts run at most 1 a day, only with a real idea (a debate, education, hours labels, or an endurance number), and never under 10s.

**W2. Burst posting splits the test pool.**
- Reels posted with 3 or more others inside the same hour (n=91): median 1,356 views, 10% hit.
- Reels posted alone, with nothing else within 3h either side (n=37): median 1,908, 24% hit.
- Alone and with 17s or more of watch: 5 of 14 hit (36%).
- In a burst and under 10s of watch: 4 of 54 hit (7%).
- All three account outliers this month went up alone in daytime: DeE6fyBKWpz at 11:06, DebCcJvqDI4 at 06:08, DehJPlnKk8I at 10:16.
- **Fix:** at most 2 posts a day. The first goes up alone, and the second at least 3h later. Never post a batch.

**W3. Zero-post days are the most expensive thing on the account right now.**
- Nothing has been posted since 10/15 11:29.
- Account reach per day went 14.3K → 7.4K → 4.2K, down 70% in 2 days. Profile visits went from ~530 to ~370 a day.
- **Fix:** never zero. Always keep 4 or more rule-passing cuts in a HOLD queue for dry days.

**W4. Claude's cuts aren't getting posted, and the batch deliveries fed the bursts.**
- No Claude cut has been posted since Oct 8. Atlas C3–C5 and the Oct 17 B-roll batch are unposted.
- When Claude delivered 5–17 reels at once, they went up in bursts:
  - Oct 7: 12 posted in 2.5h, median 522.
  - batch18: 5 posted in 22 minutes at 1:30am, median 161.
  - Oct 17: 10 B-roll reels delivered at once.
- Overproduction: `D:\ClaudeInk\out\shorts` holds about 180 rendered shorts (the S01–S15 versions, AA/BB/RAW/SDR/T/U/W/X/Y/Z, F/K/N/P/Men). Only about 30 were ever posted, on IG or TikTok.
- **Fix:** drip delivery. Deliver exactly 2 post-ready reels a day, each with a dated slot, 2 hooks and the caption. Extras wait in HOLD and are never sent together. Catalog the old renders as "legacy". Never re-deliver one as-is: they carry old hooks, old formats and retired station openers.

**W5. The hooks recycle dead angles. Claude's own Oct 17 list failed its own data.**
- 8 of the 10 BB hooks were already used, retired, or a proven losing type:
  - "Best $1,200 you'll ever spend": used Oct 7. The $-hook on a frame-1 short was retired 10/9.
  - "POV: you finally booked…": S03, 1,081.
  - "Which one would you get? 1–7": S02, 1,159. The "which one" family runs 47–75% skip.
  - "Cheap tattoos aren't good. Good tattoos aren't cheap.": S01, 1,351.
  - "Rate my work 1–10": "Rate these 1-10" was used Oct 7.
  - "Booked out for a reason": used Oct 7, and booking announcements flop (74.8% skip).
  - "This is what $1,200 a day gets you": a $-hook.
  - "Black and grey realism hits different": a brag type. The brag shorts got 146–385.
- "Stop getting small tattoos" is weak (median type). Only "Would you sit 8 hours for one of these?" passes.
- The winners carry a concrete stakes number or a question about the viewer: "Was it worth the 50 hours?" (81K), "Would u tap on this spot?" (54K), "drove from Jacksonville… 10 hours" (9.1K).
- **Fix:** a hook gate. Check every hook against a burned-hook ledger and a list of losing types. It must have a number, stakes, or a question about the viewer, it must match frame 1, and it must be 55 characters or fewer.

---

## Content

**W6. The ideas, not the editing, decide the outliers, and nothing plans the ideas.**
- Outliers came from ideas:
  - The return after a year with a 50-hour project.
  - The healed reveal re-filmed with every number spoken: 9.1K vs 1.6K for take 1, same script.
  - The price debate.
  - The posterboy back piece.
- The 26-account niche scan in the bible found the same thing: the idea separates outliers, not the cut pace.
- Footage is whatever the glasses caught. Stakes numbers often never get said on camera.
- **Fix:** send a shoot card before every session. On camera: the hours, the sessions, where the client came from, and the price inside the story. Also film:
  - the moving greeting at the door;
  - the design on the body;
  - one pain question;
  - the finished piece held 3s+ in natural light;
  - a healed revisit.

**W7. Frame 1 still decides most outcomes.**
- Same session, different first frame:
  - Back piece: moving greeting 58.8K vs static back 2,057 (29×).
  - MJ: his greeting cut 25.7K vs Claude's faceless-print cuts 1.2–1.6K.
- What loses: static backs, faceless hands holding prints, still clients, title cards, blur.
- **Fix:** frame-1 gate. A face or the design on the body, in motion, in 0–1s, bright enough natively. Pick bright clips; don't grade them.

**W8. Breakout momentum gets wasted.**
- After the 50h reel, 4 follow-ups with generic captions ("What do you think?", "Was it worth it?") held 1.2–2.9K.
- Re-cuts of the same footage don't travel: all of Claude's MJ re-cuts sat at the floor.
- One new-angle follow-up within ~4h still did 2.6× median (DecWGguqIak, 10 shares).
- **Fix:** after a breakout, make 1 new-angle sequel (healed, day 2, or the reaction) within 24h. Never re-cut it. Then rest that session for 7 days.

**W9. Sessions get spent.**
- After about 4 posts from one session, new cuts land at the median at best (cathedral, MJ, NY sleeve).
- Back piece and healed Virgin Mary are spent until ~10/21–10/23.
- **Fix:** a session ledger with a post count and a rest date. 1 hero cut plus 1 follow-up per session.

**W10. Reels give viewers no reason to follow.**
- About 1.8 follows per 1K reach on the 50h breakout (estimated).
- Profile visits ran 6.5% of reach in a normal week but 3.5% in the breakout week. The endurance crowd watches and sends, but doesn't click.
- Shares come from endurance/hours stories (50h = 2.0% share rate). Saves come from healed and finished reveals and price talk (0.8% save rate).
- **Fix:** plan each day as two reels.
  - Slot A, a SEND reel: an endurance or hours story, first and alone.
  - Slot B, a SAVE/TRUST reel: a healed reveal, a talking head with the numbers, or price/value.
  - Test a series tease ("healed version next") as an ending (experiment X3).
  - Free levers to test: invite the client as a Collab on story reels (the bible cites Meta on Collab amplification; X6), and put the search words in the profile name field, e.g. "Claudin | Miami Black & Grey Realism Tattoo". He makes both changes himself.

**W11. Copy_DehRelXKsds is a re-upload risk.**
- It rebuilds your Oct 15 reel from the same files.
- Re-uploads have gotten 13–43% of the original's reach, and 3 duplicate uploads got 0 views.
- **Fix:** use it on TikTok or as a template with new pieces, never as an IG re-post.

## Posting

**W12. Cadence whiplash.**
- Some days have 12–20 posts and some have 0 (10/6, 10/9, 10/16, 10/17).
- Volume buys floor views (each trial reel gets about 1.3–1.5K) but not follows:
  - ~50 follows a day with zero posts.
  - 53–114 on days with 12–20 reels.
  - 100–158 on breakout days.
- **Fix:** 2 a day, every day, and optimize the hit rate rather than the count.

**W13. The TikTok pipeline has been dead since Oct 12.**
- The Metricool free-plan cap has failed 14 slots in a row.
- The 50h reel (81K on IG) never went to TikTok, along with 4 other held grid reels.
- **Fix (free):** post the held reels yourself from your phone, or delete the 13 ERROR posts in the Metricool planner to see whether the cap frees. Otherwise it resets Nov 1.

## System

**W14. Four rulebooks reach opposite conclusions.**
- Six scheduled tasks each write lessons to a different place: project docs, memory, Desktop files, and an artifact database.
- They conflict:
  - `Desktop\igserver\reel_learnings.md` says "keep posting 3–6 cut variants per concept" and "8pm–midnight ET is best".
  - `reel-video-learnings.md` says one version per script per day, and the daily log says 11am–1pm.
  - The skill says music bed + full-bleed SDR 1080x1920.
  - The daily review says 2K HDR letterbox, no music.
  - Memory says sessions are 2K HDR raw (Oct 11–12) and B-roll is SDR (Oct 17).
- Every edit re-derives the rules, and the rules churn: HDR→SDR, music→none, letterbox→full-bleed→letterbox, station-first→hero-first→station-first.
- **Fix:** one canonical `RULES.md` with dated precedence:
  1. Safety rules.
  2. Your latest explicit instruction.
  3. Data rules, changed only by pre-registered tests.
  4. Observations.
- Everything else is input, not authority.

**W15. The data is partly wrong.**
- `reels_history.csv` marks 274 of 275 reels as trial, including the pinned posterboy and at least 14 other grid reels. Any trial-vs-grid comparison from it is invalid.
- Porter has no retention or follows per reel. Metricool isn't connected in scheduled runs.
- Times are mixed across UTC, PT hour keys and ET.
- **Fix:** keep everything in America/New_York. Take grid status only from the grid listing. Label follows-per-reel as an estimate.

**W16. Claude can't tell its own cuts from yours once you add text and music.**
- Many reels are logged as UNSURE, so the learning loop on Claude's edits stalls.
- **Fix:** fingerprint every delivered render (perceptual frame hashes, the same method as `tt_dedupe.py`) against every posted reel automatically.

**W17. Over-processing versus the brief.**
- Rounds went to music beds, grading, gamma and HDR mapping.
- `audit.py` scores music share ≥0.45 although story reels are now no-music.
- You approved the work the moment it was "just chop and put em together".
- **Fix:** spend the intelligence on selection and order (moments, opener, chronology). Gate only what predicts reach.

**W18. A fragile pipeline and lost context.**
- Cloud↔PC staging allows one file per call. Drive download quotas, HEVC decode timeouts and context compaction add friction.
- Claude twice assumed the wrong account (SMUVE vs tattoo).
- The memory file content-ideas.md is at its size cap (49,140/49,152 bytes).
- **Fix:** run the engine on the PC where the footage lives (Claude Code), with a local metrics table and ledgers on disk instead of prose logs.

## What's working (keep)
- His own session-story formula: a moving face plus greeting at 0s, the stakes fact, the reaction line, and the finished piece held ≥3s (58.8K, 25.7K).
- Healed reveal plus a joke before the photo plus every number spoken (9.1K, 71 saves).
- The labelled-hours montage ("1 session / 7 hours"), which beat unlabelled montages 2,477 and 1,470 vs 1,144 and 498.
- The price debate on new footage.
- The approved B-roll look: SDR, raw, the station-first 13.6s template.
- Strict chronology, complete sentences, audio kept with its own footage, no black frames.
