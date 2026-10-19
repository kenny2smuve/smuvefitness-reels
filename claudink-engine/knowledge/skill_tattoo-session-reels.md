---
name: "tattoo-session-reels"
description: "Turn Claudin's latest Drive session folder of Meta-glasses tattoo clips into music-led POV story reels (approved tracks, drop on the reveal, no text) as SDR 1080x1920 full-bleed FULL + DX masters, built and audited with the Reel Bible system."
---

# Tattoo session -> reel cuts

Trigger: Claudin says "edit my content", "cut this session" or something similar.

## READ FIRST: THE REEL BIBLE (Claudin, Oct 9: "call back on this for each and every answer, reply, comment, edit, video")
- Project doc `claude/reel-bible.md` is the master system: measured model of his winners, formats, hook system, structure templates, pacing, dialogue, music, text, export, posting, benchmarks + audit targets, anti-patterns, and the measured niche corpus (section 14). Read it (Projects project_read) before every edit. Raw research: `claude/reel-bible/research-01..08`. Tools: `claude/reel-bible/tools/` (copy to /home/claude/bible/tools/ if missing).
- He called the old no-music, letterboxed, HLG, dialogue-chain cuts (Atlas B2/B3, Oct 8) "terrible, absolutely atrocious". Measured cause: speech 0.56, 152 wpm, no music bed, 13 cuts/min, first cut 3.0s, letterboxed HLG at 3.5 Mbps (audit 61/100).
- THE F1 MODEL (his 872K posterboy profile): speech coverage 0.22-0.50, 35-110 wpm, music/non-speech share >=0.45, 4-7 dialogue islands with the music ducked 10-12 dB under each line, 18-34 cuts/min, first edit <=2s, montage cuts on the beat, the song's DROP lands on the reveal frame, reveal held >=2.5-3s, end on a downbeat.
- SOUND RULE (Claudin, Oct 5, all videos): approved tracks only (files in /home/claude/snd/hi/): Blue Dream, Bulgarian Eternal Funk, Lil Peep "right here", tweakin together, Eem Triplin "Awkward", black finger, funeral, million dollar, eat it up, NO RESPECT; spare: baddie. NEVER generic hoodtrap/drill remixes ("bland"), never Ghetto (E.K.E.), skip uziastro. The bible section 8 has the measured drop map (eat it up drop 47.24, Peep 41.87, funeral 45.1, NO RESPECT bass 33.9, Eternal Funk 7.40).
- Builder: `c_build.py EDL.json --preview` then `--pc` (section 6). Audit: `audit.py FULL.mp4 --format story --clean --dx DX.mp4 --tl NAME_tl.json --match NAME_prev_FULL.mp4`. Ship only at >=85 with zero gate fails.
- Also offer a TikTok-first F3b speed-run of every multi-day session (18-25s, music only, 2-beat cuts, print on the body at 0s, drop on the reveal; `audit.py --format speedrun`, EDL `texture_db: null`): it copies his best TikTok ("Tattoo Idea: Gothic Castle Back Piece", 53.1K, 72x his ~740 median).

## THE BASELINE (set Oct 2, 2026). Every edit must be at this level
The cathedral-window elbow reels E1_WorthIt, E2_Rapper and E3_TopThree are the standard. Claudin called them the best pieces yet: "every single piece of content needs to be on this level". What they did:
- Each reel takes the viewer through the WHOLE session in TRUE chronological order: design/consult, stencil and client reaction, tattooing, done, mirror/camera, reveal.
- NO flash-forward opener. Nothing is shown before it happened: no stencil before it goes on, no finished tattoo before "let's do this". He rejected cuts that broke this ("it starts with the stencil already on and then it doesn't have a stencil", "the tattoo will be already done before I even say I started").
- The session order is VERIFIED, never guessed. Clip filenames are not capture order (section 1).
- Each reel is one complete conversation thread. Every line is a full sentence, every answer has its question, and nothing appears out of context.
- The opener is a visual-stakes moment from the START of the session (design print held on the body, iPad design, client showing a healed piece), with a line in the first second.
- A mini-hook roughly every 5s: a pain answer ("wouldn't put it in my top three"), a joke ("the nail on my shoulder is killing me, not the tattoo"), a stakes line ("10,000 people sold out", "it's two in the morning"), the approval ("it does look crazy, bro"), the payoff ("Is it worth it? 100%").
- The ending is "we're done" or "go check it out" (mirror), then the camera in hand ("Wow, that's the one", posing directions), then a 2-3s close-up reveal.
- Audio always plays with its own footage. About 38-46s each, up to 80% shared footage between versions is fine (quality first). Since Oct 9: an approved music bed with the drop on the reveal (FULL master) plus a no-music DX master, SDR 1080x1920 full-bleed, every frame, transcript and audit score checked before delivery.

