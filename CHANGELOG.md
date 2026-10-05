# Changelog

## Unreleased

- Audit of Barid with random operation sequences (hypothesis, 1500 sequences of 60 steps) and garbage input: two real findings fixed. (1) The panel API crashed with a 500 on a field of the wrong type (`"title": true`, `"needs": false`, `args` as a list); it now answers a clean `bad_input` error (translated in the panel). (2) A task the person set to "running" by hand had no claim (no holder, no lease); it now gets one.
- New tests: random operation sequences and garbage actions (`tests/test_robust.py`, needs `hypothesis`, skipped without it) and boards made by every earlier release (`tests/fixtures`, `tests/test_migration.py`): they open, plan, export, accept reports and take new tasks with the current code, and reading never rewrites the file.
## Unreleased

- Fix: the "What to do now" card showed a later draft whose prompt was due instead of the session's first task in the plan when that first task was blocked. The card now shows the task the plan puts first (and drafts that wait for other tasks count as waiting).
- examples: the example board describes its tasks with profiles, so `barid lint` is clean on it.
## 0.5.1 (2026-10-06)

- The panel is now tested in a real browser: 20 tests drive Firefox headless over Marionette (standard library only, `tests/test_ui.py`): language list, "+" new session, Connect, the Appearance menu, forms, drawer, review inbox, hints, colours of the status labels, narrow screens, Kazakh. They were checked against three bugs we had (a menu that closed, colourless status labels, a missing conflict hint). CI runs them on one Linux job; `BARID_SKIP_UI=1` skips them.
- `?lang=en|ru|kk` in the panel link opens it in that language.
- The task drawer lists the tasks it cannot run with (not only the running ones).
- `claim` warns when one name holds running tasks in two sessions (probably two windows with the same name).
## 0.5.0 (2026-10-06)

- Fix: the panel server no longer looks up its host name when it starts (`socket.getfqdn`), which could take half a minute or hang with a slow or broken DNS.
- **A more reliable "can these run together".** Tasks get a `profile` (built in: `light`, `dev`, `bench`, `attended`; your own with `barid profile add`) next to resources, quiet/noisy marks and file scopes. The conflict rules are one pure function (`conflicts_between`) with a symmetry test over random boards. A task that declares nothing is shown as *not checked*, never as compatible. `barid lint` (and `add`/`edit`) suggests marks the text gives away (GPU, builds, speed measurement, ports; silence with `--lint-ok`). `barid explain A B` shows each rule. The run journal records which tasks ran next to a finished one and flags a measurement that ran next to something that loaded the machine or declared nothing. A port or a folder can be an exclusive resource (`port:8091`). The panel shows profiles, "not checked", lint hints and the journal in all three languages.
- "What to do now" has a summary under the cards for the whole board: what runs now, what can start (together or one at a time), and what waits and why. The hint line on each card and this summary now come from one place on the server (`focus`, `summary` in `/api/board`) and are tested against every combination of states of two sessions, so no kind of task can be left without its line.
- A next task that cannot start yet because of a conflict now shows the same coloured line with the reason ("⏳ Wait, conflict: M11 (GPU)") instead of a bare "Waiting for: M11".
- A refused `claim` on a task that is already running now says who holds it and since when, and what to do if that was the same session before an interruption.
- "What to do now" now also compares the next tasks of different sessions with each other, drafts whose turn has come included: "⛔ Do not run together with: M2 (GPU)" or "✓ Can run together with: …". Before, it only looked at tasks that were already running.
- `barid init` takes the language of the agent texts from the system language when `--lang` is not given, and the skill tells the agent to pass the language the user writes in.
- The language of the texts Barid writes for agents (footer, connection text, handoff) can now be changed: `barid lang en|ru|kk`. It is separate from the panel language.
- "What to do now": every next task (ready or waiting for its prompt) now says whether it can start next to what is already running ("✓ Can start now alongside: M6") or must wait ("⏳ Wait, conflict: M6 (GPU)").
- README (all three languages): adding sessions, the Connect button and `barid connect`, Kazakh; a new demo GIF and panel screenshots. Deep links also work in the live panel (`#newlane`, `#connect:LANE`, `#lang`, `#T3`); session cards no longer wrap their Connect button.

