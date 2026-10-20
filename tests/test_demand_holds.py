"""BARIDRES1: a resource a task needs only for some of its steps (NAME@demand, taken with `hold`), a check that refuses while something
outside Barid uses it, reads declared apart from writes, hints when a claim is refused, and lint that does not cry wolf."""
import datetime as dt
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RB_PATH = ROOT / "skills" / "barid" / "scripts" / "barid.py"
spec = importlib.util.spec_from_file_location("rb_demand", RB_PATH)
rb = importlib.util.module_from_spec(spec)
sys.modules["rb_demand"] = rb
spec.loader.exec_module(rb)
HUMAN = rb.HUMAN
AGENT_A = rb.Actor("agent", "agent-a")
AGENT_B = rb.Actor("agent", "agent-b")


def new_board(tmp):
    path = Path(tmp) / ".barid" / "board.json"
    path.parent.mkdir(parents=True)
    rb.save(path, rb.new_board("t", [("a", "A"), ("b", "B"), ("c", "C")]))
    rb.mutate(path, lambda d: d["resources"].update({"gpu": {"label": "GPU", "exclusive": True}}))
    return path


def add(path, iid, lane="a", **kw):
    kw.setdefault("text", "do it")
    kw.update(id=iid, lane=lane)
    return rb.mutate(path, lambda d: rb.op_add(d, HUMAN, kw))


def task(d, iid):
    return next(i for i in d["items"] if i["id"] == iid)


class SplitUses(unittest.TestCase):
    def test_a_demand_suffix_is_split_from_the_whole_task_list(self):
        self.assertEqual(rb.split_uses("gpu,user@demand"), (["gpu"], ["user"]))

    def test_a_resource_that_is_both_is_whole_task(self):
        self.assertEqual(rb.split_uses("gpu,gpu@demand"), (["gpu"], []))

    def test_nothing_is_nothing(self):
        self.assertEqual(rb.split_uses(None), ([], []))


class DemandAndWholeTask(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = new_board(self._tmp.name)

    def claim(self, iid, actor):
        return rb.claim_task(self.path, actor, iid)

    def test_a_demand_task_and_a_whole_task_run_together_until_one_holds_it(self):
        add(self.path, "CODE", lane="a", uses="gpu@demand")
        add(self.path, "MEASURE", lane="b", uses="gpu", quiet=True)
        self.claim("CODE", AGENT_A)
        self.claim("MEASURE", AGENT_B)        # a demand task does not block the whole-task one
        d = rb.load(self.path)
        self.assertEqual(rb.compute(d)[0]["conflicts"], [])

    def test_hold_is_refused_while_another_task_uses_the_resource_for_the_whole_task(self):
        add(self.path, "CODE", lane="a", uses="gpu@demand")
        add(self.path, "MEASURE", lane="b", uses="gpu", quiet=True)
        self.claim("CODE", AGENT_A)
        self.claim("MEASURE", AGENT_B)
        with self.assertRaises(rb.RBError) as cm:
            rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_A, "CODE", "gpu"))
        self.assertEqual(cm.exception.code, "conflict")
        self.assertIn("MEASURE", str(cm.exception))

    def test_a_second_hold_waits_for_the_first_and_succeeds_after_drop(self):
        add(self.path, "C1", lane="a", uses="gpu@demand")
        add(self.path, "C2", lane="b", uses="gpu@demand")
        self.claim("C1", AGENT_A)
        self.claim("C2", AGENT_B)
        rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_A, "C1", "gpu"))
        with self.assertRaises(rb.RBError) as cm:
            rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_B, "C2", "gpu"))
        self.assertEqual(cm.exception.code, "conflict")
        self.assertIn("held by C1", str(cm.exception))
        rb.mutate(self.path, lambda d: rb.op_drop(d, AGENT_A, "C1", "gpu"))
        rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_B, "C2", "gpu"))
        self.assertEqual(rb.load(self.path)["holds"]["gpu"]["task"], "C2")

    def test_a_live_hold_blocks_a_queued_whole_task_claim(self):
        add(self.path, "CODE", lane="a", uses="gpu@demand")
        add(self.path, "LATER", lane="b", uses="gpu", quiet=True)
        self.claim("CODE", AGENT_A)
        rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_A, "CODE", "gpu"))
        with self.assertRaises(rb.RBError) as cm:
            self.claim("LATER", AGENT_B)
        self.assertEqual(cm.exception.code, "conflict")
        d = rb.load(self.path)
        self.assertEqual([c["id"] for c in next(c for c in rb.compute(d) if c["id"] == "LATER")["conflicts"]], ["CODE"])

    def test_the_refusal_says_what_to_try(self):
        add(self.path, "MEASURE", lane="b", uses="gpu", quiet=True)
        add(self.path, "LATER", lane="c", uses="gpu", quiet=True)
        self.claim("MEASURE", AGENT_B)
        with self.assertRaises(rb.RBError) as cm:
            self.claim("LATER", AGENT_A)
        self.assertIn("gpu@demand", str(cm.exception))

    def test_hold_needs_the_demand_declaration_and_a_running_task(self):
        add(self.path, "PLAIN", lane="a", uses="user")
        self.claim("PLAIN", AGENT_A)
        with self.assertRaises(rb.RBError) as cm:
            rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_A, "PLAIN", "gpu"))
        self.assertEqual(cm.exception.code, "undeclared")
        add(self.path, "WAIT", lane="b", uses="gpu@demand")
        with self.assertRaises(rb.RBError) as cm:
            rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_B, "WAIT", "gpu"))
        self.assertEqual(cm.exception.code, "wrong_state")


