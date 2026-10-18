---
name: cut-talkinghead
description: 'Cut a talking-head reel (30-42 s): A-roll only, full-script jump-cut, raw voice, one background, no music, captions or b-roll. Use for slot B when Talking head footage exists (healed reveal with the numbers, price/value).'
---
Full spec: START_HERE.md section 6C. Format rules: knowledge/RULES.md wins (healed-reveal script
order: RULES.md D11).

1. Footage: only a Talking head folder (e.g. "Talking head 10 8") or TH clips he named. One
   version per script per day (W4, W17); a re-cut of a posted script is not a new reel
   (py -m tools.fingerprint frame1 "<reel>" must not match a posted one).
2. Transcribe every take: py -m tools.transcribe "<clip>" --both. Build the script from the words
   he actually said, complete sentences, every number spoken (hours, sessions, price, where the
   client came from). A healed reveal follows the RULES.md D11 order (the joke before the photo,
   every number spoken, then the reveal). The strongest take of each line wins; never splice
   half-sentences.
3. EDL (SPEC.md schema): type "th", kind I segs only, one background, the script order, order_key by
   capture time, tags (talking, healed, hours, humor, finished, ...), hooks [2], caption with the
   exact config.CAPTION_TAIL, audio_from null. No b-roll inserts, no burned captions, no music, no
   grading.
4. py -m tools.edl validate "<edl>"; py -m tools.edl refine "<edl>" --out "<edl>" --jumpcut
   (every cut lands in a pause; no cut-off word; no dead air over 0.6 s inside a line).
5. Opening: his face moving and talking within 0.3 s, bright; the stake or the number by 3 s. No
   title card, no still frame.
6. Hooks: 2, <= 55 chars, the number or the question from the script, matching frame 1:
   py -m tools.hook_gate --edl "<edl>".
7. Preview: py -m tools.render_story "<edl>" --out "<path>" --preview, then the gates skill (G1,
   G2, G3 no stretch > 2 s without speech or a cut, G6 complete lines, G8 30-42 s, G9, G10, G11).
   Fix the EDL; at most 4 passes. Master once: py -m tools.render_story "<edl>" --out
   "post/<date>/B_<HHMM>_<name>.mp4" (HLG source stays HLG, SDR stays SDR; chat copy <= 25 MB).
8. Deliver as the SAVE/TRUST reel (slot B) or HOLD. In runs/<date>.md say what the script tests
   (X4 street vs green screen when it applies).

Never: post, log in, pay, delete/move footage, burst, re-upload.
