# Weak-point register (seeded from WEAK_POINTS.md 2026-10-18; re-verify every item from fresh data)

Status: OPEN | FIXED | WONT-FIX (reason) | BLOCKED-ON-CLAUDIN (the one action he must take).
"Fix" names the machine part that enforces it. "Regression test" names the automated check.

| id | claim | evidence (re-verified) | fix (in the machine) | regression test | status | date |
|----|-------|------------------------|----------------------|-----------------|--------|------|
| W1 | October's output was mostly a format that almost never breaks out (57% under 10s watch, 1 of 50 hit). Short reels win only with an idea. | WEAK_POINTS W1 numbers; recompute with `py -m tools.reels_analysis data-check` (note the length confound) | daily-run: slot A is always a session story; shorts max 1/day, only with an idea (debate, education, hours labels, endurance number), never under 10 s (G8) | tools/tests/test_gates.py (G8), test_reels_analysis.py (watch_buckets flags the confound) | OPEN | 2026-10-18 |
| W2 | Burst posting splits the test pool (3+ in an hour: median 1,356, 10% hit; alone: 1,908, 24%). | W2; recompute alone_vs_burst | slot_planner: max 2/day, A alone, B >= 3h later | test_slot_planner.py | OPEN | 2026-10-18 |
| W3 | Zero-post days are the most expensive thing (nothing since 10/15; reach -70%). | W3; zero_post_days() | deliver: never zero (HOLD queue >= 4, deliver best HOLD when nothing passes) | test_deliver.py (hold round trip), test_slot_planner.py | OPEN | 2026-10-18 |
| W4 | Claude's cuts are not getting posted; batch deliveries fed the bursts; ~180 legacy shorts, ~30 posted. | W4 | deliver: exactly 2 post-ready reels/day with slots, 2 hooks, caption; extras to HOLD; legacy catalog never re-delivered as-is | test_deliver.py (cap, NEVER_REUPLOAD) | OPEN | 2026-10-18 |
| W5 | Hooks recycle dead angles; 8 of 10 Oct 17 hooks failed their own data. | W5 | hook_gate: burned ledger + losing types + number/stakes/viewer + <= 55 chars + frame-1 match | test_hook_gate.py | OPEN | 2026-10-18 |
| W6 | Ideas decide the outliers; nothing plans the ideas; stakes numbers never get said on camera. | W6 | SHOOT_CARD in every POST_THIS; G2 stakes-by-3s | test_deliver.py (shoot card appended) | OPEN | 2026-10-18 |
| W7 | Frame 1 decides most outcomes (moving greeting 58.8K vs static back 2,057). | W7; skill 4 (luma std >= 60, p=0.005) | G1 frame-1 gate (face/design, motion, luma std >= 60 on the active area) | test_gates.py (G1 variants) | OPEN | 2026-10-18 |
| W8 | Breakout momentum gets wasted (generic follow-ups, re-cuts). | W8 | daily-run step 9: one new-angle sequel within 24h, never a re-cut; then rest 7 days | test_deliver.py (breakout line), sessions.csv rest | OPEN | 2026-10-18 |
| W9 | Sessions get spent after ~4 posts. | W9 | sessions.csv rest dates; G11 session-rest check | test_gates.py (G11), test_ledgers.py | OPEN | 2026-10-18 |
| W10 | Reels give no reason to follow (1.8 follows/1K reach). | W10 | two-slot design (SEND then SAVE/TRUST); X3, X6 | experiments.md | OPEN | 2026-10-18 |
| W11 | Copy_DehRelXKsds is a re-upload risk. | W11 | config.NEVER_REUPLOAD + frame-1 phash re-upload guard in deliver | test_deliver.py | OPEN | 2026-10-18 |
| W12 | Cadence whiplash (12-20 posts some days, 0 others). | W12 | slot_planner 2/day every day | test_slot_planner.py | OPEN | 2026-10-18 |
| W13 | TikTok pipeline dead since Oct 12 (Metricool free cap). | W13 | POST_THIS TikTok line until Nov 1; held list in config | test_deliver.py (TikTok line) | BLOCKED-ON-CLAUDIN: post the 5 held reels from his phone | 2026-10-18 |
| W14 | Four rulebooks reach opposite conclusions. | W14 | RULES.md is the only rulebook; skills and CLAUDE.md link to it; rules-keeper is the only editor | (manual: grep skills for contradicting rules) | OPEN | 2026-10-18 |
| W15 | The data is partly wrong (trial flag, mixed time zones, no retention/follows). | W15 | reels_analysis ignores trial, converts everything to ET, labels follows as estimate/n/a | test_reels_analysis.py | OPEN | 2026-10-18 |
| W16 | Claude cannot tell its own cuts from his once he adds text and music. | W16 | fingerprint: 9 frames x 3 crop scales, text bands masked, median <= 12 | test_fingerprint.py | OPEN | 2026-10-18 |
| W17 | Over-processing versus the brief (music beds, grading, HDR mapping). | W17 | render_story: raw audio, no grade, no music; gates test only what predicts reach | test_render_story.py | OPEN | 2026-10-18 |
| W18 | Fragile pipeline and lost context (cloud/PC staging, compaction, wrong account). | W18 | engine runs on the PC; state/ files; CLAUDE.md names the account first | preflight.py | OPEN | 2026-10-18 |

## Session notes (kept out of sessions.csv, whose status is an enum: new|active|resting|spent)
- Shoulder 7h: 1 more post allowed as of 2026-10-18 (START_HERE Phase 0 step 6). Unused moments: lying in the chair with headphones, the mirror check ~40 s, the Canon shot.
- Atlas: spent (6+ posts); its C3 EDL is music-led, do not use.
- Back piece and Virgin Mary healed: spent until ~10/21 and 10/23.
