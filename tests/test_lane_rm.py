"""Removing a lane keeps completed task history usable."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RB = ROOT / "skills" / "barid" / "scripts" / "barid.py"


class LaneRemoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.board = Path(self.tmp.name) / ".barid" / "board.json"
        self.board.parent.mkdir()
        spec = __import__("importlib.util").util.spec_from_file_location("lane_rm_barid", RB)
        self.rb = __import__("importlib.util").util.module_from_spec(spec)
        sys.modules[spec.name] = self.rb
        spec.loader.exec_module(self.rb)
        self.rb.save(self.board, self.rb.new_board("test", [("main", "Main"), ("old", "Old")]))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(RB), "--board", str(self.board), *args],
                              capture_output=True, text=True, encoding="utf-8")

    def add_task(self, iid, status):
        self.rb.mutate(self.board, lambda d: self.rb.op_add(d, self.rb.HUMAN,
                        {"id": iid, "lane": "old", "title": iid, "text": "work"}))
        self.rb.mutate(self.board, lambda d: next(i for i in d["items"] if i["id"] == iid).update(status=status))

    def test_agent_and_unknown_lane_are_refused(self):
        p = self.run_cli("lane", "rm", "old", "--by", "worker")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("only the person", p.stderr)
        p = self.run_cli("lane", "rm", "missing", "--human")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no such lane", p.stderr)

    def test_open_tasks_refuse_with_count_and_move_advice(self):
        for n, status in enumerate(("draft", "queued", "running", "proposed", "sent", "review")):
            self.add_task(f"T{n}", status)
        p = self.run_cli("lane", "rm", "old", "--human")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("6 open task(s)", p.stderr)
        self.assertIn("move them to another lane", p.stderr)
        self.assertIn("old", [l["id"] for l in self.rb.load(self.board)["lanes"]])

    def test_remove_preserves_archived_ids_and_board_views(self):
        self.add_task("DONE", "done")
        self.add_task("CANCEL", "cancelled")
        p = self.run_cli("lane", "rm", "old", "--human")
        self.assertEqual(p.returncode, 0, p.stderr)
        d = self.rb.load(self.board)
        self.assertEqual([l["id"] for l in d["lanes"]], ["main"])
        self.assertEqual([i["lane"] for i in d["items"]], ["old", "old"])
        self.assertEqual((d["events"][-1]["action"], d["events"][-1]["detail"]), ("lane", "remove"))
        self.assertIn("old", self.run_cli("list", "--all").stdout)
        self.assertNotIn("old", self.run_cli("overview").stdout)
        self.assertEqual(self.run_cli("plan").returncode, 0)
        self.assertEqual(self.run_cli("doctor").returncode, 0)
        self.assertEqual(self.run_cli("lint").returncode, 0)
        p = self.run_cli("next", "--lane", "old")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no such lane", p.stderr)
        self.assertEqual([i["lane"] for i in self.rb.load(self.board)["items"]], ["old", "old"])
        # The panel uses the lane ID as the visible fallback when its title is gone.
        panel = (ROOT / "skills" / "barid" / "scripts" / "panel.html").read_text("utf-8")
        self.assertIn("return id;", panel)
        self.assertEqual(json.loads(self.board.read_text("utf-8"))["schema"], self.rb.SCHEMA)


if __name__ == "__main__":
    unittest.main()
