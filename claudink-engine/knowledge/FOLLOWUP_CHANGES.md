# Follow-up changes to the kit (collected while the build ran; apply with REVIEW_FINDINGS.md)

1. `tools/render_story.py` and `tools/render_broll.py` must build every video command through
   `tools/encode.py` (SegJob -> render_segments -> join_segments -> make_chat_copy; multi_output for
   proxy/sheet/strip; stage_clip for every source). No inline ffmpeg video commands remain in the
   renderers. `--preview` uses SegJob(preview=True). `<stem>.render.json` records realtime factors
   and the profile used.
2. `tools/render_broll.py template-from <approved.mp4>`: derive the slot timings from an approved
   render (ffmpeg scdet / select scene changes -> shot list -> roles alternate station/piece starting
   with station; total = duration) and write templates/broll_station_first.json without the
   PLACEHOLDER key. Fallback when RULES.md has no exact timings.
3. `tools/ledgers.py seed [--dir ledgers/seed]`: merge every ledgers/seed/*.csv into the live
   ledgers by key (hooks: normalized hook text; pieces: piece; sessions: session; others: all
   columns), never dropping or overwriting an existing row; report added/kept counts. Phase 0 step 5
   calls it.
4. `tools/footage_index.py` budget: a header probe counts min(size, 32 MB) against the daily budget
   (Drive streams chunks on seek, not the whole file); a full copy (encode.stage_clip) counts the
   full size. Keep the 20 GB/day cap.
5. `tools/gates.py`: read `data/calibration.json` for the motion threshold; `calibrate` subcommand
   uses `encode.proxy_chain`. G9 reads `edl.export.transfer`. G5 crops to the active area.
6. `tools/deliver.py`: top BLOCKED line also when `now` is past the slot's relay time (A relay
   10:05 ET, B relay 13:05 ET): "relay passed: take <slot> from the Drive folder". Read
   `state/blocked.txt` and `state/breakout.json`. Never two HOLD cuts delivered together.
7. `tools/preflight.py`: also report `state/render_profile.json` age (warn > 30 days, blocker if
   missing: tell the run to execute `py -m tools.encode bench`), and whether
   `templates/broll_station_first.json` still has the PLACEHOLDER key.
8. `tools/clean_cache.py`: keep `cache/segs/` and `cache/clips/` for 7 days (encode.prune_segment_cache).
9. `tools/panel.py`: `download` stops the batch for 24 h on the first login wall / 429 and writes
   `state/panel.md` with "unobtainable" shortcodes; the daily cap is 6, burn-in cap 20.
10. `tools/reels_analysis.py`: `views_72h` takes the last snapshot at or before posted+72h; reels
    younger than 72 h are "unknown" (not misses); the monthly hit rate reports unknown counts.
11. `tools/tests/`: the whole suite stays under 6 minutes here; mark anything slower `@pytest.mark.slow`.
12. `tools/preflight.py --bench` must call `tools.encode.bench()` (not render_story.bench); encode
    writes `generated_et` (ET ISO) and `chosen` into state/render_profile.json so preflight's detail
    line is informative. The daily-run skill calls `py -m tools.preflight` (report) and the weekly
    skill `py -m tools.preflight --bench`.
13. `tools/contact_sheet.py` exposes BOTH names: `sheet()/strip()/frame()` (SPEC) and the aliases
    `make_sheet(video, out, every=)`, `make_strip(video, out, start=, end=, step=)` that panel.py
    probes for; panel then uses contact_sheet and its own chain only as the fallback.
14. `tools/gates.py` G11 uses `fingerprint.frame1_report(video, store, sig=...)` for the detail
    string (shortcode + reused flag) when available.
15. `tools/hook_gate.py`: rows with status `ok` in hooks.csv are candidates that passed the gate but
    were never posted; they still count as "used" for similarity ONLY when `used_on` is set.
    Burned/retired/losing_type always count. Align with ledgers.burned_hooks().
16. DECISION, transfer policy for a mixed story (supersedes the builder spec's "mix -> sdr"):
    `edl.expected_transfer` returns "hlg" when the HLG segments carry at least half of the EDL's
    duration (the Canon SDR reveal, a few seconds, is mapped INTO the HLG container with the zscale
    bt709->bt2020/arib-std-b67 chain: SDR content shown correctly inside HLG, no fake HDR), and
    "sdr" only when SDR sources carry the majority (then HLG segs are tonemapped). All-HLG -> hlg,
    all-SDR -> sdr, `export.transfer` + `forced: true` still overrides. Reason strings must say which
    rule fired. Why: his explicit instruction is "2K HDR in the original format"; tonemapping 90% of
    a reel to keep a 3 s reveal untouched inverts that preference. Update tools/edl.py, its tests,
    tools/render_story.py (uses the decision), SPEC.md and START_HERE.md section 6A/14 to match.
17. End-to-end dry run `tools/tests/test_e2e_daily.py` (module-scoped fixtures, under 3 minutes):
    synthetic reels_history.csv -> `reels_analysis import` -> `slot_planner`/`deliver plan` at
    06:40 ET -> an EDL over 3 fixture clips (HLG + HLG + Canon rotated, tags that satisfy G4) ->
    `edl refine` with the stub transcript -> `render_story` preview and master via encode.py ->
    `gates` PASS -> `deliver` A and B (B = render_broll from 9 fixture shots) -> POST_THIS.txt
    checked line by line (order, exact tail, TikTok line, labels, shoot card) -> Drive mirror ->
    second `deliver` refused (cap) -> `hold` round trip -> `clean_cache` leaves post/ untouched.
    This is E6 of the burn-in exit criteria, run here before the kit ships.
