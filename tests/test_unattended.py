"""Unattended runs: reasons a task waits, `next` and `claim` exit codes and --wait, `after`, start times, presence, the worker, digest."""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from test_rb import HUMAN, RB_PATH, add, board_with, rb

AGENT = rb.Actor("agent", "night-1")


def cli(board, *args, input=None, timeout=60):
    """Run the real command line; returns (exit code, stdout, stderr)."""
    env = {**os.environ, "BARID_BOARD": str(board)}
    env.pop("BARID_AGENT", None)
    p = subprocess.run([sys.executable, str(RB_PATH), *args], capture_output=True, text=True, input=input, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = board_with(self._tmp.name)
        rb.mutate(self.path, lambda d: rb.op_resource_add(d, HUMAN, "gpu") if hasattr(rb, "op_resource_add") else d["resources"].update({"gpu": {"label": "GPU", "shared": False}}))

    def comp(self, iid):
        return {c["id"]: c for c in rb.compute(rb.load(self.path))}[iid]

    def add(self, iid, **kw):
        kw.setdefault("lane", "main")
        return add(self.path, HUMAN, iid, **kw)

    def run_(self, iid, lane_actor=AGENT):
        rb.mutate(self.path, lambda d: rb.op_claim(d, lane_actor, iid))

    def finish(self, iid, outcome="complete", actor=AGENT):
        rb.mutate(self.path, lambda d: rb.op_finish(d, actor, iid, "/tmp/report.md", "", False, outcome))


class Reasons(Base):
    def test_each_cause_gets_its_own_reason_code(self):
        self.add("A", uses=["gpu"])
        self.add("B", lane="second", uses=["gpu"], needs=["A"])
        self.add("C", lane="third", not_before="+2h")
        self.add("D", lane="third", text="", draft=True)
        self.add("E", lane="third", needs=["D"])
        self.run_("A")
        self.add("F", lane="second", uses=["gpu"])
        codes = lambda i: [r["code"] for r in self.comp(i)["reasons"]]
        self.assertEqual(codes("B"), ["dependency", "conflict"])    # waits for A, and A holds the GPU
        self.assertEqual(codes("C"), ["begin_time"])
        self.assertEqual(codes("E"), ["decision"])                 # waits for a draft that the person has not written
        self.assertEqual(codes("F"), ["conflict"])
        self.assertEqual(self.comp("F")["state"], "blocked")
        self.assertEqual(self.comp("C")["state"], "waiting")
        self.assertGreater(self.comp("C")["begin_in"], 7000)

    def test_needs_on_a_partial_report_waits_for_the_person_but_after_does_not(self):
        self.add("A")
        self.add("B", lane="second", needs=["A"])
        self.add("C", lane="third", after=["A"])
        self.run_("A")
        self.finish("A", "partial")
        self.assertEqual([r["code"] for r in self.comp("B")["reasons"]], ["decision"])
        self.assertEqual(self.comp("B")["reasons"][0]["outcome"], "partial")
        self.assertEqual(self.comp("C")["state"], "ready")          # `after` needs the end, not the success
        self.assertEqual(self.comp("C")["reasons"], [])

    def test_a_cancelled_dependency_can_never_be_satisfied(self):
        self.add("A")
        self.add("B", lane="second", needs=["A"])
        rb.mutate(self.path, lambda d: rb.op_cancel(d, HUMAN, "A", "not needed"))
        self.assertEqual([r["code"] for r in self.comp("B")["reasons"]], ["never"])
        self.assertEqual(self.comp("B")["reasons"][0]["status"], "cancelled")

    def test_after_counts_for_the_plan_order_and_for_cycles(self):
        self.add("A")
        self.add("B", lane="second", after=["A"])
        d = rb.load(self.path)
        steps = rb.plan_steps(d, rb.compute(d))
        self.assertLess([i for i, s in enumerate(steps) if "A" in s][0], [i for i, s in enumerate(steps) if "B" in s][0])
        with self.assertRaises(rb.RBError) as e:
            rb.mutate(self.path, lambda dd: rb.op_edit(dd, HUMAN, "A", {"after": "B"}))
        self.assertEqual(e.exception.code, "cycle")
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda dd: rb.op_edit(dd, HUMAN, "A", {"after": "A"}))

    def test_a_task_cannot_be_purged_while_another_runs_after_it(self):
        self.add("A")
        self.add("B", lane="second", after=["A"])
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_purge(d, HUMAN, "A"))


