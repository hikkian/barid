"""The 1.0 contract (docs/COMPATIBILITY.md): what a script, an agent prompt or an old board may rely on, and the budgets that keep Barid cheap.

Everything here may GROW in a 1.x release (a new command, a new field in the output, a new reason code is allowed to be added with a note in the
changelog); nothing listed here may disappear or change its meaning. If one of these tests has to change, that is a 2.0.
"""
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RB_PATH = ROOT / "skills" / "barid" / "scripts" / "barid.py"
spec = importlib.util.spec_from_file_location("rb_contract", RB_PATH)
rb = importlib.util.module_from_spec(spec)
sys.modules["rb_contract"] = rb
spec.loader.exec_module(rb)

COMMANDS = ["accept", "accept-clean", "add", "approve", "cancel", "check", "claim", "connect", "context", "digest", "doctor", "edit", "explain", "export", "finish", "gen",
            "heartbeat", "init", "lane", "lang", "lint", "list", "log", "move", "next", "note", "open", "outcome", "overview", "plan", "policy", "profile", "protect", "purge",
            "reject", "release", "resource", "restore", "sent", "serve", "shim", "show", "status", "template", "trust", "where", "worker", "worktree"]
LIST_ITEM_KEYS = {"after", "begin_in", "branch", "can_run_with", "cannot_run_with", "claim", "conflicts", "created", "created_by", "described", "effective", "id",
                  "lane", "lint", "lint_ok", "needs", "noisy", "not_before", "notes", "outline", "profile", "quiet", "reasons", "report", "stale", "state", "status",
                  "text", "timebox", "title", "touches", "updated", "uses", "waiting_info", "waiting_on", "when", "workdir"}
NEXT_KEYS = {"id", "reason", "retry_in", "status", "waiting"}
DIGEST_KEYS = {"ended", "needs_you", "next_ready", "now", "running", "sessions", "since", "waiting"}
ITEM_FILE_KEYS = {"after", "branch", "claim", "created", "created_by", "id", "lane", "lint_ok", "needs", "noisy", "not_before", "notes", "outline", "profile",
                  "quiet", "report", "status", "text", "timebox", "title", "touches", "updated", "uses", "when", "workdir"}
BOARD_KEYS = {"context", "created", "events", "items", "lanes", "lang", "policy", "project", "protect", "resources", "rev", "schema", "updated"}


def make_board(tmp, tasks=0, seed=1):
    path = Path(tmp) / ".barid" / "board.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    d = rb.new_board("contract", [("main", "Main"), ("b", "B")])
    rng, now = random.Random(seed), rb.now_iso()
    for k in range(tasks):
        d["items"].append({"id": f"T{k}", "title": f"task {k}", "text": "x" * 300, "status": "queued", "lane": rng.choice(["main", "b"]),
                           "needs": [f"T{rng.randrange(k)}"] if k and rng.random() < 0.3 else [], "after": [], "uses": [], "notes": [], "profile": "",
                           "touches": [f"src/f{rng.randrange(40)}.py"], "created": now, "updated": now})
    rb.save(path, d)
    return path


