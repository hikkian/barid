#!/usr/bin/env python3
"""Barid: a prompt relay board for people who work with several AI agent sessions.

One file, Python 3.9+, no dependencies. The browser panel (panel.html) lives next to this file.
Agents use the command line (`rb ...`), the person uses the panel (`barid open`).
Run `rb --help` or read SKILL.md next to this script's folder.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fnmatch
import glob
import hashlib
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import textwrap
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.6.0"
SCHEMA = 1
HERE = Path(__file__).resolve().parent
STATUSES = ("draft", "proposed", "queued", "sent", "running", "review", "done", "cancelled")
ACTIVE = ("sent", "running")
OUTCOMES = ("complete", "partial", "failed")  # how a finished task ended; only "complete" unlocks dependents by itself
FINISHED = ("review", "done", "cancelled")    # a task that is over (the person may still have to look at it)
# Why a queued task cannot start yet, like the reason column of a batch scheduler. The first four end by themselves (time passes,
# other tasks finish); the last two need the person.
REASONS = ("begin_time", "dependency", "conflict", "decision", "never")
SELF_RESOLVING = ("begin_time", "dependency", "conflict")
NEXT_TASK, NEXT_NONE, NEXT_WAIT, NEXT_STUCK = "task", "none", "wait", "stuck"   # outcomes of `next`; exit codes 0, 2, 3, 4
EXIT_NONE, EXIT_WAIT, EXIT_STUCK = 2, 3, 4
PRESENCE_STATES = ("idle", "waiting", "working")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
AGENT_RE = re.compile(r"^[A-Za-z0-9 ._+-]{0,24}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
DEFAULT_PORT = 8765
DATA_DIRS = (".barid", ".relayboard")  # the second is the old name, still found
MAX_BODY = 512 * 1024
MAX_EVENTS = 500


class RBError(Exception):
    """A user-facing error: printed without a traceback, exit code 1.

    `code` and `info` let a client (the panel) show the message in its own language."""

    def __init__(self, message: str, code: str = "", **info):
        super().__init__(message)
        self.code = code
        self.info = info


# ----------------------------------------------------------------------------- time, actors

def now_dt() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now_dt().isoformat()


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def parse_when(text: str, now: dt.datetime | None = None) -> str:
    """A start time typed by a person or an agent, as an ISO time with offset (UTC). Accepts: `23:00` (the next 23:00 in the
    local time zone), `2026-10-07 23:00` or `2026-10-07T23:00` (local time), `+90m`, `+3h`, `+1d` (from now), or a full ISO time
    with an offset. An empty text clears the time (returns "")."""
    text = (text or "").strip()
    if not text:
        return ""
    now = now or now_dt()
    m = re.fullmatch(r"\+\s*(\d+)\s*([mhd])", text.lower())
    if m:
        n = int(m.group(1))
        delta = {"m": dt.timedelta(minutes=n), "h": dt.timedelta(hours=n), "d": dt.timedelta(days=n)}[m.group(2)]
        return (now + delta).isoformat()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        local_now = now.astimezone()
        try:
            at = local_now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        except ValueError:
            raise RBError(f"not a time of day: {text!r}", "bad_time", value=text) from None
        if at <= local_now:
            at += dt.timedelta(days=1)
        return at.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
    ts = None
    try:
        iso = text.replace(" ", "T")
        ts = dt.datetime.fromisoformat(iso[:-1] + "+00:00" if iso[-1:] in ("Z", "z") else iso)    # older Pythons do not read a trailing Z
    except ValueError:
        pass
    if ts is None:
        raise RBError(f"cannot read the time {text!r}: use 23:00, 2026-10-07 23:00, +90m, +3h or +1d", "bad_time", value=text)
    if ts.tzinfo is None:
        ts = ts.astimezone()           # a time without an offset is local time
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def local_time(iso: str) -> str:
    """`2026-10-07 23:00` in the local time zone, for messages."""
    ts = parse_ts(iso)
    return ts.astimezone().strftime("%Y-%m-%d %H:%M") if ts else ""


def parse_minutes(v, what: str = "timebox") -> int:
    """A duration typed as 90, 90m, 8h or 1d; minutes as an integer (0 = none)."""
    if v in (None, "", 0, "0"):
        return 0
    if isinstance(v, int) and not isinstance(v, bool):
        n = v
    else:
        m = re.fullmatch(r"(\d+)\s*([mhd]?)", str(v).strip().lower())
        if not m:
            raise RBError(f"cannot read the {what} {v!r}: use minutes, or 90m, 8h, 1d", "bad_duration", value=str(v))
        n = int(m.group(1)) * {"": 1, "m": 1, "h": 60, "d": 1440}[m.group(2)]
    if not 0 <= n <= 14 * 1440:
        raise RBError(f"the {what} must be between 1 minute and 14 days", "bad_duration", value=str(v))
    return n


class Actor:
    """Who is acting. Identity is advisory: the board protects against accidents, not against malice."""

    def __init__(self, kind: str, name: str):
        self.kind, self.name = kind, name

    @property
    def human(self) -> bool:
        return self.kind == "human"


HUMAN = Actor("human", "human")


def env(name: str, default=None):
    """BARID_X, falling back to the old RB_X names (the tool was first called RelayBoard)."""
    return os.environ.get("BARID_" + name) or os.environ.get("RB_" + name) or default


def cli_actor(args) -> Actor:
    if getattr(args, "human", False) or env("ACTOR") == "human":
        return HUMAN
    name = getattr(args, "by", None) or env("AGENT") or "agent"
    return Actor("agent", name)


# ----------------------------------------------------------------------------- storage

def find_board(explicit=None) -> Path:
    cand = explicit or env("BOARD")
    if cand:
        p = Path(cand).expanduser()
        if p.is_dir():
            p = p / "board.json" if (p / "board.json").is_file() else next(
                (p / n / "board.json" for n in DATA_DIRS if (p / n / "board.json").is_file()), p / ".barid" / "board.json")
        return p.resolve()
    cur = Path.cwd().resolve()
    for d in [cur, *cur.parents]:
        for name in DATA_DIRS:
            f = d / name / "board.json"
            if f.is_file():
                return f
    raise RBError("no board found here: run `barid init` in your project folder, or pass --board PATH / set BARID_BOARD")


@contextlib.contextmanager
def locked(path: Path):
    """Cross-process lock around read-modify-write of the board file."""
    lock_path = path.with_name(path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            deadline = time.time() + 15
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise RBError("board is locked by another process")
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _retry_io(fn, attempts: int = 40, delay: float = 0.05):
    """Windows refuses to replace or read a file another process holds for a moment: retry briefly."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def load(path: Path) -> dict:
    try:
        d = json.loads(_retry_io(lambda: path.read_text("utf-8")))
    except FileNotFoundError:
        raise RBError(f"board file not found: {path}")
    except json.JSONDecodeError as e:
        raise RBError(f"board file is not valid JSON ({path}): {e}")
    if d.get("schema") != SCHEMA:
        raise RBError(f"unsupported board schema {d.get('schema')!r} (this barid understands {SCHEMA})")
    if d.get("lang") == "kz":  # people write kz; the language code is kk
        d["lang"] = "kk"
    d["_root"] = str(path.resolve().parent.parent)  # the project folder (real path); lives in memory only
    return d


def save(path: Path, d: dict) -> None:
    d["updated"] = now_iso()
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    clean = {k: v for k, v in d.items() if not k.startswith("_")}
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=1) + "\n", "utf-8")
    try:
        _retry_io(lambda: os.replace(tmp, path))
    except PermissionError:
        raise RBError(f"could not write {path}: it is held by another program")


def mutate(path: Path, fn):
    """Run fn(board) under the lock; save only if it did not raise."""
    with locked(path):
        d = load(path)
        out = fn(d)
        d["rev"] = int(d.get("rev", 0)) + 1
        save(path, d)
        return out


def system_lang() -> str:
    """en, ru or kk from the usual locale variables; anything else means en."""
    loc = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "").lower()
    return "ru" if loc.startswith("ru") else "kk" if loc.startswith(("kk", "kz")) else "en"


def new_board(project: str, lanes, lang: str = "en") -> dict:
    return {
        "schema": SCHEMA, "project": project, "rev": 1, "created": now_iso(), "updated": now_iso(), "lang": "kk" if lang == "kz" else lang,
        "lanes": [{"id": i, "title": t} for i, t in lanes],
        "resources": {
            "gpu": {"label": "GPU", "exclusive": True},
            "user": {"label": "User present", "exclusive": True},
        },
        "policy": {"direct_agents": [], "dependents_wait_for_accept": False, "lease_hours": 12, "footer": True, "handoff": True},
        "context": [], "protect": [], "items": [], "events": [],
    }


def log_event(d: dict, actor: Actor, action: str, iid: str = "", detail: str = "") -> None:
    d.setdefault("events", []).append({"t": now_iso(), "by": actor.name, "action": action, "id": iid, "detail": detail})
    if len(d["events"]) > MAX_EVENTS:
        del d["events"][: len(d["events"]) - MAX_EVENTS]


# ----------------------------------------------------------------------------- scheduling

# ----------------------------------------------------------------------------- file scopes, protected paths, git

GLOB_CHARS = re.compile(r"[*?\[]")


def project_root(d: dict) -> Path:
    return Path(d.get("_root") or Path.cwd())


def _case(p: str) -> str:
    return p.lower() if os.name == "nt" else p


def task_root(d: dict, it: dict) -> Path:
    """Where a task's relative scopes live: its own worktree, else the project folder."""
    return Path(os.path.realpath(os.path.expanduser(it["workdir"]))) if it.get("workdir") else project_root(d)


def norm_scope(d: dict, s: str, root=None) -> str:
    """A scope is a file, a folder or a glob; relative ones are relative to the task's checkout (default: the project folder)."""
    s = os.path.expanduser(str(s).strip())
    p = s if os.path.isabs(s) else str(Path(root) / s if root else project_root(d) / s)
    # real path: /var is /private/var on macOS and git reports the real one; Windows short names collapse too
    return _case(os.path.realpath(os.path.normpath(p)).replace("\\", "/"))


def show_scope(d: dict, p: str, root=None) -> str:
    base = _case(Path(root or project_root(d)).as_posix())
    return p[len(base) + 1:] if p.startswith(base + "/") else p


def own_scopes(d: dict, it: dict) -> list:
    return [norm_scope(d, x, task_root(d, it)) for x in it.get("touches", [])]


def _static_prefix(p: str) -> str:
    m = GLOB_CHARS.search(p)
    if not m:
        return p
    head = p[: m.start()]
    return head[: head.rfind("/")] if "/" in head else ""


def _contains(a: str, b: str) -> bool:
    """Is b the same as a, or inside folder a?"""
    return a == b or b.startswith(a.rstrip("/") + "/")


def scopes_overlap(a: str, b: str) -> bool:
    """Conservative: two scopes overlap if they could name the same file."""
    ga, gb = bool(GLOB_CHARS.search(a)), bool(GLOB_CHARS.search(b))
    if not ga and not gb:
        return _contains(a, b) or _contains(b, a)
    if ga != gb:
        lit, pat = (b, a) if ga else (a, b)
        return fnmatch.fnmatchcase(lit, pat) or _contains(lit, _static_prefix(pat))
    pa, pb = _static_prefix(a), _static_prefix(b)
    return not pa or not pb or _contains(pa, pb) or _contains(pb, pa)


def path_in_scope(path: str, scope: str) -> bool:
    if GLOB_CHARS.search(scope):
        return fnmatch.fnmatchcase(path, scope)
    return _contains(scope, path)