class Leases(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = new_board(self._tmp.name)
        add(self.path, "CODE", lane="a", uses="gpu@demand")
        rb.claim_task(self.path, AGENT_A, "CODE")
        rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_A, "CODE", "gpu"))

    def set_lease(self, when):
        rb.mutate(self.path, lambda d: d["holds"]["gpu"].update(lease_until=when.isoformat()))

    def test_a_lease_that_ran_out_within_the_grace_still_counts(self):
        self.set_lease(rb.now_dt() - dt.timedelta(seconds=60))
        self.assertIn("gpu", rb.live_holds(rb.load(self.path)))

    def test_a_lease_that_ran_out_long_ago_frees_the_resource(self):
        self.set_lease(rb.now_dt() - dt.timedelta(hours=2))
        self.assertNotIn("gpu", rb.live_holds(rb.load(self.path)))
        add(self.path, "NEXT", lane="b", uses="gpu@demand")
        rb.claim_task(self.path, AGENT_B, "NEXT")
        rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_B, "NEXT", "gpu"))
        self.assertEqual(rb.load(self.path)["holds"]["gpu"]["by"], "agent-b")

    def test_a_heartbeat_of_the_task_extends_its_holds(self):
        self.set_lease(rb.now_dt() + dt.timedelta(minutes=1))
        rb.mutate(self.path, lambda d: rb.op_heartbeat(d, AGENT_A, "CODE", lease_minutes=120))
        lease = rb.parse_ts(rb.load(self.path)["holds"]["gpu"]["lease_until"])
        self.assertGreater(lease, rb.now_dt() + dt.timedelta(minutes=60))


class HoldsEndWithTheTask(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = new_board(self._tmp.name)
        add(self.path, "CODE", lane="a", uses="gpu@demand")
        add(self.path, "CODE2", lane="b", uses="gpu@demand")
        rb.claim_task(self.path, AGENT_A, "CODE")
        rb.mutate(self.path, lambda d: rb.op_hold(d, AGENT_A, "CODE", "gpu"))

    def test_finish_gives_the_resource_back(self):
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT_A, "CODE", report="r.md", outcome="complete"))
        self.assertEqual(rb.load(self.path).get("holds", {}), {})

    def test_release_gives_the_resource_back(self):
        rb.mutate(self.path, lambda d: rb.op_release(d, AGENT_A, "CODE"))
        self.assertEqual(rb.load(self.path).get("holds", {}), {})

    def test_a_task_cannot_drop_what_it_does_not_hold(self):
        with self.assertRaises(rb.RBError) as cm:
            rb.mutate(self.path, lambda d: rb.op_drop(d, AGENT_B, "CODE2", "gpu"))
        self.assertEqual(cm.exception.code, "not_held")


