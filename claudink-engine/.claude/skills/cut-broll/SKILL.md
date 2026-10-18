---
name: cut-broll
description: 'Cut a silent SDR 1440x2560 station-first b-roll short (~13.6 s) from Shop shorts with hour labels taken only from pieces.csv. Use for slot B when no healed reveal or talking head exists, and as the always-makeable never-zero fallback.'
---
Full spec: START_HERE.md section 6B. Format rules: knowledge/RULES.md wins.

1. Template first. templates/broll_station_first.json ships as a PLACEHOLDER (it carries a
   "PLACEHOLDER" key and preflight warns while it does). Before the first real b-roll replace its
   slot timings with the exact approved method from RULES.md section 2, or derive them from an
   approved render in D:\ClaudeInk\out\shorts\Broll_SDR*_SDR2K.mp4 (py -m tools.render_broll
   template-from "<approved.mp4>" when the module offers it, see --help; otherwise read the shot
   boundaries with ffprobe/contact sheets and type them in), then delete the PLACEHOLDER key.
2. Pieces: only pieces present in pieces.csv with color=no and rest_until passed. Hour or session
   labels only when pieces.csv has hours (seeded: Mudvayne 10h, Shoulder 7h; the Virgin Mary rests
   until 10/23). Unknown hours = no label, and POST_THIS asks him once ("Reply with hours/sessions
   for: <pieces>"). Never claim hours that are not in the ledger. Max 2 reels per piece per day.
3. Shots: scrub every candidate clip in F:\My Drive\Shop shorts end to end
   (py -m tools.contact_sheet sheet "<clip>" --out "cache/sheets/<name>.jpg", then look). Only million-dollar moments: the station
   shot in motion first, then the pieces. Vary his shirt, the angle and the piece mix across days.
   No TV or screen, no color piece, no still client with only linework, no blur.
4. Write shots.json: {name, shots: [{clip, in, role station|piece, piece, order_key}], hooks [2],
   caption (body + exact config.CAPTION_TAIL), pieces, labels}. Roles must match the template
   slots exactly (counts and order); each slot's duration comes from the template, so timings land
   within 0.2 s and no shot runs over 4.6 s. Total >= 10 s (G8).
5. Hooks: a real idea (an hours label, an endurance number, a debate, education) or a question to
   the viewer; never "which one", "rate my work", a $-hook, a brag or a booking announcement
   (losing types, W5). py -m tools.hook_gate --edl shots.json. Hooks and labels go in POST_THIS
   (he adds them as text); nothing is burned into the picture.
6. Render: py -m tools.render_broll shots.json --out "post/<date>/B_<HHMM>_<name>.mp4". SDR
   1440x2560 bt709, silent track, Canon clips untouched except scale/pad (autorotate, no
   transpose), iPhone/Meta HLG through the approved tonemap chain, HEVC master + chat copy
   <= 25 MB. Never upscale, never crop.
7. Gates: py -m tools.gates "<reel>" --type broll (G1 station shot bright and moving, G2, G3
   template timings, G5 no black frames, G8, G9 spec, G10 hooks + caption tail, G11 uniqueness,
   G12 every piece known and color=no). Fix shots.json, re-render; at most 4 passes. Critic flags
   (tv_or_screen, color_piece, static_opener) block delivery.
8. Deliver or HOLD (daily-run step 8). Shorts run at most 1 a day and never as slot A (W1).

Never: post, log in, pay, delete/move footage, burst, re-upload.