Standing brief (apply to every edit):
- Only content that is highly engaging, entertaining, builds trust with his audience, and attracts his ideal clients (men aged 20-28, large-scale black and grey realism, $1,200+ budget) to follow and book.
- Before cutting, pull his latest reel insights from Porter Metrics (skip rate, avg watch, reach, like/share/save rates) and copy what the current top reels did.
- Before cutting, also read the project doc claude/reel-video-learnings.md (Projects project_read). A daily scheduled task watches his newest trial reels and keeps its "Current rules" up to date (section 4d). Apply them. If one ever conflicts with a rule in this skill, this skill wins.
- THE FIRST 3 SECONDS ARE THE MOST IMPORTANT PART OF EVERY VIDEO (section 4), always taken from the start of the session.
- RETENTION THROUGH 10s IS THE NEXT PRIORITY (section 4b).
- SHARES ARE THE GATE PAST THE ~1.3K TEST POOL. Keep humor/personality lines and visible craft (section 4c).
- QUALITY BEATS VARIETY (Claudin, Oct 2): batches may share up to 80% of their footage. He noticed later versions in a batch dropped off in quality when each one was forced to use different beats. Every version gets the strongest beats; vary mainly the opener, a few mid beats and the payoff (section 5).
- NEVER CUT A VOICE OFF (Claudin, Oct 2: "the voices get cut off at the end, almost all of them"). Every out-point lands in the pause after the speaker finishes (section 5).
- FULL-BLEED FRAMING (changed Oct 9 by the Reel Bible research): crop the 3:4 glasses frame to fill 9:16 (keeps 75% of the width, subject 1.33x bigger), with the crop x set per shot to keep the face, design and needle in frame. Destin Inks / Oliver Ayre are all full-bleed; letterbox is a measured loss on platform data (TikTok: native 9:16 +91%; IG down-ranks borders; the bottom UI covers 30% of a letterboxed picture), though not on his own grid (see bible 12.1b). His older rule was full-width letterbox so he could zoom himself: say so in the delivery message and send letterboxed DX copies if he asks (section 6).
- EVERY VERSION INCLUDES THE CLIENT'S APPROVAL, AND ONE OF ITS FINAL SCENES IS THE CLIENT REACTING TO THE TATTOO. It ends on the camera-in-hand transition.

## 0. Source: latest Drive folder, confirm first
- Find the most recently uploaded folder in his Google Drive, including folders shared with him from his other account.
- If he pastes a folder link, use that folder directly. No need to confirm.
- Otherwise, confirm the folder with him in one line before pulling anything: "Pulling '<folder name>' (<N> clips, uploaded <date>)?"
- If the Drive connector can't list the folder's contents, fetch `https://drive.google.com/embeddedfolderview?id=<FOLDER_ID>` and parse the `/file/d/<ID>` links and the `flip-entry-title` names.
- After that, work start to finish without asking more questions.

