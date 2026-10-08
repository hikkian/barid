# Changelog

## 1.0.0-rc.1 (2026-10-07)

**The 1.0 release candidate.** What stays the same inside 1.x is written down in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) (also in Russian and Kazakh) and checked by `tests/test_contract.py`. 1.0.0 is tagged after a real unattended night on this version.

Found by a red-team review of 0.6.0 (twelve processes writing for a minute, a killed writer, clock changes, a stuck lock holder, a worker driven by hostile commands), each with a test that fails on 0.6.0:

- **A wait that cannot end is no longer called a wait.** A task below a cancelled one, or below a holder whose lease ran out, got exit `3` ("it will start by itself") and `--wait` could sit for 14 hours; now it is `4` at once, with `via` naming the task it waits for. A task marked `sent` kept no order (it could be claimed before the task it needs, and two tasks could wait for each other); now `claim` refuses it, and a sent task that waits does not hold back the task it waits for.
- **`worker`:** stops the agent when its task is cancelled or returned; no longer blocks on a prompt that does not fit the pipe (it ignored SIGTERM until the agent ended); closes the task as failed when the agent cannot start, instead of leaving it `running`; gives up on a task that keeps coming back (`--max-attempts`) instead of starting it every second for ever; stops what the agent left running in the background; quotes the prompt path (a board in a folder with a space); counts the timebox on the monotonic clock; renews the lease once a minute instead of twice (half the board writes); refuses a `--cwd` that is not a folder before claiming anything.
- **Names with non-ASCII letters or spaces** (Russian, Kazakh) were read from git in quoted octal form, matched no scope and made a finished task look as if it had left its scope, so its dependents stalled. Read with `-z` now.
- **The digest** lost a finished task after about 500 events (the event log is capped); "Ended" is built from the tasks themselves now.
- **A time of day** (`23:00`) was an hour off on the evening before a clock change; absurd times (`+99999999d`) and a time without an offset in a hand-edited board raised tracebacks.
- **The board file:** a lock that is held by a stopped process gave every other command an endless wait; now they refuse clearly after `BARID_LOCK_WAIT` seconds (60). `claim` and `finish` ran git and hashed files inside the lock (a note waited 5 s behind a slow git); now they do it before. A symlinked `board.json` was replaced by a regular file and its two paths had separate locks. Saves are flushed to the disk, the version before the last change is kept as `board.json.bak` (a hard link: no extra data written), old temp files are removed, and a damaged file, a full disk or a read-only folder give a message instead of a traceback.
- **Speed:** `compute()` found the marks and the normalised scopes of a task again for every pair of tasks; with file scopes a board of 500 tasks took 4 s, now about 0.25 s. The cost of each command is kept in check by a budget test.
- **Fix in the release itself:** `.claude-plugin/plugin.json` was an empty file in 0.6.0 (the plugin could not be read); a test now checks that the manifests are valid and carry the version of the program.

Panel: the order of work ("Order, and what can run together") is a grid with one column per session and a coloured header over each column, so a card always stays under its own session however narrow the window is (before, cards wrapped like text and the third session dropped below the others); titles wrap instead of being cut; on a phone the cards stack. A browser test checks the alignment.

A documentation check of 134 claims against the code (done by an AI agent, spot-checked by hand; 13 mismatches, 4 stale) led to: `barid policy` (show the board policy, or set `dependents_wait_for_accept`, `lease_hours`, `footer`, `handoff`, `git_check` as the person: the README promised a "policy switch" that no command could set); `--json` now works for `where`, `doctor`, `lane list`, `resource list`, `profile list` and `protect list` (the documents said every command accepted it); a task that waits only for its start time no longer prints `(after )`; and corrected wording about the event log (it keeps the last 500 entries), the worker lease (renewed once a minute), person-only commands in the examples (`--human`), trusted planners, the footer languages, `GET /api/digest` and `BARID_ACTOR`.

The "While you were away" card no longer lists what you have already seen: a report you accepted, or a task that finished while the panel was open in front of you. It lists what ended while you were really away (a browser test checks it).

**Kazakh is hidden for now.** The panel does not offer it, a Kazakh locale gets Russian, and the README and the compatibility promise in Kazakh moved to `docs/drafts/` (nothing links to them) until a native reader has reviewed them; boards that already say `kk` still open, and `?lang=kk` still shows the panel in Kazakh. The Kazakh texts of this release were checked by two independent AI passes (a blind back-translation into English, then a comparison with the originals) because no native reader was at hand: it found and fixed an ambiguous notification title ("you are needed" / "you are not needed"), a README sentence that said bug reports were "pleasant" instead of "welcome", the informal form in one panel label, cancelled tasks called deleted, and mixed terms (folder, release, flag, lease). The remaining risk is unnatural phrasing that only a native speaker would notice.