def cli(board, *args, timeout=60):
    t = time.perf_counter()
    p = subprocess.run([sys.executable, str(RB_PATH), "--board", str(board), *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return p.returncode, p.stdout, p.stderr, time.perf_counter() - t


class Frozen(unittest.TestCase):
    def test_the_board_format_and_the_words_in_it(self):
        self.assertEqual(rb.SCHEMA, 1)
        self.assertEqual(rb.STATUSES, ("draft", "proposed", "queued", "sent", "running", "review", "done", "cancelled"))
        self.assertEqual(rb.OUTCOMES, ("complete", "partial", "failed"))
        self.assertEqual(rb.REASONS[:5], ("begin_time", "dependency", "conflict", "decision", "never"))

    def test_exit_codes(self):
        self.assertEqual((rb.EXIT_NONE, rb.EXIT_WAIT, rb.EXIT_STUCK), (2, 3, 4))
        self.assertEqual((rb.NEXT_TASK, rb.NEXT_NONE, rb.NEXT_WAIT, rb.NEXT_STUCK), ("task", "none", "wait", "stuck"))

    def test_no_command_has_disappeared(self):
        par = rb.build_parser()
        have = set(next(a for a in par._actions if a.__class__.__name__ == "_SubParsersAction").choices)
        self.assertEqual(set(COMMANDS) - have, set())

    def test_a_new_board_and_a_new_task_have_every_field_a_reader_relies_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_board(tmp)
            rb.mutate(path, lambda d: rb.op_add(d, rb.HUMAN, {"id": "A", "text": "x", "lane": "main"}))
            d = rb.load(path)
            self.assertEqual(BOARD_KEYS - set(k for k in d if not k.startswith("_")), set())
            self.assertEqual(ITEM_FILE_KEYS - set(d["items"][0]), set())

    def test_json_outputs_keep_their_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_board(tmp)
            rb.mutate(path, lambda d: rb.op_add(d, rb.HUMAN, {"id": "A", "text": "x", "lane": "main", "profile": "light"}))
            rb.mutate(path, lambda d: rb.op_add(d, rb.HUMAN, {"id": "B", "text": "x", "lane": "b", "profile": "light", "needs": ["A"]}))
            self.assertEqual(LIST_ITEM_KEYS - set(json.loads(cli(path, "--json", "list")[1])[0]), set())
            nxt = json.loads(cli(path, "--json", "next", "--lane", "b")[1])
            self.assertEqual(NEXT_KEYS - set(nxt), set())
            self.assertEqual({"id", "lane", "reasons", "title"} - set(nxt["waiting"][0]), set())
            self.assertEqual(DIGEST_KEYS - set(json.loads(cli(path, "--json", "digest")[1])), set())

    def test_exit_codes_of_next_and_claim_through_the_real_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_board(tmp)
            self.assertEqual(cli(path, "next", "--lane", "main")[0], 2)                  # nothing queued
            rb.mutate(path, lambda d: rb.op_add(d, rb.HUMAN, {"id": "A", "text": "x", "lane": "main", "profile": "light"}))
            rb.mutate(path, lambda d: rb.op_add(d, rb.HUMAN, {"id": "B", "text": "x", "lane": "b", "profile": "light", "needs": ["A"]}))
            self.assertEqual(cli(path, "next", "--lane", "main")[0], 0)                  # a task
            self.assertEqual(cli(path, "next", "--lane", "b")[0], 3)                     # will start by itself
            self.assertEqual(cli(path, "--by", "w", "claim", "B")[0], 3)
            rb.mutate(path, lambda d: rb.op_cancel(d, rb.HUMAN, "A"))
            self.assertEqual(cli(path, "next", "--lane", "b")[0], 4)                     # needs the person
            self.assertEqual(cli(path, "--by", "w", "claim", "B")[0], 4)


class Release(unittest.TestCase):
    """What a release ships must be well formed: 0.6.0 went out with an empty plugin.json."""

    def test_the_plugin_manifests_are_valid_and_carry_the_version_of_the_program(self):
        for name in ("plugin.json", "marketplace.json"):
            f = ROOT / ".claude-plugin" / name
            self.assertGreater(f.stat().st_size, 20, f"{name} is empty")
            self.assertIsInstance(json.loads(f.read_text("utf-8")), dict)
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text("utf-8"))
        self.assertEqual(manifest["version"], rb.__version__)
        self.assertEqual(manifest["name"], "barid")

    def test_the_changelog_has_an_entry_for_this_version(self):
        text = (ROOT / "CHANGELOG.md").read_text("utf-8")
        self.assertIn("## " + rb.__version__.split("-")[0], text)

    def test_the_documents_exist_in_english_and_russian_and_the_kazakh_drafts_stay_unlinked(self):
        for f in ("README.md", "README.ru.md", "docs/COMPATIBILITY.md", "docs/COMPATIBILITY.ru.md", "docs/drafts/README.kk.md", "docs/drafts/COMPATIBILITY.kk.md"):
            self.assertGreater((ROOT / f).stat().st_size, 1000, f)


class Budgets(unittest.TestCase):
    """What an agent reads costs tokens, and what a command costs is time and battery. Ceilings are about 20% above what is measured now."""

    def test_the_texts_an_agent_reads_stay_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_board(tmp)
            for lang in ("en", "ru", "kk"):
                rb.mutate(path, lambda d: rb.op_board_lang(d, rb.HUMAN, lang))
                d = rb.load(path)
                self.assertLess(len(rb.connect_text(d, "main", path, False)), 1200, lang)
                self.assertLess(len(rb.connect_text(d, "main", path, True)), 1700, lang)
                rb.mutate(path, lambda dd: rb.op_add(dd, rb.HUMAN, {"id": "A", "text": "do it", "lane": "main"}))
                d = rb.load(path)
                self.assertLess(len(rb.prompt_for(d, rb.get_item(d, "A"), path)) - len("do it"), 1850, lang)       # the footer every prompt carries
                rb.mutate(path, lambda dd: dd["items"].clear())

    def test_a_wait_answer_is_a_few_lines(self):
        res = {"status": "wait", "waiting": [{"id": f"T{k}", "reasons": [{"code": "begin_time", "at": "2030-01-01T05:00:00+00:00", "in": 1000}]} for k in range(20)]}
        self.assertLessEqual(len(rb.describe_next(res).splitlines()), 8)             # never a page, however many tasks wait

    def test_commands_stay_quick_on_a_board_of_100_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_board(tmp, 100)
            for args in (("list",), ("plan",), ("next", "--lane", "main"), ("digest",), ("overview",), ("lint",)):
                t = min(cli(path, *args)[3] for _ in range(2))
                self.assertLess(t, 6.0, f"{' '.join(args)} took {t:.1f}s")        # about 0.15 s here (Python start included); the margin is for a slow CI machine

    def test_a_board_of_500_tasks_is_still_worked_out_in_a_fraction_of_a_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = rb.load(make_board(tmp, 500))
            t = time.perf_counter()
            rb.compute(d)
            self.assertLess(time.perf_counter() - t, 2.0)


@unittest.skipUnless(os.name == "posix", "measures the disk write volume through /proc")
class WriteVolume(unittest.TestCase):
    def test_a_board_write_is_one_file_the_size_of_the_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_board(tmp, 100)
            size = path.stat().st_size
            rb.mutate(path, lambda d: rb.op_note(d, rb.Actor("agent", "w"), "T1", "hello"))
            leftovers = [p.name for p in path.parent.iterdir()]
            self.assertEqual(sorted(leftovers), ["board.json", "board.json.bak", "board.json.lock"])     # no temp file is left behind
            self.assertLess(path.stat().st_size, size + 2000)


if __name__ == "__main__":
    unittest.main()
