---
name: barid
description: Run a shared prompt board for several AI agent sessions. Use it when the user wants to plan work across more than one agent session, write or queue a prompt for another session, ask "what should I run next" or "what can run in parallel", record that a task is finished with its report, review proposals, or open the board panel in the browser. The board tracks order, dependencies, exclusive resources (like a GPU) and who holds what, so sessions never collide.
---

# Barid

A board of prompts for people who run several AI agent sessions at once. The person copies prompts from the panel into the sessions; the sessions (you included) keep the board current with the `barid` command. Everything is one Python file with no dependencies.

## Find the command

`barid` is `scripts/barid.py` in this skill's folder. Run it with Python 3:

```
python3 "${CLAUDE_SKILL_DIR}/scripts/barid.py" <command>      # Claude Code
python3 <folder that contains this SKILL.md>/scripts/barid.py <command>   # any other agent
```

Run `python3 .../barid.py shim` once to install a short `barid` command. In the examples below `barid` means whichever form works.

The board lives in `.barid/board.json`, found by searching upward from the current folder (or set `BARID_BOARD`, or pass `--board PATH`). If there is no board yet, create one with `barid init` in the project folder.

## Who you are

- **The person** works in the panel (`barid open`). Only the person can approve proposals, accept reports and set any status. Never pass `--human` yourself and never set `BARID_ACTOR=human`.
- **A planner agent** (the session that plans the work) adds, edits and cancels tasks. If the person did not trust it, its additions become *proposals* the person approves. Do not try to get around this.
- **A worker agent** (a session that executes a task) claims its task, does the work, finishes with a report. It may leave notes and propose follow-up tasks, nothing else.

Always identify yourself: `--by "<your name>"` (or set `BARID_AGENT`). Use the same name every time; the board uses it to decide who may finish a task.

## Setting up a board (planner, first time)

1. `barid init --project "<name>" --lanes "<id>:<title>,<id>:<title>"`: one lane for every agent session that will receive prompts. Add `--lang en|ru|kk` for the language the user writes in: it is the language of the footers and connection texts the board writes for agents (without it the system language is used, else English). It can be changed later with `barid lang`.
2. Define the shared things tasks compete for: `barid resource add gpu --label "GPU"` (exclusive by default). Two tasks that use the same exclusive resource never run together.
3. `barid context add <path>`: files that anyone writing a prompt should read (README, architecture notes).
4. Ask the person whether you may add tasks without approval. If yes, they run `barid trust add "<your name>" --human`.

## Adding tasks (planner)

```
barid add T1 --lane <lane> --title "<short title>" --text-file prompt.md --uses gpu --needs T0 --by "<you>"
barid add T2 --lane <lane> --title "<title>" --outline "<what the prompt must achieve>" --needs T1 --by "<you>"
```

- A task with text is *queued*. A task with only an `--outline` is a *draft*: its prompt is written when its turn comes (see below).
- `--needs A,B` lists tasks that must be finished first. `--uses` lists exclusive resources. `--quiet` marks a task that needs a quiet machine (nothing noisy in parallel, e.g. a user-rated comfort test or a precise benchmark). `--noisy` marks a task that loads the CPU or the desktop (tests, builds, a browser).
- **Describe every task, or Barid cannot check it.** Give each task a profile (`--profile light|dev|bench|attended`, see `barid profile list`) and, where it applies, `--uses` resources (a GPU, a test server, a port as `port:8091`) and `--touches` files. `light` means "needs nothing special" and counts as described; a task with no marks at all is shown as *not checked*, never as compatible. `add` and `edit` print suggestions when the text gives something away (a GPU, a build, a measurement, a port); fix them or silence one with `--lint-ok CODE`, and run `barid lint` before you tell the person what can run together. `barid explain A B` shows why two tasks can or cannot run together.
- Tasks in the same lane never run together: a lane is one session.
- **Declare what a task may change:** `--touches src/api,tests/api` (files, folders or globs). Tasks with overlapping scopes never run together, and at the end Barid flags every changed file outside the scope. Do this for every task that edits files.
- **Protect what must never change** (live configuration, secrets, production data): `barid protect add <path>`. Barid compares checksums when a task finishes.
- **Parallel code work needs separate checkouts:** `barid worktree T1` creates a git worktree and branch for the task and puts "work only in ..." into its prompt. Run `barid check T1` to see what could collide before you queue it.
- Write prompts so that a session with no memory of this conversation can act on them: concrete steps, exact paths and commands, a hard deadline, acceptance checks, what must not be touched, where the report goes. `barid template` shows skeletons. Do **not** write the tracking footer; the board appends it when the prompt is copied.
- Check the result with `barid plan`: steps, and which tasks may run at the same time. Explain it to the person in a sentence or two.

## Writing a draft's prompt (when the person asks, or its turn has come)

