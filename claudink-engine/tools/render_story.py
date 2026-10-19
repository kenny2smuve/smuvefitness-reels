"""Render a session story (or talking head) from an EDL (START_HERE 6A, 6B export, 12; SPEC "Render speed").

What it does, per EDL segment: ffmpeg -ss in -to out -i clip (accurate seek, the segment's OWN
audio), scale to the canvas width keeping aspect (never upscaling: a narrower source is only
padded), pad to 1440x2560 centred black, setsar=1, fps=30. Colour follows the export transfer
decided by edl.expected_transfer: an HLG export keeps HLG (HEVC Main10, bt2020, arib-std-b67;
SDR segments are converted with zscale), an SDR export tonemaps HLG segments with the standard
zscale/hable chain and passes SDR segments through. Canon clips rely on ffmpeg autorotate (no
transpose). Canon range (skill 6, Oct 9: every Canon MVI needs scale=in_range=full:out_range=
limited): the filter is applied when ffprobe reports color_range=pc, and ASSUMED for an MVI_ file
whose stream carries no range tag (recorded as "range_assumed": "full (canon)" per segment; verify
on one real MVI during the first PC run and note it in runs/<date>.md). A tagged tv/limited Canon
file is trusted. Segments are encoded with identical parameters in parallel (tools.encode: NVENC
when the measured profile has it, else libx265), cached in cache/segs/, then joined with the
concat demuxer and -c copy; if the joined duration is off by more than 0.1 s the join is
re-encoded as a fallback. Audio: each segment's own audio, 48 kHz stereo AAC 256k, no
processing and no music; the ONLY touch is a 10 ms afade in/out per segment so cuts never click.
A dialogue (I) segment whose clip has no audio stream still renders (anullsrc), but the segment is
marked "audio": "silent" and the result/sidecar carry a `warnings` list so gates and the critic
see the dead line.

--transfer hlg|sdr FORCES the export (edl.export {transfer, forced: true}, honoured by
edl.expected_transfer and recorded in the sidecar); auto applies the policy in tools.edl.

Outputs: master -> out; chat copy -> <stem>_chat.mp4 (bitrate-targeted under
config.CHAT_COPY_MAX_BYTES, capped at 12 Mbps, same transfer tags); <stem>.edl.json (the EDL
with export info); <stem>.render.json (ffprobe facts, timings). --preview renders 360x640
H.264 into cache/previews/ (overwrite allowed there; masters are never overwritten).

CLI: py -m tools.render_story edl.json --out post/2026-10-19/A_1100_name.mp4 [--preview]
     [--transfer auto|hlg|sdr] [--encoder hevc_nvenc|libx265] [--json]
     py -m tools.render_story bench        (measure the machine, write state/render_profile.json)
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Sequence

from . import config, edl as edlmod, safeio

PREVIEW_DIR = "previews"
FULL_TO_LIMITED = "scale=in_range=full:out_range=limited"
FULL_RANGE_TAGS = ("pc", "full", "jpeg")
LIMITED_RANGE_TAGS = ("tv", "mpeg", "limited")
CANON_PREFIX = "MVI_"              # SPEC footage.csv camera rule: MVI_ -> canon (SDR, rotated)


def is_canon_name(clip: Path | str) -> bool:
    return Path(clip).name.upper().startswith(CANON_PREFIX)


def range_filter(info: dict, clip: Path | str) -> tuple[str, str]:
    """(extra video filter, range note) for one source.

    ("scale=in_range=full:out_range=limited", "full (tagged pc)") when ffprobe reports a full-range
    stream; the same filter with "full (canon)" when the stream carries NO range tag and the file is
    a Canon MVI_ (skill 6: every Canon MVI needed the full->limited conversion on Oct 9, and Canon
    MOVs often carry no tag); ("", "") otherwise. HLG sources never get it (they are limited
    range by format and are tonemapped/kept as such).
    """
    if info.get("is_hlg"):
        return "", ""
    rng = str(info.get("range") or "").lower()
    if rng in FULL_RANGE_TAGS:
        return FULL_TO_LIMITED, "full (tagged pc)"
    if rng in LIMITED_RANGE_TAGS:
        return "", ""
    if is_canon_name(clip):
        return FULL_TO_LIMITED, "full (canon)"
    return "", ""


# --------------------------------------------------------------------------- helpers
def _encode():
    try:
        from . import encode  # noqa: WPS433
    except ImportError as e:   # pragma: no cover - the module ships with the kit
        raise RuntimeError(f"tools.encode is required by render_story ({e})") from e
    return encode


def profile_for(encoder: str | None = None, preview: bool = False, profile=None):
    """The render profile: state/render_profile.json (or a cheap detection), optionally
    overridden by an explicit encoder name. Previews never need the GPU."""
    encode = _encode()
    prof = profile or encode.load_profile()
    if encoder == "hevc_nvenc":
        prof.nvenc = True
        prof.chosen = "gpu"
        if prof.nvenc_sessions < 1:
            prof.nvenc_sessions = 1
    elif encoder in ("libx265", "cpu"):
        prof.chosen = "cpu"
    elif encoder:
        raise ValueError(f"unknown encoder {encoder!r}: use hevc_nvenc or libx265")
    if not preview and prof.chosen == "cpu" and not prof.libx265:
        # fail here, before multi-GB clips are staged, not as an opaque ffmpeg error later
        raise ValueError("ffmpeg has no libx265 (profile: libx265=false); use --encoder hevc_nvenc "
                         "on the GPU or install the full ffmpeg build")
    return prof


def preview_path(out: Path) -> Path:
    """Previews live in cache/previews/ (rewritable); an out already inside cache/ is used as is."""
    out = Path(out)
    if safeio.is_under(out, config.CACHE):
        return out
    return config.CACHE / PREVIEW_DIR / f"{out.stem}_preview.mp4"


def probe_facts(path: Path) -> dict:
    """ffprobe facts for the render sidecar / G9."""
    encode = _encode()
    info = encode.probe(path)
    info["bytes"] = Path(path).stat().st_size
    return info


# --------------------------------------------------------------------------- render
def render(edl, out: Path | str, preview: bool = False, encoder: str | None = None, *,
           transfer: str = "auto", now: datetime | None = None, profile=None,
           stage: bool = True, workers: int | None = None) -> dict:
    """Render an EDL (object or path) to `out`. Returns a dict with paths, facts and timings.

    transfer: auto (edl.expected_transfer policy) | hlg | sdr (forces edl.export.transfer).
    now: timestamp written to the sidecars (passed in so tests are deterministic).
    profile: a tools.encode.Profile to use instead of state/render_profile.json.
    stage: copy clips into cache/clips/ first (Drive never stalls a render); False for local clips.
    """
    encode = _encode()
    t_wall = time.time()
    now = now or datetime.now(config.TZ)
    e = edlmod.load(edl) if isinstance(edl, (str, Path)) else edl.copy()
    errs = edlmod.validate(e)
    if errs:
        raise ValueError("invalid EDL: " + "; ".join(errs))
    out = Path(out)
    if preview:
        out = preview_path(out)
    elif out.suffix.lower() != ".mp4":
        raise ValueError(f"out must be an .mp4 path, got {out}")
    safeio.assert_writable(out, overwrite=preview)

    prof = profile_for(encoder, preview, profile)
    # clips: stage once, probe once
    clips: dict[str, Path] = {}
    infos: dict[str, dict] = {}
    for s in e.segs:
        if s.clip in clips:
            continue
        src = Path(s.clip)
        if not src.is_file():
            raise FileNotFoundError(f"segment clip missing: {src}")
        local = encode.stage_clip(src) if stage else src
        clips[s.clip] = local
        infos[s.clip] = encode.probe(local)
        if s.out > infos[s.clip]["duration"] + 0.05 and infos[s.clip]["duration"] > 0:
            raise ValueError(f"segment out {s.out} beyond clip end {infos[s.clip]['duration']:.2f}: {src.name}")
    if transfer in ("hlg", "sdr"):
        # an explicit --transfer is a FORCE: edl.forced_transfer honours it only with forced=true
        e.export.update({"transfer": transfer, "forced": True})
    elif transfer != "auto":
        raise ValueError(f"transfer must be auto|hlg|sdr, got {transfer!r}")
    elif e.export.get("transfer") == "auto":
        del e.export["transfer"]
    transfer_out = edlmod.expected_transfer(e, lambda c: infos[c])

    # segments
    canvas = e.canvas_wh
    ranges = {c: range_filter(infos[c], c) for c in clips}
    jobs = [encode.SegJob(clip=clips[s.clip], start=s.in_, end=s.out, transfer_out=transfer_out,
                          canvas=canvas, preview=preview, audio="own", info=infos[s.clip],
                          extra_vf=ranges[s.clip][0])
            for s in e.segs]
    warnings: list[str] = []
    for i, s in enumerate(e.segs):
        if s.kind == "I" and not infos[s.clip].get("has_audio"):
            warnings.append(f"segs[{i}] {Path(s.clip).name} {s.in_:.2f}-{s.out:.2f} is a dialogue (I) "
                            "segment but the clip has no audio stream: rendered silent")
    t0 = time.time()
    seg_results = encode.render_segments(jobs, prof, workers=workers)
    t_segs = time.time() - t0
    expected = sum(j.duration for j in jobs)

    # join (into cache first for a master so `out` is written exactly once; the name carries a
    # random suffix so two renders of the same stem in the same second never collide)
    stem = out.stem
    tag = f"{int(t_wall)}_{uuid.uuid4().hex[:8]}"
    join_target = out if preview else encode.seg_cache_dir() / f"{stem}_join_{tag}.mp4"
    join = encode.join_segments([r["path"] for r in seg_results], join_target, expected=expected,
                                overwrite=preview)
    join_mode = "copy"
    if not join["ok"]:
        join_mode = "reencode"
        if preview:
            safeio.remove_cache_file(join_target)
        else:
            join_target = encode.seg_cache_dir() / f"{stem}_join_{tag}_re.mp4"
        join = reencode_join([r["path"] for r in seg_results], join_target, prof, transfer_out,
                             preview=preview, expected=expected)
    if not preview:
        safeio.copy_file(join_target, out)
        safeio.remove_cache_file(join_target)
    master_facts = probe_facts(out)
    result: dict = {
        "out": str(out), "preview": preview, "transfer": transfer_out, "export": dict(e.export),
        "encoder": _encoder_name(prof, preview), "profile": prof.chosen, "canvas": list(canvas),
        "segments": [{"clip": Path(j.clip).name, "kind": s.kind, "in": j.start, "out": j.end,
                      "duration": j.duration,
                      "audio": "own" if infos[s.clip].get("has_audio") else "silent",
                      "range_filter": ranges[s.clip][0], "range_assumed": ranges[s.clip][1],
                      "cached": r["cached"], "seconds": r["seconds"], "key": r["key"]}
                     for s, j, r in zip(e.segs, jobs, seg_results)],
        "warnings": warnings,
        "expected_duration": round(expected, 3), "duration": master_facts["duration"],
        "join": {"mode": join_mode, "ok": join["ok"], "duration": join["duration"]},
        "master": master_facts, "decode_errors": encode.decode_errors(out),
        "seconds_segments": round(t_segs, 2),
    }
    # chat copy (masters only)
    if not preview:
        chat_out = out.with_name(f"{stem}_chat.mp4")
        chat = encode.make_chat_copy(out, chat_out, prof, transfer_out, master_facts["duration"])
        chat_facts = probe_facts(chat_out)
        chat_facts["decode_errors"] = encode.decode_errors(chat_out)
        result["chat"] = {"path": str(chat_out), "bytes": chat["bytes"], "target_kbps": chat["target_kbps"],
                          "ok": chat["ok"] and chat_facts["decode_errors"] == 0, "facts": chat_facts}
    wall = time.time() - t_wall
    result["seconds_wall"] = round(wall, 2)
    result["x_realtime"] = round(expected / wall, 2) if wall > 0 else None
    result["rendered_et"] = now.isoformat(timespec="seconds")
    # sidecars
    e.export.update({"rendered_et": result["rendered_et"], "out": str(out), "preview": preview,
                     "encoder": result["encoder"], "canvas": {"w": canvas[0], "h": canvas[1]},
                     "duration": master_facts["duration"], "warnings": list(warnings)})
    assumed = {Path(c).name: note for c, (_f, note) in ranges.items() if note}
    if assumed:
        e.export["range_assumed"] = assumed
    if "chat" in result:
        e.export["chat"] = result["chat"]["path"]
    edl_side = out.with_name(f"{stem}.edl.json")
    render_side = out.with_name(f"{stem}.render.json")
    edlmod.save(e, edl_side)
    safeio.write_json(render_side, result, overwrite=True)
    result["edl_sidecar"] = str(edl_side)
    result["render_sidecar"] = str(render_side)
    return result


def _encoder_name(prof, preview: bool) -> str:
    encode = _encode()
    args = encode.encoder_args(prof, "sdr", preview=preview)
    return args[1] if len(args) > 1 else "?"


def reencode_join(paths: Sequence[Path], out: Path, prof, transfer_out: str, *, preview: bool = False,
                  expected: float | None = None) -> dict:
    """Fallback join: concat demuxer with the video re-encoded (same encoder parameters)."""
    encode = _encode()
    import hashlib
    lst = encode.seg_cache_dir() / f"concat_re_{hashlib.sha1('|'.join(map(str, paths)).encode()).hexdigest()[:12]}.txt"
    lines = "".join("file '" + str(Path(p).resolve()).replace("'", r"'\''") + "'\n" for p in paths)
    safeio.write_text(lst, lines, overwrite=True)
    vf = "format=yuv420p10le" if (transfer_out == "hlg" and not preview) else "format=yuv420p"
    args = ["-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0", "-map", "0:a:0",
            "-vf", f"{vf},setsar=1,fps=30", *encode.encoder_args(prof, transfer_out, preview=preview),
            *encode.AUDIO_ARGS, "-movflags", "+faststart"]
    res = safeio.run_ffmpeg(args, out, inputs=list(paths), overwrite=preview)
    if res.returncode != 0:
        raise RuntimeError(f"re-encode join failed: {(res.stderr or '').strip()[-600:]}")
    got = encode.probe(out)["duration"]
    exp = expected if expected is not None else got
    return {"path": Path(out), "duration": got, "expected": round(exp, 3), "ok": abs(got - exp) <= 0.15}


def bench(max_sessions: int = 8):
    """Measure this machine and write state/render_profile.json (delegates to tools.encode)."""
    return _encode().bench(max_sessions=max_sessions)


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["bench"]:
        bp = argparse.ArgumentParser(prog="render_story bench", description="measure NVDEC/NVENC/libplacebo/CPU")
        bp.add_argument("--max-sessions", type=int, default=8)
        bp.add_argument("--json", action="store_true")
        a = bp.parse_args(argv[1:])
        prof = bench(a.max_sessions)
        print(json.dumps(prof.to_json(), indent=2) if a.json else
              f"render profile: {prof.chosen} nvenc={prof.nvenc} sessions={prof.nvenc_sessions} "
              f"libplacebo={prof.libplacebo} cpu_preset={prof.cpu_preset}")
        return 0
    ap = argparse.ArgumentParser(description="Render a story/TH reel from an EDL (master + chat copy + sidecars)")
    ap.add_argument("edl", help="EDL json")
    ap.add_argument("--out", required=True, help="master mp4, e.g. post/2026-10-19/A_1100_name.mp4")
    ap.add_argument("--preview", action="store_true", help="360x640 H.264 preview into cache/previews/")
    ap.add_argument("--transfer", default="auto", choices=["auto", "hlg", "sdr"],
                    help="auto = tools.edl policy; hlg|sdr FORCES the export (recorded as export.forced)")
    ap.add_argument("--encoder", default=None, choices=["hevc_nvenc", "libx265"])
    ap.add_argument("--no-stage", action="store_true", help="read clips in place (skip the cache/clips copy)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        res = render(args.edl, Path(args.out), preview=args.preview, encoder=args.encoder,
                     transfer=args.transfer, stage=not args.no_stage)
    except (ValueError, FileNotFoundError, RuntimeError, safeio.SafeIOError) as e:
        print(f"error: {e}")
        return 1
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        m = res["master"]
        chat = res.get("chat")
        print(f"{res['out']}: {m['width']}x{m['height']} {m['codec']} {m['transfer']} {m['duration']:.2f}s "
              f"{res['encoder']} join={res['join']['mode']} {res['x_realtime']}x realtime"
              + (f" chat={chat['bytes'] / 1048576:.1f}MB ok={chat['ok']}" if chat else ""))
        for w in res.get("warnings") or []:
            print(f"  ! {w}")
    ok = res["join"]["ok"] and res["decode_errors"] == 0 and (res.get("chat", {"ok": True})["ok"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
