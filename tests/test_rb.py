import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RB_PATH = ROOT / "skills" / "barid" / "scripts" / "barid.py"
spec = importlib.util.spec_from_file_location("rb", RB_PATH)
rb = importlib.util.module_from_spec(spec)
sys.modules["rb"] = rb
spec.loader.exec_module(rb)

HUMAN = rb.HUMAN
AGENT = rb.Actor("agent", "worker-1")
PLANNER = rb.Actor("agent", "planner")


def board_with(tmp, **policy):
    path = Path(tmp) / ".barid" / "board.json"
    path.parent.mkdir(parents=True)
    d = rb.new_board("test", [("main", "Main"), ("second", "Second"), ("third", "Third")])
    d["policy"].update(policy)
    rb.save(path, d)
    return path


def add(path, actor, iid, **kw):
    kw.setdefault("text", "do the thing")
    kw["id"] = iid
    return rb.mutate(path, lambda d: rb.op_add(d, actor, kw))


class CoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = board_with(self._tmp.name, direct_agents=["planner"])

    def state(self, iid):
        d = rb.load(self.path)
        return {c["id"]: c for c in rb.compute(d)}[iid]

    def test_agent_add_becomes_proposal_until_approved(self):
        item = add(self.path, AGENT, "A", lane="main")
        self.assertEqual(item["status"], "proposed")
        rb.mutate(self.path, lambda d: rb.op_approve(d, HUMAN, "A"))
        self.assertEqual(self.state("A")["state"], "ready")

    def test_trusted_planner_adds_directly_and_empty_text_is_draft(self):
        self.assertEqual(add(self.path, PLANNER, "A", lane="main")["status"], "queued")
        self.assertEqual(add(self.path, PLANNER, "B", lane="main", text="")["status"], "draft")
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_sent(d, HUMAN, "B"))  # a draft without text cannot be sent

    def test_agent_cannot_approve_or_accept_or_set_status(self):
        add(self.path, AGENT, "A", lane="main")
        for fn in (lambda d: rb.op_approve(d, AGENT, "A"), lambda d: rb.op_set_status(d, AGENT, "A", "done"), lambda d: rb.op_accept(d, AGENT, "A")):
            with self.assertRaises(rb.RBError):
                rb.mutate(self.path, fn)

    def test_dependencies_gate_readiness_and_review_counts_as_done(self):
        add(self.path, PLANNER, "A", lane="main")
        add(self.path, PLANNER, "B", lane="second", needs="A")
        self.assertEqual(self.state("B")["state"], "waiting")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="complete"))
        self.assertEqual(self.state("A")["state"], "review")
        self.assertEqual(self.state("B")["state"], "ready")  # a completely finished report unlocks dependents

    def test_partial_failed_or_unstated_outcome_does_not_unlock_dependents(self):
        add(self.path, PLANNER, "A", lane="main")
        add(self.path, PLANNER, "B", lane="second", needs="A")
        for outcome in ("partial", "failed", ""):
            rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
            rb.mutate(self.path, lambda d, o=outcome: rb.op_finish(d, AGENT, "A", report="r.md", outcome=o))
            b = self.state("B")
            self.assertEqual(b["state"], "waiting", outcome)
            self.assertEqual(b["waiting_info"]["A"], {"status": "review", "outcome": outcome})
            rb.mutate(self.path, lambda d: rb.op_set_status(d, HUMAN, "A", "queued"))  # sent back
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="partial"))
        rb.mutate(self.path, lambda d: rb.op_accept(d, HUMAN, "A"))  # the person accepts it as it is
        self.assertEqual(self.state("B")["state"], "ready")

    def test_outcome_can_be_corrected_by_the_person_but_not_by_a_worker(self):
        add(self.path, PLANNER, "A", lane="main")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="complete"))
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_outcome(d, AGENT, "A", "failed"))
        rb.mutate(self.path, lambda d: rb.op_outcome(d, HUMAN, "A", "partial"))
        self.assertEqual(self.state("A")["outcome"], "partial")
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_outcome(d, HUMAN, "A", "great"))
        rb.mutate(self.path, lambda d: rb.op_set_status(d, HUMAN, "A", "queued"))
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        self.assertNotIn("outcome", rb.load(self.path)["items"][0])  # a new run starts without a verdict

    def test_dependents_can_wait_for_acceptance(self):
        rb.mutate(self.path, lambda d: d["policy"].update(dependents_wait_for_accept=True))
        add(self.path, PLANNER, "A", lane="main")
        add(self.path, PLANNER, "B", lane="second", needs="A")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="complete"))
        self.assertEqual(self.state("B")["state"], "waiting")
        rb.mutate(self.path, lambda d: rb.op_accept(d, HUMAN, "A"))
        self.assertEqual(self.state("B")["state"], "ready")

    def test_exclusive_resource_and_quiet_conflicts(self):
        add(self.path, PLANNER, "G1", lane="main", uses="gpu")
        add(self.path, PLANNER, "G2", lane="second", uses="gpu")
        add(self.path, PLANNER, "Q", lane="third", quiet=True)
        add(self.path, PLANNER, "N", lane="second", noisy=True)
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "G1"))
        g2 = self.state("G2")
        self.assertEqual(g2["state"], "blocked")
        self.assertEqual(g2["conflicts"], [{"id": "G1", "why": "gpu"}])
        self.assertEqual(self.state("Q")["state"], "ready")  # shares nothing with G1
        self.assertIn("Q", self.state("G1")["can_run_with"])
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "Q"))
        n = self.state("N")
        self.assertEqual(n["state"], "blocked")
        self.assertEqual(n["conflicts"], [{"id": "Q", "why": "quiet"}])

    def test_same_lane_tasks_never_run_together(self):
        add(self.path, PLANNER, "A", lane="main")
        add(self.path, PLANNER, "B", lane="main")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        self.assertEqual(self.state("B")["conflicts"], [{"id": "A", "why": "lane"}])

    def test_claim_refused_on_conflict_but_allowed_for_sent_task(self):
        add(self.path, PLANNER, "G1", lane="main", uses="gpu")
        add(self.path, PLANNER, "G2", lane="second", uses="gpu")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "G1"))
        with self.assertRaises(rb.RBError) as cm:
            rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "G2"))
        self.assertIn("conflicts", str(cm.exception))
        rb.mutate(self.path, lambda d: rb.op_sent(d, HUMAN, "G2"))  # the person decided
        warnings = rb.mutate(self.path, lambda d: rb.op_claim(d, rb.Actor("agent", "other"), "G2"))
        self.assertTrue(warnings)

    def test_only_the_owner_can_finish_or_release(self):
        add(self.path, PLANNER, "A", lane="main")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        thief = rb.Actor("agent", "thief")
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_finish(d, thief, "A", report="x"))
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_release(d, thief, "A"))
        rb.mutate(self.path, lambda d: rb.op_release(d, HUMAN, "A"))
        self.assertEqual(self.state("A")["state"], "ready")

    def test_finish_needs_report_and_cycle_is_rejected(self):
        add(self.path, PLANNER, "A", lane="main")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A"))
        add(self.path, PLANNER, "B", lane="second", needs="A")
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_edit(d, PLANNER, "A", {"needs": "B"}))

    def test_stale_lease_is_flagged(self):
        add(self.path, PLANNER, "A", lane="main")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))

        def age(d):
            d["items"][0]["claim"]["lease_until"] = "2000-01-01T00:00:00+00:00"
        rb.mutate(self.path, age)
        self.assertTrue(self.state("A")["stale"])

    def test_cancel_rules(self):
        add(self.path, AGENT, "P", lane="main")
        rb.mutate(self.path, lambda d: rb.op_cancel(d, AGENT, "P", "mine"))  # own proposal
        add(self.path, PLANNER, "A", lane="main")
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_cancel(d, AGENT, "A"))
        rb.mutate(self.path, lambda d: rb.op_restore(d, HUMAN, "P"))
        self.assertEqual(self.state("P")["status"], "queued")

    def test_edit_replace_is_exact_and_draft_becomes_queued(self):
        add(self.path, PLANNER, "A", lane="main", text="")
        rb.mutate(self.path, lambda d: rb.op_edit(d, PLANNER, "A", {"text": "hello world"}))
        self.assertEqual(self.state("A")["status"], "queued")
        rb.mutate(self.path, lambda d: rb.op_edit(d, PLANNER, "A", {"replace": ["world", "there"]}))
        self.assertEqual(rb.load(self.path)["items"][0]["text"], "hello there")
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_edit(d, PLANNER, "A", {"replace": ["zzz", "q"]}))

    def test_unknown_references_are_rejected(self):
        with self.assertRaises(rb.RBError):
            add(self.path, PLANNER, "A", lane="nope")
        with self.assertRaises(rb.RBError):
            add(self.path, PLANNER, "A", lane="main", needs="ghost")
        with self.assertRaises(rb.RBError):
            add(self.path, PLANNER, "A", lane="main", uses="tpu")
        with self.assertRaises(rb.RBError):
            add(self.path, PLANNER, "bad id!", lane="main")

    def test_lane_colour_and_agent_are_validated_and_trusted_only(self):
        rb.mutate(self.path, lambda d: rb.op_lane_edit(d, HUMAN, "main", title="Build", color="#FEB83B", agent="codex"))
        lane = rb.load(self.path)["lanes"][0]
        self.assertEqual((lane["title"], lane["color"], lane["agent"]), ("Build", "#feb83b", "codex"))
        for bad in ({"color": "red"}, {"color": "#12345"}, {"agent": "x" * 40}, {"agent": "<script>"}):
            with self.assertRaises(rb.RBError):
                rb.mutate(self.path, lambda d, b=bad: rb.op_lane_edit(d, HUMAN, "main", **b))
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_lane_edit(d, AGENT, "main", color="#000000"))
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_lane_edit(d, HUMAN, "nope", color="#000000"))
        rb.mutate(self.path, lambda d: rb.op_lane_edit(d, HUMAN, "main", color="", agent=""))  # reset
        self.assertEqual(rb.load(self.path)["lanes"][0]["color"], "")

    def test_failed_operation_does_not_change_the_file(self):
        before = self.path.read_text("utf-8")
        with self.assertRaises(rb.RBError):
            add(self.path, PLANNER, "A", lane="nope")
        self.assertEqual(self.path.read_text("utf-8"), before)

    def test_footer_and_generation_request(self):
        add(self.path, PLANNER, "A", lane="main", text="Run the benchmark.", outline="goal")
        add(self.path, PLANNER, "B", lane="second", text="", needs="A", outline="analyse results", title="Analyse")
        d = rb.load(self.path)
        text = rb.prompt_for(d, d["items"][0], self.path)
        self.assertIn("Run the benchmark.", text)
        self.assertIn('claim A --by', text)
        self.assertIn(str(self.path), text)
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="/tmp/report-a.md"))
        d = rb.load(self.path)
        req = rb.gen_request(d, d["items"][1], self.path)
        self.assertIn("/tmp/report-a.md", req)  # the dependency's report is handed to the writer
        self.assertIn("analyse results", req)
        self.assertIn("edit B --text-file", req)
        self.assertEqual(rb.prompt_for(d, {"id": "X", "text": ""}, self.path), "")

    def test_russian_footer(self):
        rb.mutate(self.path, lambda d: d.update(lang="ru"))
        add(self.path, PLANNER, "A", lane="main")
        d = rb.load(self.path)
        self.assertIn("Перед началом работы", rb.prompt_for(d, d["items"][0], self.path))