`barid gen T2` prints a request containing the outline, the reports of the finished tasks it depends on, the context files and the skeleton. Follow it, write the full prompt to a file, then store it:

```
barid edit T2 --text-file prompt.md --by "<you>"
```

Read the dependency reports it lists before writing; the prompt should build on what they found.

## Running a task (worker)

1. `barid next --lane <your lane>` prints the next ready task and its prompt. Exit code `0`: a task was printed. `2`: nothing is queued for the lane. `3`: nothing can start yet but will without anyone's help (a start time, a task of another session, a conflict): ask again later, or use `next --wait 300`. `4`: it waits for the person. When the person pasted a prompt to you, it already contains your task id and the exact commands: use those.
2. `barid claim T1 --by "<you>"`. If it is refused (a conflicting task is running, or the task must wait), **stop and tell the person why**, unless you were told to work through the night: then a refusal with exit code 3 is waiting, repeat it with `--wait 300`. Do not work around it and never use `--force`.
3. Do the work. For long tasks run `barid heartbeat T1 --by "<you>"` now and then; an expired lease shows up as stale in the panel.
4. Write your report file, then `barid finish T1 --report <path> --outcome <complete|partial|failed> --by "<you>"`. Be honest about the outcome: `complete` only when every acceptance check was met and nothing is left; `partial` when you stopped early (a safety limit, the deadline, an error) or skipped parts; `failed` when the main goal was not reached. Only `complete` starts dependent tasks by itself, the person decides about the rest. The task goes to *review*; the person accepts it.
5. If something blocks you, `barid note T1 "<what>" --by "<you>"`. If you think of a follow-up task, `barid add <ID> --lane <lane> --title ... --text-file ... --by "<you>"`; it arrives as a proposal.

Touch only your own task. Never edit, cancel or delete other tasks. If the prompt lists files you may change, change nothing else; if you need another file, write a note (`barid note`) and stop instead of editing it. Never change the protected paths listed in the prompt.

## Planner loop

- `barid list` shows every task with its state: ready, waiting (for which tasks), blocked (by which running task and why), proposed, review.
- **Chains that run without anyone watching** (a night): `--after T1` (run after T1 whatever its outcome; `--needs` waits for a success), `--not-before 23:00` (earliest start), `--timebox 8h`; `barid lane edit <lane> --unattended` and `barid connect <lane>` give the session a text that makes it wait for its turn and never stop to ask; `barid worker --lane <lane> --cmd '<agent command>'` runs the lane in a loop outside the model; `barid digest --since 12h` tells the person what happened. See `docs/UNATTENDED.md`.
- Read the report of a task in *review* before building on it. The person accepts reports; you do not.
- When a report changes the plan, edit the tasks that follow (`barid edit`) or cancel them (`barid cancel T5 --reason "..."`). Cancelled tasks stay in the archive.
- `barid log` shows who did what.

## Showing the panel

`barid open` starts the panel in the background and opens it in the browser (use `--no-browser` and print the URL if you cannot open one). It exits by itself after 30 idle minutes. `barid export board.html` writes a read-only snapshot that opens without a server (`--no-text` leaves the prompts out).

## Things never to do

- Do not edit `board.json` by hand or with other tools; use `barid`.
- Do not use `--force`, `--human` or `BARID_ACTOR=human` unless the person told you to in this conversation.
- Do not claim a task that is not yours, and do not start work on a task that is waiting or blocked.
- Do not put secrets in prompts or notes; the board file is plain JSON.

## Command cheat sheet

| Goal | Command |
|---|---|
| create board | `barid init --lanes a,b` |
| add / change / reorder task | `barid add ID ...`, `barid edit ID ...` (`--replace OLD NEW` swaps one exact fragment), `barid move ID POSITION` (earlier in the list = earlier slot in the plan) |
| see order and parallelism | `barid plan`, `barid list` |
| next task for a lane | `barid next --lane L` |
| add a session | `barid lane add ID --title T --agent codex` (or "+" in the panel) |
| connect an agent window to a session | `barid connect L` prints the first message to paste into that window |
| scopes, protection, isolation | `barid add ID --touches P`, `barid protect add P`, `barid worktree ID`, `barid check ID` |
| take / finish / give back | `barid claim ID`, `barid finish ID --report P --outcome complete\|partial\|failed`, `barid release ID` |
| note, cancel | `barid note ID "text"`, `barid cancel ID --reason ...` |
| prompt request for a draft | `barid gen ID` |
| person's actions | `barid approve ID`, `barid accept ID`, `barid sent ID`, `barid status ID S` (with `--human`) |
| panel | `barid open`, `barid serve`, `barid export FILE` |
| health check | `barid doctor` |

Every command that reports data accepts `--json` for machine-readable output (`template`, `check`, `gen` and `export` print plain text; read their exit codes). Full rules and the data format: `docs/PROTOCOL.md` in the repository.
