---
name: relayboard
description: Run a shared prompt board for several AI agent sessions. Use it when the user wants to plan work across more than one agent session, write or queue a prompt for another session, ask "what should I run next" or "what can run in parallel", record that a task is finished with its report, review proposals, or open the board panel in the browser. The board tracks order, dependencies, exclusive resources (like a GPU) and who holds what, so sessions never collide.
---

# RelayBoard

A board of prompts for people who run several AI agent sessions at once. The person copies prompts from the panel into the sessions; the sessions (you included) keep the board current with the `rb` command. Everything is one Python file with no dependencies.

## Find the command

`rb` is `scripts/rb.py` in this skill's folder. Run it with Python 3:

```
python3 "${CLAUDE_SKILL_DIR}/scripts/rb.py" <command>      # Claude Code
python3 <folder that contains this SKILL.md>/scripts/rb.py <command>   # any other agent
```

Run `python3 .../rb.py shim` once to install a short `rb` command. In the examples below `rb` means whichever form works.

The board lives in `.relayboard/board.json`, found by searching upward from the current folder (or set `RB_BOARD`, or pass `--board PATH`). If there is no board yet, create one with `rb init` in the project folder.

## Who you are

- **The person** works in the panel (`rb open`). Only the person can approve proposals, accept reports and set any status. Never pass `--human` yourself and never set `RB_ACTOR=human`.
- **A planner agent** (the session that plans the work) adds, edits and cancels tasks. If the person did not trust it, its additions become *proposals* the person approves. Do not try to get around this.
- **A worker agent** (a session that executes a task) claims its task, does the work, finishes with a report. It may leave notes and propose follow-up tasks, nothing else.

Always identify yourself: `--by "<your name>"` (or set `RB_AGENT`). Use the same name every time; the board uses it to decide who may finish a task.

## Setting up a board (planner, first time)

1. `rb init --project "<name>" --lanes "<id>:<title>,<id>:<title>"`: one lane for every agent session that will receive prompts. Add `--lang ru` for Russian footers.
2. Define the shared things tasks compete for: `rb resource add gpu --label "GPU"` (exclusive by default). Two tasks that use the same exclusive resource never run together.
3. `rb context add <path>`: files that anyone writing a prompt should read (README, architecture notes).
4. Ask the person whether you may add tasks without approval. If yes, they run `rb trust add "<your name>" --human`.

## Adding tasks (planner)

```
rb add T1 --lane <lane> --title "<short title>" --text-file prompt.md --uses gpu --needs T0 --by "<you>"
rb add T2 --lane <lane> --title "<title>" --outline "<what the prompt must achieve>" --needs T1 --by "<you>"
```

- A task with text is *queued*. A task with only an `--outline` is a *draft*: its prompt is written when its turn comes (see below).
- `--needs A,B` lists tasks that must be finished first. `--uses` lists exclusive resources. `--quiet` marks a task that needs a quiet machine (nothing noisy in parallel, e.g. a user-rated comfort test or a precise benchmark). `--noisy` marks a task that loads the CPU or the desktop (tests, builds, a browser).
- Tasks in the same lane never run together: a lane is one session.
- Write prompts so that a session with no memory of this conversation can act on them: concrete steps, exact paths and commands, a hard deadline, acceptance checks, what must not be touched, where the report goes. `rb template` shows skeletons. Do **not** write the tracking footer; the board appends it when the prompt is copied.
- Check the result with `rb plan`: steps, and which tasks may run at the same time. Explain it to the person in a sentence or two.

## Writing a draft's prompt (when the person asks, or its turn has come)

`rb gen T2` prints a request containing the outline, the reports of the finished tasks it depends on, the context files and the skeleton. Follow it, write the full prompt to a file, then store it:

```
rb edit T2 --text-file prompt.md --by "<you>"
```

Read the dependency reports it lists before writing; the prompt should build on what they found.

## Running a task (worker)

1. `rb next --lane <your lane>` prints the next ready task and its prompt (exit code 2 and the reason if nothing is ready). When the person pasted a prompt to you, it already contains your task id and the exact commands: use those.
2. `rb claim T1 --by "<you>"`. If it is refused (a conflicting task is running, or the task must wait), **stop and tell the person why**. Do not work around it and never use `--force`.
3. Do the work. For long tasks run `rb heartbeat T1 --by "<you>"` now and then; an expired lease shows up as stale in the panel.
4. Write your report file, then `rb finish T1 --report <path> --by "<you>"`. The task goes to *review*; the person accepts it.
5. If something blocks you, `rb note T1 "<what>" --by "<you>"`. If you think of a follow-up task, `rb add <ID> --lane <lane> --title ... --text-file ... --by "<you>"`; it arrives as a proposal.

Touch only your own task. Never edit, cancel or delete other tasks.

## Planner loop

- `rb list` shows every task with its state: ready, waiting (for which tasks), blocked (by which running task and why), proposed, review.
- Read the report of a task in *review* before building on it. The person accepts reports; you do not.
- When a report changes the plan, edit the tasks that follow (`rb edit`) or cancel them (`rb cancel T5 --reason "..."`). Cancelled tasks stay in the archive.
- `rb log` shows who did what.

## Showing the panel

`rb open` starts the panel in the background and opens it in the browser (use `--no-browser` and print the URL if you cannot open one). It exits by itself after 30 idle minutes. `rb export board.html` writes a read-only snapshot that opens without a server (`--no-text` leaves the prompts out).

## Things never to do

- Do not edit `board.json` by hand or with other tools; use `rb`.
- Do not use `--force`, `--human` or `RB_ACTOR=human` unless the person told you to in this conversation.
- Do not claim a task that is not yours, and do not start work on a task that is waiting or blocked.
- Do not put secrets in prompts or notes; the board file is plain JSON.

## Command cheat sheet

| Goal | Command |
|---|---|
| create board | `rb init --lanes a,b` |
| add / change task | `rb add ID ...`, `rb edit ID ...` (`--replace OLD NEW` swaps one exact fragment) |
| see order and parallelism | `rb plan`, `rb list` |
| next task for a lane | `rb next --lane L` |
| take / finish / give back | `rb claim ID`, `rb finish ID --report P`, `rb release ID` |
| note, cancel | `rb note ID "text"`, `rb cancel ID --reason ...` |
| prompt request for a draft | `rb gen ID` |
| person's actions | `rb approve ID`, `rb accept ID`, `rb sent ID`, `rb status ID S` (with `--human`) |
| panel | `rb open`, `rb serve`, `rb export FILE` |
| health check | `rb doctor` |

Every command accepts `--json` for machine-readable output. Full rules and the data format: `docs/PROTOCOL.md` in the repository.