def _git_available():
    import shutil
    return shutil.which("git") is not None


def _run_git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", check=True).stdout


class ScopeTests(unittest.TestCase):
    """File scopes, protected paths and the git check at the end of a task."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = board_with(self._tmp.name, direct_agents=["planner"])  # board in <root>/.relayboard/board.json
        self.d = rb.load(self.path)

    def test_scope_overlap_rules(self):
        n = lambda x: rb.norm_scope(self.d, x)
        yes = [("src", "src/a.py"), ("src/a.py", "src/a.py"), ("src/*.py", "src/a.py"), ("src", "src/*.py"), ("src/**", "src/x/y.txt"), ("**", "a/b"), ("src/*.py", "src/**")]
        no = [("src", "docs"), ("src/a.py", "src/b.py"), ("src/*.py", "src/b.txt"), ("src/sub", "src/subtle"), ("src/a", "src/a2/x")]
        for a, b in yes:
            self.assertTrue(rb.scopes_overlap(n(a), n(b)), (a, b))
        for a, b in no:
            self.assertFalse(rb.scopes_overlap(n(a), n(b)), (a, b))

    def test_tasks_with_overlapping_scopes_never_run_together(self):
        add(self.path, PLANNER, "A", lane="main", touches="src/gateway")
        add(self.path, PLANNER, "B", lane="second", touches="src/gateway/monitor.py")
        add(self.path, PLANNER, "C", lane="third", touches="docs")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        d = rb.load(self.path)
        comp = {c["id"]: c for c in rb.compute(d)}
        self.assertEqual(comp["B"]["state"], "blocked")
        self.assertTrue(comp["B"]["conflicts"][0]["why"].startswith("paths:"))
        self.assertEqual(comp["C"]["state"], "ready")  # a different folder runs in parallel
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "B"))

    def test_changing_a_protected_file_is_flagged_and_blocks_dependents(self):
        live = self.root / "live.json"
        live.write_text('{"a": 1}')
        rb.mutate(self.path, lambda d: rb.op_protect(d, HUMAN, "add", "live.json"))
        add(self.path, PLANNER, "A", lane="main")
        add(self.path, PLANNER, "B", lane="second", needs="A")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        live.write_text('{"a": 2}')  # the session breaks the rule
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="complete"))
        it = rb.load(self.path)["items"][0]
        self.assertEqual([v["type"] for v in it["violations"]], ["protected"])
        self.assertEqual(it["violations"][0]["path"], "live.json")
        b = {c["id"]: c for c in rb.compute(rb.load(self.path))}["B"]
        self.assertEqual(b["state"], "waiting")  # "complete" with a broken rule does not unlock anything
        rb.mutate(self.path, lambda d: rb.op_accept(d, HUMAN, "A"))
        self.assertEqual({c["id"]: c for c in rb.compute(rb.load(self.path))}["B"]["state"], "ready")

    def test_untouched_protected_files_pass(self):
        (self.root / "live.json").write_text("x")
        rb.mutate(self.path, lambda d: rb.op_protect(d, HUMAN, "add", "live.json"))
        add(self.path, PLANNER, "A", lane="main")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="complete"))
        self.assertEqual(rb.load(self.path)["items"][0]["violations"], [])

    def test_only_trusted_may_change_the_protected_list(self):
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_protect(d, AGENT, "add", "x"))

    @unittest.skipUnless(_git_available(), "git is not available")
    def test_git_changes_outside_the_declared_scope_are_flagged(self):
        repo = self.root
        _run_git(repo, "init", "-q")
        (repo / "src").mkdir()
        (repo / "docs").mkdir()
        (repo / "src" / "a.py").write_text("1")
        (repo / "docs" / "d.md").write_text("1")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-q", "-m", "init")
        add(self.path, PLANNER, "A", lane="main", touches="src")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        (repo / "src" / "a.py").write_text("2")        # inside the scope
        (repo / "docs" / "d.md").write_text("2")       # outside
        (repo / "src" / "new.py").write_text("n")      # new file inside
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="complete"))
        it = rb.load(self.path)["items"][0]
        self.assertEqual(it["changed"]["count"], 3)
        outside = [v for v in it["violations"] if v["type"] == "outside_scope"]
        self.assertEqual([v["path"] for v in outside], ["docs/d.md"])

    @unittest.skipUnless(_git_available(), "git is not available")
    def test_collision_with_another_running_task_is_reported(self):
        repo = self.root
        _run_git(repo, "init", "-q")
        (repo / "x.txt").write_text("1")
        (repo / "y.txt").write_text("1")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-q", "-m", "init")
        add(self.path, PLANNER, "A", lane="main", touches="x.txt")
        add(self.path, PLANNER, "B", lane="second", touches="y.txt")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        rb.mutate(self.path, lambda d: rb.op_claim(d, rb.Actor("agent", "b"), "B"))
        (repo / "y.txt").write_text("2")  # A edits the file that B owns
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="complete"))
        kinds = {(v["type"], v.get("with")) for v in rb.load(self.path)["items"][0]["violations"]}
        self.assertIn(("collision", "B"), kinds)
        self.assertIn(("outside_scope", None), kinds)

    @unittest.skipUnless(_git_available(), "git is not available")
    def test_worktree_command_and_footer(self):
        repo = self.root
        _run_git(repo, "init", "-q")
        (repo / "f.txt").write_text("1")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-q", "-m", "init")
        add(self.path, PLANNER, "A", lane="main", touches="f.txt")
        wt = self.root.parent / (self.root.name + "-barid-A")
        self.addCleanup(lambda: __import__("shutil").rmtree(wt, ignore_errors=True))
        p = subprocess.run([sys.executable, str(RB_PATH), "--board", str(self.path), "worktree", "A", "--human"], capture_output=True, encoding="utf-8", errors="replace")
        self.assertEqual(p.returncode, 0, p.stderr)
        d = rb.load(self.path)
        it = d["items"][0]
        self.assertTrue(Path(it["workdir"]).is_dir())
        self.assertEqual(it["branch"], "barid/A")
        footer = rb.prompt_for(d, it, self.path)
        self.assertIn("Work only in", footer)
        self.assertIn("f.txt", footer)
        # changes made in the worktree are attributed to the task, changes elsewhere are not
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        (Path(it["workdir"]) / "f.txt").write_text("2")
        (repo / "other.txt").write_text("not mine")
        rb.mutate(self.path, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="complete"))
        done = rb.load(self.path)["items"][0]
        self.assertEqual(done["changed"]["count"], 1)
        self.assertEqual(done["violations"], [])

    @unittest.skipUnless(_git_available() and hasattr(os, "symlink"), "git or symlinks are not available")
    def test_a_symlinked_project_folder_gives_the_same_answers(self):
        """macOS temp folders are symlinks (/var -> /private/var) while git reports the real path."""
        real = Path(self._tmp.name) / "real"
        real.mkdir()
        link = Path(self._tmp.name) / "link"
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        board = link / ".relayboard" / "board.json"
        board.parent.mkdir(parents=True)
        d = rb.new_board("t", [("main", "Main")])
        d["policy"]["direct_agents"] = ["planner"]
        rb.save(board, d)
        _run_git(real, "init", "-q")
        (real / "a.txt").write_text("1")
        _run_git(real, "add", "-A")
        _run_git(real, "commit", "-q", "-m", "init")
        add(board, PLANNER, "A", lane="main", touches="a.txt")
        rb.mutate(board, lambda d: rb.op_claim(d, AGENT, "A"))
        (real / "a.txt").write_text("2")
        rb.mutate(board, lambda d: rb.op_finish(d, AGENT, "A", report="r.md", outcome="complete"))
        it = rb.load(board)["items"][0]
        self.assertEqual(it["changed"]["count"], 1)
        self.assertEqual(it["violations"], [])

    def test_check_command_and_documented_forms(self):
        add(self.path, PLANNER, "A", lane="main", touches="src")
        add(self.path, PLANNER, "B", lane="second", touches="src/x.py")
        p = subprocess.run([sys.executable, str(RB_PATH), "--board", str(self.path), "check", "B"], capture_output=True, encoding="utf-8", errors="replace")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("overlaps with A", p.stdout)
        for args in (["protect", "add", "secrets.env", "--human"], ["protect", "list"]):
            q = subprocess.run([sys.executable, str(RB_PATH), "--board", str(self.path), *args], capture_output=True, encoding="utf-8", errors="replace")
            self.assertEqual(q.returncode, 0, q.stderr)
        self.assertIn("secrets.env", q.stdout)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = board_with(self._tmp.name, direct_agents=["planner"])

    def steps(self):
        d = rb.load(self.path)
        return rb.plan_steps(d, rb.compute(d))

    def test_parallel_and_sequential_steps(self):
        add(self.path, PLANNER, "M1", lane="main", uses="gpu")
        add(self.path, PLANNER, "O1", lane="second")
        add(self.path, PLANNER, "O2", lane="second", needs="O1")
        add(self.path, PLANNER, "M2", lane="main", uses="gpu", needs="M1", quiet=True)
        add(self.path, PLANNER, "M3", lane="main", uses="gpu", needs="M2")
        s = self.steps()
        self.assertEqual(set(s[0]), {"M1", "O1"})
        flat = [i for step in s for i in step]
        self.assertLess(flat.index("M1"), flat.index("M2"))
        self.assertLess(flat.index("M2"), flat.index("M3"))

    def test_moving_a_task_up_gives_it_the_earlier_step(self):
        add(self.path, PLANNER, "N", lane="second", noisy=True)
        add(self.path, PLANNER, "Q", lane="main", quiet=True)
        self.assertEqual(self.steps(), [["N"], ["Q"]])  # list order decides who goes first
        rb.mutate(self.path, lambda d: rb.op_move(d, PLANNER, "Q", 1))
        self.assertEqual(self.steps(), [["Q"], ["N"]])
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_move(d, AGENT, "Q", 2))
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_move(d, PLANNER, "Q", 9))

    def test_noisy_tasks_are_pushed_away_from_quiet_ones(self):
        add(self.path, PLANNER, "Q", lane="main", quiet=True)
        add(self.path, PLANNER, "N", lane="second", noisy=True)
        add(self.path, PLANNER, "F", lane="third")
        s = self.steps()
        self.assertNotIn({"Q", "N"}, [set(x) for x in s if len(x) == 2 and {"Q", "N"} <= set(x)])
        self.assertTrue(any({"Q", "F"} <= set(x) for x in s) or any({"N", "F"} <= set(x) for x in s))

    def test_done_and_cancelled_are_not_planned(self):
        add(self.path, PLANNER, "A", lane="main")
        add(self.path, PLANNER, "B", lane="main")
        rb.mutate(self.path, lambda d: rb.op_set_status(d, HUMAN, "A", "done"))
        rb.mutate(self.path, lambda d: rb.op_cancel(d, HUMAN, "B"))
        self.assertEqual(self.steps(), [])

    def test_dependency_cycle_does_not_hang_the_planner(self):
        add(self.path, PLANNER, "A", lane="main")
        add(self.path, PLANNER, "B", lane="second", needs="A")

        def force_cycle(d):
            d["items"][0]["needs"] = ["B"]  # bypass validation on purpose
        rb.mutate(self.path, force_cycle)
        flat = [i for step in self.steps() for i in step]
        self.assertEqual(sorted(flat), ["A", "B"])


class ConcurrencyTests(unittest.TestCase):
    def test_two_agents_cannot_both_claim_the_same_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = board_with(tmp, direct_agents=["planner"])
            for i in range(6):
                add(path, PLANNER, f"T{i}", lane=["main", "second", "third"][i % 3], uses="gpu")
            results = []

            def worker(iid, name):
                try:
                    rb.mutate(path, lambda d: rb.op_claim(d, rb.Actor("agent", name), iid))
                    results.append(("ok", iid))
                except rb.RBError:
                    results.append(("refused", iid))

            threads = [threading.Thread(target=worker, args=(f"T{i}", f"w{i}")) for i in range(6)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(sum(1 for r in results if r[0] == "ok"), 1)
            d = rb.load(path)
            self.assertEqual(sum(1 for i in d["items"] if i["status"] == "running"), 1)

    def test_parallel_adds_from_processes_do_not_lose_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = board_with(tmp, direct_agents=["*"])
            procs = [subprocess.Popen([sys.executable, str(RB_PATH), "--board", str(path), "add", f"P{i}", "--lane", "main", "--text", "x", "--by", f"a{i}"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(8)]
            for p in procs:
                p.wait()
            d = rb.load(path)
            self.assertEqual(sorted(i["id"] for i in d["items"]), [f"P{i}" for i in range(8)])
            self.assertEqual(d["rev"], 9)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = self._tmp.name

    def run_rb(self, *args, check=True, env=None):
        e = dict(os.environ)
        e.pop("BARID_BOARD", None)
        e.pop("BARID_ACTOR", None)
        e.update(env or {})
        p = subprocess.run([sys.executable, str(RB_PATH), *args], cwd=self.cwd, capture_output=True, encoding="utf-8", errors="replace", env=e)
        if check and p.returncode != 0:
            self.fail(f"rb {' '.join(args)} failed: {p.stderr}")
        return p

    def test_end_to_end_workflow(self):
        self.run_rb("init", "--project", "demo", "--lanes", "main:Main session,helper:Helper")
        self.run_rb("trust", "add", "planner", "--human")
        self.run_rb("add", "T1", "--lane", "main", "--title", "First", "--text", "step one", "--uses", "gpu", "--by", "planner")
        self.run_rb("add", "T2", "--lane", "helper", "--title", "Second", "--needs", "T1", "--outline", "later", "--by", "planner")
        listing = self.run_rb("list").stdout
        self.assertIn("ready", listing)
        self.assertIn("draft", listing)
        nxt = self.run_rb("next", "--lane", "main").stdout
        self.assertIn("step one", nxt)
        self.assertIn("claim T1", nxt)
        none = self.run_rb("next", "--lane", "helper", check=False)
        self.assertEqual(none.returncode, 2)
        self.run_rb("claim", "T1", "--by", "w")
        self.run_rb("finish", "T1", "--report", "r.md", "--by", "w")
        status = json.loads(self.run_rb("show", "T1", "--json").stdout)
        self.assertEqual(status["status"], "review")
        refused = self.run_rb("accept", "T1", check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("only the person", refused.stderr)
        self.run_rb("accept", "T1", "--human")
        plan = self.run_rb("plan").stdout
        self.assertIn("Step 1", plan)
        self.assertIn("T2", plan)
        self.assertTrue(self.run_rb("gen", "T2").stdout.strip())
        doctor = self.run_rb("doctor").stdout
        self.assertNotIn("FAIL", doctor)

    def test_documented_command_forms_work(self):
        """Every command shown in the README and the skill must parse (found by a clean-room run)."""
        self.run_rb("init", "--lanes", "a,b")
        self.run_rb("resource", "add", "staging", "--label", "Staging server", "--human")
        self.run_rb("resource", "tpu", "--shared", "--human")  # the short form too
        self.assertIn("staging", self.run_rb("resource", "list").stdout)
        self.run_rb("lane", "add", "c", "--title", "Third", "--human")
        self.assertIn("Third", self.run_rb("lane", "list").stdout)
        self.run_rb("context", "add", "README.md", "--human")
        self.run_rb("trust", "add", "planner", "--human")
        self.run_rb("add", "T1", "--lane", "a", "--text", "x", "--uses", "staging", "--by", "planner")
        bad = self.run_rb("lane", "add", "x", "y", "--human", check=False)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("usage", bad.stderr)

    def test_lane_edit_command(self):
        self.run_rb("init", "--lanes", "a,b")
        self.run_rb("lane", "edit", "a", "--title", "Builder", "--color", "#aabbcc", "--agent", "claude", "--human")
        out = self.run_rb("lane", "list").stdout
        self.assertIn("Builder", out)
        self.assertIn("#aabbcc", out)
        self.assertIn("claude", out)
        bad = self.run_rb("lane", "edit", "a", "--color", "blue", "--human", check=False)
        self.assertEqual(bad.returncode, 1)

    def test_non_ascii_titles_do_not_crash_a_narrow_console(self):
        self.run_rb("init", "--lang", "ru")
        self.run_rb("add", "T1", "--title", "Проверка кириллицы", "--text", "текст", "--human")
        p = self.run_rb("list", env={"PYTHONIOENCODING": "ascii"})
        self.assertIn("T1", p.stdout)

    def test_board_is_found_from_a_subfolder(self):
        self.run_rb("init")
        sub = Path(self.cwd) / "deep" / "er"
        sub.mkdir(parents=True)
        p = subprocess.run([sys.executable, str(RB_PATH), "where"], cwd=sub, capture_output=True, encoding="utf-8", errors="replace")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.strip().endswith("board.json"))

    def test_error_without_board_is_friendly(self):
        p = self.run_rb("list", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("barid init", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


class PanelI18nTests(unittest.TestCase):
    """The panel's texts: both languages complete, every used key defined, placeholders match."""

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.html = (RB_PATH.parent / "panel.html").read_text("utf-8")
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not available")
        block = cls.html[cls.html.index("var STR = {"): cls.html.index("var S = {board:null")]
        out = subprocess.run([node, "-e", block + "\nconsole.log(JSON.stringify(STR))"], capture_output=True, encoding="utf-8", check=True).stdout
        cls.STR = json.loads(out)

    def flat(self, d, prefix=""):
        out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                out.update(self.flat(v, prefix + k + "."))
            else:
                out[prefix + k] = v
        return out

    def test_both_languages_have_the_same_keys_and_placeholders(self):
        import re
        en, ru = self.flat(self.STR["en"]), self.flat(self.STR["ru"])
        self.assertEqual(sorted(en), sorted(ru))
        for k in en:
            self.assertTrue(en[k].strip() and ru[k].strip(), k)
            self.assertEqual(sorted(re.findall(r"\{(\w+)\}", en[k])), sorted(re.findall(r"\{(\w+)\}", ru[k])), f"placeholders differ in {k}")

    def test_every_key_used_by_the_panel_exists(self):
        import re
        keys = set(self.STR["en"]) | {"s"}
        used = set(re.findall(r'\bt\("(\w+)"\)', self.html))
        self.assertFalse(used - keys, f"undefined keys: {used - keys}")
        for status in rb.STATUSES:
            self.assertIn(status, self.STR["ru"]["s"])
        for key in re.findall(r'"(ok_\w+)"', self.html):
            self.assertIn(key, self.STR["en"], key)

    def test_every_palette_exists_in_both_modes_and_has_a_name(self):
        import re
        for pal in ("teal", "graphite", "midnight"):
            for mode in ("dark", "light"):
                self.assertIn(f'html[data-pal="{pal}"][data-theme="{mode}"]', self.html)
        listed = re.search(r"var PALS = \[(.*?)\]", self.html).group(1)
        for pal in re.findall(r'"(\w+)"', listed):
            for lang in ("en", "ru"):
                self.assertIn("pal_" + pal, self.STR[lang])

    def test_every_error_code_raised_by_rb_has_a_translation(self):
        import re
        codes = set(re.findall(r'RBError\([^\n]*?, "(\w+)"', RB_PATH.read_text("utf-8")))
        self.assertTrue(codes)
        for lang in ("en", "ru"):
            self.assertFalse(codes - set(self.STR[lang]["ERR"]), f"{lang}: {codes - set(self.STR[lang]['ERR'])}")

    def test_feedback_after_an_action_is_past_tense_not_the_button_label(self):
        for lang in ("en", "ru"):
            d = self.STR[lang]
            self.assertNotEqual(d["ok_accept"], d["accept"])
            self.assertNotEqual(d["ok_approve"], d["approve"])
            self.assertNotEqual(d["ok_sent"], d["sent"])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = board_with(cls._tmp.name, direct_agents=["planner"])
        add(cls.path, PLANNER, "A", lane="main", text="hello", title="Alpha")
        add(cls.path, AGENT, "P", lane="second", text="proposal")
        add(cls.path, AGENT, "Q", lane="third", text="another proposal")
        cls.proc = subprocess.Popen([sys.executable, str(RB_PATH), "--board", str(cls.path), "serve"], stdout=subprocess.PIPE, encoding="utf-8", errors="replace")
        line = cls.proc.stdout.readline()
        cls.port = int(line.split("localhost:")[1].split("/")[0])
        cls.base = f"http://localhost:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.proc.stdout.close()
        cls._tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def post(self, body, headers=None):
        h = {"Content-Type": "application/json", "X-Barid-Edit": "1"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + "/api/act", data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_panel_and_board_api(self):
        code, html = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("Barid", html)
        code, body = self.get("/api/board")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual({i["id"] for i in data["items"]}, {"A", "P", "Q"})
        self.assertIn("P", data["proposed"])
        self.assertEqual(data["next_by_lane"]["main"], "A")
        code, body = self.get("/api/prompt/A")
        self.assertIn("claim A", json.loads(body)["text"])
        code, body = self.get("/api/gen/A")
        self.assertEqual(code, 200)

    def test_actions_work_and_are_logged(self):
        code, r = self.post({"action": "approve", "id": "Q"})
        self.assertEqual((code, r["result"]), (200, "queued"))
        code, r = self.post({"action": "note", "id": "A", "args": {"text": "hi"}})
        self.assertEqual(code, 200)
        code, r = self.post({"action": "nonsense", "id": "A"})
        self.assertEqual(code, 400)

    def test_errors_carry_a_code_for_translation(self):
        code, r = self.post({"action": "add", "id": "A", "args": {"text": "x"}})
        self.assertEqual((code, r["code"], r["info"]), (400, "exists", {"id": "A"}))
        code, r = self.post({"action": "accept", "id": "A"})
        self.assertEqual((code, r["code"]), (400, "wrong_state"))
        self.assertEqual(r["info"]["id"], "A")
        code, r = self.post({"action": "add", "id": "Z9", "args": {"lane": "nope", "text": "x"}})
        self.assertEqual(r["code"], "unknown_lane")

    def test_security_checks(self):
        self.assertEqual(self.get("/api/board", {"Host": "evil.example.com"})[0], 403)
        code, _ = self.post({"action": "note", "id": "A", "args": {"text": "x"}}, {"X-Barid-Edit": "0"})
        self.assertEqual(code, 403)
        code, _ = self.post({"action": "note", "id": "A", "args": {"text": "x"}}, {"Origin": "http://evil.example.com"})
        self.assertEqual(code, 403)
        code, _ = self.post({"action": "note", "id": "A", "args": {"text": "x"}}, {"Host": "evil.example.com"})
        self.assertEqual(code, 403)
        self.assertEqual(self.get("/..%2f..%2fetc%2fpasswd")[0], 404)
        self.assertEqual(self.get("/api/prompt/..")[0], 404)

    def test_subdomain_of_localhost_is_accepted(self):
        code, _ = self.get("/api/rev", {"Host": f"my-board.localhost:{self.port}"})
        self.assertEqual(code, 200)

    def test_refuses_to_bind_a_public_address(self):
        p = subprocess.run([sys.executable, str(RB_PATH), "--board", str(self.path), "serve", "--host", "0.0.0.0"], capture_output=True, encoding="utf-8", errors="replace")
        self.assertEqual(p.returncode, 1)
        self.assertIn("loopback", p.stderr)


if __name__ == "__main__":
    unittest.main()
