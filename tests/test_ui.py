"""The panel in a real browser (Firefox headless over Marionette): clicks, popovers, forms, layout. Skipped when Firefox is missing."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ui_driver  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RB = ROOT / "skills" / "barid" / "scripts" / "barid.py"


def barid(board, *args):
    r = subprocess.run([sys.executable, str(RB), "--board", str(board), "--human", *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise AssertionError(f"barid {' '.join(args)}: {r.stderr}")
    return r.stdout


@unittest.skipUnless(shutil.which("firefox") and os.environ.get("BARID_SKIP_UI") != "1", "needs Firefox; BARID_SKIP_UI=1 skips")
class PanelInBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = ui_driver.Browser()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.board = Path(self.tmp.name) / ".barid" / "board.json"
        self.board.parent.mkdir()
        barid(self.board, "init", "--project", "UI test", "--lanes", "a:Build agent,b:Docs agent", "--lang", "en")
        barid(self.board, "lane", "edit", "a", "--agent", "codex")
        barid(self.board, "add", "T1", "--lane", "a", "--title", "First", "--text", "do it", "--profile", "bench", "--uses", "gpu")
        barid(self.board, "add", "T2", "--lane", "b", "--title", "Second", "--text", "do it too", "--profile", "bench", "--uses", "gpu")
        self.proc = subprocess.Popen([sys.executable, str(RB), "--board", str(self.board), "serve", "--port", "0"], stdout=subprocess.PIPE, text=True)
        box = []
        threading.Thread(target=lambda: box.append(self.proc.stdout.readline()), daemon=True).start()
        for _ in range(300):
            if box:
                break
            import time
            time.sleep(0.1)
        self.assertTrue(box, "the panel server did not start")
        self.url = box[0].split("Barid panel:")[1].split()[0]
        self.addCleanup(self.stop)
        self.b = self.browser
        self.b.exec("try { localStorage.clear(); } catch (e) {}")
        self.open()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    def open(self, hash_=""):
        self.b.goto(self.url + "?lang=en" + ("#" + hash_ if hash_ else ""))
        self.b.wait("document.querySelectorAll('#now .lane-card').length > 0", what="the lane cards")

    def text(self, css):
        return self.b.exec("var e = document.querySelector(arguments[0]); return e ? e.textContent : null;", css)

    def assertNoJsErrors(self):
        self.assertEqual(self.b.errors(), [])

    def test_the_page_renders_cards_and_no_script_errors(self):
        self.assertIn("Build agent", self.text("#now"))
        self.assertIn("Docs agent", self.text("#now"))
        self.assertNoJsErrors()

    def test_two_ready_tasks_that_share_the_gpu_say_so_on_both_cards(self):
        cards = self.b.exec("return [].map.call(document.querySelectorAll('#now .lane-card'), function (c) { return c.textContent; });")
        self.assertEqual(len(cards), 2)
        for c in cards:
            self.assertIn("Do not run together with", c)
            self.assertIn("GPU", c)
        self.assertIn("Start one at a time", self.text("#together"))

    def test_claiming_one_turns_the_other_into_a_wait(self):
        barid(self.board, "claim", "T1", "--by", "w1", "--force")
        self.b.wait("document.querySelector('#now').textContent.indexOf('Wait, conflict') >= 0", what="the wait hint after the claim")
        self.assertIn("Running now: T1", self.text("#together"))

    def test_status_labels_keep_their_colours(self):
        barid(self.board, "claim", "T1", "--by", "w1", "--force")
        self.b.wait("document.querySelector('.chip.s-running')", what="a running label")
        colours = self.b.exec("var run = getComputedStyle(document.querySelector('.chip.s-running')).color;"
                              "var wait = getComputedStyle(document.querySelector('.chip.s-blocked, .chip.s-waiting, .chip.s-ready')).color; return [run, wait];")
        self.assertNotEqual(colours[0], colours[1])
        self.assertNotEqual(colours[0], self.b.exec("return getComputedStyle(document.body).color;"))

    def test_language_dropdown_lists_three_languages_and_switches(self):
        self.b.click("#langbtn")
        self.b.wait("!document.getElementById('langpop').hidden", what="the language list")
        names = self.b.exec("return [].map.call(document.querySelectorAll('#langpop button'), function (x) { return x.textContent; });")
        self.assertEqual(len(names), 3)
        self.b.click_text("#langpop button", "Русский")
        self.b.wait("document.querySelector('#h-now').textContent.indexOf('Что делать') >= 0", what="Russian headings")
        self.assertTrue(self.b.exec("return document.getElementById('langpop').hidden;"))
        self.assertIn("Русский", self.text("#langbtn"))
        self.b.click("#langbtn")
        self.b.click_text("#langpop button", "Қазақша")
        self.b.wait("document.querySelector('#h-now').textContent.indexOf('Қазір') >= 0", what="Kazakh headings")
        self.assertNoJsErrors()

    def test_appearance_menu_stays_open_while_choosing_an_agent(self):
        self.b.click("#themebtn")
        self.b.wait("!document.getElementById('themepop').hidden", what="the appearance menu")
        self.b.click_text("#themepop .agentbtn", "Codex")
        self.b.wait("document.querySelectorAll('#themepop .agchip').length > 0", what="the agent buttons")
        self.assertFalse(self.b.exec("return document.getElementById('themepop').hidden;"))
        self.b.click_text("#themepop .agchip", "Claude Code")
        self.b.wait("[].some.call(document.querySelectorAll('#themepop .agchip'), function (x) { return x.classList.contains('on') && x.textContent.indexOf('Claude') >= 0; })", what="the chosen agent")
        self.assertFalse(self.b.exec("return document.getElementById('themepop').hidden;"))
        self.assertEqual(json.loads(barid(self.board, "lane", "list").split("\n")[0].count("claude") and '1' or '0'), 1)
        self.assertNoJsErrors()

    def test_a_new_session_can_be_created_and_connected_from_the_panel(self):
        self.b.click(".pill.add")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the new session dialog")
        self.b.type("#modalbox input[type=text]", "Tests agent")
        self.b.click_text("#modalbox .agchip", "Codex")
        self.b.click_text("#modalbox button", "Create session")
        self.b.wait("document.querySelector('#modalbox .connectbox') && document.querySelector('#modalbox .connectbox').textContent.indexOf('--lane') >= 0", what="the connection text")
        self.assertIn("Tests agent", self.text("#modalbox"))
        self.assertIn("tests-agent", self.text("#modalbox .connectbox"))
        self.assertIn("tests-agent", barid(self.board, "lane", "list"))
        self.assertNoJsErrors()

    def test_the_connect_button_shows_the_text_for_that_session(self):
        self.b.click_text("#now .lane-card button", "Connect")
        self.b.wait("document.querySelector('#modalbox .connectbox') && document.querySelector('#modalbox .connectbox').textContent.length > 50", what="the connection text")
        t = self.text("#modalbox .connectbox")
        self.assertIn("--lane a", t)
        self.assertIn("next", t)
        self.assertNoJsErrors()

    def test_a_task_can_be_added_with_a_profile_from_the_form(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        fields = self.b.exec("return [].map.call(document.querySelectorAll('#modalbox input[type=text]'), function (x, i) { return i; }).length;")
        self.assertGreaterEqual(fields, 4)
        self.b.type("#modalbox input[type=text]", "T9")
        self.b.exec("var i = document.querySelectorAll('#modalbox input[type=text]'); i[1].value = 'From the form';")
        self.b.exec("var s = document.querySelectorAll('#modalbox select'); for (var k = 0; k < s.length; k++) { for (var o = 0; o < s[k].options.length; o++) if (s[k].options[o].value === 'dev') { s[k].value = 'dev'; } }")
        self.b.exec("document.querySelector('#modalbox textarea').value = 'Write the thing';")
        self.b.click_text("#modalbox button", "Create")
        self.b.wait("document.getElementById('modal').classList.contains('open') === false", what="the form to close")
        out = barid(self.board, "list", "--json")
        t9 = [x for x in json.loads(out) if x["id"] == "T9"][0]
        self.assertEqual(t9["profile"], "dev")
        self.assertEqual(t9["title"], "From the form")
        self.assertNoJsErrors()

    def test_a_finished_report_can_be_accepted(self):
        barid(self.board, "claim", "T1", "--by", "w1", "--force")
        barid(self.board, "finish", "T1", "--report", "r.md", "--outcome", "complete", "--by", "w1")
        self.b.wait("document.getElementById('inboxwrap').textContent.indexOf('T1') >= 0", what="the review inbox")
        self.b.click_text("#inboxwrap button", "Accept report")
        self.b.wait("document.getElementById('inboxwrap').textContent.indexOf('T1') < 0", what="T1 to leave the inbox")
        self.assertIn("done", barid(self.board, "list", "--all"))
        self.assertNoJsErrors()

    def test_clean_reports_are_accepted_together_after_a_second_click_and_the_rest_stays(self):
        for iid in ("C1", "C2", "P1"):
            barid(self.board, "add", iid, "--lane", "a", "--title", "Quiet " + iid, "--text", "x", "--profile", "light")
        for iid, outcome, who in (("C1", "complete", "w1"), ("C2", "complete", "w2"), ("P1", "partial", "w3")):
            barid(self.board, "claim", iid, "--by", who, "--force")
            barid(self.board, "finish", iid, "--report", "r.md", "--outcome", outcome, "--by", who)
        self.b.wait("document.getElementById('acceptall') !== null", what="the accept-all button")
        self.assertIn("Accept 2 clean reports", self.text("#acceptall"))
        self.b.click("#acceptall")
        self.assertIn("Sure? Accept 2", self.text("#acceptall"))        # one click only asks
        self.assertIn("review", barid(self.board, "list", "--all"))
        self.b.click("#acceptall")
        self.b.wait("document.getElementById('acceptall') === null", what="the accept-all button to go")
        listing = barid(self.board, "list", "--all")
        self.assertEqual(sum(1 for ln in listing.splitlines() if ln.startswith(("C1", "C2")) and " done " in ln), 2, listing)
        self.assertTrue(any(ln.startswith("P1") and " review " in ln for ln in listing.splitlines()), listing)    # the partial one still waits for a person
        self.assertNoJsErrors()

    def test_a_reminder_comes_only_when_more_things_wait_and_the_tab_is_in_the_background(self):
        self.b.exec("""window.__notes = [];
            window.Notification = function (title, opts) { window.__notes.push([title, opts && opts.body]); };
            Notification.permission = "granted"; Notification.requestPermission = function () { return Promise.resolve("granted"); };
            document.hasFocus = function () { return false; };
            localStorage.setItem("barid.notify", "1");""")
        barid(self.board, "claim", "T1", "--by", "w1", "--force")
        barid(self.board, "finish", "T1", "--report", "r.md", "--outcome", "complete", "--by", "w1")
        self.b.wait("window.__notes.length === 1", what="one reminder")
        self.assertEqual(self.b.exec("return document.getElementById('notifybtn').hidden;"), False)
        self.assertEqual(self.b.exec("return document.getElementById('notifybtn').getAttribute('aria-pressed');"), "true")
        self.assertEqual(self.b.exec("return window.__notes[0][0];"), "Barid: needs you")
        barid(self.board, "--human", "accept", "T1")                      # fewer things wait: no new reminder
        import time
        time.sleep(4)
        self.assertEqual(self.b.exec("return window.__notes.length;"), 1)
        self.assertNoJsErrors()

    def test_a_measurement_that_ran_next_to_load_is_flagged_in_the_report(self):
        barid(self.board, "add", "L1", "--lane", "b", "--title", "Load", "--text", "x", "--profile", "dev")
        barid(self.board, "claim", "T1", "--by", "w1", "--force")
        barid(self.board, "claim", "L1", "--by", "w2", "--force")
        barid(self.board, "finish", "T1", "--report", "r.md", "--outcome", "complete", "--by", "w1")
        self.b.wait("document.getElementById('inboxwrap').textContent.indexOf('ran next to') >= 0", what="the disturbed-measurement warning")

    def test_the_card_shows_the_task_that_is_first_in_the_plan(self):
        barid(self.board, "claim", "T2", "--by", "w1", "--force")  # a measurement runs in lane b
        barid(self.board, "add", "LATER", "--lane", "a", "--title", "Later draft", "--outline", "only an outline", "--profile", "dev")
        self.b.wait("document.querySelector('#now').textContent.indexOf('Wait, conflict') >= 0", what="the blocked first task")
        card = self.b.exec("return document.querySelector('#now .lane-card').textContent;")
        self.assertIn("First", card)       # T1 comes first in the plan for this session
        self.assertNotIn("Later draft", card)

    def agent(self, *args):
        r = subprocess.run([sys.executable, str(RB), "--board", str(self.board), *args], capture_output=True, text=True, encoding="utf-8")
        return r

    def test_a_waiting_session_shows_what_it_waits_for_and_a_start_time_is_a_hint(self):
        self.agent("claim", "T1", "--by", "w1", "--force")
        self.agent("next", "--lane", "b", "--by", "night-b")             # T2 waits for the GPU: the session reports that it waits
        self.b.wait("document.querySelector('#now').textContent.indexOf('waiting:') >= 0", what="the presence line")
        self.assertIn("T1", self.b.exec("return document.querySelectorAll('#now .lane-card')[1].textContent;"))
        barid(self.board, "edit", "T2", "--not-before", "+3h")
        barid(self.board, "release", "T1")
        self.b.wait("document.querySelector('#now').textContent.indexOf('Starts not before') >= 0 || document.querySelector('#now').textContent.indexOf('not before') >= 0", what="the start time hint")
        self.assertNoJsErrors()

    def test_the_connect_window_has_a_night_mode_that_changes_the_text(self):
        self.b.click_text("#now .lane-card button", "Connect")
        self.b.wait("document.querySelector('#modalbox .connectbox') && document.querySelector('#modalbox .connectbox').textContent.length > 50", what="the connection text")
        self.assertNotIn("--wait 300", self.text("#modalbox .connectbox"))
        self.b.exec("var c = document.querySelector('#modalbox input[type=checkbox]'); c.checked = true; c.dispatchEvent(new Event('change'));")
        self.b.wait("document.querySelector('#modalbox .connectbox').textContent.indexOf('--wait 300') >= 0", what="the night text")
        self.assertIn("exit code 3", self.text("#modalbox .connectbox"))
        self.assertNoJsErrors()

    def test_a_digest_card_tells_what_finished_while_the_person_was_away(self):
        self.agent("claim", "T1", "--by", "w1", "--force")
        self.agent("finish", "T1", "--report", "/tmp/r.md", "--outcome", "partial", "--by", "w1")
        self.b.exec("location.reload();")
        self.b.wait("document.querySelector('#digest') && document.querySelector('#digest').textContent.indexOf('While you were away') >= 0", what="the digest card")
        card = self.text("#digest")
        self.assertIn("T1", card)
        self.assertIn("r.md", card)
        self.assertIn("accept the report of T1", card)
        self.b.click_text("#digest button", "Got it")
        self.b.wait("document.querySelector('#digest').textContent.indexOf('While you were away') < 0", what="the card to go away")
        self.assertIn("T1", self.text("#inbox") if self.b.exec("return !!document.getElementById('inbox');") else self.text("body"))   # the report still waits in its own section
        self.assertNoJsErrors()

    def test_the_edit_form_saves_start_time_limit_and_after(self):
        self.b.click_text("#flow .item", "Second")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('Profile') >= 0", what="the drawer")
        self.b.click_text("#drawer button", "Edit")
        self.b.wait("document.querySelector('#drawer .formgrid')", what="the edit form")
        labels = self.b.exec("return Array.prototype.map.call(document.querySelectorAll('#drawer label'), function (l) { return l.textContent; }).join('|');")
        self.assertIn("Not before", labels)
        self.assertIn("Time limit", labels)
        self.b.exec("""var inputs = document.querySelectorAll('#drawer input[type=text]');
          Array.prototype.forEach.call(inputs, function (i) { var l = i.parentNode.querySelector('label'); if (!l) return;
            if (l.textContent.indexOf('Not before') === 0) i.value = '23:00'; if (l.textContent.indexOf('Time limit') === 0) i.value = '3h'; if (l.textContent.indexOf('After') === 0) i.value = 'T1'; });""")
        self.b.click_text("#drawer button", "Save")
        self.b.wait("true", what="a moment")
        import time
        for _ in range(50):
            d = json.loads(self.board.read_text("utf-8"))
            it = [i for i in d["items"] if i["id"] == "T2"][0]
            if it.get("timebox") == 180:
                break
            time.sleep(0.2)
        self.assertEqual((it["timebox"], it["after"]), (180, ["T1"]))
        self.assertTrue(it["not_before"])
        self.assertNoJsErrors()

    def test_narrow_screen_has_no_horizontal_scroll(self):
        self.b.cmd("WebDriver:SetWindowRect", {"width": 390, "height": 800})
        self.addCleanup(lambda: self.b.cmd("WebDriver:SetWindowRect", {"width": 1280, "height": 900}))
        self.open()
        self.b.click("#themebtn")
        self.b.wait("!document.getElementById('themepop').hidden")
        over = self.b.exec("return document.documentElement.scrollWidth - window.innerWidth;")
        self.assertLessEqual(over, 1)

    def test_clicking_a_task_opens_its_drawer_with_the_profile_and_marks(self):
        self.b.click_text("#flow .item", "First")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('Profile') >= 0", what="the drawer")
        drawer = self.text("#drawer")
        self.assertIn("Profile", drawer)
        self.assertIn("Measurement", drawer)
        self.assertIn("gpu", drawer)
        self.assertNoJsErrors()

    def test_mark_as_sent_changes_the_task(self):
        self.b.click_text("#now .lane-card button", "Mark as sent")
        self.b.wait("document.querySelector('#now').textContent.indexOf('Running') >= 0 || document.querySelector('#now').textContent.indexOf('In progress') >= 0", what="the card to show the task as running")
        self.assertIn("sent", barid(self.board, "list", "--all"))

    def test_a_partial_report_asks_for_a_decision(self):
        barid(self.board, "claim", "T1", "--by", "w1", "--force")
        barid(self.board, "finish", "T1", "--report", "r.md", "--outcome", "partial", "--by", "w1")
        self.b.wait("document.getElementById('inboxwrap').textContent.indexOf('Accept as is') >= 0", what="the partial-report buttons")
        self.assertIn("stopped early", self.text("#inboxwrap"))
        self.b.click_text("#inboxwrap button", "Send back")
        self.b.wait("document.getElementById('inboxwrap').textContent.indexOf('T1') < 0", what="T1 to leave the inbox")
        self.assertEqual([x["status"] for x in json.loads(barid(self.board, "list", "--json")) if x["id"] == "T1"], ["queued"])

    def test_editing_a_task_in_the_drawer_saves(self):
        self.b.click_text("#flow .item", "Second")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('Profile') >= 0", what="the drawer")
        self.b.click_text("#drawer button", "Edit")
        self.b.wait("document.querySelector('#drawer .formgrid')", what="the edit form")
        self.b.exec("var i = document.querySelectorAll('#drawer .formgrid input[type=text]'); i[1].value = 'Renamed in the panel';")
        self.b.click_text("#drawer button", "Save")
        self.b.wait("document.body.textContent.indexOf('Renamed in the panel') >= 0", what="the new title")
        self.assertIn("Renamed in the panel", barid(self.board, "list"))
        self.assertNoJsErrors()

    def test_light_mode_and_a_palette_can_be_chosen(self):
        self.b.click("#themebtn")
        self.b.wait("!document.getElementById('themepop').hidden")
        self.b.click_text("#themepop .seg button", "Light")
        self.b.wait("document.documentElement.dataset.theme === 'light'", what="light theme")
        self.b.click_text("#themepop .pal", "Midnight")
        self.b.wait("document.documentElement.dataset.pal === 'midnight'", what="midnight palette")
        self.assertNoJsErrors()

    def test_a_duplicate_task_id_shows_an_error_message(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')")
        self.b.type("#modalbox input[type=text]", "T1")
        self.b.exec("document.querySelector('#modalbox textarea').value = 'x';")
        self.b.click_text("#modalbox button", "Create")
        self.b.wait("document.body.textContent.indexOf('already exists') >= 0", what="the error toast")
        self.assertTrue(self.b.exec("return document.getElementById('modal').classList.contains('open');"))

    def test_the_panel_can_be_opened_in_kazakh_and_stays_readable(self):
        self.b.goto(self.url + "?lang=kk")
        self.b.wait("document.querySelector('#h-now').textContent.indexOf('Қазір') >= 0", what="Kazakh headings")
        now = self.text("#now")
        self.assertIn("Бірге іске қоспаңыз", now)
        self.assertNoJsErrors()


    def test_a_task_that_declares_nothing_is_shown_as_not_checked(self):
        barid(self.board, "add", "BARE", "--lane", "b", "--title", "Bare", "--text", "x")
        barid(self.board, "claim", "T1", "--by", "w1", "--force")
        barid(self.board, "release", "T1", "--by", "w1")
        barid(self.board, "edit", "T2", "--profile", "")
        barid(self.board, "edit", "T2", "--uses", "")
        self.b.wait("document.querySelector('#now').textContent.indexOf('declares nothing') >= 0", what="the not-checked hint")
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()
