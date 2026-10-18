# ClaudInk Reel Engine kit

The pre-built tooling plus the operating prompt for Claudin Pierre's tattoo reels
(IG @claudink, TikTok @claudn_p). It runs on his Windows PC inside Claude Code and makes
2 post-ready reels a day from his own footage. He posts them himself; the engine never posts.
This is the tattoo account and nothing else.

- START_HERE.md: the operating prompt (v2). Paste it into Claude Code to start.
- SPEC.md: the module spec and the shared data contracts (EDL, gates.json, ledger CSVs).
- CLAUDE.md: what Claude Code reads every session (account, non-negotiables, paths, how to resume).
- knowledge/RULES.md (his, on the PC): the one rulebook. It wins on every format detail.

## Install on the PC (once, about 15 minutes)
1. Copy this whole `claudink-engine` folder over `D:\ClaudeInk\reel-engine`, merging. Never replace
   what is already there in `knowledge\`, `ledgers\` or `data\`: the kit only adds files beside them
   (`ledgers\seed\` is merged by a tool, not copied over your ledgers).
2. Python 3.12 (`py`): `winget install -e --id Python.Python.3.12` only if missing. Then
   `py -m pip install --user faster-whisper opencv-python numpy pillow imagehash scipy yt-dlp curl_cffi tzdata pytest`
3. ffmpeg 7.1 full build at `D:\ClaudeInk\ffmpeg71\bin\ffmpeg.exe` and `ffprobe.exe` (set
   `CLAUDINK_FFMPEG` / `CLAUDINK_FFPROBE` if they live elsewhere).
4. From `D:\ClaudeInk\reel-engine`, with `set PYTHONUTF8=1`:
   - `py -m tools.preflight` (writes `state\preflight.md`; fix the blockers it names)
   - `py -m tools.encode bench` (writes `state\render_profile.json`: NVDEC, NVENC sessions, libplacebo)
   - `py -m tools.ledgers seed` (merges `ledgers\seed\*.csv` into your ledgers without dropping a row)
   - `py -m pytest tools/tests -q` (must be green)
5. Windows settings, once: Settings -> System -> Keep computer awake ON; the Claude desktop app in
   Startup apps; PC plugged in, lid open; the PC clock on Eastern time.

## How to start
Open the Claude desktop app -> Code -> folder `D:\ClaudeInk\reel-engine` -> permission mode Auto ->
paste everything below the line in START_HERE.md -> send. Stay about 10 minutes for the first run:
approve the folder-trust dialog and every "always allow" prompt. `.claude\settings.json` is the
seatbelt (allows for py/ffmpeg/ffprobe/yt-dlp, reads under `F:\My Drive` and `D:\ClaudeInk`, edits
only under `reel-engine` and `F:\My Drive\ClaudeInk Reels`, denies for rm/del/move/git push); project
allows only apply once the folder is trusted, so copy the same entries into `~\.claude\settings.json`.
The first session runs Phase 0 (preflight, today's 2 reels, data, ledgers, scheduling) and then the
burn-in (Phase 1) until the exit criteria hold, writes `BURN_IN_REPORT.md` and ends. From then on the
scheduled task is the program.

## Folder map
```
START_HERE.md SPEC.md CLAUDE.md README.md experiments.md
.claude/   settings.json, agents/ (critic, analyst, scout, rules-keeper), skills/ (daily-run, weekly,
           burn-in, cut-session, cut-broll, cut-talkinghead, gates, learn)