class Times(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)

    def test_relative_times(self):
        self.assertEqual(rb.parse_when("+90m", self.NOW), "2026-10-06T13:30:00+00:00")
        self.assertEqual(rb.parse_when("+3h", self.NOW), "2026-10-06T15:00:00+00:00")
        self.assertEqual(rb.parse_when("+1d", self.NOW), "2026-10-07T12:00:00+00:00")
        self.assertEqual(rb.parse_when("", self.NOW), "")

    def test_a_time_of_day_means_the_next_one_in_local_time(self):
        local_now = self.NOW.astimezone()
        later = (local_now + dt.timedelta(hours=2)).strftime("%H:%M")
        at = rb.parse_ts(rb.parse_when(later, self.NOW))
        self.assertGreater(at, self.NOW)
        self.assertLessEqual(at - self.NOW, dt.timedelta(hours=2, minutes=1))
        earlier = (local_now - dt.timedelta(hours=2)).strftime("%H:%M")
        tomorrow = rb.parse_ts(rb.parse_when(earlier, self.NOW))
        self.assertGreater(tomorrow - self.NOW, dt.timedelta(hours=21))       # already past today: tomorrow
        self.assertLessEqual(tomorrow - self.NOW, dt.timedelta(hours=22, minutes=1))

    def test_dates_with_and_without_an_offset(self):
        self.assertEqual(rb.parse_when("2026-10-07T23:00+00:00"), "2026-10-07T23:00:00+00:00")
        naive = rb.parse_ts(rb.parse_when("2026-10-07 23:00"))
        self.assertEqual(naive, dt.datetime(2026, 10, 7, 23, 0).astimezone())
        for bad in ("tomorrow", "25:99", "+x", "2026-13-01"):
            with self.assertRaises(rb.RBError) as e:
                rb.parse_when(bad)
            self.assertEqual(e.exception.code, "bad_time")

    def test_durations(self):
        self.assertEqual(rb.parse_minutes("90"), 90)
        self.assertEqual(rb.parse_minutes("8h"), 480)
        self.assertEqual(rb.parse_minutes("1d"), 1440)
        self.assertEqual(rb.parse_minutes(""), 0)
        for bad in ("soon", "99d", "-5"):
            with self.assertRaises(rb.RBError):
                rb.parse_minutes(bad)


class Claim(Base):
    def test_claim_refuses_before_the_start_time_and_works_after_it(self):
        self.add("A", not_before="+2h")
        with self.assertRaises(rb.RBError) as e:
            rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A"))
        self.assertEqual(e.exception.code, "begin_time")
        rb.mutate(self.path, lambda d: rb.op_edit(d, HUMAN, "A", {"not_before": ""}))
        self.run_("A")
        self.assertEqual(self.comp("A")["status"], "running")

    def test_claim_refusal_codes(self):
        self.add("A", uses=["gpu"])
        self.add("B", lane="second", uses=["gpu"])
        self.add("C", lane="third", needs=["B"])
        self.add("D", lane="third", text="", draft=True)
        self.add("E", lane="third", needs=["D"])
        self.run_("A")
        code = lambda iid: self._code(iid)
        self.assertEqual(code("B"), "conflict")
        self.assertEqual(code("C"), "waiting")
        self.assertEqual(code("E"), "stuck")

    def _code(self, iid):
        with self.assertRaises(rb.RBError) as e:
            rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, iid))
        return e.exception.code

    def test_a_timebox_sets_a_deadline_and_a_longer_lease(self):
        self.add("A", timebox="10h")
        self.run_("A")
        claim = self.comp("A")["claim"]
        self.assertIn("deadline", claim)
        lease = rb.parse_ts(claim["lease_until"]) - rb.now_dt()
        self.assertGreater(lease, dt.timedelta(hours=10))
        self.assertTrue(claim["signal"])

    def test_a_worker_style_short_lease_and_a_heartbeat_renew(self):
        self.add("A")
        rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "A", False, 3))
        first = rb.parse_ts(self.comp("A")["claim"]["lease_until"])
        self.assertLess(first - rb.now_dt(), dt.timedelta(minutes=4))
        time.sleep(1.1)
        rb.mutate(self.path, lambda d: rb.op_heartbeat(d, AGENT, "A", 3))
        self.assertGreater(rb.parse_ts(self.comp("A")["claim"]["lease_until"]), first)
        self.assertEqual(rb.load(self.path)["events"][-1]["action"], "claim")        # heartbeats do not flood the event log


