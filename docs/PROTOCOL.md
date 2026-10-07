# Barid protocol

The rules every client (CLI, panel, agent) follows, and the file format. Implemented in `skills/barid/scripts/barid.py`.

## Roles

| Role | Who | Can |
|---|---|---|
| person (`human`) | you, through the panel or `--human` / `BARID_ACTOR=human` | everything: approve, accept, set any status, restore, purge, change trust |
| trusted agent | a name listed in `policy.direct_agents` (or `*`) | add, edit, cancel tasks directly; add lanes, resources, context |
| agent | any other name (`--by`, `BARID_AGENT`, default `agent`) | propose tasks, claim and finish its own task, note, release its own claim, cancel or edit its own proposal |

Identity is advisory. The board protects against mistakes, not against a hostile process.

## Lanes

A lane is one agent session. `color` (`#rrggbb`, empty = the palette default) and `agent` (a free label up to 24 characters; known ones: `claude`, `codex`, `opencode`, `gemini`, `cursor`, `aider`, `local`) are cosmetic: they change how the panel draws the session, never what the board allows. Only the person or a trusted agent may edit lanes.

## Task states

Stored `status`: `draft` (no prompt text yet), `proposed` (added by an untrusted agent, waits for approval), `queued`, `sent` (the person handed the prompt to a session), `running` (claimed), `review` (finished, report written), `done` (accepted), `cancelled`.

Computed `state` for a queued task: `ready`, `waiting` (some `needs` or `after` are not finished, or `not_before` has not come; `waiting_on` lists the tasks, `begin_in` the seconds left to the start time) or `blocked` (a conflicting task is active; `conflicts` lists `{id, why}`). For every other status `state` equals `status`. A queued task also carries `reasons`, a list of `{code, ...}` with the codes `begin_time`, `dependency`, `conflict` (they end by themselves), `decision` and `never` (they need the person); see [UNATTENDED.md](UNATTENDED.md).

`after` is a second list next to `needs`: it is satisfied when the task is over (`review`, `done` or `cancelled`), whatever the outcome. Both lists count for the order of the plan and for cycle checks. `not_before` (an ISO time) is the earliest start: `claim` is refused with code `begin_time` before it. `timebox` (minutes) is how long the task may run; the claim gets a `deadline` and a lease of at least that plus an hour.

A need is *satisfied* when the task is `done` (the person accepted it, even a partial result), or when it is in `review` with `outcome: "complete"` unless `policy.dependents_wait_for_accept` is true (the person sets it with `barid policy dependents_wait_for_accept true --human`; `barid policy` lists `dependents_wait_for_accept`, `lease_hours`, `footer`, `handoff` and `git_check` with their values). A report that ended `partial`, `failed` or without a stated outcome never unlocks dependents by itself: the person decides (accept as is, or send it back).

## Conflicts and parallelism

Two tasks conflict (must not be active together) when any of these holds:

1. they are in the same lane (`why: "lane"`): a lane is one session;
2. they share an exclusive resource in `uses` (`why: "<resource>"`);
3. one has `quiet: true` and the other `noisy: true` (`why: "quiet"`);
4. their file scopes overlap (`why: "paths:<path>"`): both tasks declare `touches` (files, folders or globs, relative ones resolved against the task's checkout: its `workdir`, else the project folder) and the scopes could name the same file. The test is conservative. Tasks with different `workdir`s never conflict on paths: they work in different checkouts.

Rules 2 and 3 look at the task's *effective* marks: its own `uses`, `quiet`, `noisy` plus those of its `profile`. Built-in profiles: `light` (declares that the task needs nothing special), `dev` (noisy), `bench` (quiet and noisy: a measurement that needs a quiet machine and loads it), `attended` (quiet, and the `user` resource when the board has it). A board can add its own in `profiles` (`barid profile add NAME --uses gpu --quiet`). A resource can be anything exclusive, a port (`port:8091`) or a folder included.

**A task that declares nothing is not checked.** A task is *described* when it has a profile, resources, quiet/noisy marks or file scopes. The panel says "compatible" only when both tasks are described; otherwise it says "not checked". `barid lint` (and every `add`/`edit`) suggests marks the text gives away: it mentions the GPU or a model server without `uses gpu`, builds or tests without noisy, speed measuring without quiet, a port without `port:N` (`--lint-ok CODE` silences one suggestion). `barid explain A B` shows every rule for a pair.

**The run journal does not depend on the marks.** `claim` stores the start, and `finish` stores `ran` and `ran_with`: the tasks that were active at any moment of the run. A task that asked for quiet gets `disturbed_by` when something that loads the machine, or declares nothing, ran next to it; the panel shows it on the report.

Active tasks are those in `sent` or `running`. `claim` is refused when it would conflict with an active task, unless the task is already `sent` (the person chose it, a warning is returned) or `--force` is used.

`can_run_with` lists active/ready tasks that do not conflict and are unrelated by dependencies. `steps` (from `barid plan`) is a greedy schedule in list order: each task goes into the earliest step after its unfinished needs where nothing it conflicts with is placed.

## Files: scopes, protected paths, the git check

- `touches` (per task) and `workdir`/`branch` (set by `barid worktree`) describe where a task may work. `protect` (per board) lists paths no task may change.
- On `claim` Barid stores in the claim a SHA-256 of every file under the protected paths and, when the checkout is a git repository, its `HEAD`.
- On `finish` it recomputes the checksums and asks git for the files that differ from the claim-time `HEAD` plus new untracked files (the board's own `.barid` folder is ignored). It records `changed` (count, first 100 files, repo, branch) and `violations`: `protected` (a protected file changed), `outside_scope` (a changed file is outside the task's declared `touches`, only when it declared any), `collision` (a changed file lies in the scope of another active task that works in the same checkout).
- A task with `violations` is never *satisfied* by `outcome: "complete"` alone; the person accepts it or sends it back.
- Limits: with several sessions in one shared checkout, git cannot tell whose uncommitted change is whose, so use `barid worktree` for parallel code work. This is detection, not enforcement.

## Leases

`claim` records `{by, at, lease_until, signal}` (`policy.lease_hours`, default 12; with a `timebox` also `deadline`, and the lease is at least timebox + 1 hour; `barid worker` uses 3 minutes and renews it once a minute). `heartbeat` extends it and refreshes `signal` (the last sign of life; heartbeats are not logged as events). An expired lease marks the task `stale` in the views but does not free it; the person (or the owner) releases it.

Each lane may carry `seen`: `{at, by, state: idle|waiting|working, task, reason}`, written by `next`, `claim`, `heartbeat`, `note` and `finish` (not by a background process) and shown in the panel; and `unattended: true`, which selects the connection text for a session that waits and never stops to ask.

Exit codes of `next`: `0` a task was printed, `2` nothing is queued in the lane, `3` nothing can start yet but will by itself, `4` nothing will change without the person. `claim` exits `3` when the task must wait (`begin_time`, `waiting`, `conflict`) and `4` when it needs the person (`stuck`); `--wait SECONDS` on both waits for the first kind.

## Operations and who may do them

| Operation | Allowed | Result |
|---|---|---|
| `add` | anyone | `queued` (or `draft` without text); `proposed` for untrusted agents |
| `edit` | person, trusted agents, or the proposer of a `proposed`/`draft` task | text, title, needs (cycle-checked), uses, flags |
| `approve`, `reject` | person | `proposed` to `queued`/`draft`, or to `cancelled` |
| `sent` | person | `queued` to `sent` |
| `claim` | anyone | to `running`, refused on conflicts or unmet needs |
| `finish` | the claimer or the person | to `review`; a report path is required; `--outcome complete\|partial\|failed` says how it ended (be honest: stopping on a safety limit or a deadline is `partial`) |
| `outcome` | person, trusted agents | corrects the outcome of a reported task |
| `release` | the claimer or the person | back to `queued` |
| `accept` | person | `review` to `done` |
| `cancel` | person, trusted agents, the proposer (not while `running`, except the person) | to `cancelled`, kept |
| `restore` | person | `cancelled` to `queued`/`draft` |
| `status` | person | any status |
| `note` | anyone | appends to `notes` |
| `purge` | person, CLI only | removes a task nothing else needs |
| `lane`, `resource`, `context` | person, trusted agents | board configuration |
| `trust` | person | edits `policy.direct_agents` |

## File format (`.barid/board.json`, schema 1)

```json
{
  "schema": 1, "project": "name", "rev": 42, "created": "...", "updated": "...", "lang": "en",
  "lanes": [{"id": "main", "title": "Main session", "color": "#7c5cff", "agent": "codex", "unattended": false, "seen": {"at": "...", "by": "worker-a", "state": "working", "task": "T1", "reason": ""}}],
  "resources": {"gpu": {"label": "GPU", "exclusive": true}},
  "profiles": {"gpu-bench": {"label": "GPU measurement", "uses": ["gpu"], "quiet": true, "noisy": true}},
  "policy": {"direct_agents": ["planner"], "dependents_wait_for_accept": false, "lease_hours": 12, "footer": true},
  "context": ["README.md"],
  "protect": ["config/live.json"],
  "items": [{
    "id": "T1", "lane": "main", "title": "Build", "status": "running",
    "needs": ["T0"], "after": ["T9"], "not_before": "2026-10-07T18:00:00+00:00", "timebox": 480, "uses": ["gpu"], "quiet": false, "noisy": true,
    "when": "", "outline": "", "text": "the prompt without the footer",
    "profile": "dev", "lint_ok": [], "touches": ["src/api"], "workdir": "", "branch": "", "report": "", "outcome": "complete|partial|failed|", "violations": [], "changed": {}, "ran": {"from": "...", "to": "..."}, "ran_with": [{"id": "T2", "lane": "b", "noisy": true, "described": true}], "disturbed_by": [], "notes": [{"t": "...", "by": "agent", "text": "..."}],
    "created": "...", "updated": "...", "created_by": "planner",
    "claim": {"by": "worker-a", "at": "...", "lease_until": "...", "signal": "...", "deadline": "..."}
  }],
  "events": [{"t": "...", "by": "worker-a", "action": "claim", "id": "T1", "detail": ""}]
}
```

`rev` increases on every write. `events` keeps the last 500 entries. All writes go through `barid` (file lock plus atomic replace); do not edit the file by hand while agents are active.

## Handoff

Unless `policy.handoff` is false, the prompt of a task starts (before the footer) with a handoff block for every task it `needs` that is in `review` or `done`: the task, its outcome, who did it (lane title and agent), the report path, the branch and workdir, the number and names of changed files (first 8) and its last two notes. A lane whose `agent` is `local` also gets a one-line hint for smaller models.

## Prompt footer

`barid show ID --text`, `barid next` and the panel's *Copy prompt* return the task text followed by a footer (English, Russian or Kazakh by `lang`, switch off with `policy.footer: false`) that contains the `claim`, `finish` and `note` commands with the absolute path of `barid.py` and of the board file. The footer is generated on the fly and never stored in `text`.

## Panel HTTP API (loopback only)

`GET /` panel, `GET /api/connect/<lane>` (the text that connects an agent window to a lane), `GET /api/board` (computed view), `GET /api/rev`, `GET /api/digest?since=12h` (the digest as JSON), `GET /api/prompt/<id>`, `GET /api/gen/<id>`, `POST /api/act` (JSON `{action, id, args}`, header `X-Barid-Edit: 1`, `Origin` must match `Host`). Requests whose `Host` is not `localhost`, `127.0.0.1`, `[::1]` or `*.localhost` are refused. Systemd socket activation is supported (`LISTEN_FDS=1`).
