<p align="center"><img src="assets/hero.png" width="900" alt="Barid (بريد): a relay board for AI agent sessions"></p>

<p align="center">
<a href="https://github.com/hikkian/barid/actions/workflows/ci.yml"><img src="https://github.com/hikkian/barid/actions/workflows/ci.yml/badge.svg" alt="tests"></a>
<img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT">
<img src="https://img.shields.io/badge/python-3.9%2B-3776ab" alt="Python 3.9+">
<img src="https://img.shields.io/badge/dependencies-none-feb83b" alt="no dependencies">
<img src="https://img.shields.io/badge/agent-skill-005b5d" alt="agent skill">
</p>

<p align="center">English · <a href="README.ru.md">Русский</a> · <a href="README.kz.md">Қазақша</a></p>

**A prompt board for people who run several AI agent sessions at once.**
Your planner agent writes the prompts, you copy them into the other sessions, and the board keeps track of the order, what depends on what, what may run in parallel, and what could collide: a shared GPU or test server, the same files, or the same session. It ships as an **agent skill** (so your agent can run it for you) plus a small **browser panel** (so you can see and click).

![Barid in action](docs/img/demo.gif)

- **No more "which prompt next?"** The panel shows what to send now, and which tasks may run together or must wait ("blocked by T1: GPU", "same files: src/api").
- **Agents manage the board themselves.** Install the skill; your agent adds tasks, writes missing prompts, records reports. Other sessions claim their task and finish it with a report.
- **Safe by construction.** Exclusive resources (a GPU, a test server, "user present") are leased, and file scopes with a git check catch sessions that edit the same files, so sessions do not step on each other. Agent-added tasks wait for your approval. Nothing is ever deleted, only cancelled. Every change is logged.
- **Zero dependencies, tiny, local.** One Python file (3.9+), one JSON file, a local panel that starts on demand and exits when idle. No account, no cloud, no telemetry.

## 60-second start

```bash
git clone https://github.com/hikkian/barid
cd your-project
python3 /path/to/barid/skills/barid/scripts/barid.py init --lanes planner,worker-a,worker-b
python3 /path/to/barid/skills/barid/scripts/barid.py shim      # optional: installs a short `barid`
barid open                                                              # the panel
```

Or try the example board first:

```bash
python3 examples/build_example.py /tmp/barid-demo
cd /tmp/barid-demo && python3 /path/to/barid/skills/barid/scripts/barid.py open
```

## Install it into your AI agent

The skill is a normal [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) folder (`skills/barid/`: a `SKILL.md`, the `barid.py` script, the panel and prompt templates). Put that folder where your agent looks for skills:

| Agent | Where |
|---|---|
| Claude Code (plugin) | `/plugin marketplace add hikkian/barid`, then `/plugin install barid@barid` (or `claude plugin marketplace add hikkian/barid` in a shell) |
| Claude Code (manual) | copy `skills/barid` to `~/.claude/skills/barid` (all projects) or `.claude/skills/barid` (one project) |
| Codex CLI | copy it to `~/.codex/skills/barid` (Windows: `%USERPROFILE%\.codex\skills\barid`) |
| OpenCode and forks | copy it to `~/.config/opencode/skills/barid`; OpenCode also reads `~/.claude/skills` and `~/.agents/skills` |
| Anything else | paste the contents of [docs/AGENT.md](docs/AGENT.md) into the agent, or add the one-line instruction below to its `AGENTS.md` |