class NextCli(Base):
    def test_exit_codes(self):
        self.add("A", uses=["gpu"])
        self.add("B", lane="second", uses=["gpu"], not_before="+2h")
        self.add("C", lane="third", text="", draft=True)
        self.add("D", lane="third", needs=["C"])
        code, out, _ = cli(self.path, "next", "--lane", "main")
        self.assertEqual(code, 0)
        self.assertIn("# A:", out)
        code, out, _ = cli(self.path, "next", "--lane", "second")
        self.assertEqual(code, rb.EXIT_WAIT, out)
        self.assertIn("not before", out)
        code, out, _ = cli(self.path, "next", "--lane", "third")
        self.assertEqual(code, rb.EXIT_STUCK, out)
        self.assertIn("needs you", out)
        self.run_("A")
        self.finish("A")
        rb.mutate(self.path, lambda d: rb.op_accept(d, HUMAN, "A"))
        code, out, _ = cli(self.path, "next", "--lane", "main")
        self.assertEqual(code, rb.EXIT_NONE, out)
        self.assertIn("nothing", out)

    def test_json_carries_the_reasons(self):
        self.add("B", lane="second", not_before="+2h")
        code, out, _ = cli(self.path, "next", "--lane", "second", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["status"]), (rb.EXIT_WAIT, "wait"))
        self.assertEqual(data["waiting"][0]["reasons"][0]["code"], "begin_time")
        self.assertGreaterEqual(data["retry_in"], 5)

    def test_wait_returns_the_task_as_soon_as_it_can_start(self):
        self.add("A", uses=["gpu"])
        self.add("B", lane="second", uses=["gpu"])
        self.run_("A")
        started = time.monotonic()
        threading.Timer(1.5, lambda: (self.finish("A"))).start()
        code, out, _ = cli(self.path, "next", "--lane", "second", "--wait", "30")
        self.assertEqual(code, 0, out)
        self.assertIn("# B:", out)
        self.assertLess(time.monotonic() - started, 15)

    def test_wait_gives_up_with_code_3_and_never_waits_for_the_person(self):
        self.add("A", uses=["gpu"])
        self.add("B", lane="second", uses=["gpu"])
        self.run_("A")
        started = time.monotonic()
        code, out, _ = cli(self.path, "next", "--lane", "second", "--wait", "2")
        self.assertEqual(code, rb.EXIT_WAIT)
        self.assertLess(time.monotonic() - started, 12)
        self.add("C", lane="third", text="", draft=True)
        self.add("D", lane="third", needs=["C"])
        started = time.monotonic()
        code, out, _ = cli(self.path, "next", "--lane", "third", "--wait", "30")
        self.assertEqual(code, rb.EXIT_STUCK)
        self.assertLess(time.monotonic() - started, 10)        # waiting would not help: it answers at once

    def test_claim_wait(self):
        self.add("A", uses=["gpu"])
        self.add("B", lane="second", uses=["gpu"])
        self.run_("A")
        code, _, err = cli(self.path, "claim", "B", "--by", "night-2")
        self.assertEqual(code, rb.EXIT_WAIT)
        self.assertIn("conflicts", err)
        threading.Timer(1.5, lambda: self.finish("A")).start()
        code, out, _ = cli(self.path, "claim", "B", "--by", "night-2", "--wait", "30")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.comp("B")["status"], "running")
        code, _, err = cli(self.path, "claim", "B", "--by", "night-3", "--wait", "2")
        self.assertEqual(code, 1)                              # not a waiting case: it is held by another session

    def test_old_scripts_that_only_look_for_exit_code_zero_still_work(self):
        self.add("A")
        code, out, _ = cli(self.path, "next", "--lane", "main")
        self.assertEqual(code, 0)


