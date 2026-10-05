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
import subprocess
import sys
import textwrap
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.3.0"
SCHEMA = 1
HERE = Path(__file__).resolve().parent
STATUSES = ("draft", "proposed", "queued", "sent", "running", "review", "done", "cancelled")
ACTIVE = ("sent", "running")
OUTCOMES = ("complete", "partial", "failed")  # how a finished task ended; only "complete" unlocks dependents by itself
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


def new_board(project: str, lanes, lang: str = "en") -> dict:
    return {
        "schema": SCHEMA, "project": project, "rev": 1, "created": now_iso(), "updated": now_iso(), "lang": lang,
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


def conflict_reason(d: dict, a: dict, b: dict):
    """Why two tasks must not run at the same time (None if they can)."""
    if a["lane"] == b["lane"]:
        return "lane"
    res = d.get("resources", {})
    for r in a.get("uses", []):
        if r in b.get("uses", []) and res.get(r, {}).get("exclusive", True):
            return r
    if (a.get("quiet") and b.get("noisy")) or (a.get("noisy") and b.get("quiet")):
        return "quiet"
    # different checkouts (git worktrees) cannot overwrite each other's files; their changes meet at merge time
    if a.get("touches") and b.get("touches") and _case(str(task_root(d, a))) == _case(str(task_root(d, b))):
        for x in own_scopes(d, a):
            for y in own_scopes(d, b):
                if scopes_overlap(x, y):
                    return "paths:" + show_scope(d, x if len(x) >= len(y) else y, task_root(d, a))
    return None


def _satisfied(d: dict, items: dict, nid: str) -> bool:
    n = items.get(nid)
    if not n:
        return False
    if n["status"] == "done":
        return True  # the person accepted it (even a partial result)
    return (n["status"] == "review" and n.get("outcome") == "complete" and not n.get("violations")
            and not d.get("policy", {}).get("dependents_wait_for_accept", False))


def _closure(items: dict, iid: str, seen=None) -> set:
    seen = seen if seen is not None else set()
    for n in items.get(iid, {}).get("needs", []):
        if n not in seen:
            seen.add(n)
            _closure(items, n, seen)
    return seen


def compute(d: dict, now=None) -> list:
    """Return the tasks with computed fields: state, waiting_on, conflicts, can_run_with, stale."""
    now = now or now_dt()
    items = {i["id"]: i for i in d["items"]}
    active = [i for i in d["items"] if i["status"] in ACTIVE]
    out = []
    for i in d["items"]:
        c = dict(i)
        c["waiting_on"] = [n for n in i.get("needs", []) if not _satisfied(d, items, n)]
        c["waiting_info"] = {n: {"status": items[n]["status"], "outcome": items[n].get("outcome", "")} for n in c["waiting_on"] if n in items}
        c["conflicts"] = []
        for a in active:
            if a["id"] != i["id"]:
                r = conflict_reason(d, i, a)
                if r:
                    c["conflicts"].append({"id": a["id"], "why": r})
        if i["status"] == "queued":
            c["state"] = "waiting" if c["waiting_on"] else ("blocked" if c["conflicts"] else "ready")
        else:
            c["state"] = i["status"]
        cl = i.get("claim")
        ts = parse_ts(cl.get("lease_until")) if cl else None
        c["stale"] = bool(i["status"] == "running" and ts and ts < now)
        c["can_run_with"], c["cannot_run_with"] = [], []
        out.append(c)
    live = [c for c in out if c["state"] in ("ready", "blocked", "sent", "running")]
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
            unfinished = [n for n in c.get("needs", []) if n in liveids]
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
    state = {}

    def visit(n):
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        for m in items[n].get("needs", []):
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


def check_refs(d: dict, lane, needs, uses) -> None:
    if lane is not None and lane not in [l["id"] for l in d["lanes"]]:
        raise RBError(f"unknown lane {lane!r}; lanes: {', '.join(l['id'] for l in d['lanes'])} (add one with `barid lane add`)", "unknown_lane", lane=lane)
    ids = {i["id"] for i in d["items"]}
    for n in needs or []:
        if n not in ids:
            raise RBError(f"unknown task in needs: {n}", "unknown_task", id=n)
    for r in uses or []:
        if r not in d.get("resources", {}):
            raise RBError(f"unknown resource {r!r}; define it with `barid resource add {r}`", "unknown_resource", name=r)


def op_add(d: dict, actor: Actor, f: dict) -> dict:
    iid = f["id"]
    if not ID_RE.match(iid or ""):
        raise RBError("task id: letters, digits, '.', '_' or '-', up to 32 characters, starting with a letter or digit", "bad_id")
    if any(i["id"] == iid for i in d["items"]):
        raise RBError(f"task {iid} already exists", "exists", id=iid)
    lane = f.get("lane") or d["lanes"][0]["id"]
    needs, uses = split_list(f.get("needs")), split_list(f.get("uses"))
    check_refs(d, lane, needs, uses)
    text = (f.get("text") or "").strip()
    status = "draft" if not text else "queued"
    if f.get("draft"):
        status = "draft"
    if not direct_allowed(d, actor):
        status = "proposed"
    item = {
        "id": iid, "lane": lane, "title": (f.get("title") or iid).strip(), "status": status, "needs": needs, "uses": uses,
        "quiet": bool(f.get("quiet")), "noisy": bool(f.get("noisy")), "when": f.get("when") or "", "outline": f.get("outline") or "",
        "touches": split_list(f.get("touches")), "workdir": f.get("workdir") or "", "branch": f.get("branch") or "",
        "text": text, "report": "", "notes": [], "created": now_iso(), "updated": now_iso(), "created_by": actor.name, "claim": None,
    }
    d["items"].append(item)
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
    if f.get("needs") is not None:
        needs = split_list(f["needs"])
        if iid in needs:
            raise RBError("a task cannot need itself", "cycle")
        check_refs(d, None, needs, None)
        old = it["needs"]
        it["needs"] = needs
        if has_cycle({i["id"]: i for i in d["items"]}):
            it["needs"] = old
            raise RBError("that would create a dependency cycle", "cycle")
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


def op_claim(d: dict, actor: Actor, iid: str, force: bool = False) -> list:
    it = get_item(d, iid)
    if it["status"] not in ("queued", "sent") and not force:
        raise RBError(f"{iid} is {it['status']} and cannot be claimed (only queued or sent tasks can)", "wrong_state", id=iid, status=it["status"])
    comp = {c["id"]: c for c in compute(d)}[iid]
    if comp["waiting_on"] and not force:
        raise RBError(f"{iid} must wait for: {', '.join(comp['waiting_on'])}")
    warnings = []
    for c in comp["conflicts"]:
        other = get_item(d, c["id"])
        msg = f"{c['id']} ({other['status']}) conflicts on '{c['why']}'"
        if it["status"] == "sent" or force:
            warnings.append(msg)
        else:
            raise RBError(f"refused: {msg}; wait for it to finish, or ask the person")
    hours = float(d.get("policy", {}).get("lease_hours", 12))
    it["status"] = "running"
    it.pop("outcome", None)
    it["claim"] = {"by": actor.name, "at": now_iso(), "lease_until": (now_dt() + dt.timedelta(hours=hours)).isoformat()}
    it["claim"].update(snapshot_for_claim(d, it))
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
    it["status"] = "review"
    it["outcome"] = outcome
    it["report"] = report or it.get("report", "")
    it["claim"] = None
    it["updated"] = now_iso()
    if note:
        it["notes"].append({"t": now_iso(), "by": actor.name, "text": note})
    log_event(d, actor, "finish", iid, f"{outcome or 'no outcome stated'}: {report}" + (f" ({len(violations)} file warnings)" if violations else ""))
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


def op_heartbeat(d: dict, actor: Actor, iid: str) -> dict:
    it = get_item(d, iid)
    if it["status"] != "running":
        raise RBError(f"{iid} is not running")
    _owner_check(it, actor, False, "extend")
    hours = float(d.get("policy", {}).get("lease_hours", 12))
    it["claim"]["lease_until"] = (now_dt() + dt.timedelta(hours=hours)).isoformat()
    log_event(d, actor, "heartbeat", iid)
    return it


def op_release(d: dict, actor: Actor, iid: str, force: bool = False) -> dict:
    it = get_item(d, iid)
    if it["status"] not in ("running", "sent"):
        raise RBError(f"{iid} is {it['status']}: nothing to release", "wrong_state", id=iid, status=it["status"])
    _owner_check(it, actor, force, "release")
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


def op_lane_edit(d: dict, actor: Actor, lane_id: str, title=None, color=None, agent=None) -> dict:
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
    log_event(d, actor, "lane", lane_id, "edit")
    return lane


def op_purge(d: dict, actor: Actor, iid: str) -> None:
    require_human(actor, "purge a task for good")
    it = get_item(d, iid)
    if any(iid in i.get("needs", []) for i in d["items"] if i["id"] != iid):
        raise RBError(f"other tasks still need {iid}")
    d["items"].remove(it)
    log_event(d, actor, "purge", iid)


# ----------------------------------------------------------------------------- prompts for agents

FOOTER = {
    "en": textwrap.dedent("""\
        ---
        Barid tracking: this task ({id}) lives on a shared board. Use Python 3, nothing to install.
        1. Before you start:  {py} "{rb}" --board "{board}" claim {id} --by "<your agent name>"
           If this is refused, STOP and tell the user why.
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
           Если команда отказала, ОСТАНОВИСЬ и сообщи пользователю причину.
        2. Когда остановился и записал отчёт:
           {py} "{rb}" --board "{board}" finish {id} --report "<путь к твоему отчёту>" --outcome <complete|partial|failed> --by "<имя твоего агента>"
           Честно оцени итог: complete = все критерии приёмки выполнены и ничего не осталось; partial = остановился раньше (порог безопасности,
           срок, ошибка) или часть не сделана; failed = главная цель не достигнута. Только complete сам запускает зависимые задачи.
        3. Заметка по ходу работы (необязательно):  {py} "{rb}" --board "{board}" note {id} "<текст>" --by "<имя твоего агента>"
        Трогай только задачу {id}: чужие задачи не меняй, не отменяй и не удаляй. Чтобы предложить следующую задачу:
        {py} "{rb}" --board "{board}" add <НОВЫЙ_ID> --lane <дорожка> --title "<заголовок>" --text-file <файл> --by "<имя твоего агента>"
        (она сохранится как предложение, пользователь его одобрит)."""),
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


def footer_for(d: dict, it: dict, board: Path) -> str:
    lang = d.get("lang", "en")
    tpl = FOOTER.get(lang, FOOTER["en"])
    base = tpl.format(id=it["id"], py=py_cmd(), rb=str(Path(__file__).resolve()), board=str(board))
    extra = scope_footer(d, it)
    return base + ("\n" + extra if extra else "")


HANDOFF = {
    "en": {"head": "Handoff from the tasks this one builds on (read it first):", "outcome": "outcome", "report": "report", "by": "done by", "branch": "branch",
           "files": "files changed", "note": "note", "unstated": "not stated"},
    "ru": {"head": "Передача от задач, на которых строится эта (прочитай сначала):", "outcome": "итог", "report": "отчёт", "by": "делал", "branch": "ветка",
           "files": "изменено файлов", "note": "заметка", "unstated": "не указан"},
}

AGENT_HINT = {
    "en": "You may be a smaller or local model: work through the steps one at a time, run the commands exactly as written, and say so in your report if a step is unclear instead of guessing.",
    "ru": "Ты можешь быть небольшой или локальной моделью: выполняй шаги по одному, запускай команды ровно как написано, а если шаг неясен, скажи об этом в отчёте, а не угадывай.",
}


def handoff_text(d: dict, it: dict) -> str:
    """What the finished tasks this one needs hand over: outcome, report, who did it, branch, changed files, last notes."""
    if not d.get("policy", {}).get("handoff", True):
        return ""
    tx = HANDOFF.get(d.get("lang", "en"), HANDOFF["en"])
    byid = {i["id"]: i for i in d["items"]}
    lanes = {l["id"]: l for l in d["lanes"]}
    blocks = []
    for nid in it.get("needs", []):
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


def prompt_for(d: dict, it: dict, board: Path) -> str:
    text = it.get("text", "").strip()
    if not text:
        return ""
    extra = [x for x in (handoff_text(d, it), agent_hint(d, it)) if x]
    if extra:
        text = text + "\n\n" + "\n\n".join(extra)
    if d.get("policy", {}).get("footer", True):
        return text + "\n\n" + footer_for(d, it, board)
    return text


def template_text(name: str = "task") -> str:
    f = HERE.parent / "templates" / f"{name}.md"
    return f.read_text("utf-8").strip() if f.is_file() else DEFAULT_TEMPLATE


def gen_request(d: dict, it: dict, board: Path) -> str:
    lang = d.get("lang", "en")
    byid = {i["id"]: i for i in d["items"]}
    reports = []
    for n in it.get("needs", []):
        r = byid.get(n, {}).get("report")
        if r:
            reports.append(f"- {n}: {r}")
    ctx = d.get("context", [])
    head_r = ("Reports of the tasks this one depends on (read them first):\n" if lang == "en" else "Отчёты задач, от которых зависит эта (прочитай сначала):\n")
    head_c = ("Project files worth reading:\n" if lang == "en" else "Файлы проекта, которые стоит прочитать:\n")
    return GEN.get(lang, GEN["en"]).format(
        id=it["id"], title=it["title"], project=d.get("project", ""), lane=next((l.get("title") or l["id"] for l in d["lanes"] if l["id"] == it["lane"]), it["lane"]), outline=it.get("outline") or it["title"],
        needs=", ".join(it.get("needs", [])) or "-", uses=", ".join(it.get("uses", [])) or "-",
        quiet="yes" if it.get("quiet") else "no", noisy="yes" if it.get("noisy") else "no",
        reports=(head_r + "\n".join(reports) + "\n") if reports else "",
        context=(head_c + "\n".join(f"- {c}" for c in ctx) + "\n") if ctx else "",
        template=textwrap.indent(template_text(), "  "), py=py_cmd(), rb=str(Path(__file__).resolve()), board=str(board))


# ----------------------------------------------------------------------------- views

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
        "items": comp, "steps": steps, "next_by_lane": now_by_lane, "events": d.get("events", [])[-60:],
        "proposed": [c["id"] for c in comp if c["state"] == "proposed"],
        "review": [c["id"] for c in comp if c["state"] == "review"],
    }


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


def apply_action(board: Path, req: dict):
    """Panel actions: always performed as the person."""
    a = str(req.get("action", ""))
    iid = str(req.get("id", ""))
    args = req.get("args") or {}
    h = HUMAN

    def fn(d):
        if a == "add":
            f = dict(args)
            f["id"] = iid
            return op_add(d, h, f)["id"]
        if a == "edit":
            f = {k: args.get(k) for k in ("title", "lane", "needs", "uses", "quiet", "noisy", "when", "outline", "text", "touches", "workdir", "branch") if k in args}
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
        if a == "laneadd":
            lane = op_lane_add(d, h, iid, args.get("title"), args.get("color"), args.get("agent"))
            return {"id": lane["id"]}
        if a == "lane":
            lane = op_lane_edit(d, h, iid, args.get("title"), args.get("color"), args.get("agent"))
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


def serve(board: Path, host: str = "127.0.0.1", port: int = 0, idle_exit: float = 0.0, announce=None) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RBError("Barid binds to loopback addresses only (there is no authentication)")
    tracker = {"last": time.time()}
    handler = make_handler(board, tracker)
    srv = None
    if os.environ.get("LISTEN_FDS") == "1":  # systemd socket activation
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler, bind_and_activate=False)
        srv.socket.close()
        srv.socket = socket.socket(fileno=3)
        srv.server_address = srv.socket.getsockname()
    else:
        for p in ([port] if port else range(DEFAULT_PORT, DEFAULT_PORT + 30)):
            try:
                srv = ThreadingHTTPServer((host, p), handler)
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
    save(base, new_board(args.project or Path.cwd().name, lanes, args.lang))
    print(f"created {base}")
    print("next: add tasks with `barid add`, then look at them with `barid open`")


