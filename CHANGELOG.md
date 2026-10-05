# Changelog

## 0.1.0 (2026-10-05)

First public version.

- Board with lanes (one per agent session), tasks, dependencies, exclusive resources and the quiet/noisy flags; the plan groups tasks into steps that may run together.
- Agent side: the `barid` command (`add`, `edit`, `next`, `claim`, `finish`, `gen`, `note`, `plan`, ...), a skill for Claude Code, Codex and OpenCode, a Claude Code plugin and marketplace, prompt templates, `docs/AGENT.md` for any other agent.
- Reports end with an honest outcome (`complete`, `partial`, `failed`); only `complete` unlocks dependent tasks by itself.
- Agents' additions are proposals until the person approves; agents can only cancel, never delete; every change is logged.
- Browser panel: what to do now, inbox (proposals and reports), order with parallel groups, details with editing, English and Russian, light/dark/auto and four colour palettes. Starts on demand and exits when idle.
- Safety: atomic locked writes, leases on claimed tasks, loopback-only server with Origin and Host checks.
- Tests on Linux, macOS and Windows (Python 3.9, 3.12, 3.13).