## 0.4.1 (2026-10-05)

- The language button shows the current language and opens a list of all three (English, Русский, Қазақша) with a tick on the active one.
- Fix: status labels (in progress, ready, waiting…) lost their colours because the new agent buttons reused the same style name.
- Kazakh (Қазақша) in the panel, in the prompt footers, handoff and connection texts (`barid init --lang kz`), next to English and Russian; the language button cycles through the three.
- README: a "How is this different?" section comparing Barid with Claude Squad, Vibe Kanban, MCP Agent Mail, Aqua and Claude Code agent teams; README translations in Russian (`README.ru.md`) and Kazakh (`README.kz.md`).

## 0.4.0 (2026-10-05)

- Fix: in the Appearance menu, opening the agent list of a session no longer closes the menu (a click on a button that the menu itself had just redrawn was taken for a click outside).
- Sessions from the panel: a "+" button next to the session chips creates one (name and agent), then shows the connection text; every session card has a "Connect" button. `barid connect LANE` prints the same text: paste it as the first message of the agent window and the agent takes its tasks from the board (`next --lane`, `claim`, `finish`). The agent kind is picked from chips instead of a native drop-down that could jump to the wrong place on some desktops.
- Many sessions: from the seventh on, each session gets its own hue (the first six use the palette); long "can run together" lists are shortened. Checked with 8 sessions and 60 tasks (plan and export in under 0.1 s).
- Handoff between agents: the prompt of a task automatically includes what the tasks it builds on handed over (outcome, who and which agent, report, branch, changed files, last notes). A lane whose agent is `local` gets a short hint for smaller models.

## 0.3.0 (2026-10-05)

- Sessions get their own colour, name and agent kind (`barid lane edit ID --color --agent`, or the Sessions section of the Appearance menu); the agent shows as a badge.
- Fix for macOS and Windows: paths are compared by real path, so symlinked project folders (macOS temp folders) and short Windows names no longer break the file checks.

## 0.2.0 (2026-10-05)

Sessions no longer collide on files either.

- File scopes (`--touches`): tasks whose scopes overlap never run together; the panel and `barid plan` say why.
- Protected paths (`barid protect`): checksums at claim, verified at finish.
- Isolated checkouts (`barid worktree`): a git worktree and branch per task, written into its prompt; tasks in different worktrees do not conflict on paths.
- Git check at finish: files changed outside the declared scope, inside another running task's scope or in protected paths are listed in the inbox and keep dependents locked until the person decides.
- `barid check`, `barid lane list`, `barid resource list`; fixed `barid resource add` and `barid lane add` as documented.
- Panel: Appearance button (light/dark/auto, four palettes), last-updated indicator, scopes and file warnings.

## 0.1.0 (2026-10-05)

First public version.

- Board with lanes (one per agent session), tasks, dependencies, exclusive resources and the quiet/noisy flags; the plan groups tasks into steps that may run together.
- Agent side: the `barid` command (`add`, `edit`, `next`, `claim`, `finish`, `gen`, `note`, `plan`, ...), a skill for Claude Code, Codex and OpenCode, a Claude Code plugin and marketplace, prompt templates, `docs/AGENT.md` for any other agent.
- Reports end with an honest outcome (`complete`, `partial`, `failed`); only `complete` unlocks dependent tasks by itself.
- Agents' additions are proposals until the person approves; agents can only cancel, never delete; every change is logged.
- Browser panel: what to do now, inbox (proposals and reports), order with parallel groups, details with editing, English and Russian, light/dark/auto and four colour palettes. Starts on demand and exits when idle.
- Safety: atomic locked writes, leases on claimed tasks, loopback-only server with Origin and Host checks.
- Tests on Linux, macOS and Windows (Python 3.9, 3.12, 3.13).
