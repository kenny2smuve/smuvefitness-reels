"""Write guard used by every ClaudInk engine script (START_HERE §12).

Rules it enforces, with no way to switch them off:
- A write is allowed only when the resolved path is inside one of config.WRITE_ROOTS
  (the engine folder and his "ClaudeInk Reels" Drive folder).
- Output path == input path is refused.
- Existing files are never overwritten unless the caller passes overwrite=True AND the
  path is inside the engine's own cache/, runs/ or state/ folders (the only places where
  rewriting is normal). His footage, renders and deliveries are never overwritten.
- ffmpeg is always called with -nostdin -n (never -y).
- Deletion is only possible inside cache/. Anything else goes to _to_delete/ via stage_for_deletion.
- Every text file is opened with encoding="utf-8" (captions contain emoji).
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

from . import config  # noqa: F401  (relative import when used as a package)


class SafeIOError(PermissionError):
    """Raised when a write would land outside the engine's allowed trees."""


# --------------------------------------------------------------------------- helpers
def _norm(p: Path | str) -> str:
    """Resolved, case-normalised string path (Windows is case-insensitive)."""
    return os.path.normcase(str(Path(p).resolve(strict=False)))


def is_under(child: Path | str, parent: Path | str) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def write_roots() -> tuple[Path, ...]:
    # read lazily so tests can repoint config via environment + reload
    from . import config as _c
    return tuple(_c.WRITE_ROOTS)


def rewritable_dirs() -> tuple[Path, ...]:
    from . import config as _c
    return (_c.CACHE, _c.RUNS, _c.STATE, _c.DATA)


REWRITABLE_TEXT_SUFFIXES = {".txt", ".json", ".md", ".csv"}


def is_rewritable(path: Path | str) -> bool:
    """Where an existing file may be replaced: engine working dirs for anything, and the
    delivery folders (post/, hold/, Drive POST/) for text sidecars only (POST_THIS.txt,
    gates/edl json). A rendered .mp4 is never overwritten anywhere."""
    from . import config as _c
    p = Path(path)
    if any(is_under(p, d) for d in rewritable_dirs()):
        return True
    if p.suffix.lower() in REWRITABLE_TEXT_SUFFIXES and any(
            is_under(p, d) for d in (_c.POST, _c.HOLD, _c.DRIVE_POST, _c.LEDGERS, _c.REGISTER,
                                     _c.ENGINE_ROOT / "weekly", _c.KNOWLEDGE)):
        return True
    return False


def assert_writable(path: Path | str, *, overwrite: bool = False) -> Path:
    """Return the resolved path if a write there is allowed, else raise SafeIOError."""
    p = Path(path)
    if not any(is_under(p, r) for r in write_roots()):
        raise SafeIOError(f"refusing to write outside the engine roots: {p}")
    if p.exists():
        if p.is_dir():
            raise SafeIOError(f"refusing to write over a directory: {p}")
        if not overwrite:
            raise SafeIOError(f"refusing to overwrite an existing file (pass overwrite=True "
                              f"only inside cache/runs/state/data): {p}")
        if not is_rewritable(p):
            raise SafeIOError(f"overwrite is only allowed inside cache/, runs/, state/, data/ "
                              f"(and for text sidecars in post/, hold/, ledgers/): {p}")
    return p


def assert_not_same(out: Path | str, inputs: Iterable[Path | str]) -> None:
    o = _norm(out)
    for i in inputs:
        if _norm(i) == o:
            raise SafeIOError(f"output equals input: {out}")


def ensure_parent(path: Path | str) -> Path:
    p = Path(path)
    parent = p.parent
    if not any(is_under(parent, r) for r in write_roots()):
        raise SafeIOError(f"refusing to create folders outside the engine roots: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- writers
def open_write(path: Path | str, mode: str = "w", *, overwrite: bool = False, **kw):
    """Open a file for writing inside the allowed roots. Text mode is always utf-8."""
    if "r" in mode and "+" not in mode:
        raise ValueError("open_write is for writing; use builtin open() to read")
    p = assert_writable(path, overwrite=overwrite or "a" in mode)
    ensure_parent(p)
    if "b" not in mode:
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("newline", "")
    return open(p, mode, **kw)


def write_text(path: Path | str, text: str, *, overwrite: bool = False) -> Path:
    with open_write(path, "w", overwrite=overwrite) as f:
        f.write(text)
    return Path(path)


def write_json(path: Path | str, obj, *, overwrite: bool = False, indent: int = 2) -> Path:
    with open_write(path, "w", overwrite=overwrite) as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent, default=str)
        f.write("\n")
    return Path(path)


def write_bytes(path: Path | str, data: bytes, *, overwrite: bool = False) -> Path:
    with open_write(path, "wb", overwrite=overwrite) as f:
        f.write(data)
    return Path(path)


