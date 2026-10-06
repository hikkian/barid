# Barid: instructions for an AI agent

You are an AI agent and your user asked you to set up or use Barid, a prompt board for several agent sessions. Read this once, then follow it. Everything needs only Python 3.9+.

## 1. Install

Pick the first that applies:

1. **Your agent supports skills.** Copy the folder `skills/barid/` of this repository into the agent's skills directory (Claude Code: `~/.claude/skills/barid` or `.claude/skills/barid`; Codex: `~/.codex/skills/barid`; OpenCode: `~/.config/opencode/skills/barid`). Tell the user to restart the session if skills are loaded only at start. Then read `skills/barid/SKILL.md` and follow it.
2. **Claude Code plugin.** `/plugin marketplace add hikkian/barid`, then `/plugin install barid@barid`.
3. **No skills.** Clone the repository somewhere stable and always call `python3 <clone>/skills/barid/scripts/barid.py ...`. Add this to the project's `AGENTS.md` so later sessions find it:

   ```
   This project uses Barid. Command: python3 <clone>/skills/barid/scripts/barid.py
   Rules: <clone>/docs/AGENT.md
   ```

Run `python3 .../barid.py doctor` in the project folder; it must print no `FAIL`. Optionally `python3 .../barid.py shim` installs a short `barid`.

## 2. First-time setup (planner)

Ask the user: *how many agent sessions do you run, and what do they do?* Then:

```bash
barid init --project "<name>" --lanes "<id>:<title>,<id>:<title>" --lang <en|ru|kk>   # one lane per session; --lang = the language your user writes in
barid resource add gpu --label "GPU"                                   # anything exclusive (a GPU, a test server, "user present")
barid context add README.md                                            # files a prompt-writer should read
```

Tell the user to run `barid trust add "<your agent name>" --human` if they want your additions to skip the approval step; otherwise your tasks arrive as proposals in the panel. Then show them the panel: `barid open`.

To add a session later: the person presses "+" in the panel, or `barid lane add ID --title "Name" --agent codex`. Then `barid connect ID` prints the text to paste as the first message of that agent's window; from then on it works through `next --lane ID`, `claim` and `finish` by itself.

## 3. Day-to-day rules

- Identify yourself in every command: `--by "<your name>"`.
- **Planner:** add tasks with `barid add`, set `--needs`, `--profile` (or `--uses`, `--quiet`, `--noisy`), run `barid lint` and fix what it suggests, check with `barid plan`, explain what can run in parallel. Write prompts that a session with no memory can act on (see `barid template`). Drafts (`--outline`, no text) get their prompt later through `barid gen ID` then `barid edit ID --text-file FILE`.
- **Worker:** `barid next --lane <lane>` (exit code 0 task, 2 nothing queued, 3 wait: it will start by itself, 4 needs the person), `barid claim ID`, do the work, write a report, `barid finish ID --report PATH`. If `claim` is refused, stop and tell the user why, except in a night session whose connection text says to wait: there a refusal with exit code 3 means "repeat with `--wait 300`". Touch only your task.
- **Unattended chains:** [UNATTENDED.md](UNATTENDED.md): `--after`, `--not-before`, `--timebox`, `lane edit --unattended`, `barid worker`, `barid digest`.
- **Never** edit `board.json` directly, pass `--human`, set `BARID_ACTOR=human`, use `--force`, or work on a task you did not claim.

Full rules: [PROTOCOL.md](PROTOCOL.md). Command reference: `barid --help` and `skills/barid/SKILL.md`.

## 4. Showing the user what is going on

`barid plan` for a text summary, `barid open` for the panel (it starts a background server that exits after 30 idle minutes), `barid export board.html` for a read-only snapshot.
