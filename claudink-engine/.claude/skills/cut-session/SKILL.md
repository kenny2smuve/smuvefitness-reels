---
name: cut-session
description: 'Cut a session story (Meta-glasses POV; 36-48 s single session, <= 75 s multi-day) from one session folder into an EDL and a 2K HDR letterbox master with raw audio. Use for every slot-A story and for burn-in candidates.'
---
Full spec: START_HERE.md section 6A. Format rules: knowledge/RULES.md wins.

Selection over processing: the work is which moments, in which order, with which in/out points.
Never grade, never add music, never crop, never upscale, never move audio off its own picture.

1. Ledgers first: sessions.csv (rest_until, posts_so_far, status) and pieces.csv (color=no; hours
   known?). A resting or spent session is not cut. Max 2 reels per piece per day.
2. Scout: hand ONE session folder to the scout agent. It returns the capture order (footage.csv
   capture_time, not export names; cross-checked against tattoo progress; Canon clips only as the
   final reveal) and the beat map. Look at the contact sheets yourself before committing a shot
   (Read shows images): drop eyeline-elsewhere, blur, ceiling or floor, any TV or screen, dark frames.
3. Transcripts: py -m tools.transcribe "<clip>" --both. Run both passes before deciding a line
   does not exist. Complete sentences only; a line that is cut off is not used.
4. Structure, strict chronology (single session 36-48 s, multi-day <= 75 s):
   0-1 s a moving face + greeting, or the design on the body (bright, native, no title card, no
   faceless print); by 3 s the stakes, spoken (hours, sessions, travel, first tattoo, return);
   by 5 s the strongest line or the first needle/reaction; consult = one line only; first needle
   by ~14 s; a mini-hook every 5-8 s; keep one humor line; >= 4 needle shots; close: approval in
   their words -> reaction -> camera in hand -> finished piece full-frame >= 3 s, last.
5. Write the EDL (SPEC.md schema, tools/edl.py owns it):
   {name, type "story", session (sessions.csv key), multi_day, canvas {1440, 2560}, segs [...],
    hooks [2], caption (body + the exact config.CAPTION_TAIL), pieces [pieces.csv keys],
    labels {piece: "1 session / 7 hours"} only from pieces.csv, audio_from null}
   One seg per shot: {clip, in, out, kind I|M|C (line, montage, Canon reveal), order_key (capture
   time ISO with offset, or dNN-sNN), tags from the SPEC list (greeting design stencil stakes hours
   pain humor needle approval reaction mirror camera finished station piece talking healed done
   consult travel first-tattoo return), note}. order_key monotonic; C segs only after all others.
   Tag honestly: the gates read the tags and the critic confirms them.
6. py -m tools.edl validate "<edl>"; py -m tools.edl refine "<edl>" --out "<edl>" --jumpcut
   (out-points land in the pause: last word end + ~0.12 s, extended while 300-3400 Hz voice energy
   is on, max +0.8 s; silences > 0.6 s inside a line are jump-cut). py -m tools.edl timeline "<edl>".
7. Hooks: 2 per reel, never burned in, <= 55 chars, a number, a stake or a question to the viewer,
   matching the frame-1 tag, hours only from pieces.csv: py -m tools.hook_gate --edl "<edl>".
   Rewrite until both pass (no burned hook, no losing type: $-hooks, "which one", brags, booking
   announcements, generic reaction bait).
8. Preview: py -m tools.render_story "<edl>" --out "<path>" --preview (360x640, seconds). Gates
   skill on it; fix the EDL, not the pixels. At most 4 passes.
9. Master once: py -m tools.render_story "<edl>" --out "post/<date>/A_<HHMM>_<name>.mp4" (or
   hold/). HLG stays HLG (HEVC Main10, BT.2020, arib-std-b67), full source width on 1440x2560,
   letterboxed; a Canon SDR reveal is mapped into the HLG container; an all-SDR story stays SDR.
   Raw audio with its own footage, only a 10 ms fade at each cut. The chat copy (<= 25 MB) comes
   with it. Speed is logged in <stem>.render.json; more than 3x the 90 s target: name the slow
   path and what bench reported in runs/<date>.md.
10. Gates on the master (gates skill), the critic when calibrated, then deliver or HOLD
    (daily-run step 8). On delivery update sessions.csv (posts_so_far, last_post) and footage.csv
    (used_in, used_as_opener_in); hooks go to hooks.csv as used.

Never: post, log in, pay, delete/move footage, burst, re-upload.