## 1. Ingest (download each file ONCE, capture order FIRST)
- Drive blocks public-link downloads for up to 24h ("Quota exceeded") after a file is pulled too many times. Copies of the files get blocked too.
- So download each original exactly once, from `https://drive.usercontent.google.com/download?id=ID&export=download&confirm=t`. Use 2 workers, each holding one original at a time, because disk is tight.
- From each original, BEFORE deleting it:
  - ffprobe the format tags and save `com.apple.quicktime.creationdate` to a capture-times file. This is the Meta glasses RECORDING time. Ignore `creation_time` (the app export time) and Drive's upload time.
  - Note resolution, fps, color transfer and duration. Meta/iPhone clips are HLG and can differ in size; Canon MVI clips are SDR bt709 and rotated.
  - Make a 960p analysis proxy (x264 ultrafast). Always pass `-nostdin`, or ffmpeg eats a shell read loop's input and truncates everything.
- CAPTURE ORDER:
  - Sort clips by recording time. Filename timestamps (YYYYMMDD_HHMMSS), when present, also work.
  - Meta export names (video-NNN_singular_display) are NOT capture order.
  - Cross-check with visible tattoo progress (clean skin, then stencil, then linework, then shading, then finished) and with outfit changes.
  - Canon clips use their own camera clock. Use them only as the finished-piece reveal at the end.
- Transcribe each proxy with faster-whisper small (CPU, int8, word timestamps).
  - If faster-whisper fails opening the file through PyAV, decode with ffmpeg to 16k mono s16le PCM and pass the numpy array instead.
  - The VAD filter can drop real speech, so also run a pass without VAD (`vad_filter=False, condition_on_previous_text=False`) on key clips before deciding a line doesn't exist.
- Make a timestamped contact sheet of each clip and look at it. The footage is POV from his glasses, so throw out any line where his eyeline is elsewhere.

## 2. Reference structures that performed on his page
- Baseline E1-E3 (above): the full session in true order, ~40s, a mini-hook every ~5s.
- Dd9rti4qIND (his own edit, NY 4-day sleeve, 75s): day 1 to day 4 in order, lots of needle-on-skin shots, banter kept, ends on "was it worth it / hell yeah" + camera. Beat Claude's 45s cut of the same session (section 4c). Its finished-piece flash-forward open is superseded by the baseline: no flash-forwards.
- DcaS1GggTH3 (posterboy, 872K): arrival, then iPad design reveal and reaction, then stencil, then needle work. Measured: speech 0.29, music share 0.6+, ~25 cuts/min, music bed throughout.
- DdCXyjKqI4I (Jacksonville): emotional backstory hook, with an arrival beat.
- DdIo7YkgJ9d (memorial): emotional-stakes hook.
- Ddf6nmHAAEW (10 hours): endurance-stat hook.
- DbmLMx2K38W ("The most important part of any tattoo"): nearly all consultation. The client picks references on the iPad while he guides, and she is visibly invested. Long takes. Ends on the finished design.
- DbOxwH7KE3x ("Day in the life of a tattoo artist from Miami"): design prints, warm parking-lot greeting and banter, stencil, tattooing, mirror check, phone/camera shot, then the reveal. Fast cutting, about 21 cuts.
- Common threads:
  - A one-idea hook, which he adds as text.
  - The consultation or design reveal showing him listening.
  - Warm arrival or banter showing personality.
  - Care during the process.
  - A camera shot right before the reveal.

## 3. Find the beats (for prospect clients)
- Sizing doubt: "you like this sizing?", "don't want dead space", "I could go smaller, it's up to you".
- Listening / body fit: "do you like where it ends", placement lower or higher, "perfectly onto the bone", "I'd rather you have something you enjoy at all times", "first session is always about how everything fits together and what you think".
- Consultation: picking references, the design reveal, the client's first reaction.
- Social proof: the client showing a healed piece he did ("this one healed super good, a lot of people ask me about it").
- Personality: arrival greeting, banter, humor or tension ("not a bad time to tell you I'm scared of needles", "I'm a picky motherfucker", "the nail on my shoulder is killing me"), the client's own life (music, events). These are share triggers: never cut all of them.
- Anticipation and time lines: "later on in the story you're gonna love it", "day number two, bro", "final stretch", "it's two in the morning". Use as mini re-hooks.
- Pain Q&A: "it hurt, but not in my top three", "it's always the position that hurts more than the tattoo".
- Originality and expertise: "I do similar but never the exact same", "I'm not gonna pack this in white", technique explanations.
- Care during tattooing: "tiny section so you get used to the feeling", "chilling right?", "let's take pictures so you can get some rest".
- Aftercare: second-skin or wrap explanations. These build trust.
- CLIENT APPROVAL AND REACTION (required in every version): the stencil or mirror reaction ("Oh yeah, let's f***ing go!", "it does look crazy, bro"), "sick as hell", "Is it worth it? 100%", and the client looking at the finished piece.
- Camera bridge closer:
  - The client facing his handheld camera, or him directing the pose ("stand right here, face that way").
  - Camera in hand while he says "Wow, that's the one" or asks "What do you think?".
  - The last shot has the camera in his hand, then a short close-up reveal (or the photo he adds himself).
