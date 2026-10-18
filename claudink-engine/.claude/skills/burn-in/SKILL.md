---
name: burn-in
description: 'Phase 1 loop: re-verify every W-item from fresh data, build each fix into the machine with a test, cut up to 3 real candidates, hunt new failure modes, and repeat until the exit criteria hold. Use after Phase 0 and whenever state/burn-in.md still has open items.'
---
Full spec: START_HERE.md section 4. Format rules: knowledge/RULES.md wins.

State: state/burn-in.md (iteration number, open items, next step). Read it first, at the start of
every iteration and after any compaction. Register: register/weak-points.md (W1-W18 seeded; every
new problem is W19+; columns id, claim, evidence re-verified, fix in the machine, regression test,
status OPEN | FIXED | WONT-FIX reason | BLOCKED-ON-CLAUDIN the one action, date).

Before every iteration: today's 2 reels come first (daily-run skill, Phase 0 step 6). The burn-in
pauses 06:30-10:30 and never causes a zero-post day. Use subagents (scout, analyst) for heavy reading.

Each iteration (at most 3 candidates, one tiled image per reel for the critic, a fixed footage bench):
1. Re-verify: py -m tools.reels_analysis data-check -> register/data-check.md. Recompute the evidence
   of every OPEN item from fresh data; mark refuted claims, keep the true ones.
2. Build the fixes into the machine. Each fix = code + a test in tools/tests/ + the register row:
   W2/W3/W4 -> slot_planner + deliver (HOLD >= 4); W5 -> hook_gate; W7 -> gates G1; W9 ->
   sessions.csv + G11; W14 -> RULES.md as the only rulebook (strip contradicting rules out of the
   skills and CLAUDE.md); W15 -> reels_analysis; W16 -> fingerprint; W17 -> gates test only what
   predicts reach. py -m pytest tools/tests -q is green before a change reaches the daily program;
   a change that breaks a test is reverted, not shipped.
3. Make real candidates: 2 session-story cuts + 1 b-roll (or 1 talking head when TH footage exists)
   from his actual unposted footage, newest sessions first, respecting sessions.csv. Every gate
   (gates skill), then the critic. Per failure: a one-line diagnosis, fix the EDL (selection,
   order, in/out points; never grading), re-render with --preview (only changed segments
   re-encode), re-gate. At most 4 passes per candidate.
4. Hunt new failure modes: run each candidate against the regression suite and against his
   winners (what does his best reel do in the first 5 s that ours does not?). Try to break the
   pipeline: wrong capture order, cut-off words, HDR/SDR tags, rotation, black or frozen frames, a
   wrong caption tail, a duplicate opener, a TV in frame, a color piece, a slot clash, a stale
   lock, a late run, an unreadable CSV. Every new bug = a W-item + a test.
5. Append a 5-line iteration summary to state/burn-in.md.

Exit criteria (all must hold):
E1 every W-item FIXED with a passing test, WONT-FIX with a reason that follows the precedence in
   section 0, or BLOCKED-ON-CLAUDIN with the one action listed in BURN_IN_REPORT.
E2 two consecutive iterations where every candidate passes all hard gates on its first render.
E3 the critic is calibrated (winners above flops in >= 80% of pairs), or "critic uncalibrated" is
   logged after 3 tries or because fewer than 3 winners and 2 flops were obtainable; gates decide.
E4 the hook gate passes for every delivered hook: 0 burned, 0 losing types, each matches frame 1,
   each <= 55 characters.
E5 ledgers complete: footage.csv covers every listed folder; every posted reel since Oct 8 that
   exists locally or could be downloaded is fingerprint-matched or marked his own; the rest are
   "unverifiable".
E6 a full delivery dry-run passes into runs/dryrun/<date>/ (never post/): <= 2 reels, slots >= 3 h
   apart, the exact caption tail, files that play (ffprobe + a clean full decode), chat copy
   <= 25 MB, no black frames, no cut-off words, POST_THIS in the section 10 format.
E7 both scheduled tasks exist and one test run finished without stalling.

Don't stall: no progress on a criterion for 3 iterations -> change approach and log why. After
12 iterations stop anyway and carry the OPEN items into the daily program (one per day, spare
time). Usage limit: the state is on disk; the next daily-run resumes the burn-in.

On exit: write BURN_IN_REPORT.md (<= 40 lines: what was wrong, what changed, what is still open,
the first week's plan). Copy it to F:\My Drive\ClaudeInk Reels\ and post\<today>\. End the session;
the scheduled tasks are the program.

Never: post, log in, pay, delete/move footage, burst, re-upload.
