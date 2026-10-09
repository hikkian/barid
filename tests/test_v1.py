"""Barid 1.0: waits that can never end are reported as such, a sent task keeps its ordering, boards stay quick."""
import datetime as dt
import os
import json
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from test_rb import HUMAN, RB_PATH, rb
from test_unattended import AGENT, Base, cli


def expire_lease(path, iid, minutes=60):
    def go(d):
        next(i for i in d["items"] if i["id"] == iid)["claim"]["lease_until"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).isoformat()
    rb.mutate(path, go)


class UnreachableWaits(Base):
    def test_a_wait_below_a_cancelled_task_is_stuck_for_every_task_below_it(self):
        self.add("A")
        self.add("B", needs=["A"])
        self.add("C", lane="second", needs=["B"])
        rb.mutate(self.path, lambda d: rb.op_cancel(d, HUMAN, "A"))
        res = rb.next_step(rb.load(self.path), "second")
        self.assertEqual(res["status"], rb.NEXT_STUCK)
        r = res["waiting"][0]["reasons"][0]
        self.assertEqual((r["code"], r["id"], r["via"]), ("never", "A", "B"))
        self.assertEqual(cli(self.path, "next", "--lane", "second")[0], 4)
        self.assertEqual(cli(self.path, "--by", "w", "claim", "C")[0], 4)

    def test_a_chain_that_only_waits_for_time_or_a_running_task_still_waits(self):
        self.add("A", not_before="+3h")
        self.add("B", needs=["A"])
        self.add("C", lane="second", needs=["B"])
        self.assertEqual(rb.next_step(rb.load(self.path), "second")["status"], rb.NEXT_WAIT)
        self.assertEqual(cli(self.path, "next", "--lane", "second")[0], 3)

    def test_a_holder_whose_lease_ran_out_makes_its_lane_and_dependents_stuck(self):
        self.add("J1")
        self.add("J2")
        self.add("J3", lane="second", needs=["J1"])
        self.run_("J1")
        expire_lease(self.path, "J1")
        for lane in ("main", "second"):
            res = rb.next_step(rb.load(self.path), lane)
            self.assertEqual(res["status"], rb.NEXT_STUCK, lane)
        self.assertTrue(self.comp("J1")["stale"])
        self.assertEqual(cli(self.path, "next", "--lane", "main")[0], 4)
        msg = cli(self.path, "--by", "w", "claim", "J2")
        self.assertEqual(msg[0], 4)
        self.assertIn("lease", msg[2])

    def test_a_live_holder_is_still_waited_for(self):
        self.add("J1")
        self.add("J2")
        self.run_("J1")
        self.assertEqual(rb.next_step(rb.load(self.path), "main")["status"], rb.NEXT_WAIT)

    def test_returning_the_dead_holder_to_the_queue_frees_the_lane(self):
        self.add("J1")
        self.add("J2")
        self.run_("J1")
        expire_lease(self.path, "J1")
        rb.mutate(self.path, lambda d: rb.op_release(d, HUMAN, "J1"))
        self.assertEqual(rb.next_step(rb.load(self.path), "main")["id"], "J1")

    def test_random_boards_wait_only_for_what_can_still_happen(self):
        """If `next` says wait, nothing above the waiting tasks is cancelled, missing or held by an expired lease."""
        rng = random.Random(20261007)
        for _ in range(300):
            d = rb.new_board("t", [("a", "A"), ("b", "B")])
            ids = [f"T{k}" for k in range(rng.randint(2, 9))]
            now = rb.now_iso()
            for k, iid in enumerate(ids):
                status = rng.choice(["queued"] * 5 + ["cancelled", "running", "review", "done", "draft", "sent"])
                it = {"id": iid, "title": iid, "text": "x" if status != "draft" else "", "status": status, "lane": rng.choice("ab"), "needs": [],
                      "after": [], "uses": [], "notes": [], "profile": "", "created": now, "updated": now}
                for other in ids[:k]:
                    if rng.random() < 0.3:
                        it[rng.choice(["needs", "after"])].append(other)
                if status == "running":
                    past = rng.random() < 0.4
                    it["claim"] = {"by": "x", "at": now, "signal": now, "lease_until": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=-1 if past else 5)).isoformat()}
                if status in ("review", "done"):
                    it["outcome"] = rng.choice(["complete", "partial"])
                d["items"].append(it)
            comp = {c["id"]: c for c in rb.compute(d)}
            items = {i["id"]: i for i in d["items"]}
            for lane in ("a", "b"):
                res = rb.next_step(d, lane)
                if res["status"] != rb.NEXT_WAIT:
                    continue
                for w in res["waiting"]:
                    if not w["reasons"] or any(rb.is_hard(r) for r in w["reasons"]):
                        continue
                    seen, todo = set(), list(comp[w["id"]]["waiting_on"])      # what it still waits for, and what that waits for
                    while todo:
                        up = todo.pop()
                        if up in seen:
                            continue
                        seen.add(up)
                        t = items.get(up)
                        self.assertIsNotNone(t, (w, up))
                        self.assertNotEqual(t["status"], "cancelled", (w, up))
                        self.assertFalse(comp[up]["stale"], (w, up))
                        todo += comp[up]["waiting_on"]