def hash_paths(paths, limit: int = 800) -> dict:
    """SHA-256 of every file under the given paths (None for a path that does not exist yet)."""
    res = {}

    def one(fp):
        try:
            h = hashlib.sha256()
            with open(fp, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    for p in paths:
        if len(res) >= limit:
            break
        if p.is_dir():
            for root, _dirs, files in os.walk(p):
                for fn in sorted(files):
                    if len(res) >= limit:
                        break
                    fp = Path(root) / fn
                    res[_case(fp.as_posix())] = one(fp)
        else:
            res[_case(p.as_posix())] = one(p) if p.exists() else None
    return res


def expand_protected(d: dict) -> list:
    out = []
    for pat in d.get("protect", []):
        n = norm_scope(d, pat)
        out += [Path(x) for x in sorted(glob.glob(n, recursive=True))] if GLOB_CHARS.search(n) else [Path(n)]
    return out


def _git(args, cwd, timeout: int = 30):
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_state(workdir):
    if not Path(workdir).is_dir():
        return None
    top = _git(["rev-parse", "--show-toplevel"], workdir)
    if not top:
        return None
    return {"repo": Path(top).as_posix(), "head": _git(["rev-parse", "HEAD"], workdir) or "", "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], workdir) or ""}


def git_changed(state: dict) -> list:
    """Files that differ from the commit the task started at, plus new untracked files (absolute paths)."""
    repo, base = state["repo"], state.get("head")
    files = set()
    for args in ((["diff", "--name-only", base] if base else ["diff", "--name-only"]), ["ls-files", "--others", "--exclude-standard"]):
        files |= {ln for ln in (_git(args, repo) or "").splitlines() if ln}
    return sorted((Path(repo) / f).as_posix() for f in files)


def snapshot_for_claim(d: dict, it: dict) -> dict:
    snap = {}
    prot = expand_protected(d)
    if prot:
        snap["protected"] = hash_paths(prot)
    if d.get("policy", {}).get("git_check", True):
        st = git_state(it.get("workdir") or project_root(d))
        if st:
            snap["git"] = st
    return snap


def verify_at_finish(d: dict, it: dict) -> tuple:
    """Compare the claim-time snapshot with the present state: returns (violations, changed summary or None)."""
    claim = it.get("claim") or {}
    viol, changed = [], None
    before = claim.get("protected")
    if before is not None:
        after = hash_paths(expand_protected(d))
        for p in sorted(set(before) | set(after)):
            if before.get(p) != after.get(p):
                viol.append({"type": "protected", "path": show_scope(d, p)})
    st = claim.get("git")
    if st:
        root = task_root(d, it)
        mine = _case(str(root))
        skip = [_case((project_root(d) / n).as_posix()) for n in DATA_DIRS]  # the board's own files are not project changes
        files = [f for f in git_changed(st) if not any(_contains(sk, _case(f)) for sk in skip)]
        scopes = own_scopes(d, it)
        others = [(o["id"], own_scopes(d, o)) for o in d["items"]
                  if o["id"] != it["id"] and o["status"] in ACTIVE and _case(str(task_root(d, o))) == mine]
        for f in files:
            fc = _case(f)
            if scopes and not any(path_in_scope(fc, sc) for sc in scopes):
                viol.append({"type": "outside_scope", "path": show_scope(d, fc, root)})
            for oid, osc in others:
                if any(path_in_scope(fc, sc) for sc in osc):
                    viol.append({"type": "collision", "path": show_scope(d, fc, root), "with": oid})
        changed = {"count": len(files), "files": [show_scope(d, _case(f), root) for f in files[:100]], "truncated": len(files) > 100,
                   "repo": st["repo"], "branch": st["branch"]}
    return viol[:60], changed


# ----------------------------------------------------------------------------- what a task declares, and how tasks conflict

# A profile is a named bundle of marks, so that nobody has to remember the flags. A board can add its own (`barid profile add`).
# "light" is an explicit statement "this task loads nothing": it counts as described, so the panel can really check it.
BUILTIN_PROFILES = {
    "light": {"label": "Light work: no special needs", "quiet": False, "noisy": False, "uses": []},
    "dev": {"label": "Development: builds and tests load the machine", "quiet": False, "noisy": True, "uses": []},
    "bench": {"label": "Measurement: needs a quiet machine and loads it itself", "quiet": True, "noisy": True, "uses": []},
    "attended": {"label": "The person is present: needs quiet and the 'user' resource", "quiet": True, "noisy": False, "uses": ["user"]},
}
LINT_CODES = ("no_marks", "gpu_missing", "load_missing", "quiet_missing", "port_missing")


def profile_def(d: dict, name):
    if not name:
        return None
    return d.get("profiles", {}).get(name) or BUILTIN_PROFILES.get(name)


def all_profiles(d: dict) -> dict:
    out = {k: dict(v, builtin=True) for k, v in BUILTIN_PROFILES.items()}
    for k, v in d.get("profiles", {}).items():
        out[k] = dict(v, builtin=False)
    return out


def effective(d: dict, it: dict) -> dict:
    """What a task really declares: its own marks plus those of its profile (resources the board does not define are skipped)."""
    uses = list(it.get("uses", []))
    quiet, noisy = bool(it.get("quiet")), bool(it.get("noisy"))
    p = profile_def(d, it.get("profile"))
    if p:
        for r in p.get("uses", []):
            if r in d.get("resources", {}) and r not in uses:
                uses.append(r)
        quiet = quiet or bool(p.get("quiet"))
        noisy = noisy or bool(p.get("noisy"))
    return {"uses": uses, "quiet": quiet, "noisy": noisy}


def is_described(d: dict, it: dict) -> bool:
    """Does the task say anything about what it needs? Without that there is nothing to check, and the panel must not claim a check."""
    e = effective(d, it)
    return bool(e["uses"] or e["quiet"] or e["noisy"] or it.get("touches") or it.get("profile"))


def conflicts_between(d: dict, a: dict, b: dict) -> list:
    """Every reason why two tasks must not run at the same time. One pure function: symmetric, and a task never conflicts with itself."""
    if a["id"] == b["id"]:
        return []
    out = []
    if a["lane"] == b["lane"]:
        out.append({"rule": "lane", "why": "lane"})
    res = d.get("resources", {})
    ea, eb = effective(d, a), effective(d, b)
    for r in ea["uses"]:
        if r in eb["uses"] and res.get(r, {}).get("exclusive", True):
            out.append({"rule": "resource", "why": r})
    if (ea["quiet"] and eb["noisy"]) or (ea["noisy"] and eb["quiet"]):
        out.append({"rule": "quiet", "why": "quiet"})
    # different checkouts (git worktrees) cannot overwrite each other's files; their changes meet at merge time
    if a.get("touches") and b.get("touches") and _case(str(task_root(d, a))) == _case(str(task_root(d, b))):
        done = False
        for x in own_scopes(d, a):
            for y in own_scopes(d, b):
                if not done and scopes_overlap(x, y):
                    out.append({"rule": "paths", "why": "paths:" + show_scope(d, x if len(x) >= len(y) else y, task_root(d, a))})
                    done = True
    return out


def conflict_reason(d: dict, a: dict, b: dict):
    """Why two tasks must not run at the same time (the first reason, None if they can)."""
    c = conflicts_between(d, a, b)
    return c[0]["why"] if c else None


_GPU_RE = re.compile(r"\b(gpu|vram|cuda|nvidia-smi|llama-server|llama-bench|llama\.cpp)\b", re.I)
_LOAD_RE = re.compile(r"\b(pytest|unittest|npm (?:run )?(?:test|build)|cargo (?:build|test)|cmake|make -j|compil\w*|сборк\w*|собер\w*|компил\w*|прогон\w* тест\w*)\b", re.I)
_QUIET_RE = re.compile(r"\b(benchmark|bench|latency|throughput|tok/s|tokens/s|замер\w*|измер\w*|ток/с)\b", re.I)
_PORT_RE = re.compile(r"(?:--port[ =]+|\bport[ =:]+|localhost:|127\.0\.0\.1:)(\d{3,5})", re.I)


def lint_task(d: dict, it: dict) -> list:
    """Suggestions where the description of a task looks incomplete. Only suggestions: the person or planner decides (`--lint-ok CODE` silences one)."""
    if it["status"] not in ("draft", "queued", "proposed"):
        return []
    silenced = set(it.get("lint_ok", []))
    e = effective(d, it)
    text = " ".join(str(it.get(k) or "") for k in ("title", "outline", "text"))
    out = []
    if not is_described(d, it):
        out.append({"code": "no_marks"})
    if "gpu" in d.get("resources", {}) and "gpu" not in e["uses"] and _GPU_RE.search(text):
        out.append({"code": "gpu_missing"})
    if not e["noisy"] and _LOAD_RE.search(text):
        out.append({"code": "load_missing"})
    if not e["quiet"] and _QUIET_RE.search(text):
        out.append({"code": "quiet_missing"})
    seen = set()
    for m in _PORT_RE.finditer(text):
        port = m.group(1)
        if port not in seen and "port:" + port not in e["uses"]:
            seen.add(port)
            out.append({"code": "port_missing", "arg": port})
    return [x for x in out if x["code"] not in silenced]


LINT_TEXT = {
    "no_marks": "nothing is declared (no profile, resources, quiet/noisy or files), so Barid cannot check it against other tasks; add --profile light if it really needs nothing",
    "gpu_missing": "the text mentions the GPU or a model server, but the task does not use the 'gpu' resource (--uses gpu)",
    "load_missing": "the text mentions builds or tests, but the task is not marked as loading the machine (--noisy or --profile dev)",
    "quiet_missing": "the text mentions measuring speed, but the task is not marked as needing a quiet machine (--quiet or --profile bench)",
    "port_missing": "the text mentions port {arg}, but the task does not use the resource 'port:{arg}' (add it with `barid resource add port:{arg}` and --uses)",
}


def lint_line(x: dict) -> str:
    return LINT_TEXT[x["code"]].format(arg=x.get("arg", ""))


def explain_pair(d: dict, a: dict, b: dict) -> dict:
    """Everything Barid looks at when it answers 'can these two run together', rule by rule."""
    items = {i["id"]: i for i in d["items"]}
    ea, eb = effective(d, a), effective(d, b)
    res = d.get("resources", {})
    shared = [r for r in ea["uses"] if r in eb["uses"]]
    found = conflicts_between(d, a, b)
    hit = {c["rule"] for c in found}
    related = a["id"] in _closure(items, b["id"]) or b["id"] in _closure(items, a["id"])
    rules = [
        {"rule": "lane", "hit": "lane" in hit, "detail": f"{a['lane']} / {b['lane']}"},
        {"rule": "resource", "hit": "resource" in hit, "detail": ", ".join(r + (" (exclusive)" if res.get(r, {}).get("exclusive", True) else " (shared)") for r in shared) or "no common resource"},
        {"rule": "quiet", "hit": "quiet" in hit, "detail": f"{a['id']}: quiet={ea['quiet']} noisy={ea['noisy']}; {b['id']}: quiet={eb['quiet']} noisy={eb['noisy']}"},
        {"rule": "paths", "hit": "paths" in hit, "detail": next((c["why"][6:] for c in found if c["rule"] == "paths"), "no overlapping files, or not both declared")},
    ]
    da, db = is_described(d, a), is_described(d, b)
    if found:
        verdict = "conflict"
    elif related:
        verdict = "related"
    elif not (da and db):
        verdict = "unchecked"
    else:
        verdict = "ok"
    return {"a": a["id"], "b": b["id"], "described": {a["id"]: da, b["id"]: db}, "rules": rules, "related": related, "verdict": verdict,
            "why": found[0]["why"] if found else ""}


# ----------------------------------------------------------------------------- what ran next to what (a record that does not depend on the marks)

def _close_run(it: dict) -> dict:
    cl = it.get("claim") or {}
    if cl.get("at"):
        it["ran"] = {"from": cl["at"], "to": now_iso()}
    return it.get("ran") or {}


def overlap_with(d: dict, it: dict, run: dict) -> list:
    """Tasks that were active at any moment of this task's run (still active now, or with a recorded run that intersects)."""
    start, end = parse_ts(run.get("from")), parse_ts(run.get("to"))
    out = []
    if not start or not end:
        return out
    for o in d["items"]:
        if o["id"] == it["id"]:
            continue
        hit = o["status"] in ACTIVE
        r = o.get("ran")
        if not hit and r:
            a, b = parse_ts(r.get("from")), parse_ts(r.get("to"))
            hit = bool(a and b and a <= end and b >= start)  # timestamps have one-second resolution: touching counts as overlapping
        if hit:
            e = effective(d, o)
            out.append({"id": o["id"], "lane": o["lane"], "noisy": e["noisy"], "described": is_described(d, o)})
    return out


def _satisfied(d: dict, items: dict, nid: str) -> bool:
    n = items.get(nid)
    if not n:
        return False
    if n["status"] == "done":
        return True  # the person accepted it (even a partial result)
    return (n["status"] == "review" and n.get("outcome") == "complete" and not n.get("violations")
            and not d.get("policy", {}).get("dependents_wait_for_accept", False))


def deps_of(i: dict) -> list:
    """Everything a task is ordered after: `needs` (after a successful end) and `after` (after any end)."""
    return list(i.get("needs", [])) + [n for n in i.get("after", []) if n not in i.get("needs", [])]


def _closure(items: dict, iid: str, seen=None) -> set:
    seen = seen if seen is not None else set()
    for n in deps_of(items.get(iid, {})):
        if n not in seen:
            seen.add(n)
            _closure(items, n, seen)
    return seen


def wait_reasons(d: dict, items: dict, i: dict, waiting_on: list, begin_in: int, conflicts: list) -> list:
    """Why a queued task cannot start now, one entry per cause: {code, id?, ...}. Codes (see REASONS): begin_time (an earliest start
    time has not come), dependency (a task it waits for is not over yet), decision (the task it needs ended without success and
    waits for the person to accept it), never (the task it needs was cancelled: it can never be satisfied, fix the dependency),
    conflict (it cannot run next to a task that runs now)."""
    out = []
    if begin_in:
        out.append({"code": "begin_time", "at": i.get("not_before", ""), "in": begin_in})
    for n in waiting_on:
        t = items.get(n)
        if t is None or t["status"] == "cancelled":
            out.append({"code": "never", "id": n, "status": (t or {}).get("status", "missing")})
        elif t["status"] == "review" and n in i.get("needs", []):
            out.append({"code": "decision", "id": n, "outcome": t.get("outcome", "")})
        elif t["status"] in ("draft", "proposed"):
            out.append({"code": "decision", "id": n, "status": t["status"]})     # nobody can run it before the person writes or approves it
        else:
            out.append({"code": "dependency", "id": n, "status": t["status"]})
    for x in conflicts:
        out.append({"code": "conflict", "id": x["id"], "why": x["why"]})
    return out


def compute(d: dict, now=None) -> list:
    """Return the tasks with computed fields: state, waiting_on, conflicts, can_run_with, stale."""
    now = now or now_dt()
    items = {i["id"]: i for i in d["items"]}
    active = [i for i in d["items"] if i["status"] in ACTIVE]
    out = []
    for i in d["items"]:
        c = dict(i)
        c.setdefault("profile", "")
        c["effective"] = effective(d, i)
        c["described"] = is_described(d, i)
        c["lint"] = lint_task(d, i)
        c["waiting_on"] = [n for n in i.get("needs", []) if not _satisfied(d, items, n)]
        c["waiting_on"] += [n for n in i.get("after", []) if n not in c["waiting_on"] and not (items.get(n) or {}).get("status") in FINISHED]
        c["waiting_info"] = {n: {"status": items[n]["status"], "outcome": items[n].get("outcome", "")} for n in c["waiting_on"] if n in items}
        c["not_before"] = i.get("not_before", "")
        begin = parse_ts(i.get("not_before")) if i.get("not_before") else None
        c["begin_in"] = max(0, int((begin - now).total_seconds())) if begin and begin > now else 0
        c["conflicts"] = []
        for a in active:
            if a["id"] != i["id"]:
                r = conflict_reason(d, i, a)
                if r:
                    c["conflicts"].append({"id": a["id"], "why": r})
        c["reasons"] = wait_reasons(d, items, i, c["waiting_on"], c["begin_in"], c["conflicts"]) if i["status"] == "queued" else []
        if i["status"] == "queued":
            if c["waiting_on"] or c["begin_in"]:
                c["state"] = "waiting"
            else:
                c["state"] = "blocked" if c["conflicts"] else "ready"
        else:
            c["state"] = i["status"]
        cl = i.get("claim")
        ts = parse_ts(cl.get("lease_until")) if cl else None
        c["stale"] = bool(i["status"] == "running" and ts and ts < now)
        c["can_run_with"], c["cannot_run_with"] = [], []
        out.append(c)
    # everything that could be started now or already runs; a draft whose turn has come (nothing it needs is open) counts too
    live = [c for c in out if c["state"] in ("ready", "blocked", "sent", "running") or (c["status"] == "draft" and not c["waiting_on"])]
    for a in live:
        for b in live:
            if a["id"] == b["id"]:
                continue
            related = a["id"] in _closure(items, b["id"]) or b["id"] in _closure(items, a["id"])
            r = conflict_reason(d, a, b)
            if r:
                a["cannot_run_with"].append({"id": b["id"], "why": r})
            elif not related:
                a["can_run_with"].append(b["id"])
    return out


def plan_steps(d: dict, computed: list) -> list:
    """Group the live tasks into steps; tasks in one step may run at the same time."""
    items = {i["id"]: i for i in d["items"]}
    live = [c for c in computed if c["status"] not in ("done", "cancelled", "review")]
    placed, steps = {}, {}
    if d.get("policy", {}).get("dependents_wait_for_accept", False):
        for c in computed:
            if c["status"] == "review":
                placed[c["id"]] = 1  # waits for the person's acceptance: dependents come after
    for c in live:
        if c["status"] in ACTIVE:
            placed[c["id"]] = 1
            steps.setdefault(1, []).append(c["id"])
    pending = [c for c in live if c["id"] not in placed]
    liveids = {c["id"] for c in live} | set(placed)
    guard = 0
    while pending and guard < len(live) + 5:
        guard += 1
        progressed = False
        for c in list(pending):
            unfinished = [n for n in deps_of(c) if n in liveids]
            if any(n not in placed for n in unfinished):
                continue
            earliest = 1 + max([placed[n] for n in unfinished], default=0)
            s = earliest
            while any(conflict_reason(d, items[c["id"]], items[o]) for o in steps.get(s, [])):
                s += 1
            placed[c["id"]] = s
            steps.setdefault(s, []).append(c["id"])
            pending.remove(c)
            progressed = True
        if not progressed:
            break
    last = max(steps) if steps else 0
    for c in pending:  # dependency cycle or dangling reference: put at the end, in order
        last += 1
        steps.setdefault(last, []).append(c["id"])
    return [steps[k] for k in sorted(steps)]


def has_cycle(items: dict) -> bool:
    """A cycle in needs and after together."""
    state = {}

    def visit(n):
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        for m in deps_of(items[n]):
            if m in items and visit(m):
                return True
        state[n] = 2
        return False

    return any(visit(n) for n in items)


# ----------------------------------------------------------------------------- operations

def get_item(d: dict, iid: str) -> dict:
    for i in d["items"]:
        if i["id"] == iid:
            return i
    raise RBError(f"no such task: {iid}", "not_found", id=iid)


def require_human(actor: Actor, what: str) -> None:
    if not actor.human:
        raise RBError(f"only the person can {what} (agents can propose, claim, finish and note)", "human_only")


def direct_allowed(d: dict, actor: Actor) -> bool:
    if actor.human:
        return True
    direct = d.get("policy", {}).get("direct_agents", [])
    return "*" in direct or actor.name in direct


def split_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        v = v.split(",")
    return [x.strip() for x in v if x and x.strip()]


def check_refs(d: dict, lane, needs, uses, after=None) -> None:
    if lane is not None and lane not in [l["id"] for l in d["lanes"]]:
        raise RBError(f"unknown lane {lane!r}; lanes: {', '.join(l['id'] for l in d['lanes'])} (add one with `barid lane add`)", "unknown_lane", lane=lane)
    ids = {i["id"] for i in d["items"]}
    for n in needs or []:
        if n not in ids:
            raise RBError(f"unknown task in needs: {n}", "unknown_task", id=n)
    for n in after or []:
        if n not in ids:
            raise RBError(f"unknown task in after: {n}", "unknown_task", id=n)
    for r in uses or []:
        if r not in d.get("resources", {}):
            raise RBError(f"unknown resource {r!r}; define it with `barid resource add {r}`", "unknown_resource", name=r)


def check_profile(d: dict, name) -> None:
    if name and not profile_def(d, name):
        raise RBError(f"unknown profile {name!r}; known: {', '.join(all_profiles(d))} (add one with `barid profile add`)", "unknown_profile", name=name)


def check_lint_ok(codes) -> list:
    bad = [c for c in codes if c not in LINT_CODES]
    if bad:
        raise RBError(f"unknown lint code {bad[0]!r}; known: {', '.join(LINT_CODES)}", "bad_lint", name=bad[0])
    return list(dict.fromkeys(codes))


def op_add(d: dict, actor: Actor, f: dict) -> dict:
    iid = f["id"]
    if not ID_RE.match(iid or ""):
        raise RBError("task id: letters, digits, '.', '_' or '-', up to 32 characters, starting with a letter or digit", "bad_id")
    if any(i["id"] == iid for i in d["items"]):
        raise RBError(f"task {iid} already exists", "exists", id=iid)
    lane = f.get("lane") or d["lanes"][0]["id"]
    needs, uses = split_list(f.get("needs")), split_list(f.get("uses"))
    after = [n for n in split_list(f.get("after")) if n not in needs]
    check_refs(d, lane, needs, uses, after)
    not_before = parse_when(f.get("not_before") or "")
    timebox = parse_minutes(f.get("timebox"))
    profile = (f.get("profile") or "").strip()
    check_profile(d, profile)
    lint_ok = check_lint_ok(split_list(f.get("lint_ok")))
    text = (f.get("text") or "").strip()
    status = "draft" if not text else "queued"
    if f.get("draft"):
        status = "draft"
    if not direct_allowed(d, actor):
        status = "proposed"
    item = {
        "id": iid, "lane": lane, "title": (f.get("title") or iid).strip(), "status": status, "needs": needs, "after": after, "uses": uses,
        "not_before": not_before, "timebox": timebox,
        "quiet": bool(f.get("quiet")), "noisy": bool(f.get("noisy")), "profile": profile, "lint_ok": lint_ok,
        "when": f.get("when") or "", "outline": f.get("outline") or "", "touches": split_list(f.get("touches")), "workdir": f.get("workdir") or "", "branch": f.get("branch") or "",
        "text": text, "report": "", "notes": [], "created": now_iso(), "updated": now_iso(), "created_by": actor.name, "claim": None,
    }
    d["items"].append(item)
    if has_cycle({i["id"]: i for i in d["items"]}):
        d["items"].remove(item)
        raise RBError("that would create a dependency cycle", "cycle")
    log_event(d, actor, "add" if status != "proposed" else "propose", iid, item["title"])
    return item


def op_edit(d: dict, actor: Actor, iid: str, f: dict) -> dict:
    it = get_item(d, iid)
    own_proposal = it["status"] in ("proposed", "draft") and it.get("created_by") == actor.name
    if not (direct_allowed(d, actor) or own_proposal):
        raise RBError(f"you cannot edit {iid}: it is not your proposal (ask the person, or leave a note)")
    if it["status"] in ("done", "cancelled"):
        raise RBError(f"{iid} is {it['status']}; restore or recreate it instead of editing", "wrong_state", id=iid, status=it["status"])
    if "lane" in f and f["lane"] is not None:
        check_refs(d, f["lane"], None, None)
        it["lane"] = f["lane"]
    for k in ("title", "when", "outline"):
        if f.get(k) is not None:
            it[k] = f[k]
    if f.get("needs") is not None or f.get("after") is not None:
        needs = split_list(f["needs"]) if f.get("needs") is not None else list(it.get("needs", []))
        after = split_list(f["after"]) if f.get("after") is not None else list(it.get("after", []))
        if iid in needs or iid in after:
            raise RBError("a task cannot need itself", "cycle")
        after = [n for n in after if n not in needs]
        check_refs(d, None, needs, None, after)
        old = (it["needs"], it.get("after", []))
        it["needs"], it["after"] = needs, after
        if has_cycle({i["id"]: i for i in d["items"]}):
            it["needs"], it["after"] = old
            raise RBError("that would create a dependency cycle", "cycle")
    if f.get("not_before") is not None:
        it["not_before"] = parse_when(f["not_before"])
    if f.get("timebox") is not None:
        it["timebox"] = parse_minutes(f["timebox"])
    if f.get("uses") is not None:
        uses = split_list(f["uses"])
        check_refs(d, None, None, uses)
        it["uses"] = uses
    if f.get("touches") is not None:
        it["touches"] = split_list(f["touches"])
    for k in ("workdir", "branch"):
        if f.get(k) is not None:
            it[k] = f[k]
    for k in ("quiet", "noisy"):
        if f.get(k) is not None:
            it[k] = bool(f[k])
    if f.get("profile") is not None:
        check_profile(d, f["profile"])
        it["profile"] = f["profile"].strip()
    if f.get("lint_ok") is not None:
        it["lint_ok"] = check_lint_ok(split_list(f["lint_ok"]))
    if f.get("replace"):
        old, new = f["replace"]
        n = it["text"].count(old)
        if n != 1:
            raise RBError(f"the text to replace occurs {n} times in {iid}, it must occur exactly once")
        it["text"] = it["text"].replace(old, new)
    if f.get("text") is not None:
        it["text"] = f["text"].strip()
    if it["status"] == "draft" and it["text"]:
        it["status"] = "queued" if direct_allowed(d, actor) else "proposed"
    it["updated"] = now_iso()
    log_event(d, actor, "edit", iid)
    return it


def op_set_status(d: dict, actor: Actor, iid: str, status: str, detail: str = "") -> dict:
    require_human(actor, "set a status directly")
    if status not in STATUSES:
        raise RBError(f"status must be one of: {', '.join(STATUSES)}", "bad_status")
    it = get_item(d, iid)
    if status == "queued" and not it["text"].strip():
        status = "draft"
    it["status"] = status
    if status != "running":
        it["claim"] = None
    elif not it.get("claim"):
        # a task the person sets to "running" by hand still gets a claim, so that the panel shows who holds it, the lease works and nobody else can finish it
        hours = float(d.get("policy", {}).get("lease_hours", 12))
        it["claim"] = {"by": actor.name, "at": now_iso(), "lease_until": (now_dt() + dt.timedelta(hours=hours)).isoformat()}
    it["updated"] = now_iso()
    log_event(d, actor, "status", iid, f"{status} {detail}".strip())
    return it


def op_approve(d: dict, actor: Actor, iid: str) -> dict:
    require_human(actor, "approve a proposal")
    it = get_item(d, iid)
    if it["status"] != "proposed":
        raise RBError(f"{iid} is {it['status']}, not a proposal", "wrong_state", id=iid, status=it["status"])
    it["status"] = "queued" if it["text"].strip() else "draft"
    it["updated"] = now_iso()
    log_event(d, actor, "approve", iid)
    return it


def op_reject(d: dict, actor: Actor, iid: str, reason: str = "") -> dict:
    require_human(actor, "reject a proposal")
    it = get_item(d, iid)
    if it["status"] != "proposed":
        raise RBError(f"{iid} is {it['status']}, not a proposal", "wrong_state", id=iid, status=it["status"])
    it["status"] = "cancelled"
    it["updated"] = now_iso()
    if reason:
        it["notes"].append({"t": now_iso(), "by": actor.name, "text": f"rejected: {reason}"})
    log_event(d, actor, "reject", iid, reason)
    return it


def op_cancel(d: dict, actor: Actor, iid: str, reason: str = "") -> dict:
    it = get_item(d, iid)
    own = it["status"] in ("proposed", "draft") and it.get("created_by") == actor.name
    if not (direct_allowed(d, actor) or own):
        raise RBError(f"you cannot cancel {iid}: leave a note for the person instead")
    if it["status"] in ("done", "cancelled"):
        raise RBError(f"{iid} is already {it['status']}")
    if it["status"] == "running" and not actor.human:
        raise RBError(f"{iid} is running: its owner must `release` or `finish` it first")
    it["status"] = "cancelled"
    it["claim"] = None
    it["updated"] = now_iso()
    if reason:
        it["notes"].append({"t": now_iso(), "by": actor.name, "text": f"cancelled: {reason}"})
    log_event(d, actor, "cancel", iid, reason)
    return it


def op_restore(d: dict, actor: Actor, iid: str) -> dict:
    require_human(actor, "restore a cancelled task")
    it = get_item(d, iid)
    if it["status"] != "cancelled":
        raise RBError(f"{iid} is {it['status']}, not cancelled", "wrong_state", id=iid, status=it["status"])
    it["status"] = "queued" if it["text"].strip() else "draft"
    it["updated"] = now_iso()
    log_event(d, actor, "restore", iid)
    return it


def op_sent(d: dict, actor: Actor, iid: str) -> dict:
    require_human(actor, "mark a task as sent")
    it = get_item(d, iid)
    if it["status"] not in ("queued", "draft"):
        raise RBError(f"{iid} is {it['status']}, it cannot be marked as sent", "wrong_state", id=iid, status=it["status"])
    if not it["text"].strip():
        raise RBError(f"{iid} has no prompt text yet (generate it first)", "no_text", id=iid)
    it["status"] = "sent"
    it["updated"] = now_iso()
    log_event(d, actor, "sent", iid)
    return it


def refusal_for(iid: str, reasons: list) -> RBError | None:
    """The refusal a claim gets for these wait reasons (None when there are none). Codes: stuck (needs the person), begin_time,
    waiting (for other tasks), conflict: the last three end by themselves, so `claim --wait` waits for them."""
    if not reasons:
        return None
    hard = [r for r in reasons if r["code"] not in SELF_RESOLVING]
    if hard:
        r = hard[0]
        what = {"decision": "needs the person to accept or write it", "never": "was cancelled or does not exist (change the dependency)"}.get(r["code"], r["code"])
        return RBError(f"{iid} cannot start: {r.get('id', '')} {what}", "stuck", id=iid, reasons=reasons)
    r = reasons[0]
    if r["code"] == "begin_time":
        return RBError(f"{iid} may not start before {local_time(r['at'])}", "begin_time", id=iid, at=r["at"], reasons=reasons)
    if r["code"] == "dependency":
        ids = [x["id"] for x in reasons if x["code"] == "dependency"]
        return RBError(f"{iid} must wait for: {', '.join(ids)}", "waiting", id=iid, ids=ids, reasons=reasons)
    other = [x["id"] for x in reasons if x["code"] == "conflict"]
    return RBError(f"refused: {other[0]} conflicts on '{r['why']}'; wait for it to finish, or ask the person", "conflict", id=iid, ids=other, reasons=reasons)


def op_seen(d: dict, lane_id: str, by: str, state: str, task: str = "", reason: str = "") -> None:
    """Record that the session of a lane is alive and what it is doing (idle, waiting, working). No event is logged."""
    lane = next((l for l in d["lanes"] if l["id"] == lane_id), None)
    if lane is None or state not in PRESENCE_STATES:
        return
    lane["seen"] = {"at": now_iso(), "by": by, "state": state, "task": task, "reason": reason[:200]}


def op_claim(d: dict, actor: Actor, iid: str, force: bool = False, lease_minutes: int = 0) -> list:
    it = get_item(d, iid)
    if it["status"] not in ("queued", "sent") and not force:
        who = ""
        if it["status"] == "running" and it.get("claim"):
            cl = it["claim"]
            who = f" Held by {cl.get('by', '?')} since {cl.get('at', '?')[:16].replace('T', ' ')} UTC. If that was you before an interruption, do not start: tell the person to press 'Return to queue' in the panel (or run `release {iid}`), then claim again."
        raise RBError(f"{iid} is {it['status']} and cannot be claimed (only queued or sent tasks can).{who}", "wrong_state", id=iid, status=it["status"])
    comp = {c["id"]: c for c in compute(d)}[iid]
    if not force:
        # a task the person already sent to a session (status `sent`) only warns about conflicts, as before; time and dependencies still count
        refusal = refusal_for(iid, [r for r in comp["reasons"] if not (it["status"] == "sent" and r["code"] == "conflict")])
        if refusal:
            raise refusal
    warnings = []
    for c in comp["conflicts"]:
        other = get_item(d, c["id"])
        msg = f"{c['id']} ({other['status']}) conflicts on '{c['why']}'"
        if it["status"] == "sent" or force:
            warnings.append(msg)
    for other in d["items"]:
        cl = other.get("claim") or {}
        if other["status"] == "running" and other["id"] != iid and other["lane"] != it["lane"] and cl.get("by") == actor.name:
            warnings.append(f"{actor.name} already holds {other['id']} in lane {other['lane']}: if this is another window, give each window its own name (--by) so that Barid can tell them apart")
            break
    hours = float(d.get("policy", {}).get("lease_hours", 12))
    timebox = int(it.get("timebox") or 0)
    lease = dt.timedelta(minutes=lease_minutes) if lease_minutes else dt.timedelta(hours=max(hours, (timebox + 60) / 60 if timebox else 0))
    it["status"] = "running"
    it.pop("outcome", None)
    it["claim"] = {"by": actor.name, "at": now_iso(), "lease_until": (now_dt() + lease).isoformat(), "signal": now_iso()}
    if timebox:
        it["claim"]["deadline"] = (now_dt() + dt.timedelta(minutes=timebox)).isoformat()
    it["claim"].update(snapshot_for_claim(d, it))
    op_seen(d, it["lane"], actor.name, "working", iid)
    it.pop("violations", None)
    it.pop("changed", None)
    it["updated"] = now_iso()
    log_event(d, actor, "claim", iid, "; ".join(warnings))
    return warnings


def _owner_check(it: dict, actor: Actor, force: bool, verb: str) -> None:
    claim = it.get("claim") or {}
    if actor.human or force or not claim or claim.get("by") == actor.name:
        return
    raise RBError(f"{it['id']} is held by {claim.get('by')!r}, you ({actor.name!r}) cannot {verb} it")


def op_finish(d: dict, actor: Actor, iid: str, report: str = "", note: str = "", force: bool = False, outcome: str = "") -> dict:
    it = get_item(d, iid)
    if it["status"] != "running" and not force:
        raise RBError(f"{iid} is {it['status']}: claim it first", "wrong_state", id=iid, status=it["status"])
    _owner_check(it, actor, force, "finish")
    if not report and not force:
        raise RBError("a report path is required (use --report PATH, or --no-report if there is none)")
    if outcome and outcome not in OUTCOMES:
        raise RBError(f"outcome must be one of: {', '.join(OUTCOMES)}", "bad_outcome")
    violations, changed = verify_at_finish(d, it)
    it["violations"] = violations
    if changed is not None:
        it["changed"] = changed
    run = _close_run(it)
    it["ran_with"] = overlap_with(d, it, run)
    it["disturbed_by"] = []
    if effective(d, it)["quiet"]:
        it["disturbed_by"] = [{"id": o["id"], "why": "load" if o["noisy"] else "unknown"} for o in it["ran_with"] if o["noisy"] or not o["described"]]
    it["status"] = "review"
    it["outcome"] = outcome
    it["report"] = report or it.get("report", "")
    it["claim"] = None
    op_seen(d, it["lane"], actor.name, "idle", "", f"finished {iid}")
    it["updated"] = now_iso()
    if note:
        it["notes"].append({"t": now_iso(), "by": actor.name, "text": note})
    log_event(d, actor, "finish", iid, f"{outcome or 'no outcome stated'}: {report}" + (f" ({len(violations)} file warnings)" if violations else "")
              + (f" (ran next to a loaded or undescribed task: {', '.join(x['id'] for x in it['disturbed_by'])})" if it["disturbed_by"] else ""))
    return it


def op_outcome(d: dict, actor: Actor, iid: str, outcome: str) -> dict:
    """Correct how a finished task ended (the person, or a trusted planner who read the report)."""
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can change the outcome of a report", "human_only")
    if outcome not in OUTCOMES:
        raise RBError(f"outcome must be one of: {', '.join(OUTCOMES)}", "bad_outcome")
    it = get_item(d, iid)
    if it["status"] not in ("review", "done"):
        raise RBError(f"{iid} is {it['status']}: only reported tasks have an outcome", "wrong_state", id=iid, status=it["status"])
    it["outcome"] = outcome
    it["updated"] = now_iso()
    log_event(d, actor, "outcome", iid, outcome)
    return it


def op_heartbeat(d: dict, actor: Actor, iid: str, lease_minutes: int = 0) -> dict:
    """Extend the lease of a running task and mark a sign of life (no event is logged: a worker calls this every half minute)."""
    it = get_item(d, iid)
    if it["status"] != "running":
        raise RBError(f"{iid} is not running")
    _owner_check(it, actor, False, "extend")
    hours = float(d.get("policy", {}).get("lease_hours", 12))
    timebox = int(it.get("timebox") or 0)
    minutes = lease_minutes if lease_minutes else int(max(hours, (timebox + 60) / 60 if timebox else 0) * 60)
    it["claim"]["lease_until"] = (now_dt() + dt.timedelta(minutes=minutes)).isoformat()
    it["claim"]["signal"] = now_iso()
    op_seen(d, it["lane"], actor.name, "working", iid)
    return it


def op_release(d: dict, actor: Actor, iid: str, force: bool = False) -> dict:
    it = get_item(d, iid)
    if it["status"] not in ("running", "sent"):
        raise RBError(f"{iid} is {it['status']}: nothing to release", "wrong_state", id=iid, status=it["status"])
    _owner_check(it, actor, force, "release")
    _close_run(it)
    it["status"] = "queued"
    it["claim"] = None
    it["updated"] = now_iso()
    log_event(d, actor, "release", iid)
    return it


def op_accept(d: dict, actor: Actor, iid: str) -> dict:
    require_human(actor, "accept a report")
    it = get_item(d, iid)
    if it["status"] != "review":
        raise RBError(f"{iid} is {it['status']}, not waiting for review", "wrong_state", id=iid, status=it["status"])
    it["status"] = "done"
    it["updated"] = now_iso()
    log_event(d, actor, "accept", iid)
    return it


def op_note(d: dict, actor: Actor, iid: str, text: str) -> dict:
    it = get_item(d, iid)
    text = (text or "").strip()
    if not text:
        raise RBError("empty note", "empty_note")
    it["notes"].append({"t": now_iso(), "by": actor.name, "text": text[:2000]})
    it["updated"] = now_iso()
    if it["status"] == "running" and it.get("claim"):
        it["claim"]["signal"] = now_iso()
    log_event(d, actor, "note", iid)
    return it


def op_move(d: dict, actor: Actor, iid: str, position: int) -> dict:
    """Change the priority of a task: the plan fills steps in list order, so earlier tasks get the earlier slots."""
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can reorder tasks")
    it = get_item(d, iid)
    if not 1 <= position <= len(d["items"]):
        raise RBError(f"position must be between 1 and {len(d['items'])}")
    d["items"].remove(it)
    d["items"].insert(position - 1, it)
    log_event(d, actor, "move", iid, str(position))
    return it


def op_protect(d: dict, actor: Actor, action: str, path: str = "") -> list:
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can change the protected paths", "human_only")
    lst = d.setdefault("protect", [])
    if action == "add" and path and path not in lst:
        lst.append(path)
    elif action == "remove" and path in lst:
        lst.remove(path)
    log_event(d, actor, "protect", path, action)
    return lst


def op_lane_add(d: dict, actor: Actor, lane_id: str, title=None, color=None, agent=None) -> dict:
    """A new lane (session). Same rights as editing one."""
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can change lanes", "human_only")
    if not ID_RE.match(lane_id or ""):
        raise RBError("bad lane id", "bad_id")
    if any(l["id"] == lane_id for l in d["lanes"]):
        raise RBError("lane exists (use `barid lane edit` to change it)", "exists", id=lane_id)
    d["lanes"].append({"id": lane_id, "title": (title or "").strip()[:60] or lane_id})
    log_event(d, actor, "lane", lane_id, "add")
    return op_lane_edit(d, actor, lane_id, None, color, agent)


def op_board_lang(d: dict, actor: Actor, lang: str) -> str:
    """Language of the texts Barid writes for agents (footers, connection text, handoff). Not the panel language."""
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can change the board language", "human_only")
    lang = "kk" if lang == "kz" else lang
    if lang not in ("en", "ru", "kk"):
        raise RBError("language must be en, ru or kk", "bad_lang")
    d["lang"] = lang
    log_event(d, actor, "lang", "", lang)
    return lang


def op_profile_add(d: dict, actor: Actor, name: str, uses=None, quiet=False, noisy=False, label="") -> dict:
    """A board's own profile: a named bundle of resources and quiet/noisy marks."""
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can change profiles", "human_only")
    if not ID_RE.match(name or ""):
        raise RBError("bad profile name", "bad_id")
    if name in BUILTIN_PROFILES:
        raise RBError(f"{name!r} is a built-in profile", "exists", id=name)
    uses = split_list(uses)
    check_refs(d, None, None, uses)
    d.setdefault("profiles", {})[name] = {"label": (label or name)[:80], "uses": uses, "quiet": bool(quiet), "noisy": bool(noisy)}
    log_event(d, actor, "profile", name, "add")
    return d["profiles"][name]


def op_profile_rm(d: dict, actor: Actor, name: str) -> None:
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can change profiles", "human_only")
    if name not in d.get("profiles", {}):
        raise RBError(f"no such custom profile: {name}", "unknown_profile", name=name)
    users = [i["id"] for i in d["items"] if i.get("profile") == name and i["status"] not in ("done", "cancelled")]
    if users:
        raise RBError(f"profile {name} is still used by {', '.join(users)}", "wrong_state", id=name, status="in use")
    del d["profiles"][name]
    log_event(d, actor, "profile", name, "remove")


def op_lane_edit(d: dict, actor: Actor, lane_id: str, title=None, color=None, agent=None, unattended=None) -> dict:
    """Name, colour and agent kind of a lane (a session). Colour '' or agent '' reset to the default."""
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can change lanes", "human_only")
    lane = next((l for l in d["lanes"] if l["id"] == lane_id), None)
    if lane is None:
        raise RBError(f"unknown lane {lane_id!r}", "unknown_lane", lane=lane_id)
    if color is not None:
        if color and not COLOR_RE.match(color):
            raise RBError("color must look like #feb83b", "bad_color")
        lane["color"] = color.lower()
    if agent is not None:
        if not AGENT_RE.match(agent):
            raise RBError("agent: letters, digits, space, '.', '_', '+', '-', up to 24 characters", "bad_agent")
        lane["agent"] = agent
    if title is not None and title.strip():
        lane["title"] = title.strip()[:60]
    if unattended is not None:
        lane["unattended"] = bool(unattended)
    log_event(d, actor, "lane", lane_id, "edit")
    return lane


def op_purge(d: dict, actor: Actor, iid: str) -> None:
    require_human(actor, "purge a task for good")
    it = get_item(d, iid)
    if any(iid in deps_of(i) for i in d["items"] if i["id"] != iid):
        raise RBError(f"other tasks still need {iid}")
    d["items"].remove(it)
    log_event(d, actor, "purge", iid)


# ----------------------------------------------------------------------------- prompts for agents

FOOTER = {
    "en": textwrap.dedent("""\
        ---
        Barid tracking: this task ({id}) lives on a shared board. Use Python 3, nothing to install.
        1. Before you start:  {py} "{rb}" --board "{board}" claim {id} --by "<your agent name>"
           {claim_note}
        2. When you stop and your report is written:
           {py} "{rb}" --board "{board}" finish {id} --report "<path of your report>" --outcome <complete|partial|failed> --by "<your agent name>"
           Be honest about the outcome: complete = every acceptance check was met and nothing is left undone; partial = you stopped early
           (a safety limit, a deadline, an error) or some parts were not done; failed = the main goal was not reached. Only "complete" lets
           dependent tasks start by themselves.
        3. Optional progress note:  {py} "{rb}" --board "{board}" note {id} "<text>" --by "<your agent name>"
        Touch only task {id}: never edit, cancel or delete other tasks. To suggest a follow-up task:
        {py} "{rb}" --board "{board}" add <NEWID> --lane <lane> --title "<title>" --text-file <file> --by "<your agent name>"
        (it is saved as a proposal that the user approves)."""),
    "ru": textwrap.dedent("""\
        ---
        Учёт в Barid: эта задача ({id}) лежит на общей доске. Нужен Python 3, ничего ставить не надо.
        1. Перед началом работы:  {py} "{rb}" --board "{board}" claim {id} --by "<имя твоего агента>"
           {claim_note}
        2. Когда остановился и записал отчёт:
           {py} "{rb}" --board "{board}" finish {id} --report "<путь к твоему отчёту>" --outcome <complete|partial|failed> --by "<имя твоего агента>"
           Честно оцени итог: complete = все критерии приёмки выполнены и ничего не осталось; partial = остановился раньше (порог безопасности,
           срок, ошибка) или часть не сделана; failed = главная цель не достигнута. Только complete сам запускает зависимые задачи.
        3. Заметка по ходу работы (необязательно):  {py} "{rb}" --board "{board}" note {id} "<текст>" --by "<имя твоего агента>"
        Трогай только задачу {id}: чужие задачи не меняй, не отменяй и не удаляй. Чтобы предложить следующую задачу:
        {py} "{rb}" --board "{board}" add <НОВЫЙ_ID> --lane <дорожка> --title "<заголовок>" --text-file <файл> --by "<имя твоего агента>"
        (она сохранится как предложение, пользователь его одобрит)."""),
    "kk": textwrap.dedent("""\
        ---
        Barid есебі: бұл тапсырма ({id}) ортақ тақтада тұр. Python 3 керек, ештеңе орнату қажет емес.
        1. Жұмысты бастамас бұрын:  {py} "{rb}" --board "{board}" claim {id} --by "<агентіңнің аты>"
           {claim_note}
        2. Тоқтап, есебіңді жазғаннан кейін:
           {py} "{rb}" --board "{board}" finish {id} --report "<есебіңнің жолы>" --outcome <complete|partial|failed> --by "<агентіңнің аты>"
           Нәтижеңді адал бағала: complete = барлық қабылдау тексерулері орындалды және ештеңе қалмады; partial = ерте тоқтадың
           (қауіпсіздік шегі, мерзім, қате) немесе бір бөлігі жасалмады; failed = негізгі мақсатқа жетпедің. Тек complete ғана
           тәуелді тапсырмаларды өздігінен іске қосады.
        3. Жұмыс барысындағы жазба (міндетті емес):  {py} "{rb}" --board "{board}" note {id} "<мәтін>" --by "<агентіңнің аты>"
        Тек {id} тапсырмасына тиіс: басқа тапсырмаларды өзгертпе, болдырма және жойма. Келесі тапсырманы ұсыну үшін:
        {py} "{rb}" --board "{board}" add <ЖАҢА_ID> --lane <сессия> --title "<атауы>" --text-file <файл> --by "<агентіңнің аты>"
        (ол ұсыныс болып сақталады, пайдаланушы мақұлдайды)."""),
}

GEN = {
    "en": textwrap.dedent("""\
        Write the complete, self-contained prompt for task {id} ("{title}") of the Barid project "{project}".
        The prompt will be pasted into another AI agent session ("{lane}") that has none of our conversation, so it must say everything.

        Goal / outline of the task:
        {outline}

        Facts about this task: lane {lane}; depends on: {needs}; uses resources: {uses}; quiet CPU needed: {quiet}; creates noise: {noisy}.
        {reports}{context}
        Follow this skeleton (keep only the sections that apply):
        {template}

        Rules for the text: concrete steps, exact file paths and commands, a hard deadline, measurable acceptance criteria,
        what must NOT be touched, and where to write the report. Do not add the Barid tracking footer: the board appends it by itself.

        When it is written, save it to a file and store it with:
        {py} "{rb}" --board "{board}" edit {id} --text-file <that file> --by "<your agent name>"
        """),
    "ru": textwrap.dedent("""\
        Напиши полный самодостаточный промпт для задачи {id} («{title}») проекта «{project}» на Barid.
        Промпт вставят в другую сессию ИИ-агента («{lane}»), которая не видела нашего разговора, поэтому в нём должно быть всё.

        Цель и набросок задачи:
        {outline}

        Факты о задаче: дорожка {lane}; зависит от: {needs}; ресурсы: {uses}; нужна тишина процессора: {quiet}; создаёт нагрузку: {noisy}.
        {reports}{context}
        Следуй этому каркасу (оставь только подходящие разделы):
        {template}

        Требования к тексту: конкретные шаги, точные пути и команды, жёсткий срок, измеримые критерии приёмки,
        что трогать НЕЛЬЗЯ и куда писать отчёт. Не добавляй хвост про учёт в Barid: доска добавит его сама.

        Когда текст готов, сохрани его в файл и запиши командой:
        {py} "{rb}" --board "{board}" edit {id} --text-file <этот файл> --by "<имя твоего агента>"
        """),
    "kk": textwrap.dedent("""\
        Barid жобасының "{project}" тапсырмасына {id} ("{title}") толық, өздігінен жеткілікті промпт жаз.
        Промпт біздің әңгімені көрмеген басқа ЖИ-агент сессиясына ("{lane}") қойылады, сондықтан онда бәрі жазылуы керек.

        Тапсырманың мақсаты және қысқаша мазмұны:
        {outline}

        Тапсырма туралы деректер: сессия {lane}; тәуелді: {needs}; ресурстар: {uses}; процессордың тыныштығы керек: {quiet}; жүктеме жасайды: {noisy}.
        {reports}{context}
        Мына қаңқаны ұста (тек сәйкес бөлімдерді қалдыр):
        {template}

        Мәтінге қойылатын талаптар: нақты қадамдар, нақты жолдар мен командалар, қатаң мерзім, өлшенетін қабылдау
        критерийлері, нені ТҮРТПЕУ керек және есепті қайда жазу керек. Barid есебі туралы соңғы бөлікті қоспа: тақта оны өзі қосады.

        Мәтін дайын болғанда, оны файлға сақтап, мына командамен жаз:
        {py} "{rb}" --board "{board}" edit {id} --text-file <сол файл> --by "<агентіңнің аты>"
        """),
}

DEFAULT_TEMPLATE = textwrap.dedent("""\
    Task: <one-line goal>. Hard deadline: <time from start>; at the deadline stop and report what is not done.
    Safety and limits: <what must never happen; resource limits; what not to touch>.
    Context: <files, branches, earlier reports to read first>.
    Steps: <numbered, concrete, with exact commands and paths>.
    Acceptance: <measurable checks>.
    Report: <path and what it must contain, including what was not done>.""")


def py_cmd() -> str:
    return "python" if os.name == "nt" else "python3"


SCOPE_FOOTER = {
    "en": ("Files and safety (set by the board):", "- Change only these files and folders: {touches}", "- Never change these protected paths: {protect}",
           "- Work only in {workdir} (branch {branch}); do not touch other checkouts.",
           "At the end Barid compares the files and flags anything outside this list for the person."),
    "ru": ("Файлы и безопасность (задано доской):", "- Меняй только эти файлы и папки: {touches}", "- Никогда не меняй защищённые пути: {protect}",
           "- Работай только в {workdir} (ветка {branch}); другие копии проекта не трогай.",
           "В конце Barid сверит файлы и отметит для человека всё, что вне этого списка."),
    "kk": ("Файлдар және қауіпсіздік (тақта белгілеген):", "- Тек мына файлдар мен қалталарды өзгерт: {touches}", "- Мына қорғалған жолдарды ешқашан өзгертпе: {protect}",
           "- Тек {workdir} ішінде жұмыс істе (тармақ {branch}); жобаның басқа көшірмелеріне тиіспе.",
           "Соңында Barid файлдарды салыстырып, осы тізімнен тыс нәрсенің бәрін адамға белгілейді."),
}


def scope_footer(d: dict, it: dict) -> str:
    head, t_touch, t_prot, t_work, tail = SCOPE_FOOTER.get(d.get("lang", "en"), SCOPE_FOOTER["en"])
    lines = []
    if it.get("touches"):
        lines.append(t_touch.format(touches=", ".join(it["touches"])))
    if d.get("protect"):
        lines.append(t_prot.format(protect=", ".join(d["protect"])))
    if it.get("workdir"):
        lines.append(t_work.format(workdir=it["workdir"], branch=it.get("branch") or "-"))
    return "\n".join([head, *lines, tail]) if lines else ""


FOOTER_WORKER = {
    "en": textwrap.dedent("""\
        ---
        Barid tracking: this task ({id}) is already claimed for you by the worker "{name}"; do not claim it again. Use Python 3.
        When you stop and your report is written:
        {py} "{rb}" --board "{board}" finish {id} --report "<path of your report>" --outcome <complete|partial|failed> --by "{name}"
        Be honest about the outcome: complete = every acceptance check was met and nothing is left undone; partial = you stopped early or some
        parts were not done; failed = the main goal was not reached. Progress note (optional):  {py} "{rb}" --board "{board}" note {id} "<text>" --by "{name}"
        You run unattended: nobody can answer questions. Decide by this task's own rules and write your decisions into the report.
        Touch only task {id}: never edit, cancel or delete other tasks."""),
    "ru": textwrap.dedent("""\
        ---
        Учёт в Barid: эта задача ({id}) уже взята для тебя исполнителем "{name}"; повторно её не бери. Нужен Python 3.
        Когда остановился и записал отчёт:
        {py} "{rb}" --board "{board}" finish {id} --report "<путь к твоему отчёту>" --outcome <complete|partial|failed> --by "{name}"
        Честно оцени итог: complete = все критерии приёмки выполнены и ничего не осталось; partial = остановился раньше или часть не сделана;
        failed = главная цель не достигнута. Заметка по ходу (необязательно):  {py} "{rb}" --board "{board}" note {id} "<текст>" --by "{name}"
        Ты работаешь без присмотра: отвечать на вопросы некому. Решай по правилам самой задачи и записывай решения в отчёт.
        Трогай только задачу {id}: чужие задачи не меняй, не отменяй и не удаляй."""),
    "kk": textwrap.dedent("""\
        ---
        Barid есебі: бұл тапсырманы ({id}) сен үшін "{name}" орындаушысы бұрын алды; қайта алма. Python 3 керек.
        Тоқтап, есебін жазғаннан кейін:
        {py} "{rb}" --board "{board}" finish {id} --report "<есептің жолы>" --outcome <complete|partial|failed> --by "{name}"
        Нәтижені адал бағала: complete = барлық қабылдау критерийлері орындалды; partial = ерте тоқтадың немесе бір бөлігі орындалмады;
        failed = негізгі мақсатқа жетпедің. Жолда жазба (міндетті емес):  {py} "{rb}" --board "{board}" note {id} "<мәтін>" --by "{name}"
        Сен қараусыз жұмыс істейсің: сұраққа жауап беретін ешкім жоқ. Тапсырманың өз ережелерімен шеш те, шешімдерді есепке жаз.
        Тек {id} тапсырмасына тиіс: басқа тапсырмаларды өзгертпе, болдырма және жойма."""),
}


CLAIM_NOTE = {
    "en": ("If this is refused, STOP and tell the user why (exit code 3 means it only has to wait: a start time, another task or a conflict).",
           "If this is refused with exit code 3 it only has to wait (a start time, another task, a conflict): run it again with --wait 300 until it passes, up to 14 hours, and ask nobody. Any other refusal: STOP and tell the user why."),
    "ru": ("Если команда отказала, ОСТАНОВИСЬ и сообщи пользователю причину (код 3 значит, что надо лишь подождать: время старта, другая задача или конфликт).",
           "Если отказ с кодом 3, надо лишь подождать (время старта, другая задача, конфликт): повторяй команду с --wait 300, пока не пройдёт, до 14 часов, никого не спрашивая. Любой другой отказ: ОСТАНОВИСЬ и сообщи пользователю причину."),
    "kk": ("Команда бас тартса, ТОҚТА да, себебін пайдаланушыға айт (3 коды тек күту керек екенін білдіреді: бастау уақыты, басқа тапсырма немесе қақтығыс).",
           "3 кодымен бас тартса, тек күту керек (бастау уақыты, басқа тапсырма, қақтығыс): команданы --wait 300 арқылы өтетінше, 14 сағатқа дейін, ешкімнен сұрамай қайтала. Кез келген басқа бас тарту: ТОҚТА да, себебін пайдаланушыға айт."),
}


def footer_for(d: dict, it: dict, board: Path, worker: str = "") -> str:
    lang = d.get("lang", "en")
    tpl = (FOOTER_WORKER if worker else FOOTER).get(lang, (FOOTER_WORKER if worker else FOOTER)["en"])
    lane = next((l for l in d["lanes"] if l["id"] == it.get("lane")), {})
    note = CLAIM_NOTE.get(lang, CLAIM_NOTE["en"])[1 if lane.get("unattended") else 0]
    base = tpl.format(id=it["id"], py=py_cmd(), rb=str(Path(__file__).resolve()), board=str(board), name=worker, claim_note=note)
    extra = scope_footer(d, it)
    return base + ("\n" + extra if extra else "")


CONNECT = {
    "en": textwrap.dedent("""\
        You are the session "{title}" (lane `{lane}`{agent}) of the project "{project}". Your prompts come from a shared Barid board. Python 3 is all you need.
        How to work:
        1. Ask the board for your next task:  {py} "{rb}" --board "{board}" next --lane {lane}
           Exit code 0: it printed a task prompt that ends with the commands to claim and finish that task. Do exactly what the prompt says.
           Exit code 2: nothing is queued for you. Exit code 3: nothing can start yet (a start time, another session's task, a conflict). Exit code 4: it waits for the person.
           In the last three cases tell the user what the output says and stop. Do not invent work.
        2. When you have finished a task and written its report, run the command above again.
        Rules: work only on tasks of lane {lane}; never use --human or --force and never edit board.json by hand; if a command is refused, stop and tell the user why."""),
    "ru": textwrap.dedent("""\
        Ты сессия "{title}" (линия `{lane}`{agent}) проекта "{project}". Твои промпты приходят с общей доски Barid. Нужен только Python 3.
        Как работать:
        1. Спроси у доски следующую задачу:  {py} "{rb}" --board "{board}" next --lane {lane}
           Код 0: она напечатала промпт задачи, в конце которого команды, чтобы взять и закончить эту задачу. Делай ровно то, что написано в промпте.
           Код 2: для тебя ничего нет в очереди. Код 3: пока ничего нельзя начать (время старта, задача другой сессии, конфликт). Код 4: доска ждёт решения человека.
           В последних трёх случаях скажи пользователю, что написано в выводе, и остановись. Не придумывай работу.
        2. Когда закончил задачу и записал отчёт, снова запусти команду выше.
        Правила: работай только с задачами линии {lane}; не используй --human и --force и не правь board.json руками; если команда отказала, остановись и сообщи пользователю причину."""),
    "kk": textwrap.dedent("""\
        Сен "{project}" жобасының "{title}" сессиясысың (id `{lane}`{agent}). Саған арналған промпттар ортақ Barid тақтасынан келеді. Тек Python 3 керек.
        Қалай жұмыс істеу керек:
        1. Тақтадан келесі тапсырмаңды сұра:  {py} "{rb}" --board "{board}" next --lane {lane}
           Код 0: ол тапсырма промптын басып шығарды, оның соңында осы тапсырманы алу және аяқтау командалары бар. Промпттағыны дәл орында.
           Код 2: саған кезекте ештеңе жоқ. Код 3: әзірге ештеңені бастауға болмайды (бастау уақыты, басқа сессияның тапсырмасы, қақтығыс). Код 4: тақта адамның шешімін күтеді.
           Соңғы үш жағдайда шығыста не жазылғанын пайдаланушыға айт та, тоқта. Жұмыс ойлап таппа.
        2. Тапсырманы аяқтап, есебін жазғаннан кейін, жоғарыдағы команданы қайта іске қос.
        Ережелер: тек `{lane}` сессиясының тапсырмаларымен жұмыс істе; --human мен --force пайдаланба және board.json файлын қолмен өзгертпе; команда бас тартса, тоқта да, себебін пайдаланушыға айт."""),
}

# For a session that works through the night: it waits for its turn instead of stopping, and asks nothing.
CONNECT_UNATTENDED = {
    "en": textwrap.dedent("""\
        You are the session "{title}" (lane `{lane}`{agent}) of the project "{project}". A shared Barid board feeds you; you work through your lane on your own, nobody is watching, and you do not stop until the lane is empty. Python 3 is all you need.
        Loop:
        1. Run:  {py} "{rb}" --board "{board}" next --lane {lane} --wait 300
           - exit code 0: it printed a task prompt that ends with the commands to claim and finish that task. Do exactly what the prompt says, write the report, run `finish`, then go back to step 1.
           - exit code 3: nothing can start yet, but it will without anyone's help (a start time, another session's task, the GPU). That is waiting, not an error: run the same command again. Repeat for as long as it takes (up to 14 hours) and ask the user nothing.
           - exit code 2: your lane has nothing left. Say so in one sentence and stop.
           - exit code 4: the board needs the person (a report to accept, a prompt to write). Say what the output says and stop.
        2. A claim that is refused with exit code 3 is waiting too: repeat it with --wait 300. Any other refusal or error: stop and tell the user why.
        Rules: never ask the user whether to continue, decide by the task's own rules and write the decision into the report; work only on tasks of lane {lane}; never use --human or --force and never edit board.json by hand."""),
    "ru": textwrap.dedent("""\
        Ты сессия "{title}" (линия `{lane}`{agent}) проекта "{project}". Тебя кормит общая доска Barid; ты сам проходишь свою линию, за тобой никто не следит, и ты не останавливаешься, пока линия не опустеет. Нужен только Python 3.
        Цикл:
        1. Запусти:  {py} "{rb}" --board "{board}" next --lane {lane} --wait 300
           - код 0: напечатан промпт задачи, в конце которого команды, чтобы взять и закончить её. Делай ровно то, что в промпте, запиши отчёт, выполни `finish` и вернись к шагу 1.
           - код 3: пока ничего нельзя начать, но это пройдёт само (время старта, задача другой сессии, видеокарта). Это ожидание, а не ошибка: запусти ту же команду снова. Повторяй сколько нужно (до 14 часов) и ничего не спрашивай у пользователя.
           - код 2: в твоей линии больше ничего нет. Скажи это одним предложением и остановись.
           - код 4: доске нужен человек (принять отчёт, написать промпт). Скажи, что написано в выводе, и остановись.
        2. Отказ в claim с кодом 3 тоже ожидание: повтори его с --wait 300. Любой другой отказ или ошибка: остановись и скажи пользователю причину.
        Правила: не спрашивай пользователя, продолжать ли: решай по правилам самой задачи и записывай решение в отчёт; работай только с задачами линии {lane}; не используй --human и --force и не правь board.json руками."""),
    "kk": textwrap.dedent("""\
        Сен "{project}" жобасының "{title}" сессиясысың (id `{lane}`{agent}). Сені ортақ Barid тақтасы қоректендіреді; сен өз жолыңды өзің жүресің, саған ешкім қарамайды, жол бос болғанша тоқтамайсың. Тек Python 3 керек.
        Цикл:
        1. Іске қос:  {py} "{rb}" --board "{board}" next --lane {lane} --wait 300
           - код 0: тапсырма промпты басылды, оның соңында тапсырманы алу және аяқтау командалары бар. Промпттағыны дәл орында, есепті жаз, `finish` орында да, 1-қадамға оралу.
           - код 3: әзірге ештеңені бастауға болмайды, бірақ бұл өздігінен өтеді (бастау уақыты, басқа сессияның тапсырмасы, бейнекарта). Бұл күту, қате емес: сол команданы қайта іске қос. Қажет болғанша қайтала (14 сағатқа дейін), пайдаланушыдан ештеңе сұрама.
           - код 2: сенің жолыңда ештеңе қалмады. Мұны бір сөйлеммен айтып, тоқта.
           - код 4: тақтаға адам керек (есепті қабылдау, промпт жазу). Шығыста не жазылғанын айтып, тоқта.
        2. claim кодпен 3 бас тартса, бұл да күту: оны --wait 300 арқылы қайтала. Кез келген басқа бас тарту немесе қате: тоқта да, себебін пайдаланушыға айт.
        Ережелер: жалғастыру керек пе деп пайдаланушыдан сұрама: тапсырманың өз ережелерімен шеш де, шешімді есепке жаз; тек `{lane}` сессиясының тапсырмаларымен жұмыс істе; --human мен --force пайдаланба және board.json файлын қолмен өзгертпе."""),
}


def connect_text(d: dict, lane_id: str, board: Path, unattended=None) -> str:
    """The first message to paste into an agent session so that it takes its tasks from this lane. For a lane marked unattended
    (or with unattended=True) the session waits for its turn and works through the lane without stopping."""
    lane = next((l for l in d["lanes"] if l["id"] == lane_id), None)
    if lane is None:
        raise RBError(f"unknown lane {lane_id!r}", "unknown_lane", lane=lane_id)
    lang = d.get("lang", "en")
    agent = lane.get("agent", "")
    texts = CONNECT_UNATTENDED if (lane.get("unattended") if unattended is None else unattended) else CONNECT
    text = texts.get(lang, texts["en"]).format(title=lane.get("title") or lane_id, lane=lane_id, agent=(", " + agent) if agent else "",
                                                   project=d.get("project", ""), py=py_cmd(), rb=str(Path(__file__).resolve()), board=str(board))
    hint = agent_hint(d, {"lane": lane_id})
    return text + ("\n" + hint if hint else "")


HANDOFF = {
    "en": {"head": "Handoff from the tasks this one builds on (read it first):", "outcome": "outcome", "report": "report", "by": "done by", "branch": "branch",
           "files": "files changed", "note": "note", "unstated": "not stated"},
    "ru": {"head": "Передача от задач, на которых строится эта (прочитай сначала):", "outcome": "итог", "report": "отчёт", "by": "делал", "branch": "ветка",
           "files": "изменено файлов", "note": "заметка", "unstated": "не указан"},
    "kk": {"head": "Бұл тапсырма сүйенетін тапсырмалардан келген табыс (алдымен оқы):", "outcome": "нәтиже", "report": "есеп", "by": "орындаған", "branch": "тармақ",
           "files": "өзгерген файлдар", "note": "жазба", "unstated": "көрсетілмеген"},
}

AGENT_HINT = {
    "en": "You may be a smaller or local model: work through the steps one at a time, run the commands exactly as written, and say so in your report if a step is unclear instead of guessing.",
    "ru": "Ты можешь быть небольшой или локальной моделью: выполняй шаги по одному, запускай команды ровно как написано, а если шаг неясен, скажи об этом в отчёте, а не угадывай.",
    "kk": "Сен шағын немесе жергілікті модель болуың мүмкін: қадамдарды бір-бірлеп орында, командаларды дәл жазылғандай іске қос, ал қадам түсініксіз болса, болжамай, есепте солай деп жаз.",
}


def handoff_text(d: dict, it: dict) -> str:
    """What the finished tasks this one needs hand over: outcome, report, who did it, branch, changed files, last notes."""
    if not d.get("policy", {}).get("handoff", True):
        return ""
    tx = HANDOFF.get(d.get("lang", "en"), HANDOFF["en"])
    byid = {i["id"]: i for i in d["items"]}
    lanes = {l["id"]: l for l in d["lanes"]}
    blocks = []
    for nid in deps_of(it):
        n = byid.get(nid)
        if not n or n["status"] not in ("review", "done"):
            continue
        lane = lanes.get(n["lane"], {})
        who = lane.get("title") or n["lane"]
        if lane.get("agent"):
            who += f" ({lane['agent']})"
        lines = [f"- {n['id']} {n['title']}: {tx['outcome']} {n.get('outcome') or tx['unstated']}, {tx['by']} {who}"]
        if n.get("report"):
            lines.append(f"    {tx['report']}: {n['report']}")
        if n.get("branch"):
            lines.append(f"    {tx['branch']}: {n['branch']}" + (f" ({n['workdir']})" if n.get("workdir") else ""))
        ch = n.get("changed")
        if ch and ch.get("count"):
            lines.append(f"    {tx['files']}: {ch['count']}: " + ", ".join(ch.get("files", [])[:8]) + (" ..." if ch["count"] > 8 else ""))
        for note in n.get("notes", [])[-2:]:
            lines.append(f"    {tx['note']}: {note['text'][:300]}")
        blocks.append("\n".join(lines))
    return (tx["head"] + "\n" + "\n".join(blocks)) if blocks else ""


def agent_hint(d: dict, it: dict) -> str:
    lane = next((l for l in d["lanes"] if l["id"] == it["lane"]), {})
    if str(lane.get("agent", "")).lower() in ("local", "local model", "llama", "qwen", "ollama"):
        return AGENT_HINT.get(d.get("lang", "en"), AGENT_HINT["en"])
    return ""


def prompt_for(d: dict, it: dict, board: Path, worker: str = "") -> str:
    text = it.get("text", "").strip()
    if not text:
        return ""
    extra = [x for x in (handoff_text(d, it), agent_hint(d, it)) if x]
    if extra:
        text = text + "\n\n" + "\n\n".join(extra)
    if d.get("policy", {}).get("footer", True):
        return text + "\n\n" + footer_for(d, it, board, worker)
    return text


def template_text(name: str = "task") -> str:
    f = HERE.parent / "templates" / f"{name}.md"
    return f.read_text("utf-8").strip() if f.is_file() else DEFAULT_TEMPLATE


def gen_request(d: dict, it: dict, board: Path) -> str:
    lang = d.get("lang", "en")
    byid = {i["id"]: i for i in d["items"]}
    reports = []
    for n in deps_of(it):
        r = byid.get(n, {}).get("report")
        if r:
            reports.append(f"- {n}: {r}")
    ctx = d.get("context", [])
    head_r = ("Reports of the tasks this one depends on (read them first):\n" if lang == "en" else "Бұл тапсырма тәуелді тапсырмалардың есептері (алдымен оқы):\n" if lang == "kk" else "Отчёты задач, от которых зависит эта (прочитай сначала):\n")
    head_c = ("Project files worth reading:\n" if lang == "en" else "Жоба файлдары, оқуға тұрарлық:\n" if lang == "kk" else "Файлы проекта, которые стоит прочитать:\n")
    return GEN.get(lang, GEN["en"]).format(
        id=it["id"], title=it["title"], project=d.get("project", ""), lane=next((l.get("title") or l["id"] for l in d["lanes"] if l["id"] == it["lane"]), it["lane"]), outline=it.get("outline") or it["title"],
        needs=", ".join(it.get("needs", [])) or "-", uses=", ".join(it.get("uses", [])) or "-",
        quiet="yes" if it.get("quiet") else "no", noisy="yes" if it.get("noisy") else "no",
        reports=(head_r + "\n".join(reports) + "\n") if reports else "",
        context=(head_c + "\n".join(f"- {c}" for c in ctx) + "\n") if ctx else "",
        template=textwrap.indent(template_text(), "  "), py=py_cmd(), rb=str(Path(__file__).resolve()), board=str(board))


# ----------------------------------------------------------------------------- views

def lane_focus(comp: list, lanes: list, steps=None) -> dict:
    """For every session: the one task it works on or is up next with, and whether it can start next to the others.

    A running (or sent) task wins. Otherwise the next task is the session's open task that comes first in the plan (the
    steps already hold the order, dependencies and conflicts), so what the card shows is always what the plan says.
    role: running (running or sent), ready, turn (a draft whose turn has come), blocked (a conflict with something that
    runs), waiting (its dependencies are not finished). hint.kind: deps, wait (conflict with a running task), not (conflict
    with another session's next task), ok (can run together, `ids` says with what), unchecked / undescribed (nothing was
    declared, so nothing was checked), none (nothing to compare with). One function for every state, so no card can be
    left without its line."""
    order = {}
    for n, step in enumerate(steps or []):
        for pos, iid in enumerate(step):
            order[iid] = (n, pos)
    focus = {}
    for lane in lanes:
        mine = [c for c in comp if c["lane"] == lane["id"]]
        pick = None
        running = next((c for c in mine if c["state"] in ("running", "sent")), None)
        if running:
            pick = ("running", running)
        else:
            # queued tasks come before drafts: a draft whose turn has come is only the answer when the session has nothing queued
            open_ = [c for c in mine if c["state"] in ("ready", "blocked", "waiting")] or [c for c in mine if c["status"] == "draft"]
            if open_:
                first = min(open_, key=lambda c: (order.get(c["id"], (10 ** 6, 0)), mine.index(c)))
                if first["state"] == "ready":
                    role = "ready"
                elif first["state"] == "blocked":
                    role = "blocked"
                elif first["state"] == "waiting" or first["waiting_on"]:
                    role = "waiting"
                else:
                    role = "turn"
                pick = (role, first)
        if pick:
            focus[lane["id"]] = {"id": pick[1]["id"], "role": pick[0]}
    near = {f["id"]: lid for lid, f in focus.items()}
    by_id = {c["id"]: c for c in comp}
    for lid, f in focus.items():
        c = by_id[f["id"]]
        if f["role"] == "running":
            continue
        other = lambda ids: [i for i in ids if i in near and near[i] != lid]
        person = [r for r in c.get("reasons", []) if r["code"] in ("decision", "never")]
        if person:
            f["hint"] = {"kind": "person", "ids": [r["id"] for r in person], "codes": [r["code"] for r in person]}
        elif c["waiting_on"]:
            f["hint"] = {"kind": "deps", "ids": list(c["waiting_on"])}
        elif c.get("begin_in"):
            f["hint"] = {"kind": "begin", "at": c.get("not_before", ""), "in": c["begin_in"]}
        elif c["conflicts"]:
            f["hint"] = {"kind": "wait", "with": [{"id": x["id"], "why": x["why"]} for x in c["conflicts"]]}
        elif [x for x in c["cannot_run_with"] if x["id"] in near and near[x["id"]] != lid]:
            f["hint"] = {"kind": "not", "with": [{"id": x["id"], "why": x["why"]} for x in c["cannot_run_with"] if x["id"] in near and near[x["id"]] != lid]}
        else:
            # nothing conflicts: say whether that was really checked. A check needs both tasks to declare something.
            others = [i for i in other(c["can_run_with"])]
            if not others:
                others = [i for i, l2 in near.items() if l2 != lid and by_id[i]["state"] in ("running", "sent")]
            if not c["described"]:
                f["hint"] = {"kind": "undescribed", "ids": others}
            else:
                good = [i for i in others if by_id[i]["described"]]
                unchecked = [i for i in others if not by_id[i]["described"]]
                if good:
                    f["hint"] = {"kind": "ok", "ids": good, "unchecked": unchecked}
                elif unchecked:
                    f["hint"] = {"kind": "unchecked", "ids": unchecked}
                else:
                    f["hint"] = {"kind": "none"}
    return focus


def focus_summary(focus: dict) -> dict:
    """One line of truth for the whole 'what to do now' section, from the same focus data as the cards."""
    out = {"running": [], "can_start": [], "wait": [], "choose": [], "deps": [], "unchecked": [], "begin": [], "person": []}
    seen = set()
    for lid, f in focus.items():
        h = f.get("hint")
        if f["role"] == "running":
            out["running"].append(f["id"])
        elif h["kind"] == "deps":
            out["deps"].append({"id": f["id"], "ids": h["ids"]})
        elif h["kind"] == "begin":
            out["begin"].append({"id": f["id"], "at": h["at"]})
        elif h["kind"] == "person":
            out["person"].append({"id": f["id"], "ids": h["ids"], "codes": h["codes"]})
        elif h["kind"] == "wait":
            out["wait"].append({"id": f["id"], "with": h["with"]})
        elif h["kind"] == "not":
            for w in h["with"]:
                key = tuple(sorted((f["id"], w["id"])))
                if key not in seen:
                    seen.add(key)
                    out["choose"].append({"ids": list(key), "why": w["why"]})
        elif h["kind"] in ("unchecked", "undescribed"):
            out["unchecked"].append({"id": f["id"], "kind": h["kind"], "ids": h.get("ids", [])})
        else:
            out["can_start"].append(f["id"])
    return out


def board_view(path: Path) -> dict:
    d = load(path)
    comp = compute(d)
    steps = plan_steps(d, comp)
    ready = [c for c in comp if c["state"] == "ready"]
    now_by_lane = {}
    for c in ready:
        now_by_lane.setdefault(c["lane"], c["id"])
    return {
        "version": __version__, "project": d.get("project", ""), "rev": d.get("rev", 0), "updated": d.get("updated"),
        "lang": d.get("lang", "en"), "lanes": d["lanes"], "resources": d.get("resources", {}),
        "policy": {k: d.get("policy", {}).get(k) for k in ("dependents_wait_for_accept", "lease_hours", "footer", "direct_agents")},
        "items": comp, "profiles": all_profiles(d), "steps": steps, "next_by_lane": now_by_lane, "focus": lane_focus(comp, d["lanes"], steps), "summary": focus_summary(lane_focus(comp, d["lanes"], steps)), "events": d.get("events", [])[-60:],
        "proposed": [c["id"] for c in comp if c["state"] == "proposed"],
        "review": [c["id"] for c in comp if c["state"] == "review"],
    }


def digest(d: dict, since: dt.datetime, now=None) -> dict:
    """What happened since a moment, for the person coming back: sessions (alive? doing what?), tasks that ended (outcome, report),
    what runs, what stalled, what needs the person, what is still waiting and why."""
    now = now or now_dt()
    comp = compute(d, now)
    by_id = {c["id"]: c for c in comp}
    ended = []
    for e in d.get("events", []):
        t = parse_ts(e.get("t"))
        if e.get("action") == "finish" and t and t >= since and e.get("id") in by_id:
            c = by_id[e["id"]]
            ended.append({"id": c["id"], "title": c["title"], "lane": c["lane"], "at": e["t"], "outcome": c.get("outcome", ""), "status": c["status"],
                          "report": c.get("report", ""), "violations": len(c.get("violations") or [])})
    sessions = []
    for lane in d["lanes"]:
        seen = lane.get("seen") or {}
        at = parse_ts(seen.get("at"))
        sessions.append({"id": lane["id"], "title": lane.get("title") or lane["id"], "state": seen.get("state", ""), "task": seen.get("task", ""),
                         "reason": seen.get("reason", ""), "by": seen.get("by", ""), "ago": int((now - at).total_seconds()) if at else None})
    needs_you = []
    for c in comp:
        if c["status"] == "review":
            needs_you.append({"id": c["id"], "title": c["title"], "kind": "accept", "outcome": c.get("outcome", "")})
        elif c["status"] == "proposed":
            needs_you.append({"id": c["id"], "title": c["title"], "kind": "approve"})
    for c in comp:
        if c["status"] == "queued":
            for r in c["reasons"]:
                if r["code"] in ("decision", "never"):
                    needs_you.append({"id": c["id"], "title": c["title"], "kind": r["code"], "on": r["id"]})
    return {
        "since": since.isoformat(), "now": now.isoformat(), "sessions": sessions, "ended": ended,
        "running": [{"id": c["id"], "title": c["title"], "lane": c["lane"], "by": (c.get("claim") or {}).get("by", ""), "since": (c.get("claim") or {}).get("at", ""),
                     "deadline": (c.get("claim") or {}).get("deadline", ""), "stale": c["stale"]} for c in comp if c["status"] == "running"],
        "needs_you": needs_you,
        "waiting": [{"id": c["id"], "title": c["title"], "lane": c["lane"], "reasons": c["reasons"]} for c in comp if c["status"] == "queued" and c["reasons"]],
        "next_ready": [c["id"] for c in comp if c["state"] == "ready"],
    }


def parse_since(text: str | None, now=None) -> dt.datetime:
    """`12h`, `90m`, `2d` (ago) or a time (see parse_when). Default 12 hours."""
    now = now or now_dt()
    text = (text or "12h").strip()
    m = re.fullmatch(r"(\d+)\s*([mhd])", text.lower())
    if m:
        n = int(m.group(1))
        return now - {"m": dt.timedelta(minutes=n), "h": dt.timedelta(hours=n), "d": dt.timedelta(days=n)}[m.group(2)]
    try:
        iso = text.replace(" ", "T")
        exact = dt.datetime.fromisoformat(iso[:-1] + "+00:00" if iso[-1:] in ("Z", "z") else iso)
        if exact.tzinfo is not None:
            return exact                 # a time with an offset keeps its fractions of a second (the panel sends the moment of "Got it")
    except ValueError:
        pass
    return parse_ts(parse_when(text, now)) or now - dt.timedelta(hours=12)


def digest_text(g: dict) -> str:
    out = [f"Since {local_time(g['since'])}:"]
    out.append("Sessions:")
    for x in g["sessions"]:
        ago = f", signal {fmt_in(x['ago'])} ago" if x["ago"] is not None else ", no signal yet"
        out.append(f"  {x['id']:<10} {x['state'] or '-':<8} {x['task'] or ''} {x['reason'] or ''}{ago}".rstrip())
    out.append("Ended:" if g["ended"] else "Ended: nothing")
    for e in g["ended"]:
        out.append(f"  {e['id']:<6} {e['outcome'] or 'no outcome':<9} {e['status']:<7} {e['title']}  report: {e['report'] or '-'}" + (f"  ({e['violations']} file warnings)" if e["violations"] else ""))
    if g["running"]:
        out.append("Running:")
        for r in g["running"]:
            out.append(f"  {r['id']:<6} {r['by']} since {local_time(r['since'])}" + (f", deadline {local_time(r['deadline'])}" if r["deadline"] else "") + ("  LEASE EXPIRED (session may be dead)" if r["stale"] else ""))
    if g["needs_you"]:
        out.append("Needs you:")
        verb = {"accept": "accept the report", "approve": "approve the proposal", "decision": "decide: it waits for", "never": "fix the dependency on"}
        for n in g["needs_you"]:
            out.append(f"  {n['id']:<6} {verb[n['kind']]} {n.get('on', '')}".rstrip() + f"  ({n['title']})")
    if g["waiting"]:
        out.append("Waiting:")
        for w in g["waiting"]:
            out.append(f"  {w['id']:<6} " + "; ".join(fmt_reason(r) for r in w["reasons"]))
    return "\n".join(out)


def fmt_in(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400}d {seconds % 86400 // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{max(1, seconds // 60)}m" if seconds >= 60 else f"{seconds}s"


def fmt_reason(r: dict) -> str:
    code = r["code"]
    if code == "begin_time":
        return f"not before {local_time(r['at'])} (in {fmt_in(r['in'])})"
    if code == "dependency":
        return f"waits for {r['id']} ({r['status']})"
    if code == "conflict":
        return f"cannot run next to {r['id']} ({r['why']})"
    if code == "decision":
        what = "accept its report" if r.get("outcome") is not None and "status" not in r else f"approve or write it ({r.get('status', '')})"
        return f"needs you to {what}: {r['id']}"
    if code == "never":
        return f"waits for {r['id']}, which is {r['status']}: it can never finish, change the dependency"
    return code


def next_step(d: dict, lane: str | None = None, now=None) -> dict:
    """What `next` should answer for a lane (or for every lane): the first ready task in plan order; otherwise why nothing is ready.

    status: task (a task is ready: `id`), none (the lane has no queued task at all: it is done), wait (queued tasks exist and
    every one of them is held by something that ends by itself: a start time, other tasks, a conflict), stuck (nothing will
    change without the person: a report to accept, a draft to write, a dependency that was cancelled). `waiting` lists the queued
    tasks with their reasons; `retry_in` is a sensible number of seconds to look again."""
    comp = compute(d, now)
    steps = plan_steps(d, comp)
    order = {iid: (n, pos) for n, step in enumerate(steps) for pos, iid in enumerate(step)}
    mine = [c for c in comp if c["status"] == "queued" and (not lane or c["lane"] == lane)]
    mine.sort(key=lambda c: order.get(c["id"], (10 ** 6, 0)))
    for c in mine:
        if c["state"] == "ready":
            return {"status": NEXT_TASK, "id": c["id"], "title": c["title"], "lane": c["lane"], "waiting": [], "retry_in": 0}
    waiting = [{"id": c["id"], "title": c["title"], "lane": c["lane"], "reasons": c["reasons"]} for c in mine]
    if not waiting:
        return {"status": NEXT_NONE, "id": None, "waiting": [], "retry_in": 0}
    soft = [w for w in waiting if w["reasons"] and all(r["code"] in SELF_RESOLVING for r in w["reasons"])]
    begins = [r["in"] for w in soft for r in w["reasons"] if r["code"] == "begin_time"]
    retry = min(begins) if begins and len(begins) == len(soft) else 60
    return {"status": NEXT_WAIT if soft else NEXT_STUCK, "id": None, "waiting": waiting, "retry_in": max(5, min(retry, 300))}


def describe_next(res: dict) -> str:
    if res["status"] == NEXT_NONE:
        return "nothing to do: this lane has no queued task. Stop here and report; do not invent work."
    head = {NEXT_WAIT: "nothing can start yet, but it will without anyone's help: run this command again (add --wait 300 to let it wait for you)",
            NEXT_STUCK: "nothing can start until the person decides something: stop and report what is needed"}[res["status"]]
    lines = [head + ":"]
    for w in res["waiting"][:6]:
        lines.append(f"  {w['id']}: " + "; ".join(fmt_reason(r) for r in w["reasons"]))
    return "\n".join(lines)


def note_presence(board: Path, lane: str | None, by: str, state: str, task: str = "", reason: str = "", min_age: int = 60) -> None:
    """Tell the board this lane's session is alive (and what it does). Written only when something changed or `min_age` seconds
    passed, so a waiting loop does not rewrite the board every few seconds. Never raises: presence is a courtesy."""
    if not lane:
        return
    try:
        d = load(board)
        cur = next((l.get("seen") or {} for l in d["lanes"] if l["id"] == lane), {})
        at = parse_ts(cur.get("at"))
        same = cur.get("state") == state and cur.get("task", "") == task and cur.get("reason", "") == reason[:200] and cur.get("by") == by
        if same and at and (now_dt() - at).total_seconds() < min_age:
            return
        mutate(board, lambda dd: op_seen(dd, lane, by, state, task, reason))
    except (RBError, OSError, ValueError):
        pass


def fmt_line(c: dict) -> str:
    extra = ""
    if c["state"] == "waiting":
        extra = " (after " + ", ".join(c["waiting_on"]) + ")"
    elif c["state"] == "blocked":
        extra = " (blocked by " + ", ".join(f"{x['id']}:{x['why']}" for x in c["conflicts"]) + ")"
    elif c.get("stale"):
        extra = " (lease expired)"
    return f"{c['id']:<6} {c['state']:<9} {c['lane']:<10} {c['title']}{extra}"


# ----------------------------------------------------------------------------- server

def host_ok(host: str) -> bool:
    h = (host or "").strip().lower()
    if h.startswith("["):
        h = h.split("]")[0] + "]"
    else:
        h = h.split(":")[0]
    return h in ("localhost", "127.0.0.1", "[::1]") or h.endswith(".localhost")


def server_file(board: Path) -> Path:
    return board.with_name("server.json")


def make_handler(board: Path, tracker: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Barid/" + __version__

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self):
            tracker["last"] = time.time()
            if not host_ok(self.headers.get("Host")):
                self._send(403, {"error": "forbidden host"})
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            try:
                if path == "/":
                    f = HERE / "panel.html"
                    if not f.is_file():
                        return self._send(500, "panel.html is missing next to barid.py", "text/plain; charset=utf-8")
                    return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                if path == "/api/rev":
                    return self._send(200, {"rev": load(board).get("rev", 0)})
                if path == "/api/board":
                    return self._send(200, board_view(board))
                if path == "/api/digest":
                    since = parse_since(urllib.parse.parse_qs(self.path.partition("?")[2]).get("since", [None])[0])
                    return self._send(200, digest(load(board), since))
                m = re.match(r"^/api/connect/([A-Za-z0-9][A-Za-z0-9_.-]{0,31})$", path)
                if m:
                    return self._send(200, {"text": connect_text(load(board), m.group(1), board)})
                m = re.match(r"^/api/(prompt|gen)/([A-Za-z0-9][A-Za-z0-9_.-]{0,31})$", path)
                if m:
                    d = load(board)
                    it = get_item(d, m.group(2))
                    text = prompt_for(d, it, board) if m.group(1) == "prompt" else gen_request(d, it, board)
                    return self._send(200, {"text": text})
                self._send(404, {"error": "not found"})
            except RBError as e:
                self._send(400, {"error": str(e), "code": e.code, "info": e.info})
            except Exception as e:  # never kill the server on a bad request
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            if not self._guard():
                return
            origin = self.headers.get("Origin") or ""
            host = self.headers.get("Host") or ""
            if (self.path.split("?", 1)[0] != "/api/act" or "1" not in (self.headers.get("X-Barid-Edit"), self.headers.get("X-RB-Edit"))
                    or (origin and origin.split("://", 1)[-1] != host)):
                return self._send(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= MAX_BODY:
                    raise RBError("bad body size")
                req = json.loads(self.rfile.read(n))
                result = apply_action(board, req)
                self._send(200, {"ok": True, "result": result})
            except RBError as e:
                self._send(400, {"ok": False, "error": str(e), "code": e.code, "info": e.info})
            except (ValueError, KeyError, TypeError):
                self._send(400, {"ok": False, "error": "bad request"})
            except Exception as e:
                self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    return Handler


ARG_STR = ("title", "lane", "outline", "text", "when", "workdir", "branch", "profile", "status", "outcome", "color", "agent", "lang", "reason", "report",
           "not_before", "timebox")
ARG_LIST = ("needs", "after", "uses", "touches", "lint_ok")  # a comma-separated string or a list of strings
ARG_BOOL = ("quiet", "noisy", "draft", "unattended")


def check_args(args) -> dict:
    """The panel API takes JSON from a browser (or anything on this machine): refuse a field of the wrong type with a clean error."""
    if not isinstance(args, dict):
        raise RBError("args must be an object", "bad_input", field="args")
    for k in ARG_STR:
        if k in args and args[k] is not None and not isinstance(args[k], str):
            raise RBError(f"{k} must be text", "bad_input", field=k)
    for k in ARG_LIST:
        v = args.get(k)
        if v is not None and not (isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v))):
            raise RBError(f"{k} must be text or a list of text", "bad_input", field=k)
    for k in ARG_BOOL:
        if k in args and args[k] is not None and not isinstance(args[k], bool):
            raise RBError(f"{k} must be true or false", "bad_input", field=k)
    return args


def apply_action(board: Path, req: dict):
    """Panel actions: always performed as the person."""
    if not isinstance(req, dict):
        raise RBError("the request must be an object", "bad_input", field="request")
    a = str(req.get("action", ""))
    iid = str(req.get("id", ""))
    args = check_args(req.get("args") or {})
    h = HUMAN

    def fn(d):
        if a == "add":
            f = dict(args)
            f["id"] = iid
            return op_add(d, h, f)["id"]
        if a == "edit":
            f = {k: args.get(k) for k in ("title", "lane", "needs", "after", "uses", "quiet", "noisy", "profile", "lint_ok", "when", "outline", "text", "touches",
                                          "workdir", "branch", "not_before", "timebox") if k in args}
            return op_edit(d, h, iid, f)["id"]
        if a == "status":
            return op_set_status(d, h, iid, str(args.get("status", "")))["status"]
        if a == "approve":
            return op_approve(d, h, iid)["status"]
        if a == "reject":
            return op_reject(d, h, iid, str(args.get("reason", "")))["status"]
        if a == "cancel":
            return op_cancel(d, h, iid, str(args.get("reason", "")))["status"]
        if a == "outcome":
            return op_outcome(d, h, iid, str(args.get("outcome", "")))["outcome"]
        if a == "boardlang":
            return {"lang": op_board_lang(d, h, str(args.get("lang", "")))}
        if a == "laneadd":
            lane = op_lane_add(d, h, iid, args.get("title"), args.get("color"), args.get("agent"))
            if args.get("unattended") is not None:
                lane = op_lane_edit(d, h, iid, None, None, None, args.get("unattended"))
            return {"id": lane["id"]}
        if a == "lane":
            lane = op_lane_edit(d, h, iid, args.get("title"), args.get("color"), args.get("agent"), args.get("unattended"))
            return {"id": lane["id"]}
        if a == "restore":
            return op_restore(d, h, iid)["status"]
        if a == "sent":
            return op_sent(d, h, iid)["status"]
        if a == "accept":
            return op_accept(d, h, iid)["status"]
        if a == "release":
            return op_release(d, h, iid, force=True)["status"]
        if a == "note":
            return op_note(d, h, iid, str(args.get("text", "")))["id"]
        raise RBError(f"unknown action {a!r}")

    return mutate(board, fn)


class LocalServer(ThreadingHTTPServer):
    """The standard server looks up the host name (socket.getfqdn) when it binds. With a slow or broken DNS that can take
    half a minute or hang, and it is useless here: the panel only ever listens on loopback."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port


def serve(board: Path, host: str = "127.0.0.1", port: int = 0, idle_exit: float = 0.0, announce=None) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RBError("Barid binds to loopback addresses only (there is no authentication)")
    tracker = {"last": time.time()}
    handler = make_handler(board, tracker)
    srv = None
    if os.environ.get("LISTEN_FDS") == "1":  # systemd socket activation
        srv = LocalServer(("127.0.0.1", 0), handler, bind_and_activate=False)
        srv.socket.close()
        srv.socket = socket.socket(fileno=3)
        srv.server_address = srv.socket.getsockname()
    else:
        for p in ([port] if port else range(DEFAULT_PORT, DEFAULT_PORT + 30)):
            try:
                srv = LocalServer((host, p), handler)
                break
            except OSError:
                continue
    if srv is None:
        raise RBError("could not bind a local port")
    srv.daemon_threads = True
    real_port = srv.server_address[1]
    sf = server_file(board)
    sf.write_text(json.dumps({"pid": os.getpid(), "port": real_port, "host": host, "started": now_iso()}), "utf-8")
    if announce:
        announce(real_port)

    if idle_exit and idle_exit > 0:
        def watch():
            while True:
                time.sleep(15)
                if time.time() - tracker["last"] > idle_exit * 60:
                    srv.shutdown()
                    return
        threading.Thread(target=watch, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            info = json.loads(sf.read_text("utf-8"))
            if info.get("pid") == os.getpid():
                sf.unlink()
        except (OSError, ValueError):
            pass
        srv.server_close()


def server_alive(board: Path):
    try:
        info = json.loads(server_file(board).read_text("utf-8"))
        with socket.create_connection((info.get("host", "127.0.0.1"), int(info["port"])), timeout=0.4):
            return info
    except (OSError, ValueError, KeyError):
        return None


def ensure_server(board: Path, idle_exit: float = 30.0):
    info = server_alive(board)
    if info:
        return info
    try:
        server_file(board).unlink()
    except OSError:
        pass
    cmd = [sys.executable, str(Path(__file__).resolve()), "--board", str(board), "serve", "--idle-exit", str(idle_exit)]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    for _ in range(60):
        time.sleep(0.1)
        info = server_alive(board)
        if info:
            return info
    raise RBError("the panel server did not start; try `barid serve` in a terminal to see why")


# ----------------------------------------------------------------------------- CLI

def out(args, obj, human_text: str = "") -> None:
    if getattr(args, "json", False):
        print(json.dumps(obj, ensure_ascii=False, indent=1))
    elif human_text:
        print(human_text)


def read_text_arg(args) -> str | None:
    if getattr(args, "text_file", None):
        return Path(args.text_file).expanduser().read_text("utf-8")
    t = getattr(args, "text", None)
    if t == "-":
        return sys.stdin.read()
    return t


def cmd_init(args):
    base = Path(args.board).expanduser() if getattr(args, "board", None) else Path.cwd() / ".barid" / "board.json"
    if base.is_dir():
        base = base / ".barid" / "board.json"
    if base.exists() and not args.force:
        raise RBError(f"{base} already exists (use --force to overwrite)")
    lanes = []
    for spec in split_list(args.lanes):
        i, _, t = spec.partition(":")
        if not ID_RE.match(i):
            raise RBError(f"bad lane id {i!r}")
        lanes.append((i, t or i))
    base.parent.mkdir(parents=True, exist_ok=True)
    save(base, new_board(args.project or Path.cwd().name, lanes, args.lang or system_lang()))
    print(f"created {base}")
    print("next: add tasks with `barid add`, then look at them with `barid open`")


def cmd_where(args):
    print(find_board(getattr(args, "board", None)))


def cmd_add(args):
    board = find_board(getattr(args, "board", None))
    f = {"id": args.id, "lane": args.lane, "title": args.title, "needs": args.needs, "after": args.after, "not_before": args.not_before,
         "timebox": args.timebox, "uses": args.uses, "quiet": args.quiet,
         "noisy": args.noisy, "when": args.when, "outline": args.outline, "text": read_text_arg(args), "draft": args.draft,
         "touches": args.touches, "workdir": args.workdir, "profile": args.profile, "lint_ok": ",".join(args.lint_ok or [])}
    item = mutate(board, lambda d: op_add(d, cli_actor(args), f))
    out(args, item, f"{item['id']} added as {item['status']}" + (" (waiting for the person to approve it)" if item["status"] == "proposed" else ""))
    print_lint(board, item["id"], args)


def cmd_edit(args):
    board = find_board(getattr(args, "board", None))
    f = {"title": args.title, "lane": args.lane, "needs": args.needs, "after": args.after, "not_before": args.not_before, "timebox": args.timebox,
         "uses": args.uses, "when": args.when, "outline": args.outline,
         "quiet": args.quiet, "noisy": args.noisy, "text": read_text_arg(args), "touches": args.touches, "workdir": args.workdir,
         "profile": args.profile}
    if args.lint_ok is not None:
        f["lint_ok"] = ",".join(args.lint_ok)
    if args.replace:
        f["replace"] = args.replace
    f = {k: v for k, v in f.items() if v is not None}
    item = mutate(board, lambda d: op_edit(d, cli_actor(args), args.id, f))
    out(args, item, f"{item['id']} updated (status {item['status']})")
    print_lint(board, item["id"], args)


def print_lint(board, iid: str, args) -> None:
    """After add and edit: say right away what looks incomplete in the description (stderr, so --json output stays clean)."""
    if getattr(args, "json", False):
        return
    try:
        c = {x["id"]: x for x in compute(load(board))}.get(iid)
    except RBError:
        return
    for x in (c or {}).get("lint", []):
        print(f"  ! {iid}: {lint_line(x)}", file=sys.stderr)


def cmd_lint(args):
    board = find_board(getattr(args, "board", None))
    comp = compute(load(board))
    rows = [c for c in comp if (not args.id or c["id"] == args.id) and c["lint"]]
    if getattr(args, "json", False):
        print(json.dumps([{"id": c["id"], "lint": [dict(x, text=lint_line(x)) for x in c["lint"]]} for c in rows], ensure_ascii=False, indent=1))
    else:
        for c in rows:
            for x in c["lint"]:
                print(f"{c['id']:<6} {x['code']:<14} {lint_line(x)}")
        if not rows:
            print("every open task is described well enough to be checked")
    if rows and args.strict:
        raise SystemExit(1)


def cmd_explain(args):
    board = find_board(getattr(args, "board", None))
    d = load(board)
    a, b = get_item(d, args.a), get_item(d, args.b)
    ex = explain_pair(d, a, b)
    if getattr(args, "json", False):
        print(json.dumps(ex, ensure_ascii=False, indent=1))
        return
    for r in ex["rules"]:
        print(f"{'CONFLICT' if r['hit'] else 'ok      '} {r['rule']:<9} {r['detail']}")
    marks = ", ".join(f"{i}: {'described' if v else 'NOT described'}" for i, v in ex["described"].items())
    verdict = {"conflict": f"must not run together ({ex['why']})", "related": "one depends on the other, so they cannot run together anyway",
               "unchecked": "no conflict found, but not checked: at least one of them declares nothing", "ok": "can run together (checked)"}[ex["verdict"]]
    print(f"{marks}\n=> {verdict}")


def cmd_profile(args):
    board = find_board(getattr(args, "board", None))
    actor = cli_actor(args)
    if args.action == "list":
        d = load(board)
        for k, v in all_profiles(d).items():
            marks = " ".join(filter(None, ["quiet" if v.get("quiet") else "", "noisy" if v.get("noisy") else "", ("uses " + ",".join(v["uses"])) if v.get("uses") else ""])) or "nothing special"
            print(f"{k:<10} {'(built in)' if v.get('builtin') else '          '} {marks:<28} {v.get('label', '')}")
        return
    if not args.name:
        raise RBError("give the profile name")
    if args.action == "add":
        mutate(board, lambda d: op_profile_add(d, actor, args.name, args.uses, args.quiet, args.noisy, args.label or ""))
        print(f"profile {args.name} added")
    else:
        mutate(board, lambda d: op_profile_rm(d, actor, args.name))
        print(f"profile {args.name} removed")


def cmd_list(args):
    board = find_board(getattr(args, "board", None))
    comp = compute(load(board))
    rows = [c for c in comp if (not args.lane or c["lane"] == args.lane) and (not args.status or c["state"] == args.status or c["status"] == args.status)]
    if not args.all and not args.status:
        rows = [c for c in rows if c["status"] not in ("done", "cancelled")]
    out(args, rows, "\n".join(fmt_line(c) for c in rows) or "(nothing)")


def cmd_plan(args):
    board = find_board(getattr(args, "board", None))
    v = board_view(board)
    byid = {c["id"]: c for c in v["items"]}
    lines = []
    for n, ids in enumerate(v["steps"], 1):
        lines.append(f"Step {n}" + ("  (can run at the same time)" if len(ids) > 1 else ""))
        for i in ids:
            lines.append("  " + fmt_line(byid[i]))
    out(args, {"steps": v["steps"], "next_by_lane": v["next_by_lane"]}, "\n".join(lines) or "(nothing planned)")


def cmd_show(args):
    board = find_board(getattr(args, "board", None))
    d = load(board)
    c = {x["id"]: x for x in compute(d)}.get(args.id)
    if not c:
        raise RBError(f"no such task: {args.id}")
    if args.text:
        print(prompt_for(d, get_item(d, args.id), board))
        return
    out(args, c, fmt_line(c) + (f"\nreport: {c['report']}" if c.get("report") else "") + (f"\nneeds: {', '.join(c['needs'])}" if c["needs"] else ""))


def cmd_next(args):
    """Print the next ready task of a lane. Exit codes: 0 a task was printed; 2 the lane has nothing queued; 3 nothing can start
    yet but will without anyone's help (run again, or use --wait); 4 nothing will change without the person."""
    board = find_board(getattr(args, "board", None))
    by = cli_actor(args).name
    wait = max(0, int(getattr(args, "wait", 0) or 0))
    started = time.monotonic()
    last_note = -60.0
    as_json = getattr(args, "json", False)
    while True:
        d = load(board)
        res = next_step(d, args.lane)
        if res["status"] == NEXT_TASK:
            it = get_item(d, res["id"])
            if as_json:
                print(json.dumps({**res, "prompt": prompt_for(d, it, board)}, ensure_ascii=False, indent=1))
            else:
                print(f"# {res['id']}: {res['title']}\n")
                print(prompt_for(d, it, board))
            return
        elapsed = time.monotonic() - started
        if res["status"] == NEXT_WAIT:
            note_presence(board, args.lane, by, "waiting", "", "; ".join(f"{w['id']}: {fmt_reason(w['reasons'][0])}" for w in res["waiting"][:2]))
            if elapsed < wait:
                if elapsed - last_note >= 60 and not as_json:
                    print(f"[barid] still waiting after {int(elapsed)}s: " + "; ".join(f"{w['id']} {fmt_reason(w['reasons'][0])}" for w in res["waiting"][:2]), file=sys.stderr, flush=True)
                    last_note = elapsed
                time.sleep(min(5.0, max(0.1, wait - elapsed)))
                continue
        else:
            note_presence(board, args.lane, by, "idle", "", "nothing queued" if res["status"] == NEXT_NONE else "needs the person")
        if as_json:
            print(json.dumps({**res, "reason": describe_next(res)}, ensure_ascii=False))
        else:
            print(describe_next(res))
        sys.exit({NEXT_NONE: EXIT_NONE, NEXT_WAIT: EXIT_WAIT, NEXT_STUCK: EXIT_STUCK}[res["status"]])


WORKER_LEASE_MINUTES = 3      # a worker renews the lease every 30 seconds: a dead worker shows as a stale task within minutes


def _kill_group(proc: subprocess.Popen, grace: float = 20.0) -> None:
    """Stop the agent process and everything it started (its own process group on POSIX, its own process tree on Windows); never
    anything else."""
    import signal
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        proc.wait(timeout=10)


def run_worker_task(board: Path, lane: str, iid: str, name: str, command: str, cwd: str | None, stop: threading.Event) -> str:
    """Claim a task, run the agent command on its prompt (stdin; also $BARID_PROMPT_FILE and {prompt_file} in the command), keep
    the lease alive, enforce the timebox, and make sure the task ends in a report. Returns what happened: finished, stopped, claim-lost."""
    actor = Actor("agent", name)
    try:
        mutate(board, lambda d: op_claim(d, actor, iid, False, WORKER_LEASE_MINUTES))
    except RBError as e:
        print(f"[barid worker] could not claim {iid}: {e}", flush=True)
        return "claim-lost"
    d = load(board)
    it = get_item(d, iid)
    prompt = prompt_for(d, it, board, worker=name)
    runs = board.parent / "runs"
    runs.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    prompt_file, log_file = runs / f"{iid}-{stamp}.prompt.md", runs / f"{iid}-{stamp}.log"
    prompt_file.write_text(prompt, "utf-8")
    deadline = parse_ts((it.get("claim") or {}).get("deadline"))
    env_ = dict(os.environ, BARID_AGENT=name, BARID_BOARD=str(board), BARID_TASK=iid, BARID_LANE=lane, BARID_PROMPT_FILE=str(prompt_file))
    print(f"[barid worker] {iid}: started ({'deadline ' + local_time(deadline.isoformat()) if deadline else 'no timebox'}); log {log_file}", flush=True)
    with open(log_file, "wb") as logf:
        own_group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        proc = subprocess.Popen(command.replace("{prompt_file}", str(prompt_file)), shell=True, stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
                                cwd=cwd or None, env=env_, **own_group)
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass                       # the command does not read stdin; it uses the prompt file
        timed_out = stopped = False
        last_beat = 0.0
        while proc.poll() is None:
            if stop.is_set():
                stopped = True
                _kill_group(proc)
                break
            if deadline and now_dt() > deadline:
                timed_out = True
                _kill_group(proc)
                break
            if time.monotonic() - last_beat >= 30:
                last_beat = time.monotonic()
                try:
                    mutate(board, lambda dd: op_heartbeat(dd, actor, iid, WORKER_LEASE_MINUTES))
                except RBError:
                    pass               # e.g. the person released the task meanwhile; the check below handles it
            time.sleep(1.0)
    code = proc.returncode
    d = load(board)
    it = get_item(d, iid)
    if it["status"] == "running" and (it.get("claim") or {}).get("by") == name:
        why = ("the timebox ran out" if timed_out else "the worker was stopped" if stopped else
               f"the agent ended with exit code {code} without reporting")
        outcome = "partial" if (code == 0 and not timed_out and not stopped) else "failed"
        mutate(board, lambda dd: op_finish(dd, actor, iid, str(log_file), f"Closed by the worker: {why}. Log: {log_file}", False, outcome))
        print(f"[barid worker] {iid}: {why}; closed as {outcome}", flush=True)
    else:
        print(f"[barid worker] {iid}: reported by the agent ({it['status']}, {it.get('outcome') or 'no outcome'})", flush=True)
    return "stopped" if stopped else "finished"


def cmd_worker(args):
    """Run a lane without an agent session that has to stay polite: a loop outside the model. For every task of the lane it starts
    the command you give (a fresh agent each time, the prompt on stdin), keeps the lease alive, stops it at the timebox, closes a
    task whose agent died without a report, and then asks for the next one. It waits for start times, other tasks and conflicts by
    itself. Ends when the lane is empty (exit 0), needs the person (exit 4), or after --max-wait without a task (exit 3)."""
    import signal
    board = find_board(getattr(args, "board", None))
    d0 = load(board)
    if not any(l["id"] == args.lane for l in d0["lanes"]):
        raise RBError(f"unknown lane {args.lane!r}", "unknown_lane", lane=args.lane)
    name = getattr(args, "by", None) or env("AGENT") or f"worker-{args.lane}"
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    done = 0
    idle_since = time.monotonic()
    print(f"[barid worker] {name} serves lane {args.lane}; command: {args.cmd}", flush=True)
    while not stop.is_set():
        res = next_step(load(board), args.lane)
        if res["status"] == NEXT_TASK:
            idle_since = time.monotonic()
            result = run_worker_task(board, args.lane, res["id"], name, args.cmd, args.cwd, stop)
            done += result == "finished"
            if args.once or (args.max_tasks and done >= args.max_tasks):
                break
            continue
        if res["status"] == NEXT_NONE:
            note_presence(board, args.lane, name, "idle", "", "lane is done", 0)
            print(f"[barid worker] lane {args.lane} is empty: {done} task(s) run", flush=True)
            sys.exit(0)
        if res["status"] == NEXT_STUCK:
            note_presence(board, args.lane, name, "idle", "", "needs the person", 0)
            print(describe_next(res), flush=True)
            sys.exit(EXIT_STUCK)
        note_presence(board, args.lane, name, "waiting", "", "; ".join(f"{w['id']}: {fmt_reason(w['reasons'][0])}" for w in res["waiting"][:2]))
        if time.monotonic() - idle_since > args.max_wait:
            print(f"[barid worker] nothing could start for {int(args.max_wait)}s: giving up", flush=True)
            sys.exit(EXIT_WAIT)
        stop.wait(min(args.poll, res["retry_in"]))
    sys.exit(0)


def cmd_digest(args):
    board = find_board(getattr(args, "board", None))
    d = load(board)
    g = digest(d, parse_since(args.since))
    out(args, g, digest_text(g))


def cmd_lang(args):
    board = find_board(getattr(args, "board", None))
    actor = cli_actor(args)
    if not args.value:
        print(load(board).get("lang", "en"))
        return
    mutate(board, lambda d: op_board_lang(d, actor, args.value))
    print(f"board language (agent prompts): {'kk' if args.value == 'kz' else args.value}")


def cmd_connect(args):
    board = find_board(getattr(args, "board", None))
    print(connect_text(load(board), args.lane, board, getattr(args, "unattended", None)))


def cmd_gen(args):
    board = find_board(getattr(args, "board", None))
    d = load(board)
    print(gen_request(d, get_item(d, args.id), board))


def cmd_claim(args):
    """Take a task. Refused with exit code 3 when it must wait (a start time, other tasks, a conflict), 4 when the person must
    decide something first. With --wait SECONDS the command waits for a task that only has to wait."""
    board = find_board(getattr(args, "board", None))
    actor = cli_actor(args)
    wait = max(0, int(getattr(args, "wait", 0) or 0))
    started = time.monotonic()
    last_note = -60.0
    while True:
        try:
            warnings = mutate(board, lambda d: op_claim(d, actor, args.id, args.force))
            break
        except RBError as e:
            if e.code not in ("begin_time", "waiting", "conflict") or time.monotonic() - started >= wait:
                raise
            try:
                lane = get_item(load(board), args.id)["lane"]
            except RBError:
                lane = None
            note_presence(board, lane, actor.name, "waiting", args.id, str(e))
            elapsed = time.monotonic() - started
            if elapsed - last_note >= 60:
                print(f"[barid] still waiting after {int(elapsed)}s: {e}", file=sys.stderr, flush=True)
                last_note = elapsed
            time.sleep(min(5.0, max(0.1, wait - elapsed)))
    out(args, {"id": args.id, "warnings": warnings}, f"{args.id} claimed by {actor.name}" + ("".join(f"\nwarning: {w}" for w in warnings)))


def cmd_finish(args):
    board = find_board(getattr(args, "board", None))
    it = mutate(board, lambda d: op_finish(d, cli_actor(args), args.id, "" if args.no_report else (args.report or ""), args.note or "", args.force or args.no_report, getattr(args, "outcome", "") or ""))
    out(args, it, f"{args.id} finished: waiting for the person to review the report")


def simple(op, msg):
    def run(args):
        board = find_board(getattr(args, "board", None))
        it = mutate(board, lambda d: op(d, cli_actor(args), args))
        out(args, it, msg.format(id=args.id))
    return run


def cmd_open(args):
    board = find_board(getattr(args, "board", None))
    info = ensure_server(board, args.idle_exit)
    url = f"http://localhost:{info['port']}/"
    print(url)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass


def cmd_serve(args):
    board = find_board(getattr(args, "board", None))
    load(board)
    serve(board, args.host, args.port, args.idle_exit, announce=lambda p: print(f"Barid panel: http://localhost:{p}/  (Ctrl+C to stop)", flush=True))


def cmd_export(args):
    board = find_board(getattr(args, "board", None))
    view = board_view(board)
    if args.no_text:
        for c in view["items"]:
            c["text"] = ""
            c["outline"] = ""
    panel = (HERE / "panel.html").read_text("utf-8")
    blob = json.dumps(view, ensure_ascii=False).replace("</", "<\\/")
    marker = '<script>\n"use strict";'
    if marker not in panel:
        raise RBError("panel.html has an unexpected layout")
    html = panel.replace(marker, "<script>window.__RB_BOARD__=" + blob + ";</script>\n" + marker, 1)
    Path(args.file).expanduser().write_text(html, "utf-8")
    print(f"wrote {args.file} (read-only snapshot, open it in any browser)")


def _add_word(words, what: str):
    """`add NAME`, `edit NAME` or just `NAME`; returns None for `list`."""
    ws = list(words)
    if ws and ws[0] == "list":
        return None
    if ws and ws[0] in ("add", "edit"):
        ws = ws[1:]
    if len(ws) != 1:
        raise RBError(f"usage: barid {what} add NAME [options]  or  barid {what} list")
    return ws[0]


def cmd_protect(args):
    board = find_board(getattr(args, "board", None))
    actor = cli_actor(args)
    action = args.words[0]
    if action == "list":
        print("\n".join(load(board).get("protect", [])) or "(nothing is protected)")
        return
    if action not in ("add", "remove") or len(args.words) != 2:
        raise RBError("usage: barid protect add PATH  |  barid protect remove PATH  |  barid protect list")
    lst = mutate(board, lambda d: op_protect(d, actor, action, args.words[1]))
    print("protected: " + (", ".join(lst) or "(nothing)"))


def cmd_worktree(args):
    """Create an isolated git worktree and branch for a task, so sessions cannot overwrite each other's files."""
    board = find_board(getattr(args, "board", None))
    actor = cli_actor(args)
    d = load(board)
    it = get_item(d, args.id)
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can create a worktree for a task", "human_only")
    repo = Path(args.repo).expanduser().resolve() if args.repo else project_root(d)
    top = _git(["rev-parse", "--show-toplevel"], repo)
    if not top:
        raise RBError(f"{repo} is not inside a git repository (use --repo PATH)", "no_git")
    branch = args.branch or f"barid/{it['id']}"
    wt = Path(args.path).expanduser() if args.path else Path(top).parent / f"{Path(top).name}-barid-{it['id']}"
    r = subprocess.run(["git", "worktree", "add", "-b", branch, str(wt), args.base or "HEAD"], cwd=top, capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RBError("git worktree failed: " + (r.stderr.strip() or r.stdout.strip()))

    def fn(b):
        t = get_item(b, args.id)
        t["workdir"], t["branch"] = str(wt), branch
        t["updated"] = now_iso()
        log_event(b, actor, "worktree", args.id, f"{wt} ({branch})")
    mutate(board, fn)
    print(f"worktree {wt} on branch {branch}; the task's footer now tells the session to work only there")


def cmd_check(args):
    """Show what could collide with a task: overlapping scopes of other open tasks, and the protected paths."""
    board = find_board(getattr(args, "board", None))
    d = load(board)
    it = get_item(d, args.id)
    mine = own_scopes(d, it)
    print(f"{it['id']}: scope = {', '.join(it.get('touches', [])) or '(not declared)'}" + (f"  in {it['workdir']}" if it.get("workdir") else ""))
    found = 0
    for o in d["items"]:
        if o["id"] == it["id"] or o["status"] in ("done", "cancelled") or not o.get("touches"):
            continue
        if _case(str(task_root(d, o))) != _case(str(task_root(d, it))):
            continue  # another checkout: no shared working files
        for x in mine:
            for y in own_scopes(d, o):
                if scopes_overlap(x, y):
                    print(f"  overlaps with {o['id']} ({o['status']}): {show_scope(d, x, task_root(d, it))} <-> {show_scope(d, y, task_root(d, o))}")
                    found += 1
    if not found:
        print("  no overlap with other open tasks")
    print("protected: " + (", ".join(d.get("protect", [])) or "(nothing)"))


def cmd_lane(args):
    board = find_board(getattr(args, "board", None))
    actor = cli_actor(args)
    lane_id = _add_word(args.words, "lane")
    if lane_id is None:
        print("\n".join(f"{l['id']:<12} {l.get('title', ''):<24} {l.get('color', '') or '-':<8} {l.get('agent', '') or '-'}" for l in load(board)["lanes"]))
        return
    args.lane_id = lane_id
    editing = args.words[0] == "edit"

    def fn(d):
        require_human_or_direct(d, actor)
        exists = any(l["id"] == lane_id for l in d["lanes"])
        if editing and not exists:
            raise RBError(f"unknown lane {lane_id!r}", "unknown_lane", lane=lane_id)
        if editing:
            op_lane_edit(d, actor, lane_id, None if not args.title else args.title, getattr(args, "color", None), getattr(args, "agent", None),
                         getattr(args, "unattended", None))
        else:
            op_lane_add(d, actor, lane_id, args.title, getattr(args, "color", None), getattr(args, "agent", None))
    mutate(board, fn)
    print(f"lane {lane_id} {'updated' if editing else 'added'}")


def require_human_or_direct(d, actor):
    if not direct_allowed(d, actor):
        raise RBError("only the person (or a trusted planner agent) can change lanes, resources and policy")


def cmd_resource(args):
    board = find_board(getattr(args, "board", None))
    actor = cli_actor(args)
    name = _add_word(args.words, "resource")
    if name is None:
        print("\n".join(f"{k:<12} {v.get('label', k)}{'' if v.get('exclusive', True) else ' (shared)'}" for k, v in load(board).get("resources", {}).items()))
        return
    args.name = name

    def fn(d):
        require_human_or_direct(d, actor)
        d.setdefault("resources", {})[args.name] = {"label": args.label or args.name, "exclusive": not args.shared}
        log_event(d, actor, "resource", args.name)
    mutate(board, fn)
    print(f"resource {args.name} saved")


def cmd_context(args):
    board = find_board(getattr(args, "board", None))
    actor = cli_actor(args)
    if args.action == "list":
        print("\n".join(load(board).get("context", [])) or "(empty)")
        return

    def fn(d):
        require_human_or_direct(d, actor)
        ctx = d.setdefault("context", [])
        if args.action == "add" and args.path not in ctx:
            ctx.append(args.path)
        if args.action == "remove" and args.path in ctx:
            ctx.remove(args.path)
        log_event(d, actor, "context", args.path or "")
    mutate(board, fn)


def cmd_trust(args):
    board = find_board(getattr(args, "board", None))
    actor = cli_actor(args)

    def fn(d):
        require_human(actor, "change who may add tasks directly")
        direct = d.setdefault("policy", {}).setdefault("direct_agents", [])
        if args.action == "add" and args.name not in direct:
            direct.append(args.name)
        if args.action == "remove" and args.name in direct:
            direct.remove(args.name)
        log_event(d, actor, "trust", args.name, args.action)
        return direct
    direct = mutate(board, fn)
    print("agents that may add tasks without approval: " + (", ".join(direct) or "(none)"))


def cmd_log(args):
    d = load(find_board(getattr(args, "board", None)))
    ev = d.get("events", [])[-args.n:]
    out(args, ev, "\n".join(f"{e['t']}  {e['by']:<12} {e['action']:<10} {e['id']} {e.get('detail', '')}".rstrip() for e in ev) or "(no events)")


def cmd_template(args):
    if args.name:
        print(template_text(args.name))
        return
    tdir = HERE.parent / "templates"
    names = sorted(p.stem for p in tdir.glob("*.md")) if tdir.is_dir() else []
    print("\n".join(names) or "task (built-in)")


def cmd_doctor(args):
    ok = True

    def line(good, msg):
        nonlocal ok
        ok = ok and good
        print(("ok    " if good else "FAIL  ") + msg)
    line(sys.version_info >= (3, 9), f"python {sys.version.split()[0]} (needs 3.9+)")
    line((HERE / "panel.html").is_file(), "panel.html next to barid.py")
    try:
        board = find_board(getattr(args, "board", None))
        d = load(board)
        line(True, f"board {board} ({len(d['items'])} tasks, rev {d.get('rev')})")
        with locked(board):
            pass
        line(True, "lock works")
        line(not has_cycle({i["id"]: i for i in d["items"]}), "no dependency cycles")
        info = server_alive(board)
        print("info  panel server: " + (f"running on port {info['port']}" if info else "not running (starts on `barid open`)"))
    except RBError as e:
        line(False, str(e))
    sys.exit(0 if ok else 1)


def cmd_shim(args):
    me = str(Path(__file__).resolve())
    if os.name == "nt":
        target = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "bin" / "barid.cmd"
        body = f'@echo off\r\npython "{me}" %*\r\n'
    else:
        target = Path.home() / ".local" / "bin" / "barid"
        body = f'#!/bin/sh\nexec python3 "{me}" "$@"\n'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, "utf-8")
    if os.name != "nt":
        target.chmod(0o755)
    print(f"wrote {target}; make sure its folder is on your PATH")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--board", default=argparse.SUPPRESS, help="path to board.json or its folder (or set BARID_BOARD)")
    common.add_argument("--by", default=argparse.SUPPRESS, help="your agent name (or set BARID_AGENT)")
    common.add_argument("--human", action="store_true", default=argparse.SUPPRESS, help="act as the person (or set BARID_ACTOR=human)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    p = argparse.ArgumentParser(prog="barid", parents=[common], description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"Barid {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, **kw)
        sp.set_defaults(fn=fn)
        return sp

    s = add("init", cmd_init, "create a board in .barid/ of the current folder")
    s.add_argument("--project")
    s.add_argument("--lanes", default="main,second", help="comma list, id or id:Title (one lane per agent session)")
    s.add_argument("--lang", default=None, choices=["en", "ru", "kk", "kz"], help="language of the texts written for agents (default: from your system language, else en)")
    s.add_argument("--force", action="store_true")
    add("where", cmd_where, "print the board file in use")

    s = add("add", cmd_add, "add a task (agents' tasks become proposals unless trusted)")
    s.add_argument("id")
    s.add_argument("--lane")
    s.add_argument("--title")
    s.add_argument("--needs", help="comma list of task ids that must be finished first")
    s.add_argument("--uses", help="comma list of exclusive resources, e.g. gpu,user")
    s.add_argument("--quiet", action="store_true", help="needs a quiet machine (no noisy task in parallel)")
    s.add_argument("--noisy", action="store_true", help="loads CPU or the desktop (blocks quiet tasks)")
    s.add_argument("--profile", help="a bundle of marks: light, dev, bench, attended or your own (`barid profile list`)")
    s.add_argument("--lint-ok", action="append", metavar="CODE", help="silence one suggestion of `barid lint` for this task (repeatable)")
    s.add_argument("--touches", help="comma list of files, folders or globs this task may change; tasks with overlapping scopes never run together")
    s.add_argument("--workdir", help="the only checkout this task may work in (see `worktree`)")
    s.add_argument("--after", help="comma list of task ids to run after, whatever their outcome (`--needs` waits for a successful or accepted one)")
    s.add_argument("--not-before", dest="not_before", help="earliest start: 23:00 (next time it is 23:00), '2026-10-07 23:00', +90m, +3h, +1d; empty clears it")
    s.add_argument("--timebox", help="how long the task may run: 90 (minutes), 90m, 8h, 1d; a worker stops it after that")
    s.add_argument("--when")
    s.add_argument("--outline", help="short goal; used to generate the full prompt later")
    s.add_argument("--text", help="prompt text, or - for stdin")
    s.add_argument("--text-file")
    s.add_argument("--draft", action="store_true", help="keep as a draft even if text is given")

    s = add("edit", cmd_edit, "change a task")
    s.add_argument("id")
    for k in ("title", "lane", "needs", "after", "uses", "when", "outline", "text", "text-file", "touches", "workdir", "not-before", "timebox"):
        s.add_argument("--" + k)
    s.add_argument("--quiet", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--noisy", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--profile", help="a bundle of marks; an empty value removes it")
    s.add_argument("--lint-ok", action="append", metavar="CODE", default=None, help="silence one suggestion of `barid lint` (repeatable; replaces the list)")
    s.add_argument("--replace", nargs=2, metavar=("OLD", "NEW"), help="replace one exact fragment of the prompt text")

    s = add("lint", cmd_lint, "check that every open task says enough about what it needs (profile, resources, quiet/noisy, files) to be checked")
    s.add_argument("id", nargs="?")
    s.add_argument("--strict", action="store_true", help="exit code 1 if anything is suggested")
    s = add("explain", cmd_explain, "why two tasks can or cannot run together, rule by rule")
    s.add_argument("a")
    s.add_argument("b")
    s = add("profile", cmd_profile, "named bundles of marks: `profile list`, `profile add NAME --uses gpu --quiet`, `profile rm NAME`")
    s.add_argument("action", choices=["list", "add", "rm"])
    s.add_argument("name", nargs="?")
    s.add_argument("--uses")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--noisy", action="store_true")
    s.add_argument("--label")
    s = add("list", cmd_list, "list tasks with their computed state")
    s.add_argument("--lane")
    s.add_argument("--status")
    s.add_argument("--all", action="store_true", help="include done and cancelled")
    add("plan", cmd_plan, "show the steps and what can run at the same time")
    s = add("show", cmd_show, "show one task (--text prints the prompt with its tracking footer)")
    s.add_argument("id")
    s.add_argument("--text", action="store_true")
    s = add("next", cmd_next, "print the next task that is ready for a lane (exit code 2: nothing queued, 3: wait and ask again, 4: waits for the person)")
    s.add_argument("--lane")
    s.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="when the next task only has to wait (a start time, other tasks, a conflict), wait up to this long for it instead of answering at once")
    s = add("worker", cmd_worker, "run a lane unattended: for each task start your agent command (fresh context, prompt on stdin), keep the lease, enforce the timebox, then take the next")
    s.add_argument("--lane", required=True)
    s.add_argument("--cmd", required=True, help="the agent command, e.g. 'codex exec -' or 'claude -p'; it reads the prompt on stdin, or from $BARID_PROMPT_FILE / {prompt_file}")
    s.add_argument("--cwd", help="folder to run the command in")
    s.add_argument("--poll", type=int, default=20, metavar="SECONDS", help="how often to look for work while tasks wait (default 20)")
    s.add_argument("--max-wait", type=int, default=14 * 3600, metavar="SECONDS", help="give up when nothing could start for this long (default 14 hours)")
    s.add_argument("--max-tasks", type=int, default=0, metavar="N", help="stop after N tasks (default: until the lane is empty)")
    s.add_argument("--once", action="store_true", help="run one task and exit")
    s = add("digest", cmd_digest, "what happened while you were away: sessions, finished tasks and their reports, what needs you, what waits and why")
    s.add_argument("--since", default=None, metavar="WHEN", help="12h (default), 90m, 2d, or a time such as '2026-10-07 08:00'")
    s = add("lang", cmd_lang, "show or set the language of the texts written for agents (en, ru, kk); the panel language is separate")
    s.add_argument("value", nargs="?", choices=["en", "ru", "kk", "kz"])
    s = add("connect", cmd_connect, "print the message that connects an agent session to a lane (paste it as the session's first message)")
    s.add_argument("lane")
    s.add_argument("--unattended", action=argparse.BooleanOptionalAction, default=None, help="the night version (wait for the turn, never stop to ask); default: what the lane is set to")
    s = add("gen", cmd_gen, "print the request that makes an agent write this task's prompt")
    s.add_argument("id")

    s = add("claim", cmd_claim, "agent: take a task and start working (refused on conflicts; exit code 3: it must wait, 4: it waits for the person)")
    s.add_argument("id")
    s.add_argument("--force", action="store_true")
    s.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="when the task only has to wait (a start time, other tasks, a conflict), wait up to this long and then take it")
    s = add("finish", cmd_finish, "agent: report that the task is done")
    s.add_argument("id")
    s.add_argument("--report", help="path of the report file")
    s.add_argument("--no-report", action="store_true")
    s.add_argument("--outcome", choices=OUTCOMES, default="", help="how it ended; only 'complete' unlocks dependents by itself")
    s.add_argument("--note")
    s.add_argument("--force", action="store_true")
    s = add("outcome", simple(lambda d, a, g: op_outcome(d, a, g.id, g.value), "{id} outcome changed"), "correct how a finished task ended (person or trusted planner)")
    s.add_argument("id")
    s.add_argument("value", choices=OUTCOMES)
    s = add("heartbeat", simple(lambda d, a, g: op_heartbeat(d, a, g.id), "{id} lease extended"), "agent: extend the lease of a running task")
    s.add_argument("id")
    s = add("release", simple(lambda d, a, g: op_release(d, a, g.id, g.force), "{id} released"), "give a task back to the queue")
    s.add_argument("id")
    s.add_argument("--force", action="store_true")
    s = add("note", simple(lambda d, a, g: op_note(d, a, g.id, g.text), "note added to {id}"), "add a note to a task")
    s.add_argument("id")
    s.add_argument("text")
    s = add("cancel", simple(lambda d, a, g: op_cancel(d, a, g.id, g.reason or ""), "{id} cancelled"), "cancel a task (kept in the archive)")
    s.add_argument("id")
    s.add_argument("--reason")

    for name, op, msg, help_ in (
        ("approve", op_approve, "{id} approved", "person: approve an agent's proposal"),
        ("accept", op_accept, "{id} accepted as done", "person: accept a finished report"),
        ("restore", op_restore, "{id} restored", "person: bring back a cancelled task"),
        ("sent", op_sent, "{id} marked as sent", "person: mark a prompt as handed to a session"),
        ("purge", op_purge, "{id} removed for good", "person: delete a task permanently"),
    ):
        s = add(name, simple((lambda o: (lambda d, a, g: o(d, a, g.id)))(op), msg), help_)
        s.add_argument("id")
    s = add("reject", simple(lambda d, a, g: op_reject(d, a, g.id, g.reason or ""), "{id} rejected"), "person: reject a proposal")
    s.add_argument("id")
    s.add_argument("--reason")
    s = add("move", simple(lambda d, a, g: op_move(d, a, g.id, g.position), "{id} moved"), "reorder: tasks earlier in the list get earlier slots in the plan")
    s.add_argument("id")
    s.add_argument("position", type=int, help="1 = first")
    s = add("status", simple(lambda d, a, g: op_set_status(d, a, g.id, g.value), "{id} status changed"), "person: set any status")
    s.add_argument("id")
    s.add_argument("value", choices=STATUSES)

    s = add("export", cmd_export, "write a read-only HTML snapshot of the board (for sharing or screenshots)")
    s.add_argument("file")
    s.add_argument("--no-text", action="store_true", help="leave prompt texts out of the snapshot")
    s = add("protect", cmd_protect, "paths no task may change (verified by checksum when a task finishes): `protect add PATH`, `remove PATH`, `list`")
    s.add_argument("words", nargs="+", metavar="add|remove|list [PATH]")
    s = add("worktree", cmd_worktree, "create an isolated git worktree and branch for a task")
    s.add_argument("id")
    s.add_argument("--repo", help="the git repository (default: the project folder)")
    s.add_argument("--branch", help="default: barid/<ID>")
    s.add_argument("--path", help="default: <repo>-barid-<ID> next to the repository")
    s.add_argument("--base", help="start point, default HEAD")
    s = add("check", cmd_check, "show what could collide with a task (overlapping scopes, protected paths)")
    s.add_argument("id")
    s = add("lane", cmd_lane, "a lane is a session that receives prompts: `lane add ID --title T --color #hex --agent codex`, `lane edit ID ...`, `lane list`")
    s.add_argument("words", nargs="+", metavar="[add|edit] ID")
    s.add_argument("--title")
    s.add_argument("--color", help="e.g. #feb83b; empty resets to the palette default")
    s.add_argument("--agent", help="which agent runs there: claude, codex, opencode, gemini, cursor, aider, local, or any name (max 24 characters)")
    s.add_argument("--unattended", action=argparse.BooleanOptionalAction, default=None, help="a session that works through the night: its connection text tells it to wait for its turn and never stop to ask")
    s = add("resource", cmd_resource, "define a resource tasks can use: `resource add NAME --label L`, or `resource list`")
    s.add_argument("words", nargs="+", metavar="[add] NAME")
    s.add_argument("--label")
    s.add_argument("--shared", action="store_true", help="not exclusive")
    s = add("context", cmd_context, "files agents should read when generating prompts")
    s.add_argument("action", choices=["add", "remove", "list"])
    s.add_argument("path", nargs="?")
    s = add("trust", cmd_trust, "person: let an agent add tasks without approval")
    s.add_argument("action", choices=["add", "remove"])
    s.add_argument("name")
    s = add("log", cmd_log, "show recent events")
    s.add_argument("-n", type=int, default=20)
    s = add("template", cmd_template, "list or print prompt skeletons")
    s.add_argument("name", nargs="?")

    s = add("open", cmd_open, "start the panel in the background and open it in the browser")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--idle-exit", type=float, default=30.0, help="minutes without requests before the server exits (0 = never)")
    s = add("serve", cmd_serve, "run the panel server in the foreground")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--idle-exit", type=float, default=0.0)
    add("doctor", cmd_doctor, "check the installation and the board")
    add("shim", cmd_shim, "install a short `barid` command")
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a Russian title must never crash a narrow Windows console
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except RBError as e:
        print(f"rb: {e}", file=sys.stderr)
        return {"begin_time": EXIT_WAIT, "waiting": EXIT_WAIT, "conflict": EXIT_WAIT, "stuck": EXIT_STUCK}.get(e.code, 1)
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
