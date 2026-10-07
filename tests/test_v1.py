"""Barid 1.0: waits that can never end are reported as such, a sent task keeps its ordering, boards stay quick."""
import datetime as dt
import random
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


if __name__ == "__main__":
    unittest.main()