class CheckCommand(unittest.TestCase):
    def test_nothing_printed_means_nothing_found(self):
        self.assertEqual(rb._resource_check(f'"{sys.executable}" -c pass'), "")

    def test_output_is_reported(self):
        self.assertEqual(rb._resource_check(f'"{sys.executable}" -c "print(42)"'), "42")

    def test_a_missing_command_is_a_refusal_not_a_free_resource(self):
        self.assertIn("could not run", rb._resource_check("barid-no-such-command-xyz"))


class ReadsAreNotWrites(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = new_board(self._tmp.name)

    def test_reading_what_another_task_writes_is_a_warning_not_a_block(self):
        add(self.path, "WRITER", lane="a", touches="docs/x.md")
        add(self.path, "READER", lane="b", touches="src/a.py", reads="docs/x.md")
        rb.claim_task(self.path, AGENT_A, "WRITER")
        warnings = rb.claim_task(self.path, AGENT_B, "READER")
        self.assertTrue(any("reads" in w and "docs/x.md" in w for w in warnings), warnings)

    def test_two_writes_on_the_same_file_still_block(self):
        add(self.path, "W1", lane="a", touches="docs/x.md")
        add(self.path, "W2", lane="b", touches="docs/x.md")
        rb.claim_task(self.path, AGENT_A, "W1")
        with self.assertRaises(rb.RBError):
            rb.claim_task(self.path, AGENT_B, "W2")

    def test_two_reads_of_the_same_file_are_nothing(self):
        add(self.path, "R1", lane="a", reads="docs/x.md")
        add(self.path, "R2", lane="b", reads="docs/x.md")
        d = rb.load(self.path)
        self.assertEqual(rb.path_warnings(d, task(d, "R1"), task(d, "R2")), [])

    def test_explain_shows_the_read_against_write_warning(self):
        add(self.path, "WRITER", lane="a", touches="docs/x.md")
        add(self.path, "READER", lane="b", touches="src/a.py", reads="docs/x.md")
        d = rb.load(self.path)
        ex = rb.explain_pair(d, task(d, "READER"), task(d, "WRITER"))
        self.assertEqual(ex["verdict"], "ok")
        self.assertTrue(ex["warnings"])


class LintAndHints(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = new_board(self._tmp.name)

    def test_a_demand_declaration_satisfies_the_gpu_lint(self):
        add(self.path, "CODE", lane="a", uses="gpu@demand", text="code the change, then run the GPU test")
        d = rb.load(self.path)
        codes = [x["code"] for x in rb.lint_task(d, task(d, "CODE"))]
        self.assertNotIn("gpu_missing", codes)
        self.assertNotIn("no_marks", codes)

    def test_a_gpu_mention_with_no_declaration_is_still_linted(self):
        add(self.path, "NOGPU", lane="a", text="run the GPU test", uses="user")
        d = rb.load(self.path)
        self.assertIn("gpu_missing", [x["code"] for x in rb.lint_task(d, task(d, "NOGPU"))])

    def test_a_demand_only_task_counts_as_described(self):
        add(self.path, "CODE", lane="a", uses="gpu@demand")
        self.assertTrue(rb.is_described(rb.load(self.path), task(rb.load(self.path), "CODE")))

    def test_hints_name_the_right_fix(self):
        self.assertIn("--reads", rb.conflict_hint("paths:docs/x.md"))
        self.assertIn("gpu@demand", rb.conflict_hint("gpu"))
        self.assertEqual(rb.conflict_hint("lane"), "")


class OldBoards(unittest.TestCase):
    def test_a_board_without_holds_demand_or_reads_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = new_board(tmp)
            add(path, "OLD", lane="a", uses="gpu", quiet=True)
            d = rb.load(path)
            d.pop("holds", None)
            for it in d["items"]:
                for key in ("demand", "reads"):
                    it.pop(key, None)
            rb.save(path, d)
            loaded = rb.load(path)
            self.assertEqual(loaded["holds"], {})
            self.assertEqual(rb.compute(loaded)[0]["id"], "OLD")
            self.assertEqual(task(loaded, "OLD").get("demand", []), [])


if __name__ == "__main__":
    unittest.main()