def read_text(path: Path | str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_json(path: Path | str, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def append_csv_row(path: Path | str, row: dict, header: Sequence[str]) -> Path:
    """Append one row to a ledger CSV, writing the header first if the file is new.

    Unknown keys in `row` are dropped, missing keys become empty strings, so a ledger
    never gets a ragged line.
    """
    p = Path(path)
    new = not p.exists() or p.stat().st_size == 0
    with open_write(p, "a") as f:
        w = csv.DictWriter(f, fieldnames=list(header), extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in header})
    return p


def read_csv(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def rewrite_csv(path: Path | str, rows: Iterable[dict], header: Sequence[str]) -> Path:
    """Replace a ledger wholesale (ledgers live under the engine root, so this is allowed
    even outside cache/: a ledger is engine-owned state, not his footage). A backup copy of
    the previous version is kept in state/ledger-backups/."""
    from . import config as _c
    p = Path(path)
    if not is_under(p, _c.LEDGERS) and not any(is_under(p, d) for d in rewritable_dirs()):
        raise SafeIOError(f"rewrite_csv only touches ledgers/ data/ runs/ state/ cache/: {p}")
    if p.exists():
        bdir = _c.STATE / "ledger-backups"
        bdir.mkdir(parents=True, exist_ok=True)
        import time
        shutil.copy2(p, bdir / f"{p.stem}.{int(time.time())}{p.suffix}")
    tmp = p.with_suffix(p.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    with open_write(tmp, "w") as f:
        w = csv.DictWriter(f, fieldnames=list(header), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
    os.replace(tmp, p)
    return p


def copy_file(src: Path | str, dst: Path | str, *, overwrite: bool = False) -> Path:
    """Copy a file into the allowed roots. Source may be anywhere (his footage is read-only)."""
    s, d = Path(src), Path(dst)
    if not s.is_file():
        raise FileNotFoundError(s)
    assert_not_same(d, [s])
    assert_writable(d, overwrite=overwrite)
    ensure_parent(d)
    shutil.copy2(s, d)
    return d


def mkdir(path: Path | str) -> Path:
    p = Path(path)
    if not any(is_under(p, r) for r in write_roots()):
        raise SafeIOError(f"refusing to create a folder outside the engine roots: {p}")
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- deletion
def remove_cache_file(path: Path | str) -> bool:
    """Delete a file, only inside cache/. Returns True if something was removed."""
    from . import config as _c
    p = Path(path)
    if not is_under(p, _c.CACHE):
        raise SafeIOError(f"deletion is only allowed inside cache/: {p}")
    if p.is_file():
        p.unlink()
        return True
    return False


def stage_for_deletion(path: Path | str) -> Path:
    """Move an engine-owned file into _to_delete/ for Claudin to review. Never his footage."""
    from . import config as _c
    p = Path(path)
    if not any(is_under(p, r) for r in write_roots()):
        raise SafeIOError(f"refusing to touch a file outside the engine roots: {p}")
    if is_under(p, _c.FOOTAGE_ROOT) and not is_under(p, _c.DRIVE_OUT):
        raise SafeIOError(f"never move his footage: {p}")
    root = next(r for r in write_roots() if is_under(p, r))
    rel = Path(_norm(p)).relative_to(Path(_norm(root)))
    dst = _c.TO_DELETE / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        import time
        dst = dst.with_name(f"{dst.stem}.{int(time.time())}{dst.suffix}")
    shutil.move(str(p), str(dst))
    return dst


# --------------------------------------------------------------------------- ffmpeg
def ffmpeg_cmd(args: Sequence[str], out: Path | str, *, inputs: Iterable[Path | str] = (),
               overwrite: bool = False) -> list[str]:
    """Build an ffmpeg command line that is safe by construction.

    `args` are the ffmpeg arguments *without* the binary and without the output path.
    `-y` anywhere in args is refused. Output path must be writable and differ from inputs.
    """
    from . import config as _c
    args = list(args)
    if "-y" in args:
        raise SafeIOError("ffmpeg -y is never allowed; outputs are never overwritten")
    ins = list(inputs)
    for i, a in enumerate(args):
        if a == "-i" and i + 1 < len(args):
            ins.append(args[i + 1])
    assert_not_same(out, [p for p in ins if not str(p).startswith(("color=", "anullsrc", "lavfi"))])
    assert_writable(out, overwrite=overwrite)
    ensure_parent(out)
    cmd = [_c.FFMPEG, "-nostdin", "-hide_banner", "-n"]
    # callers may pass -v themselves; default to error
    if "-v" not in args and "-loglevel" not in args:
        cmd += ["-v", "error"]
    return cmd + args + [str(out)]


def run_ffmpeg(args: Sequence[str], out: Path | str, *, inputs: Iterable[Path | str] = (),
               overwrite: bool = False, timeout: int = 1800) -> subprocess.CompletedProcess:
    cmd = ffmpeg_cmd(args, out, inputs=inputs, overwrite=overwrite)
    if overwrite and Path(out).exists():
        Path(out).unlink()          # we already verified this is a rewritable engine path
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def run_ffprobe(args: Sequence[str], timeout: int = 120) -> subprocess.CompletedProcess:
    from . import config as _c
    return subprocess.run([_c.FFPROBE, "-hide_banner", *args], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def run_ffmpeg_analysis(args: Sequence[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    """ffmpeg that produces no file (-f null -). Used by gates and analysis."""
    from . import config as _c
    args = list(args)
    if "-y" in args:
        raise SafeIOError("ffmpeg -y is never allowed")
    cmd = [_c.FFMPEG, "-nostdin", "-hide_banner", *args, "-f", "null", "-"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")
