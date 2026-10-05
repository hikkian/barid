"""The 'can these run together' engine: rules, profiles, how well a task is described, lint, the run journal, explain."""
import importlib.util
import itertools
import json
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RB_PATH = ROOT / "skills" / "barid" / "scripts" / "barid.py"
spec = importlib.util.spec_from_file_location("rb_engine", RB_PATH)
rb = importlib.util.module_from_spec(spec)
sys.modules["rb_engine"] = rb
spec.loader.exec_module(rb)
HUMAN = rb.HUMAN
AGENT = rb.Actor("agent", "worker-1")


def new_board(tmp, lanes=(("a", "A"), ("b", "B"), ("c", "C"))):
    path = Path(tmp) / ".barid" / "board.json"
    path.parent.mkdir(parents=True)
    rb.save(path, rb.new_board("t", list(lanes)))
    return path


def add(path, iid, **kw):
    kw.setdefault("text", "do it")
    kw["id"] = iid
    return rb.mutate(path, lambda d: rb.op_add(d, HUMAN, kw))


def task(d, iid):
    return next(i for i in d["items"] if i["id"] == iid)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = new_board(self._tmp.name)

    def d(self):
        return rb.load(self.path)

    def test_each_rule_alone(self):
        add(self.path, "L1", lane="a")
        add(self.path, "L2", lane="a")
        add(self.path, "G1", lane="a", uses="gpu")
        add(self.path, "G2", lane="b", uses="gpu")
        add(self.path, "Q", lane="b", quiet=True)
        add(self.path, "N", lane="c", noisy=True)
        add(self.path, "P1", lane="b", touches="src/api")
        add(self.path, "P2", lane="c", touches="src/api/x.py")
        add(self.path, "P3", lane="c", touches="docs")
        d = self.d()
        why = lambda x, y: [c["why"] for c in rb.conflicts_between(d, task(d, x), task(d, y))]
        self.assertEqual(why("L1", "L2"), ["lane"])
        self.assertEqual(why("G1", "G2"), ["gpu"])
        self.assertEqual(why("Q", "N"), ["quiet"])
        self.assertTrue(why("P1", "P2")[0].startswith("paths:"))
        self.assertEqual(why("P1", "P3"), [])
        self.assertEqual(why("G1", "N"), [])

    def test_symmetric_and_irreflexive_on_random_boards(self):
        rng = random.Random(7)
        for round_ in range(40):
            with tempfile.TemporaryDirectory() as tmp:
                path = new_board(tmp)
                for n in range(rng.randint(2, 7)):
                    kw = {"lane": rng.choice("abc")}
                    if rng.random() < .5:
                        kw["uses"] = rng.choice(["gpu", "user", "gpu,user"])
                    if rng.random() < .4:
                        kw["quiet"] = True
                    if rng.random() < .4:
                        kw["noisy"] = True
                    if rng.random() < .3:
                        kw["profile"] = rng.choice(["light", "dev", "bench", "attended"])
                    if rng.random() < .3:
                        kw["touches"] = rng.choice(["src", "src/a", "docs", "*.md"])
                    add(path, f"T{n}", **kw)
                d = rb.load(path)
                for a, b in itertools.product(d["items"], repeat=2):
                    ab = [c["why"] for c in rb.conflicts_between(d, a, b)]
                    ba = [c["why"] for c in rb.conflicts_between(d, b, a)]
                    if a["id"] == b["id"]:
                        self.assertEqual(ab, [])
                    else:
                        self.assertEqual(bool(ab), bool(ba), (a, b))
                        self.assertEqual(sorted(x.split(":")[0] for x in ab), sorted(x.split(":")[0] for x in ba))

    def test_profiles_add_their_marks_and_unknown_ones_are_refused(self):
        add(self.path, "B1", lane="a", profile="bench")
        add(self.path, "D1", lane="b", profile="dev")
        add(self.path, "A1", lane="c", profile="attended")
        d = self.d()
        self.assertEqual(rb.effective(d, task(d, "B1")), {"uses": [], "quiet": True, "noisy": True})
        self.assertEqual(rb.effective(d, task(d, "A1"))["uses"], ["user"])
        self.assertEqual([c["why"] for c in rb.conflicts_between(d, task(d, "B1"), task(d, "D1"))], ["quiet"])
        with self.assertRaises(rb.RBError) as cm:
            add(self.path, "X", lane="a", profile="nope")
        self.assertEqual(cm.exception.code, "unknown_profile")

    def test_a_custom_profile_can_be_added_and_removed_only_when_unused(self):
        rb.mutate(self.path, lambda d: rb.op_profile_add(d, HUMAN, "gpu-bench", "gpu", True, True, "GPU measurement"))
        add(self.path, "M", lane="a", profile="gpu-bench")
        d = self.d()
        self.assertEqual(rb.effective(d, task(d, "M")), {"uses": ["gpu"], "quiet": True, "noisy": True})
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_profile_rm(d, HUMAN, "gpu-bench"))
        rb.mutate(self.path, lambda d: rb.op_cancel(d, HUMAN, "M", "x"))
        rb.mutate(self.path, lambda d: rb.op_profile_rm(d, HUMAN, "gpu-bench"))
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_profile_add(d, HUMAN, "light", "", False, False))  # built-in name
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_profile_add(d, AGENT, "mine", "", False, False))  # not trusted

    def test_described_means_something_was_declared(self):
        add(self.path, "BARE", lane="a")
        add(self.path, "R", lane="b", uses="gpu")
        add(self.path, "P", lane="c", profile="light")
        d = self.d()
        self.assertEqual([rb.is_described(d, task(d, i)) for i in ("BARE", "R", "P")], [False, True, True])

    def test_lint_finds_what_the_text_gives_away_and_can_be_silenced(self):
        add(self.path, "T1", lane="a", text="Start llama-server --port 8091 and run a benchmark, then run pytest.", profile="light")
        d = self.d()
        codes = sorted(x["code"] for x in task(rb.compute(d) and {"items": rb.compute(d)}, "T1")["lint"])
        self.assertEqual(codes, ["gpu_missing", "load_missing", "port_missing", "quiet_missing"])
        rb.mutate(self.path, lambda d: rb.op_edit(d, HUMAN, "T1", {"lint_ok": "gpu_missing,quiet_missing"}))
        rb.mutate(self.path, lambda d: rb.op_edit(d, HUMAN, "T1", {"uses": "gpu"}))
        c = {x["id"]: x for x in rb.compute(self.d())}["T1"]
        self.assertEqual(sorted(x["code"] for x in c["lint"]), ["load_missing", "port_missing"])
        self.assertEqual([x["arg"] for x in c["lint"] if x["code"] == "port_missing"], ["8091"])
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_edit(d, HUMAN, "T1", {"lint_ok": "bogus"}))
        add(self.path, "T2", lane="b")
        c2 = {x["id"]: x for x in rb.compute(self.d())}["T2"]
        self.assertEqual([x["code"] for x in c2["lint"]], ["no_marks"])
        rb.mutate(self.path, lambda d: rb.op_add(d, HUMAN, {"id": "T3", "lane": "c", "text": "", "outline": "run the nvidia-smi check"}))
        c3 = {x["id"]: x for x in rb.compute(self.d())}["T3"]
        self.assertIn("gpu_missing", [x["code"] for x in c3["lint"]])

    def test_a_port_can_be_a_resource_that_two_tasks_cannot_share(self):
        rb.mutate(self.path, lambda d: d["resources"].update({"port:8091": {"label": "port 8091", "exclusive": True}}))
        add(self.path, "S1", lane="a", uses="port:8091")
        add(self.path, "S2", lane="b", uses="port:8091")
        d = self.d()
        self.assertEqual([c["why"] for c in rb.conflicts_between(d, task(d, "S1"), task(d, "S2"))], ["port:8091"])

    def test_the_run_journal_records_who_ran_next_to_a_measurement(self):
        add(self.path, "BENCH", lane="a", profile="bench")
        add(self.path, "DEV", lane="b", profile="dev")
        add(self.path, "BARE", lane="c")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "BENCH", force=True))
        time.sleep(0.02)
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "DEV", force=True))
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "BARE", force=True))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "BENCH", report="r.md", outcome="complete", force=True))
        d = self.d()
        b = task(d, "BENCH")
        self.assertEqual(sorted(x["id"] for x in b["ran_with"]), ["BARE", "DEV"])
        self.assertEqual(sorted(x["id"] for x in b["disturbed_by"]), ["BARE", "DEV"])
        self.assertEqual({x["id"]: x["why"] for x in b["disturbed_by"]}, {"DEV": "load", "BARE": "unknown"})

    def test_a_clean_measurement_is_not_marked_disturbed_and_old_runs_are_checked_by_time(self):
        add(self.path, "BENCH", lane="a", profile="bench")
        add(self.path, "LATER", lane="b", profile="dev")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "BENCH", force=True))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "BENCH", report="r.md", outcome="complete", force=True))
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "LATER", force=True))  # started after the measurement ended
        d = self.d()
        self.assertEqual(task(d, "BENCH")["ran_with"], [])
        self.assertEqual(task(d, "BENCH")["disturbed_by"], [])
        # a task that finished while another was running counts, even though it is no longer active
        add(self.path, "BENCH2", lane="a", profile="bench")
        add(self.path, "SHORT", lane="c", profile="dev")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "BENCH2", force=True))
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "SHORT", force=True))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "SHORT", report="r.md", force=True))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "BENCH2", report="r.md", force=True))
        self.assertIn("SHORT", [x["id"] for x in task(self.d(), "BENCH2")["ran_with"]])

    def test_the_card_shows_the_task_the_plan_puts_first_not_just_any_task_whose_turn_came(self):
        # the live board: a measurement runs elsewhere, the session's first task is blocked by it, a later draft is only waiting for its prompt
        add(self.path, "RUN", lane="b", profile="bench", uses="gpu")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "RUN", force=True))
        add(self.path, "FIRST", lane="a", profile="bench", uses="gpu")
        add(self.path, "LATER", lane="a", text="", outline="a draft", profile="dev")
        view = rb.board_view(self.path)
        self.assertEqual(view["focus"]["a"]["id"], "FIRST")
        self.assertEqual(view["focus"]["a"]["role"], "blocked")
        flat = [i for step in view["steps"] for i in step]
        self.assertLess(flat.index("FIRST"), flat.index("LATER"))

    def test_a_ready_task_that_the_plan_places_earlier_wins_over_a_blocked_one(self):
        add(self.path, "RUN", lane="b", profile="bench", uses="gpu")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "RUN", force=True))
        add(self.path, "BLOCKED", lane="a", profile="bench", uses="gpu")
        add(self.path, "OPEN", lane="c", profile="light")
        add(self.path, "OPEN2", lane="a", profile="light")  # same lane as BLOCKED, but nothing in its way except BLOCKED itself
        view = rb.board_view(self.path)
        self.assertEqual(view["focus"]["c"]["id"], "OPEN")
        flat = [i for step in view["steps"] for i in step]
        self.assertEqual(view["focus"]["a"]["id"], min(["BLOCKED", "OPEN2"], key=flat.index))

    def test_a_draft_waiting_for_other_tasks_is_a_waiting_focus(self):
        add(self.path, "DEP", lane="b", profile="light")
        rb.mutate(self.path, lambda d: rb.op_edit(d, HUMAN, "DEP", {}))
        add(self.path, "DR", lane="a", text="", outline="wait", needs="DEP", profile="light")
        view = rb.board_view(self.path)
        self.assertEqual(view["focus"]["a"]["role"], "waiting")
        self.assertEqual(view["focus"]["a"]["hint"]["kind"], "deps")

    def test_claiming_in_two_lanes_under_one_name_warns(self):
        add(self.path, "W1", lane="a")
        add(self.path, "W2", lane="b")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "W1"))
        warnings = rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "W2"))
        self.assertTrue(any("own name" in w for w in warnings), warnings)
        other = rb.Actor("agent", "worker-2")
        add(self.path, "W3", lane="c")
        self.assertFalse(any("own name" in w for w in (rb.mutate(self.path, lambda d: rb.op_claim(d, other, "W3")) or [])))

    def test_the_panel_api_refuses_fields_of_the_wrong_type_with_a_clean_error(self):
        add(self.path, "OK", lane="a")
        for bad in ({"title": True}, {"needs": False}, {"uses": [1, 2]}, {"quiet": "yes"}, {"profile": 5}):
            with self.assertRaises(rb.RBError) as cm:
                rb.apply_action(self.path, {"action": "edit", "id": "OK", "args": bad})
            self.assertEqual(cm.exception.code, "bad_input", bad)
        with self.assertRaises(rb.RBError):
            rb.apply_action(self.path, {"action": "add", "id": "X1", "args": ["not", "an", "object"]})
        with self.assertRaises(rb.RBError):
            rb.apply_action(self.path, ["not an object"])

    def test_a_task_the_person_sets_to_running_gets_a_claim(self):
        add(self.path, "HAND", lane="a")
        rb.mutate(self.path, lambda d: rb.op_set_status(d, HUMAN, "HAND", "running"))
        it = task(self.d(), "HAND")
        self.assertEqual(it["claim"]["by"], "human")
        self.assertTrue(it["claim"]["lease_until"])

    def test_explain_names_every_rule_and_the_verdict(self):
        add(self.path, "X", lane="a", uses="gpu", profile="bench")
        add(self.path, "Y", lane="b", uses="gpu", noisy=True)
        add(self.path, "Z", lane="c", profile="light")
        add(self.path, "W", lane="c")
        d = self.d()
        ex = rb.explain_pair(d, task(d, "X"), task(d, "Y"))
        self.assertEqual(ex["verdict"], "conflict")
        self.assertEqual({r["rule"]: r["hit"] for r in ex["rules"]}, {"lane": False, "resource": True, "quiet": True, "paths": False})
        self.assertEqual(rb.explain_pair(d, task(d, "Y"), task(d, "Z"))["verdict"], "ok")
        self.assertEqual(rb.explain_pair(d, task(d, "Z"), task(d, "W"))["verdict"], "conflict")  # same lane
        add(self.path, "V", lane="a")  # lane a differs from W's lane c, but both declare nothing
        d = self.d()
        self.assertEqual(rb.explain_pair(d, task(d, "V"), task(d, "W"))["verdict"], "unchecked")

    def test_cli_profile_lint_explain_and_add_warnings(self):
        run = lambda *a: subprocess.run([sys.executable, str(RB_PATH), "--board", str(self.path), "--human", *a], capture_output=True, text=True, encoding="utf-8")
        r = run("add", "C1", "--lane", "a", "--text", "benchmark llama-server on port 8091")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gpu", r.stderr)  # the warning right after add
        self.assertIn("quiet", r.stderr)
        self.assertIn("8091", r.stderr)
        self.assertEqual(run("lint", "--strict").returncode, 1)
        self.assertEqual(run("edit", "C1", "--profile", "bench", "--uses", "gpu", "--lint-ok", "port_missing").stderr.strip(), "")
        self.assertEqual(run("lint", "--strict").returncode, 0)
        self.assertIn("bench", run("profile", "list").stdout)
        self.assertEqual(run("profile", "add", "mine", "--uses", "gpu", "--quiet").returncode, 0)
        self.assertIn("mine", run("profile", "list").stdout)
        run("add", "C2", "--lane", "b", "--profile", "dev", "--text", "x")
        out = run("explain", "C1", "C2").stdout
        self.assertIn("quiet", out)
        self.assertIn("=>", out)
        self.assertEqual(json.loads(run("explain", "C1", "C2", "--json").stdout)["verdict"], "conflict")


class ServerStartTests(unittest.TestCase):
    def test_the_server_does_not_look_up_its_host_name(self):
        import http.server
        import socket
        calls = []
        real = socket.getfqdn
        socket.getfqdn = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("getfqdn must not be called"))
        try:
            srv = rb.LocalServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
            srv.server_close()
        finally:
            socket.getfqdn = real
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