class FoundByReview(Base):
    """Found by an independent review of the first release candidate."""

    def test_a_sent_task_below_a_dead_holder_is_refused_too(self):
        self.add("R")
        self.add("P")
        self.add("S", lane="second", needs=["P"])
        self.run_("R")
        expire_lease(self.path, "R")
        rb.mutate(self.path, lambda d: rb.op_sent(d, HUMAN, "S"))
        self.assertEqual(cli(self.path, "--by", "bot2", "claim", "S")[0], 4)
        self.assertEqual(self.comp("S")["status"], "sent")

    def test_a_chain_of_hundreds_is_worked_out_whatever_the_order_of_the_tasks(self):
        def fill(d):
            now = rb.now_iso()
            for k in reversed(range(700)):                 # a dependent is listed before what it needs
                d["items"].append({"id": f"T{k}", "title": "t", "text": "x", "status": "queued", "lane": "main", "needs": [f"T{k - 1}"] if k else [],
                                   "after": [], "uses": [], "notes": [], "profile": "", "created": now, "updated": now})
        rb.mutate(self.path, fill)
        comp = {c["id"]: c for c in rb.compute(rb.load(self.path))}
        self.assertEqual(comp["T0"]["state"], "ready")
        self.assertEqual(comp["T699"]["state"], "waiting")

    def test_two_paths_to_one_cancelled_task_are_one_reason_and_one_line_in_the_digest(self):
        for iid in "XAB":
            self.add(iid)
        self.add("Q", lane="second", needs=["A", "B"])
        self.add("A2", lane="third")                      # keeps the board busy in another lane
        rb.mutate(self.path, lambda d: (rb.get_item(d, "A").update(needs=["X"]), rb.get_item(d, "B").update(needs=["X"])))
        rb.mutate(self.path, lambda d: rb.op_cancel(d, HUMAN, "X"))
        q = self.comp("Q")
        self.assertEqual([(r["code"], r["id"]) for r in q["reasons"]], [("never", "X")])
        g = rb.digest(rb.load(self.path), rb.now_dt() - dt.timedelta(hours=1))
        self.assertEqual([n["id"] for n in g["needs_you"]].count("Q"), 1)

    def test_a_sent_task_that_waits_is_planned_after_what_it_waits_for(self):
        self.add("P", uses=["gpu"])
        self.add("Z")
        self.add("S", lane="second", uses=["gpu"], needs=["P"])
        rb.mutate(self.path, lambda d: rb.op_sent(d, HUMAN, "S"))
        res = rb.next_step(rb.load(self.path), "main")
        self.assertEqual(res["id"], "P")                   # not Z: P is what S waits for

    def test_a_lease_that_just_ran_out_is_waited_for_a_while_a_lost_one_is_not(self):
        self.add("J1")
        self.add("J2")
        self.run_("J1")
        expire_lease(self.path, "J1", minutes=1)          # a computer that slept: the session may still renew it
        self.assertEqual(rb.next_step(rb.load(self.path), "main")["status"], rb.NEXT_WAIT)
        self.assertTrue(self.comp("J1")["stale"])           # shown as expired at once
        self.assertFalse(self.comp("J1")["gone"])
        expire_lease(self.path, "J1", minutes=30)
        self.assertEqual(rb.next_step(rb.load(self.path), "main")["status"], rb.NEXT_STUCK)

    def test_overview_counts_a_stuck_session_as_something_for_you(self):
        self.add("J1")
        self.add("J2")
        self.run_("J1")
        expire_lease(self.path, "J1")
        text = rb.status_text(rb.status_summary(rb.load(self.path)))
        self.assertIn("session(s) stuck", text)


