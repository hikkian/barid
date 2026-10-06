"""Random operation sequences and garbage input against the core: nothing may crash with anything but a clean Barid error,
and the board must keep its invariants after every step. Needs `hypothesis` (skipped without it)."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from hypothesis import HealthCheck, given, settings, strategies as st
    from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
    HAVE = True
except ImportError:  # pragma: no cover
    HAVE = False

    class _Anything:  # lets the class bodies below be defined when hypothesis is not installed (the tests are skipped)
        def __getattr__(self, name):
            return lambda *a, **k: None

    st = _Anything()
    HealthCheck = []

    def settings(*a, **k):
        return lambda f: f

    def given(*a, **k):
        return lambda f: f

ROOT = Path(__file__).resolve().parent.parent
RB_PATH = ROOT / "skills" / "barid" / "scripts" / "barid.py"
spec = importlib.util.spec_from_file_location("rb_robust", RB_PATH)
rb = importlib.util.module_from_spec(spec)
sys.modules["rb_robust"] = rb
spec.loader.exec_module(rb)

IDS = ["T0", "T1", "T2", "T3", "T4", "T5"]
LANES = ["a", "b", "c"]
RES = ["gpu", "user", "port:8091"]
HUMAN = rb.HUMAN
TRUSTED = rb.Actor("agent", "planner")
WORKER = rb.Actor("agent", "worker")
ACTORS = [HUMAN, TRUSTED, WORKER]

if HAVE:
    class BoardMachine(RuleBasedStateMachine):
        @initialize()
        def setup(self):
            self.tmp = tempfile.TemporaryDirectory()
            self.path = Path(self.tmp.name) / ".barid" / "board.json"
            self.path.parent.mkdir()
            d = rb.new_board("fuzz", [(l, l.upper()) for l in LANES])
            d["policy"]["direct_agents"] = ["planner"]
            d["resources"]["port:8091"] = {"label": "port 8091", "exclusive": True}
            rb.save(self.path, d)
            self.rev = rb.load(self.path)["rev"]

        def teardown(self):
            self.tmp.cleanup()

        def step(self, fn):
            try:
                rb.mutate(self.path, fn)
            except rb.RBError:
                pass  # a refused operation is fine; anything else is a bug and propagates
            rev = rb.load(self.path)["rev"]
            assert rev >= self.rev
            self.rev = rev

        @rule(i=st.sampled_from(IDS), lane=st.sampled_from(LANES), actor=st.sampled_from(ACTORS), uses=st.lists(st.sampled_from(RES), max_size=2, unique=True),
              needs=st.lists(st.sampled_from(IDS), max_size=2, unique=True), profile=st.sampled_from(["", "light", "dev", "bench", "attended"]),
              quiet=st.booleans(), noisy=st.booleans(), text=st.sampled_from(["", "do it"]), touches=st.sampled_from(["", "src", "src/a", "docs"]))
        def add(self, i, lane, actor, uses, needs, profile, quiet, noisy, text, touches):
            self.step(lambda d: rb.op_add(d, actor, {"id": i, "lane": lane, "uses": ",".join(uses), "needs": ",".join(needs), "profile": profile, "quiet": quiet,
                                                    "noisy": noisy, "text": text, "outline": "x", "touches": touches}))

        @rule(i=st.sampled_from(IDS), actor=st.sampled_from(ACTORS), needs=st.lists(st.sampled_from(IDS), max_size=3, unique=True), lane=st.sampled_from(LANES))
        def edit(self, i, actor, needs, lane):
            self.step(lambda d: rb.op_edit(d, actor, i, {"needs": ",".join(needs), "lane": lane}))

        @rule(i=st.sampled_from(IDS), actor=st.sampled_from(ACTORS), after=st.lists(st.sampled_from(IDS), max_size=3, unique=True),
              when=st.sampled_from(["", "+2h", "23:00", "2026-10-07 23:00", "soon"]), box=st.sampled_from(["", "90m", "8h", "0", "forever"]))
        def edit_night(self, i, actor, after, when, box):
            self.step(lambda d: rb.op_edit(d, actor, i, {"after": ",".join(after), "not_before": when, "timebox": box}))

        @rule(i=st.sampled_from(IDS), actor=st.sampled_from(ACTORS))
        def heartbeat(self, i, actor):
            self.step(lambda d: rb.op_heartbeat(d, actor, i, 3))

        @rule(lane=st.sampled_from(LANES), state=st.sampled_from(list(rb.PRESENCE_STATES) + ["bogus"]))
        def seen(self, lane, state):
            self.step(lambda d: rb.op_seen(d, lane, "w", state, "T1", "why"))

        @rule(i=st.sampled_from(IDS), actor=st.sampled_from(ACTORS), force=st.booleans())
        def claim(self, i, actor, force):
            self.step(lambda d: rb.op_claim(d, actor, i, force=force))

        @rule(i=st.sampled_from(IDS), actor=st.sampled_from(ACTORS), outcome=st.sampled_from(["", "complete", "partial", "failed"]), force=st.booleans())
        def finish(self, i, actor, outcome, force):
            self.step(lambda d: rb.op_finish(d, actor, i, report="r.md", outcome=outcome, force=force))

        @rule(i=st.sampled_from(IDS), actor=st.sampled_from(ACTORS))
        def release(self, i, actor):
            self.step(lambda d: rb.op_release(d, actor, i, force=actor is HUMAN))

        @rule(i=st.sampled_from(IDS), op=st.sampled_from(["accept", "approve", "reject", "sent", "restore"]))
        def person(self, i, op):
            fn = {"accept": rb.op_accept, "approve": rb.op_approve, "reject": rb.op_reject, "sent": rb.op_sent, "restore": rb.op_restore}[op]
            self.step(lambda d: fn(d, HUMAN, i))

        @rule(i=st.sampled_from(IDS), actor=st.sampled_from(ACTORS))
        def cancel(self, i, actor):
            self.step(lambda d: rb.op_cancel(d, actor, i, "why"))

        @rule(i=st.sampled_from(IDS), status=st.sampled_from(list(rb.STATUSES)))
        def force_status(self, i, status):
            self.step(lambda d: rb.op_set_status(d, HUMAN, i, status))

        @rule(i=st.sampled_from(IDS), outcome=st.sampled_from(["complete", "partial", "failed", "bogus"]))
        def outcome(self, i, outcome):
            self.step(lambda d: rb.op_outcome(d, HUMAN, i, outcome))

        @rule(lane=st.sampled_from(LANES + ["d"]), actor=st.sampled_from(ACTORS))
        def lane(self, lane, actor):
            self.step(lambda d: rb.op_lane_add(d, actor, lane, "New", "#aabbcc", "codex"))

        @invariant()
        def board_is_consistent(self):
            if not hasattr(self, "path"):
                return
            d = rb.load(self.path)
            json.dumps(d, default=str)
            ids = [x["id"] for x in d["items"]]
            assert len(ids) == len(set(ids)), "duplicate task ids"
            lanes = {l["id"] for l in d["lanes"]}
            for it in d["items"]:
                assert it["status"] in rb.STATUSES, it
                assert it["lane"] in lanes, it
                assert all(n in ids for n in it["needs"]), ("dangling needs", it)
                assert all(n in ids for n in it.get("after", [])), ("dangling after", it)
                assert it["id"] not in it["needs"] and it["id"] not in it.get("after", [])
                assert not set(it["needs"]) & set(it.get("after", [])), "a task in needs and after"
                assert it.get("timebox", 0) >= 0
                if it["status"] == "running":
                    assert it.get("claim"), ("running without a claim", it["id"])
            assert not rb.has_cycle({x["id"]: x for x in d["items"]}), "dependency cycle"
            comp = rb.compute(d)
            steps = rb.plan_steps(d, comp)
            placed = [i for s in steps for i in s]
            assert len(placed) == len(set(placed)), "a task in two steps"
            by = {c["id"]: c for c in comp}
            where = {i: n for n, s in enumerate(steps) for i in s}
            for n, s in enumerate(steps):
                for a in s:
                    for b in s:
                        if a < b and not (by[a]["status"] in rb.ACTIVE and by[b]["status"] in rb.ACTIVE):
                            assert not rb.conflicts_between(d, by[a], by[b]), ("conflicting tasks in one step", a, b, n)
            for c in comp:
                for need in c["needs"]:
                    if c["id"] in where and need in where and by[need]["status"] not in ("done", "cancelled", "review"):
                        assert where[need] < where[c["id"]] or by[c["id"]]["status"] in rb.ACTIVE, ("order", need, c["id"])
            for c in comp:
                if c["status"] == "queued":
                    assert bool(c["reasons"]) == (c["state"] != "ready"), ("reasons and state disagree", c["id"], c["state"], c["reasons"])
                    assert all(r["code"] in rb.REASONS for r in c["reasons"])
                else:
                    assert c["reasons"] == []
            for lane_id in LANES:
                res = rb.next_step(d, lane_id)
                assert res["status"] in (rb.NEXT_TASK, rb.NEXT_NONE, rb.NEXT_WAIT, rb.NEXT_STUCK)
                ready = [c for c in comp if c["lane"] == lane_id and c["state"] == "ready"]
                assert (res["status"] == rb.NEXT_TASK) == bool(ready), ("next and ready disagree", lane_id)
            view = rb.board_view(self.path)
            for lane_id, f in view["focus"].items():
                assert by[f["id"]]["lane"] == lane_id
                if f["role"] != "running":
                    assert "hint" in f


@unittest.skipUnless(HAVE, "needs hypothesis")
class RobustnessTests(unittest.TestCase):
    def test_random_operation_sequences_keep_the_board_consistent(self):
        BoardMachine.TestCase.settings = settings(max_examples=int(os.environ.get("BARID_FUZZ", "60")), stateful_step_count=int(os.environ.get("BARID_FUZZ_STEPS", "30")), deadline=None, suppress_health_check=list(HealthCheck))
        suite = BoardMachine.TestCase("runTest")
        suite.runTest()

    @settings(max_examples=int(os.environ.get("BARID_FUZZ", "60")) * 3, deadline=None, suppress_health_check=list(HealthCheck))
    @given(action=st.sampled_from(["add", "edit", "claim", "finish", "release", "sent", "approve", "reject", "accept", "cancel", "restore", "status", "note",
                                   "outcome", "lane", "laneadd", "boardlang", "bogus", ""]),
           iid=st.one_of(st.sampled_from(IDS), st.text(max_size=12)),
           args=st.dictionaries(st.sampled_from(["title", "lane", "needs", "uses", "quiet", "noisy", "profile", "lint_ok", "text", "status", "outcome", "color",
                                                 "agent", "lang", "touches", "report", "reason"]),
                                st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=20), st.lists(st.text(max_size=5), max_size=3)), max_size=6))
    def test_garbage_actions_never_crash_the_server_code(self, action, iid, args):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".barid" / "board.json"
            path.parent.mkdir()
            rb.save(path, rb.new_board("fuzz", [(l, l.upper()) for l in LANES]))
            rb.mutate(path, lambda d: rb.op_add(d, HUMAN, {"id": "T0", "lane": "a", "text": "x"}))
            try:
                rb.apply_action(path, {"action": action, "id": iid, "args": args})
            except rb.RBError:
                pass


if __name__ == "__main__":
    unittest.main()