class Presence(Base):
    def test_next_claim_and_finish_leave_a_sign_of_life(self):
        self.add("A", uses=["gpu"])
        self.add("B", lane="second", uses=["gpu"])
        self.run_("A")
        lane = lambda lid: next(l for l in rb.load(self.path)["lanes"] if l["id"] == lid)
        self.assertEqual(lane("main")["seen"]["state"], "working")
        self.assertEqual(lane("main")["seen"]["task"], "A")
        cli(self.path, "next", "--lane", "second", "--by", "night-2")
        seen = lane("second")["seen"]
        self.assertEqual((seen["state"], seen["by"]), ("waiting", "night-2"))
        self.assertIn("A", seen["reason"])
        self.finish("A")
        self.assertEqual(lane("main")["seen"]["state"], "idle")

    def test_a_waiting_loop_does_not_rewrite_the_board_every_poll(self):
        self.add("B", lane="second", not_before="+2h")
        cli(self.path, "next", "--lane", "second", "--by", "n")
        rev = rb.load(self.path)["rev"]
        for _ in range(3):
            rb.note_presence(self.path, "second", "n", "waiting", "", rb.load(self.path)["lanes"][1]["seen"]["reason"])
        self.assertEqual(rb.load(self.path)["rev"], rev)


class Lanes(Base):
    def test_the_night_connect_text_waits_and_the_default_one_stops(self):
        d = rb.load(self.path)
        plain = rb.connect_text(d, "main", self.path)
        night = rb.connect_text(d, "main", self.path, unattended=True)
        self.assertIn("--wait 300", night)
        self.assertNotIn("--wait", plain)
        self.assertIn("exit code 3", night)
        self.assertIn("Exit code 3", plain)
        rb.mutate(self.path, lambda dd: rb.op_lane_edit(dd, HUMAN, "main", unattended=True))
        self.assertIn("--wait 300", rb.connect_text(rb.load(self.path), "main", self.path))
        for lang in ("ru", "kk"):
            rb.mutate(self.path, lambda dd: rb.op_board_lang(dd, HUMAN, lang))
            self.assertIn("--wait 300", rb.connect_text(rb.load(self.path), "main", self.path, unattended=True))

    def test_the_cli_sets_the_flag(self):
        code, out, err = cli(self.path, "--human", "lane", "edit", "main", "--unattended")
        self.assertEqual(code, 0, err)
        self.assertTrue(next(l for l in rb.load(self.path)["lanes"] if l["id"] == "main")["unattended"])
        code, out, _ = cli(self.path, "connect", "main")
        self.assertIn("--wait 300", out)
        cli(self.path, "--human", "lane", "edit", "main", "--no-unattended")
        code, out, _ = cli(self.path, "connect", "main")
        self.assertNotIn("--wait 300", out)