class SentKeepsItsOrder(Base):
    def test_a_sent_task_cannot_be_claimed_before_what_it_needs(self):
        self.add("D")
        self.add("S", needs=["D"])
        rb.mutate(self.path, lambda d: rb.op_sent(d, HUMAN, "S"))
        code, _, err = cli(self.path, "--by", "w", "claim", "S")
        self.assertEqual(code, 3, err)
        self.assertEqual(self.comp("S")["status"], "sent")

    def test_a_sent_task_waits_for_its_start_time_and_stops_at_a_cancelled_need(self):
        self.add("T", not_before="+5h")
        rb.mutate(self.path, lambda d: rb.op_sent(d, HUMAN, "T"))
        self.assertEqual(cli(self.path, "--by", "w", "claim", "T")[0], 3)
        self.add("X", lane="second")
        self.add("Y", lane="second", needs=["X"])
        rb.mutate(self.path, lambda d: rb.op_sent(d, HUMAN, "Y"))
        rb.mutate(self.path, lambda d: rb.op_cancel(d, HUMAN, "X"))
        self.assertEqual(cli(self.path, "--by", "w", "claim", "Y")[0], 4)

    def test_a_sent_task_that_waits_for_another_does_not_block_it(self):
        """S is sent, needs D, and both would use the GPU: S must not hold D back, or they wait for each other for ever."""
        self.add("D", lane="second", uses=["gpu"])
        self.add("S", lane="third", uses=["gpu"], needs=["D"])
        rb.mutate(self.path, lambda d: rb.op_sent(d, HUMAN, "S"))
        self.assertEqual(self.comp("D")["state"], "ready")
        self.assertEqual(rb.next_step(rb.load(self.path), "second")["id"], "D")

    def test_a_sent_task_with_nothing_to_wait_for_still_claims_and_warns_about_conflicts(self):
        self.add("R", lane="second", uses=["gpu"])
        self.run_("R")
        self.add("S", lane="third", uses=["gpu"])
        rb.mutate(self.path, lambda d: rb.op_sent(d, HUMAN, "S"))
        warnings = rb.mutate(self.path, lambda d: rb.op_claim(d, AGENT, "S"))
        self.assertTrue(any("conflicts" in w for w in warnings))


class BoardsStayQuick(Base):
    def test_compute_on_a_big_board_is_quick(self):
        rng = random.Random(7)
        now = rb.now_iso()

        def fill(d):
            for k in range(500):
                d["items"].append({"id": f"T{k}", "title": f"t{k}", "text": "x", "status": "queued", "lane": rng.choice(["main", "second", "third"]),
                                   "needs": [f"T{rng.randrange(k)}"] if k and rng.random() < 0.3 else [], "after": [], "uses": [], "notes": [],
                                   "profile": "", "touches": [f"src/f{rng.randrange(40)}.py"], "created": now, "updated": now})
        rb.mutate(self.path, fill)
        d = rb.load(self.path)
        start = time.perf_counter()
        rb.compute(d)
        took = time.perf_counter() - start
        self.assertLess(took, 2.0, f"compute took {took:.2f}s on 500 tasks")        # 0.25 s here; a slow CI machine gets room


@unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset (not on Windows)")
class TimesOfDay(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()
        self.addCleanup(self.restore)

    def restore(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def local(self, iso):
        return dt.datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")

    def test_a_time_of_day_is_that_time_on_the_clock_across_a_clock_change(self):
        utc = dt.timezone.utc
        # Saturday 23:30 CEST; the clocks go back during the night, so 23:00 on Sunday is CET (one hour later in UTC than a fixed offset says)
        self.assertEqual(self.local(rb.parse_when("23:00", dt.datetime(2026, 10, 24, 21, 30, tzinfo=utc))), "2026-10-25 23:00")
        # Saturday 23:30 CET; the clocks go forward during the night
        self.assertEqual(self.local(rb.parse_when("23:00", dt.datetime(2026, 3, 28, 22, 30, tzinfo=utc))), "2026-03-29 23:00")
        self.assertEqual(self.local(rb.parse_when("02:30", dt.datetime(2026, 10, 24, 21, 30, tzinfo=utc))), "2026-10-25 02:30")

    def test_absurd_times_are_refused_with_a_message_not_a_traceback(self):
        for text in ("+99999999d", "+" + "9" * 5000 + "m", "+9999999999999h"):
            with self.assertRaises(rb.RBError, msg=text):
                rb.parse_when(text)

    def test_a_time_without_an_offset_in_a_hand_edited_board_does_not_break_it(self):
        d = rb.new_board("t", [("a", "A")])
        now = rb.now_iso()
        d["items"].append({"id": "T", "title": "T", "text": "x", "status": "queued", "lane": "a", "needs": [], "after": [], "uses": [], "notes": [],
                           "profile": "", "not_before": "2099-10-07T23:00:00", "created": now, "updated": now})
        self.assertEqual(rb.compute(d)[0]["state"], "waiting")


class Digest(Base):
    def test_a_task_that_ended_is_still_listed_after_a_flood_of_events(self):
        self.add("J1")
        self.run_("J1")
        self.finish("J1")
        self.add("J2", lane="second")
        self.run_("J2")
        for k in range(600):
            rb.mutate(self.path, lambda d, k=k: rb.op_note(d, AGENT, "J2", f"progress {k}"))
        g = rb.digest(rb.load(self.path), rb.now_dt() - dt.timedelta(hours=1))
        self.assertEqual([e["id"] for e in g["ended"]], ["J1"])
        self.assertEqual([r["id"] for r in g["running"]], ["J2"])


@unittest.skipUnless(shutil.which("git"), "needs git")
class FileNames(Base):
    def git(self, *a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, check=True, capture_output=True)

    def test_names_with_non_ascii_letters_and_spaces_stay_in_scope(self):
        self.repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, "src"))
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "init")
        self.add("T1", workdir=self.repo, touches=["src"])
        self.run_("T1")
        for name in ("caf\u00e9.txt", "with space.txt", "\u043f\u043b\u044e\u0441.txt", "\u0430\u0431\u0434\u0438\u043a\u0435\u0440\u043e\u0432\u0430 \u04d9.txt"):
            with open(os.path.join(self.repo, "src", name), "w", encoding="utf-8") as f:
                f.write("x")
        with open(os.path.join(self.repo, "outside \u043d\u0435.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.finish("T1")
        it = self.comp("T1")
        self.assertEqual(sorted(v["path"] for v in it["violations"]), ["outside \u043d\u0435.txt"])      # only the file that really is outside
        self.assertEqual(it["changed"]["count"], 5)
        self.assertIn("caf\u00e9.txt", " ".join(it["changed"]["files"]))


class AcceptClean(Base):
    def make_review(self, iid, outcome="complete", **kw):
        self.add(iid, lane=kw.pop("lane", "main"), **kw)
        self.run_(iid)
        self.finish(iid, outcome)

    def test_only_reports_that_need_no_reading_are_accepted(self):
        self.make_review("OK1")
        self.make_review("OK2")
        self.make_review("PART", "partial")
        self.make_review("FAIL", "failed")
        self.make_review("WARN")
        rb.mutate(self.path, lambda d: rb.get_item(d, "WARN").update(violations=[{"type": "outside_scope", "path": "x"}]))
        self.make_review("DIST")
        rb.mutate(self.path, lambda d: rb.get_item(d, "DIST").update(disturbed_by=[{"id": "L", "why": "load"}]))
        ids = rb.mutate(self.path, lambda d: rb.op_accept_clean(d, HUMAN))
        self.assertEqual(sorted(ids), ["OK1", "OK2"])
        for iid, status in (("OK1", "done"), ("OK2", "done"), ("PART", "review"), ("FAIL", "review"), ("WARN", "review"), ("DIST", "review")):
            self.assertEqual(self.comp(iid)["status"], status, iid)

    def test_an_agent_cannot_accept_reports(self):
        self.make_review("OK1")
        with self.assertRaises(rb.RBError):
            rb.mutate(self.path, lambda d: rb.op_accept_clean(d, AGENT))
        self.assertEqual(self.comp("OK1")["status"], "review")

    def test_the_command_line_says_what_it_did(self):
        self.make_review("OK1")
        code, out, err = cli(self.path, "--human", "accept-clean")
        self.assertEqual(code, 0, err)
        self.assertIn("accepted 1 clean report", out)
        self.assertIn("nothing to accept", cli(self.path, "--human", "accept-clean")[1])


class Overview(Base):
    def test_every_session_gets_one_line_and_the_tail_says_what_waits_for_you(self):
        self.add("A")
        self.add("B", lane="second")
        self.add("C", lane="third", not_before="+4h")
        self.run_("A")
        self.make_done = None
        g = rb.status_summary(rb.load(self.path))
        rows = {x["id"]: x for x in g["lanes"]}
        self.assertEqual((rows["main"]["state"], rows["main"]["task"]), ("working", "A"))
        self.assertEqual((rows["second"]["next"], rows["second"]["next_id"]), ("task", "B"))
        self.assertEqual(rows["third"]["next"], "wait")
        text = rb.status_text(g)
        self.assertEqual(len(text.splitlines()), 4)               # three sessions and the line about you
        self.assertIn("Needs you: nothing", text)
        brief = cli(self.path, "overview", "--brief")
        self.assertEqual(brief[0], 0)
        self.assertEqual(len(brief[1].strip().splitlines()), 1)
        self.assertIn("main: A", brief[1])

    def test_a_session_that_left_is_not_shown_as_working(self):
        self.add("A")
        self.run_("A")                                   # the session marks itself as working ...
        rb.mutate(self.path, lambda d: rb.op_release(d, HUMAN, "A"))     # ... and the task goes back to the queue: nothing runs
        row = {x["id"]: x for x in rb.status_summary(rb.load(self.path))["lanes"]}["main"]
        self.assertEqual(row["task"], "")
        self.assertNotEqual(row["state"], "working")

    def test_a_dead_holder_and_a_report_to_read_show_up(self):
        self.add("A")
        self.run_("A")
        expire_lease(self.path, "A")
        self.add("R", lane="second")
        self.run_("R", AGENT)
        self.finish("R", "partial")
        text = cli(self.path, "overview")[1]
        self.assertIn("LEASE EXPIRED", text)
        self.assertIn("1 to accept", text)


class PolicyAndJson(Base):
    def test_the_person_can_make_dependents_wait_for_the_accept(self):
        self.add("A")
        self.add("B", lane="second", needs=["A"])
        self.run_("A")
        self.finish("A")
        self.assertEqual(self.comp("B")["state"], "ready")             # a complete report unlocks it ...
        code, out, err = cli(self.path, "--human", "policy", "dependents_wait_for_accept", "true")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.comp("B")["state"], "waiting")           # ... until the setting says the person decides
        self.assertIn("dependents_wait_for_accept", cli(self.path, "policy")[1])
        self.assertEqual(json.loads(cli(self.path, "--json", "policy")[1])["dependents_wait_for_accept"], True)

    def test_an_agent_cannot_change_the_policy_and_wrong_values_are_refused(self):
        self.assertEqual(cli(self.path, "--by", "w", "policy", "footer", "false")[0], 1)
        for key, value in (("footer", "maybe"), ("lease_hours", "abc"), ("lease_hours", "0"), ("lease_hours", "99999"), ("nonsense", "true")):
            code, _, err = cli(self.path, "--human", "policy", key, value)
            self.assertEqual(code, 1, (key, value))
            self.assertNotIn("Traceback", err)
        self.assertEqual(json.loads(cli(self.path, "--json", "policy", "footer")[1]), {"footer": True})

    def test_a_policy_value_is_used_when_a_claim_is_made(self):
        self.add("A")
        self.assertEqual(cli(self.path, "--human", "policy", "lease_hours", "2")[0], 0)
        self.run_("A")
        left = (rb.parse_ts(self.comp("A")["claim"]["lease_until"]) - rb.now_dt()).total_seconds()
        self.assertTrue(3600 < left <= 7200, left)

    def test_commands_that_report_data_give_json(self):
        self.add("A")
        for args in (("where",), ("doctor",), ("lane", "list"), ("resource", "list"), ("profile", "list"), ("protect", "list")):
            code, out, err = cli(self.path, "--json", *args)
            self.assertEqual(code, 0, (args, err))
            json.loads(out)                                             # must be valid JSON, not text
        self.assertTrue(json.loads(cli(self.path, "--json", "doctor")[1])["ok"])

    def test_a_task_that_waits_only_for_a_start_time_does_not_print_an_empty_after(self):
        self.add("A", not_before="+3h")
        listing = cli(self.path, "list")[1]
        self.assertNotIn("(after )", listing)
        self.assertIn("not before", listing)


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z" if os.path.exists(f"/proc/{pid}/stat") else True
    except OSError:
        return False


@unittest.skipUnless(os.name == "posix", "the worker tests use a POSIX shell")
class WorkerSafety(Base):
    def start_worker(self, cmd, *extra, env=None, board=None):
        e = {**os.environ, "BARID_BOARD": str(board or self.path), "BARID_WORKER_BACKOFF": "0", **(env or {})}
        return subprocess.Popen([sys.executable, str(RB_PATH), "worker", "--lane", "main", "--cmd", cmd, "--poll", "1", *extra],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=e)

    def finish_worker(self, p, timeout=60):
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out

    def wait_for(self, cond, seconds=30):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.2)
        return False

    def test_a_task_that_is_cancelled_while_it_runs_stops_its_agent(self):
        self.add("A")
        pidfile = Path(self._tmp.name) / "agent.pid"
        p = self.start_worker(f"echo $$ > {pidfile}; exec sleep 300")
        self.assertTrue(self.wait_for(lambda: pidfile.exists() and self.comp("A")["status"] == "running"))
        pid = int(pidfile.read_text())
        rb.mutate(self.path, lambda d: rb.op_cancel(d, HUMAN, "A", "not needed"))
        code, out = self.finish_worker(p)
        self.assertEqual(code, 0, out)
        self.assertFalse(alive(pid), "the agent kept running after its task was cancelled")
        self.assertEqual(self.comp("A")["status"], "cancelled")
        self.assertIn("took the task away", out)

    def test_an_agent_that_keeps_returning_the_task_is_given_up_on(self):
        self.add("A")
        release = f"{sys.executable} {RB_PATH} release $BARID_TASK"
        p = self.start_worker(release, "--max-attempts", "2")
        code, out = self.finish_worker(p)
        self.assertEqual(code, 0, out)
        a = self.comp("A")
        self.assertEqual((a["status"], a["outcome"]), ("review", "failed"))
        self.assertEqual(out.count("A: started"), 2)
        self.assertIn("came back to the queue", a["notes"][-1]["text"])

    def test_a_folder_that_does_not_exist_is_refused_before_anything_is_claimed(self):
        self.add("A")
        p = self.start_worker("true", "--cwd", os.path.join(self._tmp.name, "nowhere"))
        code, out = self.finish_worker(p)
        self.assertEqual(code, 1, out)
        self.assertIn("not a folder", out)
        self.assertEqual(self.comp("A")["status"], "queued")

    def test_a_command_that_cannot_start_does_not_leave_the_task_running(self):
        self.add("A")
        result = rb.run_worker_task(self.path, "main", "A", "w", "true", os.path.join(self._tmp.name, "gone"), __import__("threading").Event())
        self.assertEqual(result, "error")
        a = self.comp("A")
        self.assertEqual((a["status"], a["outcome"]), ("review", "failed"))
        self.assertIn("could not run the agent", a["notes"][-1]["text"])

    def test_a_big_prompt_does_not_keep_the_worker_from_stopping(self):
        """The agent never reads its prompt and the prompt does not fit the pipe: the worker used to block in the write and ignore SIGTERM."""
        import signal
        self.add("A", text="x" * 300_000)
        pidfile = Path(self._tmp.name) / "big.pid"
        p = self.start_worker(f"echo $$ > {pidfile}; exec sleep 120")
        self.assertTrue(self.wait_for(lambda: pidfile.exists() and self.comp("A")["status"] == "running"))
        time.sleep(1.5)
        started = time.monotonic()
        p.send_signal(signal.SIGTERM)
        code, out = self.finish_worker(p, 60)
        self.assertLess(time.monotonic() - started, 30, out)
        self.assertFalse(alive(int(pidfile.read_text())))
        self.assertEqual(self.comp("A")["outcome"], "failed")

    def test_what_the_agent_left_running_does_not_outlive_the_task(self):
        self.add("A")
        pidfile = Path(self._tmp.name) / "bg.pid"
        stubborn = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
        p = self.start_worker(f"{sys.executable} -c '{stubborn}' & echo $! > {pidfile}")
        code, out = self.finish_worker(p, 60)
        self.assertEqual(code, 0, out)
        pid = int(pidfile.read_text())
        self.assertTrue(self.wait_for(lambda: not alive(pid), 15), "a background process of the agent survived the task")

    def test_a_board_in_a_folder_with_a_space_still_gets_its_prompt_file(self):
        board_dir = Path(self._tmp.name) / "my project"
        board_dir.mkdir()
        path = board_with_in(board_dir)
        add_to(path, "A")
        out_file = Path(self._tmp.name) / "seen.txt"
        p = self.start_worker(f"cat {{prompt_file}} > {out_file}", board=path)
        code, out = self.finish_worker(p, 60)
        self.assertEqual(code, 0, out)
        self.assertIn("do the thing", out_file.read_text())