- What separates an artist's outliers from their normal posts across 26 niche accounts is the IDEA (first tattoo + identity, healed proof, second-skin peel, pain area, comment-reply series), not the edit; the one craft metric that travels is a moving first second (20/26 accounts, p=0.016). Bible section 14.4 ranks the content to shoot next.
- In the F1 music-led cut, keep 4-7 of these as dialogue islands (2-4s each); everything else plays as beat-cut montage under the music.

## 4. THE FIRST 3 SECONDS (highest priority)
- The opener must come from the START of the session. NEVER open on a later moment (finished piece, mid-tattoo line) and then jump back.
- Rank every early-session candidate on hook strength. Give each version its strongest remaining early opener, and never repeat an opener across versions.
- The opening frame and first words must land within about 0.3s. No lead-in, breath, settling camera, door swing, or walking shot with nothing happening.
- A strong early opener has at least one of:
  - A design print or stencil on the body.
  - An iPad design on screen.
  - The client showing a healed piece.
  - A punchline, surprising line or curiosity gap in the first second.
  - A strong emotional reaction.
- Watched-reel evidence (Oct 2, 12 trials): every winner showed the design (iPad, or printout held on the body) within about 3s, with motion (DdzGOoAqWXJ 5.7K, Dd0TIK7Av2B 5.7K, Ddut0naqdFt). Every loser opened on a still client with only linework showing (Dd5M6OmqpqS, Dd61O4Kq_0V, Dd5AMguql86) or about 2s of blurry handheld (DduiGhAqVJK, 85 reach). Never open on those.
- First words: a greeting or a joke ("how was the flight bro") or a clear consult line. Never open mid-debate with no context ("if somebody come to me...", "you'd rather me keep it lighter"). Those openers flopped.
- The opening shot must be moving and well framed: face or tattoo visible, eyeline on the subject, no blur, no ceiling or floor.
- FRAME 1 IS BRIGHT AND HIGH-CONTRAST (luma std >=60): the strongest single predictor measured on his page (Oct 9 calibration on 24 story reels: passing it median 18.2K plays vs 6.0K, p=0.005). Music share >=0.45 is second (10.2K vs 5.3K).
- Seconds 0-3 must set up a question that the rest of the reel answers. The hook text he adds must match the opening footage.
- Leave the top of the frame readable for his hook text.
- Check each version with a 0-3s frame sheet (0.25s steps) and a transcript of those 3s. If the opener isn't gripping on its own, rebuild it.

## 4b. RETENTION 3-15s (learned from real trial data)
- Trial Dd48NSwqpe0 (57s, cherry-blossom chest, hook text "Can't believe we got this much done"):
  - Skip rate was 27%, lower than typical, so the hook text worked.
  - Retention fell to about 50% by 8s, then flattened at about 20-25%. Average watch time was 18s, with 0 shares, comments or follows.
  - Cause: 0-10s was the client standing and slowly turning, with no dialogue and no new information.