FAKE_AGENT = textwrap.dedent('''\
    import os, re, subprocess, sys
    prompt = sys.stdin.read()
    task = os.environ["BARID_TASK"]
    mode = os.environ.get("FAKE_MODE_" + task, "report")
    log = os.environ["FAKE_LOG"]
    open(log, "a").write(task + " " + mode + "\\n")
    if mode == "crash":
        sys.exit(3)
    if mode == "silent":
        sys.exit(0)
    if mode == "hang":
        import time
        time.sleep(600)
    m = re.search(r'(python\\S*) "([^"]+)" --board "([^"]+)" finish (\\S+) --report "<[^>]*>" --outcome <[^>]*> --by "([^"]+)"', prompt)
    assert m, "the worker prompt has no finish command"
    py, rb, board, tid, name = m.groups()
    assert "claim " + tid not in prompt, "a worker prompt must not tell the agent to claim"
    subprocess.check_call([sys.executable, rb, "--board", board, "finish", tid, "--report", "/tmp/fake-report.md", "--outcome", "complete", "--by", name])
''')


class Worker(Base):
    def setUp(self):
        super().setUp()
        self.agent = Path(self._tmp.name) / "fake_agent.py"
        self.agent.write_text(FAKE_AGENT)
        self.log = Path(self._tmp.name) / "fake.log"
        self.env = {"FAKE_LOG": str(self.log)}

    def worker(self, *extra, env=None, timeout=90):
        e = {**os.environ, "BARID_BOARD": str(self.path), **self.env, **(env or {})}
        p = subprocess.run([sys.executable, str(RB_PATH), "worker", "--lane", "main", "--cmd", f"{sys.executable} {self.agent}", "--poll", "1", *extra],
                           capture_output=True, text=True, timeout=timeout, env=e)
        return p.returncode, p.stdout + p.stderr

    def test_it_runs_the_whole_lane_in_order_and_ends_when_it_is_empty(self):
        self.add("A")
        self.add("B", needs=["A"], lane="main")
        self.add("C", after=["B"], lane="main")
        code, out = self.worker()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.log.read_text().split(), ["A", "report", "B", "report", "C", "report"])
        for iid in "ABC":
            self.assertEqual(self.comp(iid)["status"], "review")
        self.assertIn("lane main is empty", out)
        self.assertTrue(list((self.path.parent / "runs").glob("A-*.log")))

    def test_a_dead_agent_is_closed_and_the_next_task_still_runs(self):
        self.add("A")
        self.add("B", after=["A"], lane="main")
        code, out = self.worker(env={"FAKE_MODE_A": "crash"})
        self.assertEqual(code, 0, out)
        a = self.comp("A")
        self.assertEqual((a["status"], a["outcome"]), ("review", "failed"))
        self.assertIn("exit code 3", a["notes"][-1]["text"])
        self.assertEqual(self.comp("B")["status"], "review")

    def test_an_agent_that_ends_cleanly_without_reporting_is_partial(self):
        self.add("A")
        code, out = self.worker(env={"FAKE_MODE_A": "silent"})
        self.assertEqual(self.comp("A")["outcome"], "partial")

    def test_the_timebox_stops_a_hung_agent(self):
        self.add("A", timebox="1")
        self.add("B", after=["A"], lane="main")
        # the shortest timebox is a minute; make the deadline pass at once by shortening it on the claim
        rb.mutate(self.path, lambda d: rb.get_item(d, "A").update(timebox=1))
        started = time.monotonic()
        env = {"FAKE_MODE_A": "hang"}
        e = {**os.environ, "BARID_BOARD": str(self.path), **self.env, **env}
        # patch the clock of the worker: a one-minute timebox would make the test slow, so give the claim a deadline in the past
        script = textwrap.dedent(f"""
            import sys, importlib.util
            spec = importlib.util.spec_from_file_location("rb", {str(RB_PATH)!r}); rb = importlib.util.module_from_spec(spec); sys.modules["rb"] = rb; spec.loader.exec_module(rb)
            orig = rb.op_claim
            def claim(d, actor, iid, force=False, lease_minutes=0):
                r = orig(d, actor, iid, force, lease_minutes)
                it = rb.get_item(d, iid)
                if it["claim"].get("deadline"):
                    it["claim"]["deadline"] = (rb.now_dt() + rb.dt.timedelta(seconds=3)).isoformat()
                return r
            rb.op_claim = claim
            sys.argv = ["barid", "worker", "--lane", "main", "--cmd", {sys.executable + " " + str(self.agent)!r}, "--poll", "1"]
            sys.exit(rb.main())
        """)
        p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertLess(time.monotonic() - started, 90)
        a = self.comp("A")
        self.assertEqual((a["status"], a["outcome"]), ("review", "failed"))
        self.assertIn("timebox", a["notes"][-1]["text"])
        self.assertEqual(self.comp("B")["status"], "review")
        self.assertEqual(self.log.read_text().split(), ["A", "hang", "B", "report"])

    def test_it_waits_for_a_start_time_and_stops_with_code_4_when_the_person_is_needed(self):
        self.add("A", not_before="+1h")
        code, out = self.worker("--max-wait", "3")
        self.assertEqual(code, rb.EXIT_WAIT, out)
        self.assertIn("giving up", out)
        rb.mutate(self.path, lambda d: rb.op_cancel(d, HUMAN, "A", "x"))
        self.add("C", text="", draft=True)
        self.add("D", needs=["C"])
        code, out = self.worker()
        self.assertEqual(code, rb.EXIT_STUCK, out)

    def test_it_never_signals_anything_but_its_own_process_group(self):
        src = RB_PATH.read_text("utf-8")
        self.assertNotIn("pkill", src)
        self.assertIn("os.killpg(proc.pid", src)


