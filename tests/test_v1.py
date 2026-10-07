"""Barid 1.0: waits that can never end are reported as such, a sent task keeps its ordering, boards stay quick."""
import datetime as dt
import os
import random
import shutil
import subprocess
import time
import unittest

from test_rb import HUMAN, rb
from test_unattended import AGENT, Base, cli


def expire_lease(path, iid):
    def go(d):
        next(i for i in d["items"] if i["id"] == iid)["claim"]["lease_until"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
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
        self.assertLess(took, 0.6, f"compute took {took:.2f}s on 500 tasks")


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


if __name__ == "__main__":
    unittest.main()
