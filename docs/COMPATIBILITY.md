# Compatibility promise (1.0)

[Русский](COMPATIBILITY.ru.md) · [Қазақша](COMPATIBILITY.kk.md)

From 1.0 on, scripts, agent prompts and boards written for one 1.x release keep working with every later 1.x release. `tests/test_contract.py` checks the list below on every change; if one of those tests has to change, that is a 2.0.

## What is frozen

- **The board file.** Schema `1` (`"schema": 1`). The statuses (`draft`, `proposed`, `queued`, `sent`, `running`, `review`, `done`, `cancelled`), the outcomes (`complete`, `partial`, `failed`), the fields of a board and of a task as listed in the test, and their meaning. A reader may ignore a field it does not know; Barid keeps fields it does not know when it saves.
- **Boards of every release open.** A board made by 0.1 or any later release opens, plans, exports and takes new work with the current code; reading never rewrites the file (`tests/fixtures`, `tests/test_migration.py`).
- **Exit codes.** `next`: `0` a task was printed, `2` nothing is queued in the lane, `3` nothing can start yet but will without help, `4` it needs the person. `claim`: `0` taken, `3` must wait (start time, other tasks, conflict), `4` needs the person. Any other failure is `1`.
- **Reason codes** of a waiting task: `begin_time`, `dependency`, `conflict` (end by themselves), `decision`, `never` (need the person). A reason may carry more fields (`via`, `stale`); the five codes stay.
- **Command names and their flags.** A command or a flag is never removed or renamed in 1.x. A new command or flag may appear.
- **`--json` outputs.** Every field now present stays with its meaning; new fields may be added.
- **The agent protocol.** `connect` → `next` → `claim` → work → `finish` with `--report`, `--outcome`, `--by`, and the exit codes above. A session that follows the printed text keeps working after an upgrade.

## What may change

- The wording of the human-readable text (messages, the connection text, the footer of a prompt): it is not meant to be parsed, so read exit codes and `--json` in scripts.
- The look of the panel, its translations, and the layout of `runs/` logs.
- New commands, flags, fields, and reason details.
- Internal functions of `barid.py`: it is a program, not a library.

## When something has to go

A command or field that has to be removed is first marked as deprecated, with a warning, for at least one minor release, and removed only in 2.0 together with a migration note in the changelog. A board schema change comes with automatic reading of the old schema.

## Files Barid writes

`board.json` (written atomically and flushed to the disk), `board.json.bak` (the version before the last change, a hard link: no extra data written), `board.json.lock`, the temporary `board.json.tmp*` (removed when old), and `runs/` (logs of `barid worker`). Environment: `BARID_BOARD`, `BARID_AGENT` and `BARID_LOCK_WAIT` (seconds a command waits for the board lock, default 60) are stable; `BARID_WORKER_BACKOFF` exists for tests only.