- Trial Oct 1, Claude's cut vs his own: Claude's lost most viewers at 3-8s by cutting to a different day with nothing new on screen.
- Rules:
  - Seconds 3-10 must contain speech and a visible change: a new shot, new info, or an escalation. Never more than about 2s with no words and no action.
  - Never open, or follow the hook, with a static pose, turn or walk shot. Those belong only in the closer.
  - Every 5-8s, add a new mini-hook: a question, a surprise line, a laugh, a time check-in, or a stakes line.
  - LENGTH FOLLOWS THE STORY: single-session pieces about 35-45s (the baseline ran 38-41s). Multi-day or large pieces can run 60-75s. Never cut a multi-day story down at the expense of humor lines and process shots.
  - Build at least one comment trigger into the cut: an opinion question, a relatable line, or a debate. Suggest a matching caption question when delivering.
- When he shares trial or reel insights, log them in the project memory (content-ideas, under trial feedback), diagnose where the retention curve drops against the cut's timeline, and apply the fix to the next batch.

## 4c. HEAD-TO-HEAD LESSON: his edit vs Claude's (same footage, same hook text, both trials, Oct 1)
- His Dd9rti4qIND: 75s, 1,953 reach, 77 likes, 5 shares, 28.4s avg watch, skip 30.7%.
- Claude's Dd9yuXVqkry: 45s, 1,070 reach, 32 likes, 0 shares, 26.3s avg watch, skip 29.5%.
- What his did that Claude's didn't:
  1. Kept the humor and anticipation lines; Claude cut them. Shares only happened on his.
  2. About 6 needle-on-skin shots spread across the reel; Claude's had about 1.
  3. Let the 4-day story breathe to 75s.
  4. Cut pace was about the same, so pacing was not the difference.
- (His edit also opened on the finished piece. Since Oct 2 that is ruled out: strict chronology wins.)
- Self-check before delivering every version: true chronological order? at least one humor line kept? 4+ needle-on-skin shots? approval + reaction + camera closer?

## 4d. DAILY VIDEO LEARNING LOOP
- The scheduled task "Daily trial-reel video review" (9:08 AM ET) watches up to 10 of his newest trial reels each day, read-only, on his logged-in Instagram page (Your activity > Photos and videos > Reels) in the Claude browser. It pulls the videos, breaks down cuts, opener, first words, payoff timing, humor and needle shots, compares winners against losers, and updates claude/reel-video-learnings.md.
- Every edit reads that doc first (standing brief). Its rules never override this skill's established rules (strict chronology, audio with its own footage, complete sentences, ~38-46s baseline, export rules, batch variation). A conflicting finding is an observation only. Confirmed findings also go into claude/reel-bible.md.
- Pacing is not the lever: winners and losers both cut at 16-29 cuts/min (Oct 2). Fix the opener and the beats, not the cut rate.

## 5. Cut rules + batch variation
- STRICT CHRONOLOGY: segment order must follow recording time across clips and timecode within clips, verified against the capture-order list and the tattoo progress before rendering. Never show an effect before its cause. No exceptions.
- OUT-POINTS LAND IN A PAUSE, NOT ON THE LAST WORD. Transcript word-end times run early, and cutting on them clipped the last word on ~60% of cuts in the Oct 2 batch. For every segment:
  - Re-transcribe the boundary window ([in-1.0s, out+1.8s], non-VAD, word timestamps).
  - End = end of the last word that starts before the out-point + ~0.12s. If that word ends a clause without punctuation and the next word follows within 0.35s, finish the sentence (max +1.2s).
  - Then check the speech-band (300-3400 Hz) energy: if the voice is still sounding at the out-point, extend until it drops (max +0.8s), stopping 0.03s before the next word starts.
  - In-points: only move earlier if speech is already sounding at the in-point; go back to its onset (max 0.4s), never adding dead lead-in to the opener.
  - Audit: count cuts where the voice is still active at the out-point. Target zero, except where the next word starts immediately.
