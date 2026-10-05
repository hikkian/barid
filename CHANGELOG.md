# Changelog

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