class Durability(Base):
    def cli_env(self, **extra):
        return {**os.environ, "BARID_BOARD": str(self.path), **extra}

    def run_cli(self, *args, env=None, timeout=60):
        p = subprocess.run([sys.executable, str(RB_PATH), *args], capture_output=True, text=True, timeout=timeout, env=env or self.cli_env())
        return p.returncode, p.stdout, p.stderr

    @unittest.skipUnless(os.name == "posix", "uses flock")
    def test_a_stuck_lock_holder_gives_a_clear_refusal_instead_of_a_hang(self):
        self.add("A")
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, time
            fh = open({str(self.path) + '.lock'!r}, "a+"); fcntl.flock(fh, fcntl.LOCK_EX); print("held", flush=True); time.sleep(20)""")],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        holder.stdout.readline()
        started = time.monotonic()
        code, _, err = self.run_cli("--by", "x", "note", "A", "hello", env=self.cli_env(BARID_LOCK_WAIT="2"))
        self.assertEqual(code, 1)
        self.assertIn("locked by another process", err)
        self.assertLess(time.monotonic() - started, 15)

    @unittest.skipUnless(os.name == "posix" and shutil.which("git"), "needs a POSIX shell and git")
    def test_a_slow_git_during_claim_does_not_hold_up_other_commands(self):
        self.add("A")
        self.add("B", lane="second")
        shim = Path(self._tmp.name) / "shim"
        shim.mkdir()
        (shim / "git").write_text(f"#!/bin/sh\nsleep 3\nexec {shutil.which('git')} \"$@\"\n")
        (shim / "git").chmod(0o755)
        env = self.cli_env(PATH=f"{shim}{os.pathsep}{os.environ['PATH']}")
        claim = subprocess.Popen([sys.executable, str(RB_PATH), "--by", "w", "claim", "A"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.0)                           # the claim is now waiting for git, outside the lock
        started = time.monotonic()
        code, _, err = self.run_cli("--by", "x", "note", "B", "while git is slow")
        took = time.monotonic() - started
        self.assertEqual(code, 0, err)
        self.assertLess(took, 2.0, f"a note waited {took:.1f}s for a claim that was only waiting for git")
        claim.communicate(timeout=60)
        self.assertEqual(self.comp("A")["status"], "running")

    def test_a_symlinked_board_is_written_through(self):
        real_dir = Path(self._tmp.name) / "elsewhere"
        real_dir.mkdir()
        target = real_dir / "board.json"
        shutil.move(str(self.path), str(target))
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("cannot make symlinks here")
        self.add("A")
        self.assertTrue(self.path.is_symlink())
        self.assertIn("A", [i["id"] for i in json.loads(target.read_text("utf-8"))["items"]])
        self.assertTrue((real_dir / "board.json.lock").exists())

    def test_the_previous_version_is_kept_and_saves_are_flushed_to_disk(self):
        self.add("A")
        before = rb.load(self.path)["rev"]
        calls = []
        real_fsync = os.fsync
        os.fsync = lambda fd: (calls.append(fd), real_fsync(fd))[1]
        self.addCleanup(setattr, os, "fsync", real_fsync)
        self.add("B")
        self.assertGreaterEqual(len(calls), 1)
        bak = Path(str(self.path) + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(json.loads(bak.read_text("utf-8"))["rev"], rb.load(self.path)["rev"] - 1)
        self.assertEqual(before + 1, rb.load(self.path)["rev"])

    def test_old_temp_files_of_a_cut_off_save_are_removed_but_a_fresh_one_is_left(self):
        old, fresh = Path(str(self.path) + ".tmp4242"), Path(str(self.path) + ".tmp4343")
        old.write_text("x")
        fresh.write_text("x")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        self.add("A")
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs a user who cannot write to a read-only folder")
    def test_a_read_only_folder_gives_a_message_not_a_traceback(self):
        self.add("A")
        folder = self.path.parent
        folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(folder.chmod, 0o755)
        code, _, err = self.run_cli("--by", "x", "note", "A", "hello")
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_damaged_board_files_are_refused_with_a_message(self):
        self.add("A")
        for text in ("[]", '{"schema": 1}', '{"schema": 1, "items": [{"id": "A"}], "lanes": []}', "", "{not json", '{"schema": 1, "items": 5, "lanes": []}'):
            self.path.write_text(text, "utf-8")
            code, out, err = self.run_cli("list")
            self.assertEqual(code, 1, (text, err))
            self.assertNotIn("Traceback", err, text)
            self.assertIn("damaged", err, text)

    def test_the_message_about_a_damaged_board_points_at_the_previous_version(self):
        self.add("A")
        self.add("B")
        self.path.write_text("", "utf-8")
        self.assertIn(".bak", self.run_cli("list")[2])

    def test_many_processes_writing_at_once_lose_nothing(self):
        self.add("A")
        procs = [subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import subprocess, sys
            for k in range(15):
                r = subprocess.run([sys.executable, {str(RB_PATH)!r}, "--by", "p{n}", "note", "A", "n{n}-" + str(k)], capture_output=True, text=True)
                assert r.returncode == 0, r.stderr""")], env=self.cli_env(), stderr=subprocess.PIPE, text=True) for n in range(6)]
        for p in procs:
            _, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, err)
        notes = self.comp("A")["notes"]
        self.assertEqual(len(notes), 90)
        self.assertEqual(len({n["text"] for n in notes}), 90)


