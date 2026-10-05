# RelayBoard protocol

The rules every client (CLI, panel, agent) follows, and the file format. Implemented in `skills/relayboard/scripts/rb.py`.

## Roles

| Role | Who | Can |
|---|---|---|
| person (`human`) | you, through the panel or `--human` / `RB_ACTOR=human` | everything: approve, accept, set any status, restore, purge, change trust |
| trusted agent | a name listed in `policy.direct_agents` (or `*`) | add, edit, cancel tasks directly; add lanes, resources, context |
| agent | any other name (`--by`, `RB_AGENT`, default `agent`) | propose tasks, claim and finish its own task, note, release its own claim, cancel or edit its own proposal |

Identity is advisory. The board protects against mistakes, not against a hostile process.

## Task states

Stored `status`: `draft` (no prompt text yet), `proposed` (added by an untrusted agent, waits for approval), `queued`, `sent` (the person handed the prompt to a session), `running` (claimed), `review` (finished, report written), `done` (accepted), `cancelled`.

Computed `state` for a queued task: `ready`, `waiting` (some `needs` are not finished; `waiting_on` lists them) or `blocked` (a conflicting task is active; `conflicts` lists `{id, why}`). For every other status `state` equals `status`.

A need is *satisfied* when the task is `done`, or `review` unless `policy.dependents_wait_for_accept` is true.

## Conflicts and parallelism

Two tasks conflict (must not be active together) when any of these holds:

1. they are in the same lane (`why: "lane"`): a lane is one session;
2. they share an exclusive resource in `uses` (`why: "<resource>"`);
3. one has `quiet: true` and the other `noisy: true` (`why: "quiet"`).

Active tasks are those in `sent` or `running`. `claim` is refused when it would conflict with an active task, unless the task is already `sent` (the person chose it, a warning is returned) or `--force` is used.

`can_run_with` lists active/ready tasks that do not conflict and are unrelated by dependencies. `steps` (from `rb plan`) is a greedy schedule in list order: each task goes into the earliest step after its unfinished needs where nothing it conflicts with is placed.

## Leases

`claim` records `{by, at, lease_until}` (`policy.lease_hours`, default 12). `heartbeat` extends it. An expired lease marks the task `stale` in the views but does not free it; the person (or the owner) releases it.

## Operations and who may do them

| Operation | Allowed | Result |
|---|---|---|
| `add` | anyone | `queued` (or `draft` without text); `proposed` for untrusted agents |
| `edit` | person, trusted agents, or the proposer of a `proposed`/`draft` task | text, title, needs (cycle-checked), uses, flags |
| `approve`, `reject` | person | `proposed` to `queued`/`draft`, or to `cancelled` |
| `sent` | person | `queued` to `sent` |
| `claim` | anyone | to `running`, refused on conflicts or unmet needs |
| `finish` | the claimer or the person | to `review`; a report path is required |
| `release` | the claimer or the person | back to `queued` |
| `accept` | person | `review` to `done` |
| `cancel` | person, trusted agents, the proposer (not while `running`, except the person) | to `cancelled`, kept |
| `restore` | person | `cancelled` to `queued`/`draft` |
| `status` | person | any status |
| `note` | anyone | appends to `notes` |
| `purge` | person, CLI only | removes a task nothing else needs |
| `lane`, `resource`, `context` | person, trusted agents | board configuration |
| `trust` | person | edits `policy.direct_agents` |

## File format (`.relayboard/board.json`, schema 1)

```json
{
  "schema": 1, "project": "name", "rev": 42, "created": "...", "updated": "...", "lang": "en",
  "lanes": [{"id": "main", "title": "Main session"}],
  "resources": {"gpu": {"label": "GPU", "exclusive": true}},
  "policy": {"direct_agents": ["planner"], "dependents_wait_for_accept": false, "lease_hours": 12, "footer": true},
  "context": ["README.md"],
  "items": [{
    "id": "T1", "lane": "main", "title": "Build", "status": "running",
    "needs": ["T0"], "uses": ["gpu"], "quiet": false, "noisy": true,
    "when": "", "outline": "", "text": "the prompt without the footer",
    "report": "", "notes": [{"t": "...", "by": "agent", "text": "..."}],
    "created": "...", "updated": "...", "created_by": "planner",
    "claim": {"by": "worker-a", "at": "...", "lease_until": "..."}
  }],
  "events": [{"t": "...", "by": "worker-a", "action": "claim", "id": "T1", "detail": ""}]
}
```

`rev` increases on every write. `events` keeps the last 500 entries. All writes go through `rb` (file lock plus atomic replace); do not edit the file by hand while agents are active.

## Prompt footer

`rb show ID --text`, `rb next` and the panel's *Copy prompt* return the task text followed by a footer (English or Russian by `lang`, switch off with `policy.footer: false`) that contains the `claim`, `finish` and `note` commands with the absolute path of `rb.py` and of the board file. The footer is generated on the fly and never stored in `text`.

## Panel HTTP API (loopback only)

`GET /` panel, `GET /api/board` (computed view), `GET /api/rev`, `GET /api/prompt/<id>`, `GET /api/gen/<id>`, `POST /api/act` (JSON `{action, id, args}`, header `X-RB-Edit: 1`, `Origin` must match `Host`). Requests whose `Host` is not `localhost`, `127.0.0.1`, `[::1]` or `*.localhost` are refused. Systemd socket activation is supported (`LISTEN_FDS=1`).
