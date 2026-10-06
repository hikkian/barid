"""Boards written by every earlier release must open, plan, export and keep working with the current code (fixtures made with the old releases)."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RB_PATH = ROOT / "skills" / "barid" / "scripts" / "barid.py"
spec = importlib.util.spec_from_file_location("rb_migration", RB_PATH)
rb = importlib.util.module_from_spec(spec)
sys.modules["rb_migration"] = rb
spec.loader.exec_module(rb)
FIXTURES = sorted((ROOT / "tests" / "fixtures").glob("board-v*.json"))


class OldBoards(unittest.TestCase):
    def board(self, fixture):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ".barid" / "board.json"
        path.parent.mkdir()
        shutil.copy(fixture, path)
        return path

    def run_cli(self, path, *args):
        return subprocess.run([sys.executable, str(RB_PATH), "--board", str(path), "--human", *args], capture_output=True, text=True, encoding="utf-8")

    def test_there_are_fixtures_for_every_release(self):
        self.assertGreaterEqual(len(FIXTURES), 5)

    def test_every_old_board_opens_plans_and_exports(self):
        for fx in FIXTURES:
            with self.subTest(fx.name):
                path = self.board(fx)
                view = rb.board_view(path)
                self.assertEqual(len(view["items"]), 9)
                self.assertTrue(view["steps"])
                self.assertTrue(view["focus"])
                json.dumps(view, default=str)
                for cmd in (("plan",), ("list", "--all"), ("lint",), ("doctor",), ("export", str(path.parent / "snap.html"))):
                    r = self.run_cli(path, *cmd)
                    self.assertEqual(r.returncode, 0, f"{cmd}: {r.stderr}")
                self.assertTrue((path.parent / "snap.html").stat().st_size > 10000)

    def test_old_boards_keep_working_for_the_person_and_for_agents(self):
        for fx in FIXTURES:
            with self.subTest(fx.name):
                path = self.board(fx)
                self.assertEqual(self.run_cli(path, "accept", "T3").returncode, 0)  # the finished (partial) report of the fixture
                self.assertEqual(self.run_cli(path, "edit", "T2", "--profile", "light", "--uses", "").returncode, 0)
                self.assertEqual(self.run_cli(path, "claim", "T1", "--by", "w", "--force").returncode, 0)
                self.assertEqual(self.run_cli(path, "finish", "T1", "--report", "r.md", "--outcome", "complete", "--by", "w").returncode, 0)
                self.assertEqual(self.run_cli(path, "add", "N1", "--lane", "qa", "--text", "new work", "--profile", "dev").returncode, 0)
                self.assertEqual(self.run_cli(path, "explain", "T2", "N1").returncode, 0)
                d = rb.load(path)
                self.assertTrue(all("status" in i for i in d["items"]))

    def test_the_old_board_file_is_not_rewritten_by_reading(self):
        for fx in FIXTURES:
            with self.subTest(fx.name):
                path = self.board(fx)
                before = path.read_bytes()
                rb.board_view(path)
                self.run_cli(path, "plan")
                self.run_cli(path, "lint")
                self.assertEqual(path.read_bytes(), before, "reading must not change the file")


if __name__ == "__main__":
    unittest.main()