Paths are as documented by each tool in October 2026 ([Claude Code](https://code.claude.com/docs/en/skills), [Codex](https://developers.openai.com/codex/skills), [OpenCode](https://opencode.ai/docs/skills)); skill discovery changes over time, so check yours if the skill is not picked up. The skill needs nothing but Python 3. The plugin and marketplace manifests pass `claude plugin validate`.

Then just talk to your agent:

> "Set up a Barid for this project. I run three sessions: a planner, a builder and a tester."
> "Add a task for the tester: run the regression suite on the new build. It needs the builder's task first."
> "What can I run right now? Which of those can run in parallel?"
> "Write the prompt for T4 now that T3 is done."

For agents without skills, put this in `AGENTS.md`:

```
This project uses Barid. Before planning work for other sessions, read docs/AGENT.md of
https://github.com/hikkian/barid and use `python3 <path>/barid.py` as described there.
```

## How it works

```
        you                         planner agent                  worker sessions
         |  barid open (panel)              |  barid add / barid edit              |  barid next / claim / finish
         v                               v                                v
   +----------------------------------------------------------------------------+
   |   .barid/board.json   (one file, locked + atomic writes, event log)    |
   +----------------------------------------------------------------------------+
```

- **Lanes** are your sessions. A lane runs one task at a time. Add one with the "+" button in the panel or `barid lane add`; the *Connect* button (or `barid connect LANE`) gives you the first message to paste into that agent's window, after which it takes its tasks from the board by itself.
- **Tasks** have a prompt, dependencies (`--needs`), exclusive resources (`--uses gpu`) and two flags: `--quiet` (needs a quiet machine) and `--noisy` (loads the CPU or the desktop). The board computes from them what is *ready*, *waiting* or *blocked*, and plans *steps*: tasks in one step may run at the same time.
- **Prompts carry their own instructions.** When you press *Copy prompt*, the board appends a short footer with the exact commands (`claim`, `finish`, `note`) and the path of `barid.py`, so even a session without the skill knows how to report.
- **Draft tasks** hold only an outline. When their turn comes the panel offers *Generate prompt*: it copies a request (outline, the reports of finished dependencies, context files, a prompt skeleton) for your agent, which writes the prompt and stores it with `barid edit`.
- **Reports** end up in a review inbox. *Accept report* unlocks dependents (or they unlock as soon as the report is written; a policy switch decides).

```
draft -> queued -> sent -> running -> review -> done        (proposed -> queued on approval; cancelled anywhere before done)
```

## Two (or more) different agents on one project

Barid does not care which agent sits behind a session: Claude Code and Codex, Claude Code and a local model, three Codex windows. Give each its own lane and say which agent it is:

```bash
barid init --lanes "claude:Claude Code,codex:Codex,local:Local model"
barid lane edit claude --agent claude --color "#ff8fb3" --human
barid lane edit local  --agent local  --color "#86f9e4" --human
```

What makes them work as a team:
- **Handoff.** When a task has finished, the prompt of every task that depends on it automatically starts with a short handoff: who did it (lane and agent), the outcome, the report path, the branch and worktree, how many files changed and which, and the last notes. The agent that continues the work does not need you to retell anything.
- **Same rules for everyone.** The prompt itself carries the commands to claim and finish, the file scope and the protected paths, so it does not matter whether the agent reads `CLAUDE.md`, `AGENTS.md` or neither.
- **A hint for small models.** A lane whose agent is `local` gets one extra line: work through the steps one at a time, run commands exactly as written, say so if a step is unclear.
- **No collisions.** Exclusive resources, file scopes and separate worktrees apply across agents the same way (see above).

## How is this different?

Several tools already help with more than one agent. Barid is a small one with a different centre, so here is an honest map (as of October 2026; check their pages, they move fast):

| | What it mainly does | How it differs from Barid |
|---|---|---|
| [Claude Squad](https://github.com/smtg-ai/claude-squad) | A terminal UI that runs several agents, each in its own git worktree | It launches and hosts the sessions; Barid does not launch anything, it plans the work and hands you the prompts |
| [Vibe Kanban](https://github.com/BloopAI/vibe-kanban) | A kanban app that starts coding agents on cards and reviews the results | It orchestrates the agents itself; with Barid you stay in the middle and use the windows you already have |
| [MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) | Messaging between agents, with identities, inboxes and advisory file leases | Agents talk to each other through an MCP server; Barid has no agent-to-agent chat and needs no MCP |
| [Aqua](https://github.com/vignesh07/aqua) | A shared task queue with atomic claiming and file locking for CLI agents | Closest in spirit. Aqua lets agents pick tasks from a queue; Barid also stores the prompt text, plans the order and the parallel steps, and records an honest outcome |
| Claude Code agent teams | A lead agent that spawns and directs teammates inside Claude Code | Works inside one product; Barid works across products (Claude Code, Codex, OpenCode, a local model) |

What Barid adds, in one line each:

- **The prompt is the unit of work.** The board stores the text you will paste, can keep a task as a draft until the planner writes its prompt, and hands the result of a finished task to the next one.
- **You stay in the loop on purpose.** Nothing is launched or controlled. Any chat window or terminal that can run a command works, and an agent can only propose, claim and finish its own task.
- **Honest endings.** A stopped, partial or failed run never unlocks the tasks that depend on it by itself; you decide.
- **One file, no daemon.** Python only, a JSON board, optional panel. No MCP server, no database, no API keys.

Who it is not for: if you want agents to run unattended, split a goal into subtasks on their own and merge branches, use an orchestrator from the table. Barid is for people who prefer to keep the steering wheel.

## Keeping sessions out of each other's way

Two sessions rarely fight over a GPU alone: they also overwrite each other's files. Barid handles this on four levels:

1. **Exclusive resources** (`--uses gpu`): only one task at a time (as above).
2. **File scopes** (`--touches src/gateway,tests`): files, folders or globs a task may change. Tasks whose scopes overlap never run together, and the panel says why ("waiting for T1, same files: src/gateway.py"). The overlap test is conservative: it may block two tasks that would not really clash, never the other way round.
3. **Protected paths** (`barid protect add config/live.json`): files no task may change. Barid takes a checksum when a task is claimed and compares it when the task finishes.
4. **Isolated checkouts** (`barid worktree T1`): a git worktree and branch of its own for the task, written into its prompt ("work only in ..."). Tasks in different worktrees cannot overwrite each other, their work meets at merge time.

When a task finishes, Barid also asks git what changed since the claim and flags anything **outside the task's scope**, anything inside **another running task's scope**, and any **protected file** that changed. A report with such flags never unlocks dependent tasks by itself, even when it says *complete*: it waits in your inbox with the list of files. `barid check T1` shows what could collide before you start.

This is **detection plus convention, not a sandbox**: an agent can still write any file it has permission to write. The prompt tells it the rules, the worktree keeps its changes apart, and Barid makes violations impossible to miss. For hard guarantees combine it with OS-level permissions or containers.

## The panel

| | |
|---|---|
| **What to do now** | per lane: the running task, or the next ready one with *Copy prompt*, *Mark as sent* and *Connect*; a line telling whether the picked tasks can run together |
| **Inbox** | proposals from agents (*Approve / Edit / Reject*) and reports to review (*Accept / Send back*) |
| **Order** | steps with parallel groups, dependency chips, resource tags, why something is blocked |
| **Details** | prompt text, notes, history, edit form, any status |
| **Archive / Activity** | finished and cancelled tasks, the event log |

English, Russian and Kazakh built in (auto-detected, pick one from the language list in the header; the texts written for your agents have their own language, taken from your system language when the board is created, changeable with `barid lang en|ru|kk`), light, dark or automatic mode and four colour palettes (Forest, Teal, Graphite, Midnight) from the *Appearance* button, works on a phone. The same menu lets you **name and colour each session** and say which **agent** runs in it (Claude Code, Codex, OpenCode, Gemini CLI, Cursor, Aider, a local model, or any name); the agent shows as a badge on the session. The panel polls only while its tab is visible and costs nothing when closed: the server exits after 30 idle minutes (`barid open --idle-exit 0` keeps it).

![Details drawer](docs/img/panel-light-drawer.png)

## Command line

Every command accepts `--json`. `barid --help` lists them all.

```bash
barid init --lanes main,helper               # create .barid/board.json here
barid add T1 --lane main --title "Build" --text-file p.md --uses gpu --by planner
barid add T2 --lane helper --title "Analyse" --outline "summarise T1's results" --needs T1 --by planner
barid plan                                   # steps and what can run together
barid next --lane helper                     # the next ready prompt for a lane (exit code 2 if none)
barid claim T1 --by worker-a                 # refused when a conflicting task is running
barid finish T1 --report reports/t1.md --by worker-a
barid gen T2                                 # request that makes an agent write T2's prompt
barid move T3 1                              # priority: earlier in the list = earlier slot in the plan
barid add T3 --lane a --touches src/api --text-file p.md   # file scope: overlapping scopes never run together
barid protect add config/live.json        # no task may change it (verified at finish)
barid worktree T3                         # an isolated git worktree and branch for the task
barid check T3                            # what could collide with it
barid lane edit a --title Builder --color "#7c5cff" --agent codex   # name, colour and agent of a session
barid lane add tests --title Tests --agent codex       # a new session (or the "+" button in the panel)
barid connect tests                          # the first message to paste into that agent's window
barid open                                   # panel;  barid export board.html  = read-only snapshot
```

Person-only actions (`approve`, `accept`, `sent`, `status`, `restore`, `purge`, `trust`) need `--human` (or `BARID_ACTOR=human`); the panel does them for you. See [docs/PROTOCOL.md](docs/PROTOCOL.md) for every rule and the file format.

## Safety model

Barid prevents accidents, not malice. Identities are self-declared (`--by`), so any process on your machine that can run `barid` can claim to be you. What it does guarantee:

- all writes are atomic and serialised by a file lock; two agents racing for one GPU cannot both win (tested with threads and processes);
- agents cannot approve, accept, set arbitrary statuses, purge, or finish a task another agent holds;
- file changes outside a task's declared scope, in another running task's scope or in protected paths are detected at the end of the task and keep dependents locked until you decide;
- tasks are never deleted by agents, only cancelled; the event log is append-only;
- the panel server binds to loopback only, rejects foreign `Host` and `Origin` headers and writes only through one guarded endpoint (a custom header, so another website cannot post to it);
- the panel never reads files named in tasks (report paths are shown, not opened).

Keep secrets out of prompts and notes: the board is plain JSON in your project folder (add `.barid/` to `.gitignore` unless you want to commit it).

## Status

I built Barid for myself, to stop losing track of my own agent windows. If it is useful to you, please use it; ideas and bug reports are welcome, but it is a one-person project and I cannot promise fast answers.

Version 0.4, used every day by its author to run two agent sessions. Linux is the tested home; macOS and Windows are covered by CI but have had little real-world use, and nothing here has been tried on AMD or Intel GPUs (Barid itself does not care about the GPU: it only tracks who holds a shared resource). Bug reports with the output of `barid doctor` are very welcome.

## Requirements and platforms

Python 3.9 or newer, any OS. CI runs the tests on Linux, macOS and Windows. The panel needs any modern browser. If you want it as an always-on-top window, open it in an app window (`chromium --app=URL`) and pin it with your window manager.

## Development

```bash
python3 -m unittest discover -s tests -v     # no dependencies
python3 examples/build_example.py /tmp/demo  # an example board
```

The whole tool is `skills/barid/scripts/barid.py` (logic, CLI, server) and `panel.html` next to it. Contributions that keep it dependency-free are welcome.

## The name

*Barid* (بريد) is the Arabic word for mail. It was also the name of the relay post of the early caliphates: couriers handed messages from station to station along a chain of posts. Prompts are the messages, your agent sessions are the stations, and this is the board that keeps the relay in order. (The first working title was RelayBoard; the old `rb`, `RB_*` and `.relayboard` names still work.)

## Contributing, security, license

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). MIT license, see [LICENSE](LICENSE). Copyright (c) 2026 hikkian.
