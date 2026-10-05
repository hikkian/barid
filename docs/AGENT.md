# RelayBoard: instructions for an AI agent

You are an AI agent and your user asked you to set up or use RelayBoard, a prompt board for several agent sessions. Read this once, then follow it. Everything needs only Python 3.9+.

## 1. Install

Pick the first that applies:

1. **Your agent supports skills.** Copy the folder `skills/relayboard/` of this repository into the agent's skills directory (Claude Code: `~/.claude/skills/relayboard` or `.claude/skills/relayboard`; Codex: `~/.codex/skills/relayboard`; OpenCode: `~/.config/opencode/skills/relayboard`). Tell the user to restart the session if skills are loaded only at start. Then read `skills/relayboard/SKILL.md` and follow it.
2. **Claude Code plugin.** `/plugin marketplace add hikkian/relayboard`, then `/plugin install relayboard@relayboard`.
3. **No skills.** Clone the repository somewhere stable and always call `python3 <clone>/skills/relayboard/scripts/rb.py ...`. Add this to the project's `AGENTS.md` so later sessions find it:

   ```
   This project uses RelayBoard. Command: python3 <clone>/skills/relayboard/scripts/rb.py
   Rules: <clone>/docs/AGENT.md
   ```

Run `python3 .../rb.py doctor` in the project folder; it must print no `FAIL`. Optionally `python3 .../rb.py shim` installs a short `rb`.

## 2. First-time setup (planner)

Ask the user: *how many agent sessions do you run, and what do they do?* Then:

```bash
rb init --project "<name>" --lanes "<id>:<title>,<id>:<title>"     # one lane per session
rb resource add gpu --label "GPU"                                   # anything exclusive (a GPU, a test server, "user present")
rb context add README.md                                            # files a prompt-writer should read
```

Tell the user to run `rb trust add "<your agent name>" --human` if they want your additions to skip the approval step; otherwise your tasks arrive as proposals in the panel. Then show them the panel: `rb open`.

## 3. Day-to-day rules

- Identify yourself in every command: `--by "<your name>"`.
- **Planner:** add tasks with `rb add`, set `--needs`, `--uses`, `--quiet`, `--noisy`, check with `rb plan`, explain what can run in parallel. Write prompts that a session with no memory can act on (see `rb template`). Drafts (`--outline`, no text) get their prompt later through `rb gen ID` then `rb edit ID --text-file FILE`.
- **Worker:** `rb next --lane <lane>`, `rb claim ID`, do the work, write a report, `rb finish ID --report PATH`. If `claim` is refused, stop and tell the user why. Touch only your task.
- **Never** edit `board.json` directly, pass `--human`, set `RB_ACTOR=human`, use `--force`, or work on a task you did not claim.

Full rules: [PROTOCOL.md](PROTOCOL.md). Command reference: `rb --help` and `skills/relayboard/SKILL.md`.

## 4. Showing the user what is going on

`rb plan` for a text summary, `rb open` for the panel (it starts a background server that exits after 30 idle minutes), `rb export board.html` for a read-only snapshot.