class Digest(Base):
    def test_digest_tells_what_ended_what_runs_and_what_needs_the_person(self):
        self.add("A")
        self.add("B", lane="second", needs=["A"])
        self.add("C", lane="third", not_before="+2h")
        self.add("D", lane="main", uses=["gpu"])
        self.run_("A")
        self.finish("A", "partial")
        g = rb.digest(rb.load(self.path), rb.now_dt() - dt.timedelta(hours=1))
        self.assertEqual([e["id"] for e in g["ended"]], ["A"])
        self.assertEqual(g["ended"][0]["outcome"], "partial")
        self.assertIn({"id": "A", "title": "A", "kind": "accept", "outcome": "partial"}, g["needs_you"])
        self.assertTrue(any(n["kind"] == "decision" and n["id"] == "B" for n in g["needs_you"]))
        self.assertEqual([w["id"] for w in g["waiting"] if w["id"] == "C"], ["C"])
        self.assertEqual({s["id"]: s["state"] for s in g["sessions"]}["main"], "idle")
        text = rb.digest_text(g)
        self.assertIn("Needs you:", text)
        self.assertIn("partial", text)
        code, out, _ = cli(self.path, "digest", "--since", "2h")
        self.assertEqual(code, 0)
        self.assertIn("Ended:", out)

    def test_since_keeps_the_exact_moment_the_panel_sends(self):
        got = rb.parse_since("2026-10-06T10:00:05.812Z")
        self.assertEqual(got, dt.datetime(2026, 10, 6, 10, 0, 5, 812000, tzinfo=dt.timezone.utc))

    def test_since_formats(self):
        now = rb.now_dt()
        self.assertEqual(rb.parse_since("90m", now), now - dt.timedelta(minutes=90))
        self.assertEqual(rb.parse_since("2d", now), now - dt.timedelta(days=2))
        self.assertEqual(rb.parse_since(None, now), now - dt.timedelta(hours=12))