- Jump-cut out any dialogue silence longer than about 0.6s inside an island. No dead air: between islands the music carries the picture.
- The dialogue must read as one coherent conversation. Never cut from one topic to an unrelated line. Never use an answer without its question, or a line whose context isn't in the reel.
- Never cut people off mid-sentence. Never drop in lines from an unrelated day.
- Aim for a cut about every 2-3s overall: 18-34 cuts/min, montage shots 1-2 beats, dialogue islands 2-4s. A single editorial punch-in (<=125%) inside one continuous take is allowed to give the opener a first edit at ~1.3s; re-aim it (`zx`) so the face and the design stay in frame.
- AUDIO ALWAYS PLAYS WITH ITS OWN CLIP'S VIDEO. Never lay one clip's audio over another clip's footage.
- No on-screen text (he adds the hook text). MUSIC: every story gets an approved track (SOUND RULE above) from the FULL master: music between islands within 1-4 LU of the dialogue (`speech_minus_montage_lu` 0.5-5), ducked 10-12 dB under lines (0.12s attack, 0.35s release), drop on the reveal. Also deliver the DX master (no music) + a cue sheet (song, start offset, drop time) so he can add the catalog song in Edits with auto-ducking ON.
- BATCH VARIATION (quality first; the openers and payoffs still differ):
  - Each version gets its own opener and its own angle, but reuse the strongest beats freely: up to 80% shared footage between any two versions is fine (Claudin's call, Oct 2). Never weaken a later version just to make it different.
  - Never reuse the exact same full cut. Vary the opener, a few mid beats, and the payoff (or, for a clean A/B, only the track).
  - Vary the ending payoff while keeping the approval, reaction and camera beat in each.
  - Do NOT use evasion tricks (mirroring, zoom-punching to dodge duplicate detection, filters, speed changes). They risk the account. (An editorial punch-in that is identical across versions is craft, not evasion.)
  - Before rendering, compute a pairwise overlap table across the batch, including earlier batches from the same session.
  - Never reuse an opening shot or sequence that has already been posted. Reels that reused an earlier winner's opening got 85-110 reach, and re-uploads got 0 (Oct 2).
  - Tell him to space posts out; posting 8 near-identical cuts in an hour keeps every one at the ~1.3K floor.

## 5b. Preview audit before the final render
- Render a 540p preview of every version from the proxies first (`c_build.py EDL --preview`, about 1.5 min) and run `audit.py` on it (only the resolution gate may fail on a preview).
- Audit each preview:
  - Non-VAD transcript: every line complete and in context.
  - 0-3s frame sheet at 0.25s steps.
  - 1fps frame sheet: the tattoo progress only moves forward, and there are no floor, ceiling or blur stretches.
  - silencedetect on the DX master (dialogue gaps); the FULL master is never silent.
- Fix the EDL and re-preview until it passes. Only then render full quality.
- Cut full-quality ranges from each original once, as stream-copy chunks with keyframe pre-roll, and keep a chunk map. Later edits then reuse the chunks instead of re-downloading from Drive.

## 6. Export: SDR 1080x1920 FULL-BLEED via the PC Shop Engine (replaces the 2K HDR letterbox export, Oct 9)
- Why: Instagram's HDR path expects iPhone Dolby Vision metadata ffmpeg can't write; Explore/web/SDR phones see Meta's tone-map, not ours; 2192x3896 isn't a size IG serves and the <30 MiB copy was starved at 3.5 Mbps; no evidence HDR lifts reach.
- EDL (`/home/claude/atlas/c/edl_*.json`, examples in project `claude/reel-bible/tools/edl_example_c3.json` and `edl_example_c5_speedrun.json`): name, song, song_offset (= drop in song - reveal time), drop (song time), music_montage_rms (-14.5 default; -16 to -18.5 for dense tracks), texture_db (null = music only), segs `[clip, in, out, I|M|C, x-or-"x", note, {zcut, z, zx}]` (I = dialogue island, M = montage, C = Canon reveal).
- `c_build.py EDL --preview`: DP snaps every boundary to the beat grid (half-beats under 100 BPM), islands only grow (d-0.02...d+0.6s) so no word is clipped, montage +-0.7s, reveal starts ON the drop. Audio per segment from its own clip: islands leveled to -17 dBFS RMS and muted 0.15s after the line, montage machine texture <=0.25 gain, reveal silent, then HP90 -> afftdn -> compressor -> ducked music -> limiter -> two-pass -14 LUFS / -1.5 dBTP. Writes FULL + DX wavs and a 540x960 SDR preview from the proxies.
- `c_build.py EDL --pc`: Shop Engine recipe. Originals (2144x2864 HEVC Main10 HLG) -> `crop=ih*9/16:ih` at the per-shot x -> 1080x1920 -> Hable tonemap `zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,eq=gamma=1.12:contrast=1.04:saturation=1.05` -> hqdn3d 1.5:1.5:6:6 + unsharp 0.5 -> h264_nvenc qp14 segments -> concat -> mux FULL/DX (16M VBR) into `D:\ClaudeInk\out\atlas\`. Canon MVI (1920x1080 + rotation=90 side data): ffmpeg autorotates to 1080x1920, so NO transpose (a transpose on top double-rotated and squashed the Atlas reveal on Oct 9); `scale=in_range=full:out_range=limited`, fps 30. `setsar=1` after every crop/scale.
- Append the chat copies: HEVC nvenc two-pass at `(28.5*8*1024^2/dur)/1000 - 330` kbps (cap 12 Mbps), hvc1, bt709 tags, AAC 256k -> `D:\ClaudeInk\out\chat\`. Commit the wavs to `D:\ClaudeInk\shop\music\` BEFORE the .cmd to `D:\ClaudeInk\shop\recipes\`. ~2 min per reel on his GPU.
- Every recipe starts `@echo off` + `setlocal`: the engine `call`s recipes, and a recipe's `set W=` once clobbered the engine's variables so it dead-looped. If `shop\engine.alive` is stale while idle, restart it with a runner job in `D:\ClaudeInk\runner\jobs\` (`taskkill /f /fi "WINDOWTITLE eq ClaudeInk Shop Engine*"` + `start "ClaudeInk Shop Engine" /min cmd /c D:\ClaudeInk\shop\engine\engine.cmd`).
- Never re-commit a recipe name that is still queued or running (the engine moves the file it ran, so a same-name replacement can be moved to done unrun).
- Never `pkill -f` or `pgrep -f ... | kill` with a pattern that appears in your own command line, because it kills your own shell.

## 7. Verify and deliver
- Stage the chat copies back, fully decode them (`ffmpeg -v error -i out -f null -`, zero errors), ffprobe hevc / bt709 / 1080x1920 / 30fps.
- `audit.py FULL.mp4 --format story|speedrun --clean --dx DX.mp4 --tl NAME_tl.json --match NAME_prev_FULL.mp4`: >=85, zero gate fails (SDR, resolution, 0 black frames, preview_mismatch 0 = the final matches the preview frame by frame, which catches rotation/crop bugs metrics can't see).
- Look at the FINAL's own 0-3s strip (0.25s steps), its 1fps sheet AND its last 8 seconds (never only the preview's): face + design in frame through the punch-in, tattoo progress only moves forward, reveal upright and undistorted, no floor/ceiling/blur.
- The 0-3s hook check (section 4), the 3-15s retention check (section 4b) and the 4c self-check pass. Every out-point lands in a pause.
- The batch overlap table shows every pair at or under 80% shared footage; versions differ in a named variable (opener, track, payoff).
- Send each version with SendUserFile as it finishes (FULL in chat; DX too), with:
  - Its audit score and the variable it tests.
  - Its 3-second hook and its approval and reaction beat.
  - Two hook-text options (no price hooks, no emoji on screen).
  - A caption: comment-bait question + "For all tattoo inquiries text (786) 766-3774" + "Books fill up fast!" + #tattoo #miamitattooartist #art #miami #artist.
  - The cue sheet (song, start offset, drop -> reveal time, Edits auto-ducking ON), a cover frame (finished piece readable inside the 3:4 grid crop) and the posting plan (Trial Reel when the session already has 4+ posts).