def cmd_where(args):
    print(find_board(getattr(args, "board", None)))


def cmd_add(args):
    board = find_board(getattr(args, "board", None))
    f = {"id": args.id, "lane": args.lane, "title": args.title, "needs": args.needs, "uses": args.uses, "quiet": args.quiet,
         "noisy": args.noisy, "when": args.when, "outline": args.outline, "text": read_text_arg(args), "draft": args.draft,
         "touches": args.touches, "workdir": args.workdir}
    item = mutate(board, lambda d: op_add(d, cli_actor(args), f))
    out(args, item, f"{item['id']} added as {item['status']}" + (" (waiting for the person to approve it)" if item["status"] == "proposed" else ""))


def cmd_edit(args):
    board = find_board(getattr(args, "board", None))
    f = {"title": args.title, "lane": args.lane, "needs": args.needs, "uses": args.uses, "when": args.when, "outline": args.outline,
         "quiet": args.quiet, "noisy": args.noisy, "text": read_text_arg(args), "touches": args.touches, "workdir": args.workdir}
    if args.replace:
        f["replace"] = args.replace
    f = {k: v for k, v in f.items() if v is not None}
    item = mutate(board, lambda d: op_edit(d, cli_actor(args), args.id, f))
    out(args, item, f"{item['id']} updated (status {item['status']})")


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
    board = find_board(getattr(args, "board", None))
    d = load(board)
    comp = compute(d)
    for c in comp:
        if c["state"] == "ready" and (not args.lane or c["lane"] == args.lane):
            it = get_item(d, c["id"])
            if getattr(args, "json", False):
                print(json.dumps({"id": c["id"], "title": c["title"], "lane": c["lane"], "prompt": prompt_for(d, it, board)}, ensure_ascii=False, indent=1))
            else:
                print(f"# {c['id']}: {c['title']}\n")
                print(prompt_for(d, it, board))
            return
    waiting = [c for c in comp if c["state"] in ("waiting", "blocked") and (not args.lane or c["lane"] == args.lane)]
    msg = "no task is ready for you right now"
    if waiting:
        msg += ":\n" + "\n".join("  " + fmt_line(c) for c in waiting[:5])
    if getattr(args, "json", False):
        print(json.dumps({"id": None, "reason": msg}))
    else:
        print(msg)
    sys.exit(2)


