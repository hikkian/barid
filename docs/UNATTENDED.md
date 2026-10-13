# Unattended runs: nights, queues, chains

A chain of tasks that runs while nobody watches fails in a few predictable ways. Barid 1.0 handles each of them in the board, so that it does not depend on an agent staying patient.

| What goes wrong | What Barid does |
|---|---|
| The next task waits for a GPU or another session, the agent reads "nothing is ready" and stops | `next` separates **nothing queued** (exit 2) from **will start by itself** (exit 3) from **needs the person** (exit 4); `next --wait` and `claim --wait` do the waiting inside the command |
| A task that ended *partial* silently blocks everything after it until someone accepts it | `--after` (run after any end) next to `--needs` (after a success); a task that waits for a report to be accepted says `decision`, one that waits for a cancelled task says `never` |
| "Do not start before 23:00" lives only in the prompt text | `--not-before`: the board refuses the claim and `next` answers "wait" until then |
| An agent that waits cannot be told from one that died | every `next`, `claim`, `note`, `finish` and `heartbeat` is a sign of life, shown per session in the panel; a worker renews a 3-minute lease once a minute, so a dead one shows as stale within minutes |
| The chat session gets distracted, compacts its context, or ends a long loop politely | `barid worker`: a loop outside the model (the "Ralph loop" pattern) with a fresh agent per task |
| A task hangs and eats the night | `--timebox`: the worker stops its process group at the deadline and closes the task as failed |
| Coming back in the morning to find out what happened | `barid digest` and the "While you were away" card |

## The vocabulary

### Why a task waits (`reasons`)

`list --json`, the panel and `next` give each queued task a list of reasons, in the spirit of the REASON column of Slurm's `squeue`:

| code | meaning | ends by itself? |
|---|---|---|
| `begin_time` | `--not-before` has not come (like Slurm `BeginTime`) | yes, with time |
| `dependency` | a task in `needs` or `after` is not over | yes |
| `conflict` | a task that runs now uses the same exclusive resource, the same lane, or conflicting marks | yes |
| `decision` | the task it needs ended without success and waits for you to accept it, or is a draft or proposal | **no, needs you** |
| `never` | the task it needs was cancelled or does not exist (like Slurm `DependencyNeverSatisfied`) | **no, change the dependency** |

### `needs` and `after`

| | waits until | like |
|---|---|---|
| `needs T1` | T1 is *done* (accepted), or finished with outcome `complete` and no file violations | Slurm `afterok` |
| `after T1` | T1 is over: *review*, *done* or *cancelled*, whatever the outcome | Slurm `afterany` |

Use `needs` for a task that is meaningless without the other one's success; use `after` for a task that must follow in time but does not care how the other ended (the nightly tests run after the build whether or not the build was clean). Both count for the order of the plan and for cycle checks.

### `next` and `claim`: exit codes

| code | `next` | `claim` |
|---|---|---|
| 0 | a task was printed | claimed |
| 1 | an error | refused for another reason (held by someone else, wrong state, ...) |
| 2 | the lane has nothing queued | |
| 3 | nothing can start yet, but it will without help | must wait: `begin_time`, `waiting`, `conflict` |
| 4 | nothing will change without the person | `stuck`: a `decision` or `never` reason |

With `--wait SECONDS` the command waits (checking every 5 seconds, a line on stderr every minute) for a task that only has to wait, and returns as soon as it can. It never waits for the person: code 4 comes at once.

### Sessions

`lane.seen` holds `{at, by, state, task, reason}` for each lane: `working` (a task is claimed), `waiting` (with the reason), `idle` (finished, nothing queued, or needs the person). It is written by the commands above, not by a background process, and only when something changed or a minute has passed.

A claim has `lease_until` (default 12 hours; a task with a timebox gets at least timebox + 1 hour and never less than the default; a worker uses 3 minutes and renews it once a minute), `signal` (the last sign of life) and, for a task with a timebox, `deadline`. A running task whose lease expired is shown as stale.

## The night connection text

```
barid lane edit night --unattended        # or the "Night mode" box in the panel's Connect window
barid connect night                       # paste this as the session's first message
```

The text tells the session to loop on `next --lane night --wait 300`: run the task on exit 0 and come back, repeat on 3 for up to 14 hours without asking anything, stop on 2 (the lane is empty) or 4 (it needs you), stop on any other refusal. The default text (without `--unattended`) stops on 2, 3 and 4 and tells the user what the output says.

## `barid worker`

```
barid worker --lane night --cmd 'codex exec -' --cwd ~/project
barid worker --lane night --cmd 'claude -p' --max-wait 36000 --max-tasks 5
```

For each ready task of the lane the worker:

1. claims it (as `worker-<lane>`, or `--by`), with a 3-minute lease (the slow part of the claim, git and hashing the protected files, is done before the board lock is taken);
2. writes the prompt (with a footer that says the task is already claimed and gives the `finish` command) to `.barid/runs/<id>-<time>.prompt.md`, starts your command in its own process group with the prompt on **stdin**, and with `BARID_PROMPT_FILE`, `BARID_TASK`, `BARID_LANE`, `BARID_AGENT`, `BARID_BOARD` in the environment (`{prompt_file}` inside the command is replaced by the file name);
3. renews the lease once a minute and stops the process group at the task's deadline (SIGTERM, then SIGKILL after 20 s; never anything outside its own group). The deadline is counted on the monotonic clock, so a suspended computer or a clock that was set does not kill a healthy agent. It also stops the agent when you cancel the task or return it to the queue while it runs, and it stops what the agent left running in the background when the command ends;
4. when the command ends and the agent did not call `finish`, closes the task itself: `partial` if the exit code was 0, `failed` otherwise (or after a timeout), with a note and the log path;
5. asks for the next task. It waits for start times, other tasks and conflicts, and ends when the lane is empty (exit 0), when it needs you (exit 4) or when nothing could start for `--max-wait` seconds (exit 3).

Safety nets: a `--cwd` that is not a folder is refused before anything is claimed; a command that cannot even start closes its task as `failed` (never leaves it `running`) and three such failures in a row stop the worker (exit 1); a task that comes back to the queue again and again without a report (an agent that calls `release`) is started at most `--max-attempts` times (default 3, with a pause that grows), then closed as `failed` so that you see it in the morning; the prompt is written to the agent from a thread, so a big prompt that the agent does not read cannot block the worker.

Limits that are worth knowing: the worker stops the agent's **process group** (on Windows its process tree, and only while the agent's leading process is alive), so an agent that detaches itself (`setsid`, a daemon) escapes it; a worker that is killed with SIGKILL cannot close its task, and the task is taken as lost five minutes after its lease ran out (the grace is there so that a computer that slept can renew its leases first); a closed terminal or a dropped ssh session (SIGHUP) stops the agent and closes the task like SIGTERM does. `{prompt_file}` is quoted for the shell unless the command already put it in quotes.

A fresh agent for every task is deliberate: long sessions accumulate stale context, and "one task, one context, state in the board" is the pattern the unattended-agent community converged on.

## A wait that can never end

A task that waits for a task that waits for a cancelled one, or for a holder whose lease ran out (its session is most likely gone), cannot start by itself. `next` and `claim` answer `4` (needs the person) for all of them at once, with the reason code at the bottom of the chain and `via` naming the task it waits for. A task you marked as `sent` keeps its order too: `claim` refuses it before what it needs, and a sent task that itself waits does not hold back the task it waits for. Returning the dead holder to the queue (the panel's button, or `release`) frees the lane.

## `barid digest`

```
barid digest --since 12h        # also 90m, 2d, or '2026-10-07 08:00'
```

Sessions (state and how long ago the last sign of life), tasks that ended since then with outcome and report path, what runs now (and what has an expired lease), what needs you (reports to accept, proposals to approve, decisions, broken dependencies) and what still waits and why. The panel shows the same as a card at the top, from the moment of your last "Got it".

## What to put in the task prompts

Barid cannot make a prompt good, but the prompts of unattended tasks should say, in this order: what done looks like; what to do when blocked (use a documented assumption and keep going, rather than stop and ask); the error budget (retries, then give up and report `partial`); the hard limits (what must never be touched). The footer Barid adds already tells a worker "nobody can answer questions: decide by the task's own rules and write the decision into the report".

## Sources of the ideas

Batch schedulers: Slurm dependency types and job reason codes ([afterok, afterany, afternotok](https://docs.ncsa.illinois.edu/en/latest/common/slurm/dependencies-constraints.html), [reason codes](https://slurm.schedmd.com/job_reason_codes.html)). Workflow engines: [Airflow trigger rules](https://www.astronomer.io/docs/learn/airflow-3/airflow-trigger-rules). Leases and heartbeats: [Temporal long-running activities](https://docs.temporal.io/design-patterns/long-running-activity), [heartbeat-based leases](https://docs.webdock.io/how-guides/programming-guides/handling-worker-crashes-with-heartbeat-based-leases/). Unattended agents: [Ralph loop](https://www.danvega.dev/blog/ralph-loop), [patterns for agents that run without babysitting](https://dev.to/yureki_lab/how-i-stopped-babysitting-claude-code-5-patterns-for-247-ai-workers-270n), [an overnight playbook](https://trystandby.com/guides/run-claude-code-overnight).