tools/     the kit (below), tools/tests/ (pytest), run_daily.cmd, run_weekly.cmd
knowledge/ RULES.md (his), WEAK_POINTS.md (the audit), START_HERE_v1.md, reference notes
ledgers/   footage, pieces, sessions, hooks, captions_used, deliveries (.csv); seed/ (kit seeds)
register/  weak-points.md (W1-W18, status per item), data-check.md
templates/ broll_station_first.json (PLACEHOLDER, see below)
data/      imported metrics (reels.csv), calibration.json, signatures.json, analysis/
state/     burn-in.md, engine.lock, preflight.md, render_profile.json, blocked.txt, breakout.json
post/      <date>/ A_.., B_.., POST_THIS.txt; SHOOT_CARD.txt      hold/ HOLD queue, hold/next/
runs/      <date>.md run logs, runs/cmd/ task logs, runs/dryrun/   weekly/ <date>.md
cache/     clips, segs, transcripts, panel, previews (the only place anything is deleted)
_to_delete/ anything that would otherwise be deleted (never done by the engine)
```

## Modules (`py -m tools.<name> --help`)
config (paths, constants, caption tail, tz) | safeio (write guard) | preflight | clean_cache |
ledgers (seed, validate, show) | footage_index | transcribe (--both) | contact_sheet (sheet, strip) |
edl (validate, refine, timeline) | encode (bench, profile) | render_story (--preview) | render_broll |
gates (G1-G12, calibrate) | hook_gate | fingerprint (sign, match, frame1) | panel (packet, anonymize,
calibrate, plan, download) | slot_planner | deliver (plan, deliver, hold) | reels_analysis (import,
report, data-check). Every write goes through safeio; ffmpeg always runs with -nostdin -n.

## Scheduled tasks
Desktop app (preferred; Code tab -> Routines -> New routine -> Local, Desktop 1.1.5368+, or describe it
in any session): one scheduled task on folder `D:\ClaudeInk\reel-engine`, permission mode Auto, worktree
OFF; run it once with "Run now" and pick "always allow" on every prompt. It fires only while the app is
open and the PC is awake (Settings -> This computer -> System -> Keep computer awake); a missed run gets
one catch-up run on wake, so the daily-run skill starts with the late-run rule:
- `reel-daily`, every day 06:40 ET: "Read CLAUDE.md, then run the daily program exactly as
  .claude/skills/daily-run/SKILL.md says."
- `reel-weekly`, schedule Manual (Run now only): "Read CLAUDE.md, then run the weekly program exactly as
  .claude/skills/weekly/SKILL.md says." The weekly block normally runs inside Sunday's reel-daily, because
  the Desktop app skips a scheduled run while another scheduled task is still running.

schtasks fallback (run in an elevated prompt; the .cmd files run `claude -p ... --permission-mode
auto --permission-prompts none` so the classifier approves routine actions and anything that would still prompt is denied rather than waited on; the deny rules in `.claude\settings.json`
still apply. `--permission-prompts none` needs Claude Code 2.1.259+; on an older build use `--permission-mode dontAsk`.
A `-p` run never shows the folder-trust dialog, so the allow rules must also be in `%USERPROFILE%\.claude\settings.json`:
copy the whole `permissions` block from `.claude\settings.json` there, once):
```
schtasks /create /tn "reel-daily"  /tr "\"D:\ClaudeInk\reel-engine\tools\run_daily.cmd\""  /sc daily /st 06:40 /f
rem the weekly block runs inside Sunday's reel-daily; run_weekly.cmd is for on-demand use (schtasks /run)
```
Logs land in `runs\cmd\<date>.log`. A test run and the dry-run write only to `runs\dryrun\<date>\`.
Every run takes `state\engine.lock`; a lock older than 4 h or with a dead pid is stale and is taken over.

## Placeholders
- `templates\broll_station_first.json` ships with stand-in slot timings and a `PLACEHOLDER` key.
  Replace the timings with the approved method from RULES.md section 2 (or derive them from an
  approved `D:\ClaudeInk\out\shorts\Broll_SDR*_SDR2K.mp4`) and delete the key before the first
  real b-roll. preflight warns while it is there.
- `ledgers\seed\*.csv` are seeds (burned hooks, losing types, known hours, rest dates). `py -m
  tools.ledgers seed` merges them; `hours` is never invented, unknown stays empty.
- `data\calibration.json` does not exist until `py -m tools.gates calibrate <winners>` has run on
  his winner reels; until then the config default thresholds apply.

## Non-negotiables (full list: START_HERE.md section 1, knowledge/RULES.md sections 1-2)
1. Instagram, TikTok and Metricool are read-only: the engine never posts, likes, comments, DMs or logs in.
2. No passwords, no accounts, no money. A login wall or paywall is skipped and logged.
3. His footage is never deleted, moved or overwritten; writes only inside reel-engine and ClaudeInk Reels.
4. No other creators' footage or audio, no bursts, no re-uploads, no cookies to any tool.
5. His format rules as written in RULES.md: HDR raw stories, SDR silent b-roll, 2 a day 3 h apart, exact caption tail.
