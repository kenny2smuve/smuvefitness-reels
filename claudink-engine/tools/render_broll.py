"""Render a B-roll short from a shot list and the station-first template (START_HERE 6B, skill 6).

The template (templates/broll_station_first.json) is DATA: slot roles and durations, total
length, canvas, silent flag. Shots are assigned to slots in order by role (station shots fill
station slots, piece shots fill piece slots; the counts must match exactly) and each slot's
duration comes from the template, so the cut always matches the approved method within
config.BROLL_TOL.

Picture: SDR 1440x2560 bt709. iPhone/Meta HLG shots go through the exact tonemap chain from
the skill (zscale linear npl=100 -> hable -> bt709, then eq=gamma=1.12:contrast=1.04:
saturation=1.05) followed by hqdn3d=1.5:1.5:6:6,unsharp=5:5:0.5. Canon shots are untouched
except scale/pad (ffmpeg autorotate, NO transpose; scale=in_range=full:out_range=limited only
when ffprobe reports color_range=pc). Scale to width 1440 keeping aspect, never upscale, pad to
1440x2560, setsar=1, fps=30. Sound: a silent AAC track (anullsrc) so the file always has audio.
HEVC 8-bit master (hevc_nvenc when the profile has it, else libx265) + chat copy as in
render_story. Sidecars: <stem>.edl.json (type broll, tags station/piece, piece names) and
<stem>.render.json.

CLI: py -m tools.render_broll shots.json [--template templates/broll_station_first.json]
     --out post/2026-10-19/B_1430_name.mp4 [--json]
shots.json: a list of {clip, in, role, piece?, order_key?, note?} or
{"name": ..., "shots": [...], "hooks": [h1, h2], "caption": ...}.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from . import config, edl as edlmod, safeio

TEMPLATE_NAME = "broll_station_first.json"
ROLES = ("station", "piece")
HLG_LOOK = "eq=gamma=1.12:contrast=1.04:saturation=1.05"
POST_FILTERS = "hqdn3d=1.5:1.5:6:6,unsharp=5:5:0.5"
FULL_TO_LIMITED = "scale=in_range=full:out_range=limited"


def _encode():
    try:
        from . import encode  # noqa: WPS433
    except ImportError as e:   # pragma: no cover
        raise RuntimeError(f"tools.encode is required by render_broll ({e})") from e
    return encode


# --------------------------------------------------------------------------- template
def kit_templates_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "templates"


def default_template_path() -> Path:
    """config.TEMPLATES/<name> when present (his engine root), else the kit's own copy."""
    p = config.TEMPLATES / TEMPLATE_NAME
    return p if p.is_file() else kit_templates_dir() / TEMPLATE_NAME


def load_template(path: Path | str | None = None) -> dict:
    p = Path(path) if path else default_template_path()
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    errs = validate_template(t)
    if errs:
        raise ValueError(f"invalid template {p}: " + "; ".join(errs))
    t["_path"] = str(p)
    return t