@unittest.skipUnless(os.name == "posix", "the worker tests use a POSIX shell")
class WorkerFoundByReview(WorkerSafety):
    def test_an_agent_that_finishes_its_task_itself_is_not_killed_for_it(self):
        self.add("A")
        marker = Path(self._tmp.name) / "done.marker"
        finish = f"{sys.executable} {RB_PATH} finish $BARID_TASK --no-report --outcome complete"
        p = self.start_worker(f"{finish}; sleep 4; touch {marker}", "--max-tasks", "1")
        code, out = self.finish_worker(p)
        self.assertEqual(code, 0, out)
        self.assertTrue(marker.exists(), "the agent was stopped after it had reported: " + out)
        self.assertNotIn("took the task away", out)
        self.assertIn("reported by the agent (review, complete)", out)

    def test_a_task_that_is_deleted_while_it_runs_stops_its_agent(self):
        self.add("A")
        pidfile = Path(self._tmp.name) / "purge.pid"
        p = self.start_worker(f"echo $$ > {pidfile}; exec sleep 300")
        self.assertTrue(self.wait_for(lambda: pidfile.exists() and self.comp("A")["status"] == "running"))
        rb.mutate(self.path, lambda d: (rb.op_cancel(d, HUMAN, "A"), rb.op_purge(d, HUMAN, "A")))
        code, out = self.finish_worker(p)
        self.assertEqual(code, 0, out)
        self.assertFalse(alive(int(pidfile.read_text())))

    def test_a_hangup_stops_the_agent_and_closes_the_task(self):
        import signal
        self.add("A")
        pidfile = Path(self._tmp.name) / "hup.pid"
        p = self.start_worker(f"echo $$ > {pidfile}; exec sleep 300")
        self.assertTrue(self.wait_for(lambda: pidfile.exists() and self.comp("A")["status"] == "running"))
        p.send_signal(signal.SIGHUP)
        code, out = self.finish_worker(p)
        self.assertFalse(alive(int(pidfile.read_text())))
        a = self.comp("A")
        self.assertEqual((a["status"], a["outcome"]), ("review", "failed"))

    def test_lost_claim_races_are_not_attempts(self):
        self.add("A")
        calls = []
        real = rb.claim_task

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) <= 3:
                raise rb.RBError("someone else was first", "conflict")
            return real(*a, **k)
        rb.claim_task = flaky
        self.addCleanup(setattr, rb, "claim_task", real)
        os.environ["BARID_BOARD"] = str(self.path)
        os.environ["BARID_WORKER_BACKOFF"] = "0"
        self.addCleanup(os.environ.pop, "BARID_BOARD", None)
        self.addCleanup(os.environ.pop, "BARID_WORKER_BACKOFF", None)
        with self.assertRaises(SystemExit) as ctx:
            rb.main(["--board", str(self.path), "worker", "--lane", "main", "--cmd", "true", "--poll", "1", "--max-attempts", "2"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(self.comp("A")["outcome"], "partial")      # it ran (and ended without a report): it was not given up on for the lost claims

    def test_a_prompt_file_the_command_already_quotes_is_not_quoted_twice(self):
        self.assertEqual(rb.fill_prompt_file('cat "{prompt_file}"', "/a b/c.md"), 'cat "/a b/c.md"')
        self.assertEqual(rb.fill_prompt_file("cat '{prompt_file}'", "/a b/c.md"), "cat '/a b/c.md'")
        self.assertEqual(rb.fill_prompt_file("cat {prompt_file}", "/a b/c.md"), "cat '/a b/c.md'")
        board_dir = Path(self._tmp.name) / "my project"
        board_dir.mkdir()
        path = board_with_in(board_dir)
        add_to(path, "A")
        out_file = Path(self._tmp.name) / "seen2.txt"
        p = self.start_worker(f'cat "{{prompt_file}}" > {out_file}', board=path)
        code, out = self.finish_worker(p, 60)
        self.assertIn("do the thing", out_file.read_text())


class SmallThingsFoundByReview(Base):
    def test_a_board_saved_with_a_byte_order_mark_opens(self):
        self.add("A")
        text = self.path.read_text("utf-8")
        self.path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(rb.load(self.path)["items"][0]["id"], "A")

    def test_old_backup_link_leftovers_are_removed(self):
        old = Path(str(self.path) + ".bak31337")
        old.write_text("x")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        self.add("A")
        self.assertFalse(old.exists())
        self.assertTrue(Path(str(self.path) + ".bak").exists())

    def test_a_typo_in_the_lock_wait_variable_does_not_stop_the_program(self):
        self.add("A")
        env = {**os.environ, "BARID_BOARD": str(self.path), "BARID_LOCK_WAIT": "abc"}
        p = subprocess.run([sys.executable, str(RB_PATH), "list"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)


class StaleEdits(Base):
    """Two browser tabs (FIXSCN1, finding 2): an edit may name the version of the task it was opened on. If the task changed since,
    it is refused with code "changed" and nothing is written. Without the field the server behaves as before."""

    def edit(self, iid, **args):
        return rb.apply_action(self.path, {"action": "edit", "id": iid, "args": args})

    def test_an_edit_without_the_version_field_works_as_before(self):
        self.add("A", title="one")
        self.edit("A", title="two")                      # what the CLI and older panels send
        self.assertEqual(self.comp("A")["title"], "two")

    def test_an_edit_that_names_the_current_version_is_saved(self):
        self.add("A", title="one")
        self.edit("A", title="two", seen_updated=self.comp("A")["updated"])
        self.assertEqual(self.comp("A")["title"], "two")

    def test_an_edit_on_a_version_someone_else_replaced_is_refused_and_writes_nothing(self):
        self.add("A", title="one")
        seen = self.comp("A")["updated"]
        self.edit("A", title="from tab 2")                # the other tab saves first
        with self.assertRaises(rb.RBError) as cm:
            self.edit("A", title="from tab 1", seen_updated=seen)
        self.assertEqual(cm.exception.code, "changed")
        self.assertEqual(self.comp("A")["title"], "from tab 2")

    def test_a_refused_edit_made_within_the_same_second_is_still_refused(self):
        """The stored time has microseconds, so two saves in one second are told apart."""
        self.add("A", title="one")
        self.edit("A", title="two")
        seen = self.comp("A")["updated"]
        self.edit("A", title="three")
        with self.assertRaises(rb.RBError) as cm:
            self.edit("A", title="four", seen_updated=seen)
        self.assertEqual(cm.exception.code, "changed")

    def test_a_version_field_that_is_not_text_is_refused(self):
        self.add("A", title="one")
        with self.assertRaises(rb.RBError) as cm:
            self.edit("A", title="two", seen_updated=5)
        self.assertEqual(cm.exception.code, "bad_input")
        self.assertEqual(self.comp("A")["title"], "one")


def board_with_in(folder):
    path = Path(folder) / ".barid" / "board.json"
    path.parent.mkdir(parents=True)
    rb.save(path, rb.new_board("t", [("main", "Main")]))
    return path


def add_to(path, iid):
    rb.mutate(path, lambda d: rb.op_add(d, HUMAN, {"id": iid, "text": "do the thing", "lane": "main"}))


if __name__ == "__main__":
    unittest.main()