README: the "How it works" diagram is a real picture (`docs/img/how-it-works.svg`, readable on GitHub's light and dark themes) instead of a hand-drawn text box.

An open panel tab now notices when `panel.html` was replaced (an update of Barid) and shows a bar "The panel was updated. Reload": before, the data kept refreshing but the page ran the old code until you reloaded by hand, which made fixed behaviour look unfixed. (`/api/rev` carries a stamp of the panel file.)

New, small and meant for the morning:

- `barid overview` (and `--brief` for a status bar) shows every session on one line: what it does, what it would start next, what waits for you.
- `barid accept-clean` and one button in the panel (after a second click) accept the reports that ended `complete` with no file warnings and no disturbed measurement; anything else stays for you to read.
- An optional bell in the panel: a browser notification when more things wait for you, only while the tab is in the background, never a sound.

Found by two independent reviews of the first candidate and fixed before release: a sent task below a dead holder could still be claimed; a chain of several hundred tasks listed in reverse order raised a RecursionError in `compute()`; the digest listed a task twice when two paths led to one cancelled task; a worker killed an agent that had already reported its task itself, and a deleted task was not stopped; lost claim races counted as attempts; `cat "{prompt_file}"` broke in a folder with a space; SIGHUP left the agent running; a `.bak<pid>` file could be left behind; a typo in `BARID_LOCK_WAIT` raised at start; a board with a byte order mark was refused; and a lease that had just run out (a computer that slept) was taken as lost at once (there is a five-minute grace now).

Tests: 189 plus the contract tests (frozen commands, exit codes, reason codes, board and `--json` fields, text and time budgets, manifests, documents in three languages), and a board written by 0.6.0 among the old-release fixtures.

## 0.6.0 (2026-10-06)

**Nights and unattended runs.** The full description, with the reasons behind each choice, is in [docs/UNATTENDED.md](docs/UNATTENDED.md).

- A queued task now says **why it waits**, with a reason code like a batch scheduler: `begin_time`, `dependency`, `conflict` (they end by themselves), `decision` (a report waits for you to accept it, or a draft for you to write it) and `never` (the task it needs was cancelled, so it can never start). `list --json`, `next` and the panel show them; before, a task that waited for a *partial* report or a cancelled task just waited without a word.
- `--after T1` (run after T1 whatever its outcome, like Slurm `afterany`) next to `--needs` (after a success); `--not-before` (an earliest start: `23:00`, `'2026-10-07 23:00'`, `+3h`) and `--timebox` (how long a task may take) on `add` and `edit`; all three in the panel's edit form.
- `next` and `claim` tell **"nothing"** from **"wait"** from **"needs you"** with exit codes (`next`: 0 task, 2 nothing queued, 3 will start by itself, 4 needs the person; `claim`: 3 must wait, 4 needs the person), and `next --wait SECONDS` and `claim --wait SECONDS` wait for a task that only has to wait. A claim is refused before the start time. The default connection text tells a session what each code means.
- **Sessions show signs of life.** `next`, `claim`, `heartbeat`, `note` and `finish` mark the lane as `working`, `waiting` (and for what) or `idle`; the session cards show it with how long ago. A claim records its last `signal`, and a task with a timebox a `deadline`.
- **Night mode for a session:** `barid lane edit LANE --unattended` or the "Night mode" box in the Connect window makes the connection text tell the session to wait for its turn on exit code 3, never ask whether to continue, and stop only when its lane is empty or it needs you.
- **`barid worker --lane L --cmd '<agent command>'`**: a loop outside the model. For each task it starts a fresh agent with the prompt on stdin, keeps a short lease alive, stops the process group at the timebox, closes a task whose agent ended without a report (`partial` after a clean exit, `failed` otherwise), and takes the next; it waits for start times, other tasks and conflicts, and ends when the lane is empty, needs you, or nothing could start for `--max-wait`.
- **`barid digest [--since 12h]`** and the "While you were away" card in the panel: sessions, what ended with which outcome and report, what runs or has an expired lease, what needs you, what waits and why.
- "What to do now": a session's card shows its queued work before a draft whose turn has come (a draft had taken the card of a session that had a ready task), and has new hint lines for a start time and for a task that waits for you.
- `heartbeat` no longer writes an event (a worker sends one every 30 seconds).
- Tests: 40 new tests (reasons, `after`, start times, exit codes and `--wait` through the real command line, presence, a worker driven by a fake agent that crashes, hangs or forgets to report, the digest, boards without the new fields) and 4 more browser tests.
- Audit of Barid with random operation sequences (hypothesis, 1500 sequences of 60 steps) and garbage input: two real findings fixed. (1) The panel API crashed with a 500 on a field of the wrong type (`"title": true`, `"needs": false`, `args` as a list); it now answers a clean `bad_input` error (translated in the panel). (2) A task the person set to "running" by hand had no claim (no holder, no lease); it now gets one.
- New tests: random operation sequences and garbage actions (`tests/test_robust.py`, needs `hypothesis`, skipped without it) and boards made by every earlier release (`tests/fixtures`, `tests/test_migration.py`): they open, plan, export, accept reports and take new tasks with the current code, and reading never rewrites the file.
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