class Focus(Base):
    def test_a_session_card_shows_queued_work_before_a_draft_and_the_right_hint(self):
        self.add("D0", lane="main", text="", draft=True)
        self.add("A", lane="main")
        view = rb.board_view(self.path)
        self.assertEqual(view["focus"]["main"]["id"], "A")                 # a draft whose turn has come is not "what to do now" while work is queued
        self.assertEqual(view["focus"]["main"]["role"], "ready")

    def test_hint_kinds_for_time_dependencies_and_the_person(self):
        self.add("A", lane="main", not_before="+2h")
        self.add("B", lane="second")
        self.add("C", lane="third", needs=["B"])
        self.add("D", lane="main", text="", draft=True)
        self.add("E", lane="second", needs=["D"], text="e")
        view = rb.board_view(self.path)
        self.assertEqual(view["focus"]["main"]["hint"]["kind"], "begin")
        self.assertEqual(view["focus"]["third"]["hint"]["kind"], "deps")
        self.assertIn("A", [x["id"] for x in view["summary"]["begin"]])
        rb.mutate(self.path, lambda d: rb.op_cancel(d, HUMAN, "B", "x"))
        view = rb.board_view(self.path)
        self.assertEqual(view["focus"]["third"]["hint"]["kind"], "person")
        self.assertEqual(view["focus"]["third"]["hint"]["codes"], ["never"])
        self.assertTrue(view["summary"]["person"])

    def test_the_panel_api_takes_the_new_fields_and_the_night_flag(self):
        self.add("A", lane="main")
        rb.apply_action(self.path, {"action": "edit", "id": "A", "args": {"not_before": "+3h", "timebox": "2h", "after": ""}})
        it = rb.get_item(rb.load(self.path), "A")
        self.assertTrue(it["not_before"])
        self.assertEqual(it["timebox"], 120)
        rb.apply_action(self.path, {"action": "lane", "id": "main", "args": {"unattended": True}})
        self.assertTrue(rb.load(self.path)["lanes"][0]["unattended"])
        with self.assertRaises(rb.RBError) as e:
            rb.apply_action(self.path, {"action": "edit", "id": "A", "args": {"timebox": 5}})
        self.assertEqual(e.exception.code, "bad_input")
        with self.assertRaises(rb.RBError) as e:
            rb.apply_action(self.path, {"action": "edit", "id": "A", "args": {"not_before": "someday"}})
        self.assertEqual(e.exception.code, "bad_time")
        g = rb.digest(rb.load(self.path), rb.now_dt() - dt.timedelta(hours=1))
        self.assertIn("sessions", g)


class Footer(Base):
    def test_the_claim_step_of_the_footer_says_what_exit_code_3_means(self):
        self.add("A")
        d = rb.load(self.path)
        plain = rb.prompt_for(d, rb.get_item(d, "A"), self.path)
        self.assertIn("exit code 3 means it only has to wait", plain)
        self.assertNotIn("--wait 300", plain)
        rb.mutate(self.path, lambda dd: rb.op_lane_edit(dd, HUMAN, "main", unattended=True))
        d = rb.load(self.path)
        night = rb.prompt_for(d, rb.get_item(d, "A"), self.path)
        self.assertIn("--wait 300", night)
        for lang, word in (("ru", "кодом 3"), ("kk", "3 кодымен")):
            rb.mutate(self.path, lambda dd: rb.op_board_lang(dd, HUMAN, lang))
            d = rb.load(self.path)
            self.assertIn(word, rb.prompt_for(d, rb.get_item(d, "A"), self.path))


class OldBoards(Base):
    def test_a_board_without_the_new_fields_still_works(self):
        d = rb.load(self.path)
        d["items"].append({"id": "OLD", "lane": "main", "title": "old", "status": "queued", "needs": [], "uses": [], "quiet": False, "noisy": False, "when": "",
                           "outline": "", "text": "x", "report": "", "notes": [], "created": rb.now_iso(), "updated": rb.now_iso(), "created_by": "h", "claim": None})
        rb.save(self.path, d)
        c = self.comp("OLD")
        self.assertEqual((c["state"], c["reasons"], c["begin_in"]), ("ready", [], 0))
        self.assertEqual(rb.next_step(rb.load(self.path), "main")["id"], "OLD")


if __name__ == "__main__":
    unittest.main()