def cmd_gen(args):
    board = find_board(getattr(args, "board", None))
    d = load(board)
    print(gen_request(d, get_item(d, args.id), board))


def cmd_claim(args):
    board = find_board(getattr(args, "board", None))
    warnings = mutate(board, lambda d: op_claim(d, cli_actor(args), args.id, args.force))
    out(args, {"id": args.id, "warnings": warnings}, f"{args.id} claimed by {cli_actor(args).name}" + ("".join(f"\nwarning: {w}" for w in warnings)))


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
            op_lane_edit(d, actor, lane_id, None if not args.title else args.title, getattr(args, "color", None), getattr(args, "agent", None))
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
    s.add_argument("--lang", default="en", choices=["en", "ru"])
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
    s.add_argument("--touches", help="comma list of files, folders or globs this task may change; tasks with overlapping scopes never run together")
    s.add_argument("--workdir", help="the only checkout this task may work in (see `worktree`)")
    s.add_argument("--when")
    s.add_argument("--outline", help="short goal; used to generate the full prompt later")
    s.add_argument("--text", help="prompt text, or - for stdin")
    s.add_argument("--text-file")
    s.add_argument("--draft", action="store_true", help="keep as a draft even if text is given")

    s = add("edit", cmd_edit, "change a task")
    s.add_argument("id")
    for k in ("title", "lane", "needs", "uses", "when", "outline", "text", "text-file", "touches", "workdir"):
        s.add_argument("--" + k)
    s.add_argument("--quiet", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--noisy", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--replace", nargs=2, metavar=("OLD", "NEW"), help="replace one exact fragment of the prompt text")

    s = add("list", cmd_list, "list tasks with their computed state")
    s.add_argument("--lane")
    s.add_argument("--status")
    s.add_argument("--all", action="store_true", help="include done and cancelled")
    add("plan", cmd_plan, "show the steps and what can run at the same time")
    s = add("show", cmd_show, "show one task (--text prints the prompt with its tracking footer)")
    s.add_argument("id")
    s.add_argument("--text", action="store_true")
    s = add("next", cmd_next, "print the next task that is ready for a lane (exit code 2 if none)")
    s.add_argument("--lane")
    s = add("gen", cmd_gen, "print the request that makes an agent write this task's prompt")
    s.add_argument("id")

    s = add("claim", cmd_claim, "agent: take a task and start working (refused on conflicts)")
    s.add_argument("id")
    s.add_argument("--force", action="store_true")
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
        return 1
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