def validate_template(t: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(t, dict):
        return ["template must be a JSON object"]
    slots = t.get("slots")
    if not isinstance(slots, list) or not slots:
        errs.append("slots must be a non-empty list")
        return errs
    for i, s in enumerate(slots):
        if not isinstance(s, dict) or s.get("role") not in ROLES:
            errs.append(f"slots[{i}]: role must be station or piece")
        d = s.get("s") if isinstance(s, dict) else None
        if not isinstance(d, (int, float)) or d <= 0:
            errs.append(f"slots[{i}]: s must be a positive number")
        elif d > config.BROLL_MAX_SHOT:
            errs.append(f"slots[{i}]: {d}s exceeds BROLL_MAX_SHOT {config.BROLL_MAX_SHOT}s")
    total = t.get("total_s")
    if not isinstance(total, (int, float)) or total <= 0:
        errs.append("total_s must be a positive number")
    elif not errs:
        s = sum(float(x["s"]) for x in slots)
        if abs(s - float(total)) > 0.005:
            errs.append(f"slots sum to {s:.3f}s but total_s is {total}")
    canvas = t.get("canvas")
    if not (isinstance(canvas, (list, tuple)) and len(canvas) == 2 and all(isinstance(v, int) and v > 0 for v in canvas)):
        errs.append("canvas must be [w, h]")
    if not t.get("name"):
        errs.append("name is required")
    return errs


def template_has_placeholder(t: dict) -> bool:
    return "PLACEHOLDER" in t


# --------------------------------------------------------------------------- shots
def normalize_shots(shots) -> tuple[list[dict], dict]:
    """Accept a list of shots or {"shots": [...], ...meta}; return (shots, meta)."""
    meta: dict = {}
    if isinstance(shots, dict):
        meta = {k: v for k, v in shots.items() if k != "shots"}
        shots = shots.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("shots must be a non-empty list")
    out = []
    for i, s in enumerate(shots):
        if not isinstance(s, dict) or not s.get("clip"):
            raise ValueError(f"shots[{i}]: clip is required")
        role = s.get("role")
        if role not in ROLES:
            raise ValueError(f"shots[{i}]: role must be station or piece, got {role!r}")
        try:
            t_in = float(s.get("in", 0.0))
        except (TypeError, ValueError):
            raise ValueError(f"shots[{i}]: in must be a number") from None
        if t_in < 0:
            raise ValueError(f"shots[{i}]: in must be >= 0")
        out.append({"clip": str(s["clip"]), "in": t_in, "role": role, "piece": s.get("piece") or "",
                    "order_key": s.get("order_key") or "", "note": s.get("note") or ""})
    return out, meta


def assign_slots(shots: list[dict], template: dict) -> list[dict]:
    """Pair shots with template slots by role, in order. Raises ValueError when counts differ."""
    shots, _ = normalize_shots(shots)          # idempotent: raw or normalized shot dicts both work
    by_role = {r: [s for s in shots if s["role"] == r] for r in ROLES}
    need = {r: [sl for sl in template["slots"] if sl["role"] == r] for r in ROLES}
    for r in ROLES:
        if len(by_role[r]) != len(need[r]):
            raise ValueError(f"template needs {len(need[r])} {r} shot(s), got {len(by_role[r])}")
    idx = {r: 0 for r in ROLES}
    plan = []
    for i, sl in enumerate(template["slots"]):
        r = sl["role"]
        shot = by_role[r][idx[r]]
        idx[r] += 1
        plan.append({**shot, "slot": i, "s": float(sl["s"]), "out": round(shot["in"] + float(sl["s"]), 3)})
    return plan


def shot_filters(info: dict) -> str:
    """Extra video filters for one shot (applied by encode.video_chain after its scale/tonemap)."""
    if info.get("is_hlg"):
        return f"{HLG_LOOK},{POST_FILTERS}"
    if str(info.get("range") or "").lower() in ("pc", "full", "jpeg"):
        return FULL_TO_LIMITED
    return ""


def build_edl(plan: list[dict], template: dict, name: str, meta: dict | None = None) -> edlmod.EDL:
    meta = meta or {}
    keys = [p["order_key"] for p in plan]
    if any(keys) and not all(keys):
        raise ValueError("either every shot has an order_key or none (d01-sNN is then generated)")
    segs = []
    pieces: list[str] = []
    for i, p in enumerate(plan):
        if p["piece"] and p["piece"] not in pieces:
            pieces.append(p["piece"])
        segs.append(edlmod.Seg(clip=p["clip"], in_=p["in"], out=p["out"], kind="M",
                               order_key=p["order_key"] or f"d01-s{i + 1:02d}", tags=[p["role"]],
                               note=p["note"] or p["piece"] or p["role"],
                               extra={"slot": p["slot"], "piece": p["piece"]} if p["piece"] else {"slot": p["slot"]}))
    cw, ch = template["canvas"]
    hooks = list(meta.get("hooks") or [])
    e = edlmod.EDL(name=name, type="broll", session=str(meta.get("session") or ""), multi_day=False,
                   canvas={"w": int(cw), "h": int(ch)}, segs=segs, hooks=hooks,
                   caption=str(meta.get("caption") or ""), pieces=pieces,
                   labels=dict(meta.get("labels") or {}), audio_from=None,
                   export={"template": template.get("name"), "template_total_s": template.get("total_s")})
    errs = edlmod.validate(e)
    if errs:
        raise ValueError("b-roll EDL invalid: " + "; ".join(errs))
    return e


# --------------------------------------------------------------------------- render
def render(shots, template, out: Path | str, *, now: datetime | None = None, profile=None,
           encoder: str | None = None, stage: bool = True, workers: int | None = None,
           name: str | None = None) -> dict:
    """Render the b-roll short. shots: list/dict (see module doc) or a path to shots.json;
    template: dict or path (default templates/broll_station_first.json). Returns a result dict."""
    encode = _encode()
    from . import render_story
    t_wall = time.time()
    now = now or datetime.now(config.TZ)
    if isinstance(shots, (str, Path)):
        with open(shots, "r", encoding="utf-8") as f:
            shots = json.load(f)
    shots, meta = normalize_shots(shots)
    tpl = template if isinstance(template, dict) else load_template(template)
    errs = validate_template(tpl)
    if errs:
        raise ValueError("invalid template: " + "; ".join(errs))
    out = Path(out)
    if out.suffix.lower() != ".mp4":
        raise ValueError(f"out must be an .mp4 path, got {out}")
    safeio.assert_writable(out)
    plan = assign_slots(shots, tpl)
    prof = render_story.profile_for(encoder, False, profile)

    clips: dict[str, Path] = {}
    infos: dict[str, dict] = {}
    for p in plan:
        if p["clip"] not in clips:
            src = Path(p["clip"])
            if not src.is_file():
                raise FileNotFoundError(f"shot clip missing: {src}")
            local = encode.stage_clip(src) if stage else src
            clips[p["clip"]] = local
            infos[p["clip"]] = encode.probe(local)
        dur = infos[p["clip"]]["duration"]
        if dur > 0 and p["out"] > dur + 0.05:
            raise ValueError(f"shot {Path(p['clip']).name} in={p['in']} needs {p['s']}s but the clip ends at {dur:.2f}s")
    e = build_edl(plan, tpl, name or meta.get("name") or out.stem, meta)
    e.export.update({"transfer": "sdr", "reason": "b-roll shorts are always SDR bt709 (approved method)"})
    canvas = e.canvas_wh
    jobs = [encode.SegJob(clip=clips[p["clip"]], start=p["in"], end=p["out"], transfer_out="sdr", canvas=canvas,
                          preview=False, extra_vf=shot_filters(infos[p["clip"]]), audio="silent",
                          info=infos[p["clip"]]) for p in plan]
    t0 = time.time()
    seg_results = encode.render_segments(jobs, prof, workers=workers)
    t_segs = time.time() - t0
    expected = sum(j.duration for j in jobs)
    stem = out.stem
    join_target = encode.seg_cache_dir() / f"{stem}_join_{int(t_wall)}.mp4"
    join = encode.join_segments([r["path"] for r in seg_results], join_target, expected=expected)
    join_mode = "copy"
    if not join["ok"]:
        join_mode = "reencode"
        join_target = encode.seg_cache_dir() / f"{stem}_join_{int(t_wall)}_re.mp4"
        join = render_story.reencode_join([r["path"] for r in seg_results], join_target, prof, "sdr",
                                          expected=expected)
    safeio.copy_file(join_target, out)
    safeio.remove_cache_file(join_target)
    master = render_story.probe_facts(out)
    chat_out = out.with_name(f"{stem}_chat.mp4")
    chat = encode.make_chat_copy(out, chat_out, prof, "sdr", master["duration"])
    chat_facts = render_story.probe_facts(chat_out)
    chat_facts["decode_errors"] = encode.decode_errors(chat_out)
    wall = time.time() - t_wall
    result = {
        "out": str(out), "type": "broll", "template": tpl.get("name"), "template_path": tpl.get("_path"),
        "template_placeholder": template_has_placeholder(tpl), "transfer": "sdr",
        "encoder": render_story._encoder_name(prof, False), "profile": prof.chosen, "canvas": list(canvas),
        "slots": [{"slot": p["slot"], "role": p["role"], "clip": Path(p["clip"]).name, "in": p["in"],
                   "out": p["out"], "s": p["s"], "piece": p["piece"], "hlg": infos[p["clip"]]["is_hlg"],
                   "filters": shot_filters(infos[p["clip"]]), "cached": r["cached"], "seconds": r["seconds"]}
                  for p, r in zip(plan, seg_results)],
        "expected_duration": round(expected, 3), "duration": master["duration"],
        "duration_ok": abs(master["duration"] - float(tpl["total_s"])) <= config.BROLL_TOL,
        "join": {"mode": join_mode, "ok": join["ok"], "duration": join["duration"]},
        "master": master, "decode_errors": encode.decode_errors(out),
        "chat": {"path": str(chat_out), "bytes": chat["bytes"], "target_kbps": chat["target_kbps"],
                 "ok": chat["ok"] and chat_facts["decode_errors"] == 0, "facts": chat_facts},
        "pieces": list(e.pieces), "seconds_segments": round(t_segs, 2), "seconds_wall": round(wall, 2),
        "x_realtime": round(expected / wall, 2) if wall > 0 else None,
        "rendered_et": now.isoformat(timespec="seconds"),
    }
    e.export.update({"rendered_et": result["rendered_et"], "out": str(out), "encoder": result["encoder"],
                     "canvas": {"w": canvas[0], "h": canvas[1]}, "duration": master["duration"],
                     "chat": str(chat_out), "template_placeholder": result["template_placeholder"]})
    edl_side = out.with_name(f"{stem}.edl.json")
    render_side = out.with_name(f"{stem}.render.json")
    edlmod.save(e, edl_side)
    safeio.write_json(render_side, result, overwrite=True)
    result["edl_sidecar"] = str(edl_side)
    result["render_sidecar"] = str(render_side)
    return result


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Render a station-first B-roll short (SDR 1440x2560, silent, HEVC + chat copy)")
    ap.add_argument("shots", help="shots json: [{clip, in, role, piece?, order_key?}] or {shots, hooks, caption, name}")
    ap.add_argument("--template", default=None, help=f"template json (default {TEMPLATE_NAME})")
    ap.add_argument("--out", required=True, help="master mp4, e.g. post/2026-10-19/B_1430_name.mp4")
    ap.add_argument("--encoder", default=None, choices=["hevc_nvenc", "libx265"])
    ap.add_argument("--no-stage", action="store_true", help="read clips in place (skip the cache/clips copy)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        res = render(args.shots, args.template, Path(args.out), encoder=args.encoder, stage=not args.no_stage)
    except (ValueError, FileNotFoundError, RuntimeError, safeio.SafeIOError) as e:
        print(f"error: {e}")
        return 1
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        m = res["master"]
        print(f"{res['out']}: {m['width']}x{m['height']} {m['codec']} {m['transfer']} {m['duration']:.2f}s "
              f"(template {res['template']} {res['expected_duration']}s ok={res['duration_ok']}) "
              f"{res['encoder']} chat={res['chat']['bytes'] / 1048576:.1f}MB ok={res['chat']['ok']}"
              + ("  !!! template still carries PLACEHOLDER timings" if res["template_placeholder"] else ""))
    ok = res["join"]["ok"] and res["duration_ok"] and res["decode_errors"] == 0 and res["chat"]["ok"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
