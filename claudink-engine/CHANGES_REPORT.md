# ClaudInk Reel Engine: what changed and why (2026-10-19)

## In one paragraph
Your v1 prompt asked a fresh Claude on your PC to write its whole toolchain (gates, hook checks,
slot planner, fingerprinting, renderer, delivery) from scratch inside its first 90 minutes, then
deliver 2 reels. This kit ships that toolchain pre-built and tested, rewrites the prompt around it
(START_HERE.md v2), fixes the Claude Code setup so the scheduled runs do not stall, and makes the
renderer use the GPU end to end.

## Numbers
| What | Count |
|---|---|
| Tool modules written (tools/*.py) | 19 (11,428 lines) |
| Automated tests | 311 test functions in 18 files (6,501 lines); integrator run: 342 passed, 0 failed |
| Build agents (build -> independent review -> fix -> integrate) | 22 agents, 0 errors |
| Code-review findings on the modules, first pass | 90 (9 blocker, 32 major, 49 minor) |
| Code fixes applied from those reviews | 86 |
| Prompt review lenses run | 10 (stall, contradictions, hit rate, data, operations, cut mechanics, safety, footage supply, burn-in exit, critic plumbing) |
| Prompt findings raised / survived a skeptic pass | 221 raised, 190 checked, 153 survived (37 major, 116 minor), 37 refuted, 0 blockers left |
| Claude Code mechanics checked against the official docs | 12 findings (2 blocker, 5 major, 5 minor), all fixed |
| Prompt length | v1 341 lines -> v2 530 lines (the extra lines are fallbacks, deadlines and exact commands) |
| Claude Code scaffolding | 8 skills, 4 subagents, settings.json with 30 allow and 50 deny rules |

## Render speed (what makes it faster)
| Lever | Before (v1 / old Shop Engine) | Now |
|---|---|---|
| Decode | CPU | NVDEC on the GPU for the HEVC 10-bit glasses clips (CPU only for rotated Canon clips, so they autorotate) |
| Scale | CPU lanczos | scale_cuda on the GPU |
| HDR to SDR tonemap (b-roll) | CPU zscale chain | libplacebo on the GPU; zscale only as fallback |
| Encode | NVENC qp14 per segment | NVENC p4 VBR; parallel encodes up to the session count bench measures |
| Segments | rendered one after another | rendered in parallel |
| Re-render after an EDL fix | whole reel | only the segments that changed (content-addressed cache; test: 1 of 3 re-encoded) |
| Chat copy | 2 separate ffmpeg passes | 1 pass (NVENC internal two-pass) |
| Proxy + contact sheet + strip of a master | 3 decodes | 1 decode, 3 outputs |
| Drive reads | streamed on every seek | each clip staged once into cache/clips |
Target on your GPU: a 45 s story master plus chat copy in under 90 s. It is a target: this container
has no GPU, so the GPU paths are built and unit-tested as commands, and `py -m tools.encode bench`
measures them on your PC on the first run.

## Problems found and fixed
1. **The PC run would have rebuilt the tools from zero.** v1 never said a kit exists. Now 19 tested
   modules ship with it and the prompt forbids rewriting them.
2. **Two scheduled tasks would skip each other.** The desktop app skips a run while another
   scheduled task is still running, so the Sunday 05:30 weekly could kill the 06:40 daily. Now one
   task runs the day; the weekly block runs inside Sunday's daily run.
3. **The headless command was wrong.** `bypassPermissions` ignores allow rules and refuses to start
   until accepted interactively. Now `--permission-mode auto --permission-prompts none` (verified in
   the CLI docs), with the allow rules also in your user settings because a `-p` run never shows the
   trust dialog.
4. **Half the permission rules could never match.** Windows paths are matched as `//d/...`, and
   `Write(...)` rules are never consulted. Rewritten in the forms Claude Code actually uses.
5. **A story with a Canon reveal had no legal export.** "HLG stays HLG" and "never convert SDR/HDR"
   contradicted each other. Rule now: a mostly-glasses story stays HLG and the short Canon reveal is
   mapped into the HLG container; only a mostly-SDR story exports SDR.
6. **The critic's ship bar was impossible.** "Top third" of a panel holding 7 of your best reels
   blocks good cuts by design. Now: not called flop, above every flop and above half the medians.
7. **Day-one never-zero gap.** HOLD is empty on day one and v1 had no ladder. Now: one passing reel
   ships as A; tomorrow's pair is delivered the night before into post/<tomorrow>/; HOLD only keeps
   cuts from other sessions, so a fallback is never a re-cut.
8. **Relay timing.** A run finishing after 10:05 or 13:05 never reached your phone. Now hard
   deadlines (A by 09:45, B by 12:45) and the Drive folder as the delivery when a relay has passed.
9. **The hit-rate baseline was mislabelled.** The 7/15/20% were lifetime views, not 72h. The kit
   computes the real 72h rate and treats reels younger than 72h as unknown, not misses.
10. **Safety holes closed.** No browser or screen-control tools, Metricool hands-off, yt-dlp without
    any impersonation or login flags, the critic no longer loads CLAUDE.md (it could de-anonymise the
    panel), and every Python write goes through a guard that refuses anything outside the two folders.
11. **Slot B defaulted to the format with a 0% hit history** (plain b-roll). Now healed reveal or
    talking head first, then a story from another session, then only b-roll that carries an idea.
12. **Hooks claiming hours with no data passed.** Now an hours claim not in pieces.csv fails.
13. **Build-time bugs fixed along the way:** rotated test clips (ffmpeg 6 dropped `-metadata rotate`),
    a planner that could double tomorrow's A, a sessions seed that broke the ledger schema, a stray
    file named after a Windows path, 2 renderer tests asserting an upscale, and a container restart
    that killed both workflows mid-run (resumed from their logs, nothing lost).

## What only you can do
- Drop the exact b-roll timings from RULES.md section 2 into templates/broll_station_first.json
  (it ships as a placeholder; the renderer refuses deliverables until then).
- Delete the 13 ERROR posts in the Metricool planner yourself, or wait for Nov 1.
- Post the 5 held reels to TikTok from your phone.
- Decide one rule: Canon reveals stay 1080 wide inside the 1440 canvas because v1 says "never
  upscale". Say so if you want them scaled up to full width instead.

## Not verified here
GPU encoding, NVDEC, libplacebo, the Whisper model and face detection could not run in this
container (no GPU, model downloads blocked). The first PC run checks each one in preflight and bench.
