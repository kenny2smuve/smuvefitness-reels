---
name: gates
description: 'Run and read the G1-G12 quality gates on a rendered reel, know what each gate needs from the editor (tags, transcript, chat copy, pieces.csv, hooks), run the fix loop, and calibrate the frame-1 thresholds on his winners. Use after every render and before every delivery.'
---
Full spec: START_HERE.md section 7. Format rules: knowledge/RULES.md wins.

Command: py -m tools.gates "<reel.mp4>" --type story|broll|th [--edl "<edl>"] [--chat "<chat.mp4>"]
[--transcript words.json] [--multi-day] [--today YYYY-MM-DD] [--json] -> <stem>.gates.json next
to the reel. Pass = every hard gate ok. Entries marked "needs critic" (G4 content, G12 visual) are
confirmed by the critic agent on the anonymised packet. deliver refuses a reel without a passing
gates.json (--force only with a logged reason, never for G1 or G7).

What each gate needs from you:
G1 frame 1: a bright (luma std >= 60), moving (motion over 0-1 s above data/calibration.json, else
   the config default) face or a first-seg tag among design, stencil, greeting, healed, piece,
   station. Pick a bright clip; never grade. Measured on the active picture area of a tonemapped
   360p proxy, the same chain as his winners.
G2 opening: story: speech or motion within 0.3 s and a seg tagged "stakes" by 3 s; b-roll: the
   station shot is in motion. Tag the stakes seg.
G3 the 3-10 s window: story/TH: no stretch over 2 s without speech or a cut (give it the
   transcript; jump-cut dead air); b-roll: no shot over 4.6 s, timings within 0.2 s of the template.
G4 story content: tags needle x4, approval, reaction, camera, and a finished seg >= 3 s at the
   end. Tag honestly; the critic confirms from the sheet.
G5 clean picture: no black frames (blackdetect d=0.01 pix_th=0.10 on the active area), no frozen
   stretch over 4 s except the final hold.
G6 voices: no out-point while 300-3400 Hz voice energy is on; every line complete. Run
   py -m tools.edl refine before rendering; pass --transcript words.json from tools.transcribe.
G7 order and sound: order_key monotonic, Canon (C) segs last, audio_from never set.
G8 length: story 36-48 s single, <= 75 s multi-day (--multi-day); b-roll >= 10 s; TH 30-42 s.
G9 spec: ffprobe confirms codec, 1440x2560, colour tags matching the export policy, an audio
   stream; the chat copy exists next to the master, <= 25 MB, decodes with zero errors.
G10 hooks and caption: both EDL hooks pass hook_gate (<= 55 chars; not fuzzy-matched >= 0.85
   against hooks.csv + captions_used.csv; no losing type; a number, a stake or a question to the
   viewer; frame-1 tag match; hours only from pieces.csv); caption ends with the exact
   config.CAPTION_TAIL.
G11 uniqueness: frame-1 phash matches no posted reel (data/signatures.json;
   py -m tools.fingerprint frame1 "<reel>"); <= 40% of story seconds seen in the last 14 days of
   posts; session rest respected (sessions.csv); no piece in more than 2 reels that day.
G12 pieces and visual check: every piece in the EDL exists in pieces.csv with color=no (an unknown
   piece blocks until identified by frames); then the critic: no TV or screen, no color piece, no
   faceless or static opener.

Fix loop: one-line diagnosis per failure -> fix the EDL (selection, order, in/out), the tags, the
hooks or the ledgers (never the pixels) -> re-render with --preview first (only changed segments
re-encode) -> re-gate. At most 4 passes, then HOLD it or pick other footage. Never --force G1/G7.

Calibrate (Phase 0, then weekly): py -m tools.gates calibrate "<winner1.mp4>" "<winner2.mp4>" ...
[--out data/calibration.json] measures motion and luma on his winners through the same proxy chain
and writes the frame-1 motion threshold. No winners on disk: the config default applies; say so in
runs/<date>.md.

Never: post, log in, pay, delete/move footage, burst, re-upload.
