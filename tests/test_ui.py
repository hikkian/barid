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

    def test_language_dropdown_lists_two_languages_and_switches(self):
        self.b.click("#langbtn")
        self.b.wait("!document.getElementById('langpop').hidden", what="the language list")
        names = self.b.exec("return [].map.call(document.querySelectorAll('#langpop button'), function (x) { return x.textContent; });")
        self.assertEqual(len(names), 2)
        self.assertNotIn("Қазақша", " ".join(names))      # Kazakh is not offered until a native reader has reviewed it
        self.b.click_text("#langpop button", "Русский")
        self.b.wait("document.querySelector('#h-now').textContent.indexOf('Что делать') >= 0", what="Russian headings")
        self.assertTrue(self.b.exec("return document.getElementById('langpop').hidden;"))
        self.assertIn("Русский", self.text("#langbtn"))
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

    def test_the_away_card_lists_only_what_the_person_has_not_seen(self):
        for iid in ("C1", "C2", "C3"):
            barid(self.board, "add", iid, "--lane", "a", "--title", "Quiet " + iid, "--text", "x", "--profile", "light")
        barid(self.board, "claim", "C1", "--by", "w1", "--force")
        barid(self.board, "finish", "C1", "--report", "r1.md", "--outcome", "complete", "--by", "w1")      # finished before the panel was opened
        self.open()
        digest = "document.getElementById('digest').textContent"
        self.b.wait(digest + ".indexOf('C1') >= 0", what="a report that finished while the person was away")
        barid(self.board, "claim", "C2", "--by", "w1", "--force")
        barid(self.board, "finish", "C2", "--report", "r2.md", "--outcome", "complete", "--by", "w1")      # finished while the panel is open
        self.b.wait("document.getElementById('inboxwrap').textContent.indexOf('C2') >= 0", what="C2 in the inbox")
        import time
        time.sleep(18)                                                                                      # longer than the card's refresh interval
        self.assertEqual(self.b.exec("return " + digest + ".indexOf('C2') >= 0;"), False, "a task that finished in front of the person is not news")
        self.assertEqual(self.b.exec("return " + digest + ".indexOf('C1') >= 0;"), True)
        barid(self.board, "--human", "accept", "C1")                                                      # accepted: seen
        self.b.wait("document.getElementById('digest').textContent.indexOf('C1') < 0", timeout=30, what="the accepted report to leave the card")
        self.assertNoJsErrors()

    def test_kazakh_is_not_picked_by_itself_but_still_opens_when_asked(self):
        self.b.exec("localStorage.setItem('rb.lang', 'kk');")                      # an earlier visit remembered Kazakh
        self.b.goto(self.url)
        self.b.wait("document.querySelectorAll('#now .lane-card').length > 0", what="the lane cards")
        self.assertEqual(self.b.exec("return document.documentElement.lang;"), "en")          # the board and the browser say English
        barid(self.board, "lang", "kk")                                            # a board that says kk (an older board)
        self.b.exec("localStorage.removeItem('rb.lang');")
        self.b.goto(self.url)
        self.b.wait("document.querySelectorAll('#now .lane-card').length > 0", what="the lane cards")
        self.assertEqual(self.b.exec("return document.documentElement.lang;"), "ru")
        self.b.goto(self.url + "?lang=kk")                                         # explicit: still available for the drafts
        self.b.wait("document.querySelectorAll('#now .lane-card').length > 0", what="the lane cards")
        self.assertEqual(self.b.exec("return document.documentElement.lang;"), "kk")
        self.assertNoJsErrors()

    def test_an_open_tab_is_told_when_the_panel_file_is_replaced(self):
        panel = ROOT / "skills" / "barid" / "scripts" / "panel.html"
        before = panel.stat()
        self.addCleanup(lambda: os.utime(panel, ns=(before.st_atime_ns, before.st_mtime_ns)))
        self.b.wait("document.getElementById('updatebar').hidden === true", what="no update bar yet")
        import time
        time.sleep(4)                                                      # one poll has recorded the stamp of the page that was loaded
        os.utime(panel, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))      # a new panel.html arrives
        self.b.wait("document.getElementById('updatebar').hidden === false", timeout=15, what="the update bar")
        self.assertIn("Reload", self.text("#updatebar"))
        self.b.click("#reloadbtn")
        self.b.wait("document.getElementById('updatebar').hidden === true", timeout=15, what="the page to reload and the bar to go")
        self.assertNoJsErrors()

    def test_every_card_of_the_order_stays_under_its_own_session_in_a_narrow_window(self):
        barid(self.board, "lane", "add", "c", "--title", "Third agent")
        for iid, lane in (("C1", "c"), ("C2", "c"), ("B2", "b"), ("A2", "a")):
            barid(self.board, "add", iid, "--lane", lane, "--title", "A rather long title of " + iid, "--text", "x", "--profile", "light")
        self.b.cmd("WebDriver:SetWindowRect", {"width": 900, "height": 1000})
        self.addCleanup(lambda: self.b.cmd("WebDriver:SetWindowRect", {"width": 1280, "height": 900}))
        self.open()
        self.b.wait("document.querySelectorAll('#flow .lanehead').length === 3", what="a header for every session")
        result = self.b.exec("""var heads = {};
            [].forEach.call(document.querySelectorAll('#flow .lanehead'), function (e) { heads[e.dataset.lane] = Math.round(e.getBoundingClientRect().left); });
            var wrong = [], cards = document.querySelectorAll('#flow .step .item');
            [].forEach.call(cards, function (e) { if (Math.abs(Math.round(e.getBoundingClientRect().left) - heads[e.dataset.lane]) > 1) wrong.push(e.querySelector('.id').textContent); });
            return JSON.stringify({heads: Object.keys(heads).length, cards: cards.length, wrong: wrong, pageOverflow: document.documentElement.scrollWidth - window.innerWidth});""")
        data = json.loads(result)
        self.assertEqual(data["heads"], 3)
        self.assertGreaterEqual(data["cards"], 6)
        self.assertEqual(data["wrong"], [], "cards that are not under the header of their session")
        self.assertLessEqual(data["pageOverflow"], 0)
        self.assertNoJsErrors()

    def row_geometry(self, frame=None):
        """The session rows of the What-to-do-now area, in the window or in a frame of the page (a frame is how a phone width is tested here)."""
        scope = (f"var f = document.getElementById('{frame}'), d = f.contentDocument, w = f.contentWindow;" if frame else "var d = document, w = window;")
        return json.loads(self.b.exec(scope + """var cards = [].slice.call(d.querySelectorAll('#now .lane-card'));
            return JSON.stringify({width: w.innerWidth, rows: cards.map(function (c) { var r = c.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)]; }),
                withState: cards.every(function (c) { return !!c.querySelector('.chip, .empty'); }), overflow: d.documentElement.scrollWidth - w.innerWidth});"""))

    def assertOneRowPerSession(self, geo):
        width, rows = geo["width"], geo["rows"]
        self.assertEqual(len(rows), 5, f"at {width} px")
        self.assertEqual(len({r[0] for r in rows}), 1, f"at {width} px the sessions must start at one left edge, one row each")
        for above, below in zip(rows, rows[1:]):
            self.assertGreaterEqual(below[1], above[3] - 1, f"at {width} px the rows must not overlap")
        for r in rows:
            self.assertLessEqual(r[2], width, f"at {width} px a row runs past the window")
        self.assertTrue(geo["withState"], f"at {width} px every row must say what its session is doing")
        self.assertLessEqual(geo["overflow"], 0, f"at {width} px the page scrolls sideways")

    def test_each_session_is_one_full_width_row_in_what_to_do_now(self):
        for lid, title in (("c", "Third agent"), ("d", "Fourth agent"), ("e", "Fifth agent")):
            barid(self.board, "lane", "add", lid, "--title", title)
        barid(self.board, "add", "C1", "--lane", "c", "--title", "A rather long title of C1", "--text", "x", "--profile", "light")
        self.addCleanup(lambda: self.b.cmd("WebDriver:SetWindowRect", {"width": 1280, "height": 900}))
        self.open()
        self.b.wait("document.querySelectorAll('#now .lane-card').length === 5", what="a card for every session")
        for width in (1280, 950):
            self.b.cmd("WebDriver:SetWindowRect", {"width": width, "height": 900})
            self.assertOneRowPerSession(self.row_geometry())
        # Firefox will not make the window narrower than 500 px here, so a phone width is a 390 px frame of the same page
        self.b.exec("var f = document.createElement('iframe'); f.id = 'phone'; f.style.cssText = 'width:390px;height:800px;border:0'; f.src = arguments[0]; document.body.appendChild(f); return 1;", self.url + "?lang=en")
        self.b.wait("(function () { var f = document.getElementById('phone'); return !!(f && f.contentDocument && f.contentDocument.querySelectorAll('#now .lane-card').length === 5); })()", what="the 390 px frame")
        self.assertOneRowPerSession(self.row_geometry("phone"))
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

    # --- FIXSCN1: keyboard use, two tabs, typed text, a server that is down, a damaged board, search and small polish ---
    TAB, ESC, ENTER = "", "", ""

    def press(self, key, times=1):
        for _ in range(times):
            self.b.cmd("WebDriver:PerformActions", {"actions": [{"type": "key", "id": "kbd", "actions": [{"type": "keyDown", "value": key}, {"type": "keyUp", "value": key}]}]})

    def type_nth(self, css, index, text):
        """Type into the index-th element matching css, with key presses (the field is clicked first, as a person would)."""
        self.b.exec("var el = document.querySelectorAll(arguments[0])[arguments[1]]; el.setAttribute('data-ui-target', '1');", css, index)
        try:
            self.b.click('[data-ui-target="1"]')
            self.b.type('[data-ui-target="1"]', text)
        finally:
            self.b.exec("var t = document.querySelector('[data-ui-target]'); if (t) t.removeAttribute('data-ui-target');")

    def active(self):
        return self.b.exec("var e = document.activeElement; return e ? (e.id ? '#' + e.id : e.tagName.toLowerCase()) : 'none';")

    def edit_task(self, title_of, new_title):
        """Open a task's drawer, press Edit and put new_title into the title field (the form is saved by the caller)."""
        self.b.click_text("#flow .item", title_of)
        self.b.wait("document.getElementById('drawer').textContent.indexOf('Profile') >= 0", what="the drawer")
        self.b.click_text("#drawer button", "Edit")
        self.b.wait("document.querySelector('#drawer .formgrid')", what="the edit form")
        self.b.exec("var i = document.querySelectorAll('#drawer .formgrid input[type=text]'); i[1].value = arguments[0];", new_title)

    def board_title(self, iid):
        return next(i["title"] for i in json.loads(self.board.read_text("utf-8"))["items"] if i["id"] == iid)

    def install_toast_log(self):
        self.b.exec("window.__toastlog = []; var t = document.getElementById('toast');"
                    "new MutationObserver(function () { window.__toastlog.push(t.className + ' ' + t.textContent); })"
                    ".observe(t, {childList: true, characterData: true, subtree: true, attributes: true});")

    def test_the_new_task_dialog_takes_focus_and_one_tab_reaches_its_first_field(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        self.assertTrue(self.b.exec("return document.getElementById('modalbox').contains(document.activeElement);"), "focus must move into the dialog when it opens")
        self.press(self.TAB)
        self.assertTrue(self.b.exec("return document.querySelector('#modalbox input[type=text]') === document.activeElement;"), "one Tab must reach the ID field")
        self.assertNoJsErrors()

    def test_tab_stays_inside_an_open_dialog(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        for _ in range(30):
            self.press(self.TAB)
            self.assertTrue(self.b.exec("return document.getElementById('modalbox').contains(document.activeElement);"), f"focus left the dialog at {self.active()}")

    def test_the_dialog_is_labelled_as_a_modal_dialog(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        attrs = self.b.exec("var m = document.getElementById('modalbox'); var l = document.getElementById(m.getAttribute('aria-labelledby') || ''); return [m.getAttribute('role'), m.getAttribute('aria-modal'), l ? l.textContent : ''];")
        self.assertEqual(attrs[0], "dialog")
        self.assertEqual(attrs[1], "true")
        self.assertIn("New task", attrs[2])

    def test_the_task_drawer_is_a_dialog_and_keeps_focus_inside(self):
        self.b.click_text("#flow .item", "First")
        self.b.wait("document.getElementById('drawer').classList.contains('open')", what="the drawer")
        self.assertEqual(self.b.exec("return document.getElementById('drawer').getAttribute('role');"), "dialog")
        for _ in range(12):
            self.press(self.TAB)
            self.assertTrue(self.b.exec("return document.getElementById('drawer').contains(document.activeElement);"), f"focus left the drawer at {self.active()}")

    def test_escape_returns_focus_to_the_button_that_opened_the_dialog(self):
        self.b.exec("document.getElementById('addbtn').focus();")
        self.press(self.ENTER)
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        self.press(self.TAB)
        self.press(self.ESC)
        self.b.wait("!document.getElementById('modal').classList.contains('open')", what="the dialog to close")
        self.assertEqual(self.active(), "#addbtn")

    def test_two_tabs_editing_the_same_task_do_not_lose_the_first_save(self):
        """Finding 2: tab 1 opens the edit form, tab 2 saves first, then tab 1 saves. Tab 1 is refused and keeps its text; tab 2's save stays."""
        first = self.b.cmd("WebDriver:GetWindowHandle")["value"]
        second = self.b.cmd("WebDriver:NewWindow", {"type": "tab"})["handle"]
        self.addCleanup(lambda: (self.b.cmd("WebDriver:SwitchToWindow", {"handle": second, "focus": True}), self.b.cmd("WebDriver:CloseWindow", {}), self.b.cmd("WebDriver:SwitchToWindow", {"handle": first, "focus": True})))
        self.edit_task("Second", "EDIT ONE")
        self.b.cmd("WebDriver:SwitchToWindow", {"handle": second, "focus": True})
        self.open()
        self.edit_task("Second", "EDIT TWO")
        self.b.click_text("#drawer button", "Save")
        import time
        for _ in range(50):
            if self.board_title("T2") == "EDIT TWO":
                break
            time.sleep(0.1)
        self.assertEqual(self.board_title("T2"), "EDIT TWO")
        self.b.cmd("WebDriver:SwitchToWindow", {"handle": first, "focus": True})
        self.b.click_text("#drawer button", "Save")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('changed by someone else') >= 0", what="the refusal in tab 1")
        self.assertEqual(self.b.exec("return document.querySelectorAll('#drawer .formgrid input[type=text]')[1].value;"), "EDIT ONE", "tab 1 keeps its text")
        self.assertEqual(self.board_title("T2"), "EDIT TWO", "tab 2's save is not lost")
        self.b.click_text("#drawer button", "Show the current version")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('EDIT TWO') >= 0", what="the current version")
        self.b.click_text("#drawer button", "Save")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('changed by someone else') < 0", what="the save after looking at the current version")
        self.assertEqual(self.board_title("T2"), "EDIT ONE")
        self.assertNoJsErrors()

    def test_switching_the_language_keeps_what_was_typed_in_the_edit_form(self):
        self.edit_task("Second", "KEEP ME")
        # the open drawer covers the header buttons, so the choice is made from script: the same click handlers run
        self.b.exec("document.getElementById('langbtn').click();")
        self.b.wait("!document.getElementById('langpop').hidden", what="the language list")
        self.b.exec("[].filter.call(document.querySelectorAll('#langpop button'), function (x) { return x.textContent.indexOf('Русский') >= 0; })[0].click();")
        self.b.wait("document.querySelector('#h-now').textContent.indexOf('Что делать') >= 0", what="Russian headings")
        self.assertEqual(self.b.exec("return document.querySelectorAll('#drawer .formgrid input[type=text]')[1].value;"), "KEEP ME")

    def test_escape_asks_before_throwing_away_typed_text(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        self.type_nth("#modalbox input[type=text]", 1, "unsaved words")
        self.press(self.ESC)
        self.assertTrue(self.b.exec("return document.getElementById('modal').classList.contains('open');"), "Escape must not close a dialog with typed text")
        self.assertIn("Discard what you typed?", self.text("#modalbox"))
        self.b.click_text("#modalbox button", "Keep editing")
        self.assertNotIn("Discard what you typed?", self.text("#modalbox"))
        self.assertEqual(self.b.exec("return document.querySelectorAll('#modalbox input[type=text]')[1].value;"), "unsaved words")
        self.press(self.ESC)
        self.b.click_text("#modalbox button", "Discard")
        self.b.wait("!document.getElementById('modal').classList.contains('open')", what="the dialog to close after Discard")
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form again")
        self.press(self.ESC)
        self.b.wait("!document.getElementById('modal').classList.contains('open')", what="an empty dialog to close at once")
        self.edit_task("Second", "changed in the drawer")
        self.press(self.ESC)
        self.assertIn("Discard what you typed?", self.text("#drawer"))
        self.assertNoJsErrors()

    def test_saving_while_the_server_is_down_says_so_plainly_and_keeps_the_text(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        self.type_nth("#modalbox input[type=text]", 0, "OFF1")
        self.type_nth("#modalbox input[type=text]", 1, "typed while offline")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.b.click_text("#modalbox button", "Create")
        self.b.wait("document.getElementById('toast').textContent.indexOf('No connection') >= 0", what="the plain message")
        self.assertNotIn("NetworkError", self.b.exec("return document.getElementById('toast').textContent;"))
        self.assertTrue(self.b.exec("return document.getElementById('modal').classList.contains('open');"))
        self.assertEqual(self.b.exec("return document.querySelectorAll('#modalbox input[type=text]')[1].value;"), "typed while offline")

    def test_a_damaged_board_file_is_named_as_such_and_the_cards_are_marked_stale(self):
        good = self.board.read_text("utf-8")
        self.addCleanup(self.board.write_text, good, "utf-8")
        self.board.write_text("", "utf-8")
        self.b.wait("!document.getElementById('banner').hidden && document.getElementById('banner').textContent.indexOf('damaged') >= 0", timeout=15, what="the damaged-file banner")
        self.assertNotIn("No connection", self.text("#banner"))
        self.assertTrue(self.b.exec("return document.body.classList.contains('stale');"), "the cards on screen must be marked as possibly out of date")
        self.board.write_text(good, "utf-8")
        self.b.wait("document.getElementById('banner').hidden === true && !document.body.classList.contains('stale')", timeout=15, what="the page to recover")
        self.assertNoJsErrors()

    def test_a_search_with_no_match_says_nothing_found(self):
        self.b.type("#q", "zzqx")
        self.b.wait("document.getElementById('flow').textContent.indexOf('Nothing found') >= 0", what="the no-match message")
        self.assertNotIn("No tasks yet", self.text("#flow"))

    def test_enter_in_the_title_of_the_new_task_dialog_creates_the_task(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        self.type_nth("#modalbox input[type=text]", 0, "ENT1")
        self.type_nth("#modalbox input[type=text]", 1, "Created with Enter")
        self.press(self.ENTER)
        self.b.wait("!document.getElementById('modal').classList.contains('open')", what="the dialog to close after Enter")
        self.assertIn("Created with Enter", barid(self.board, "list"))

    def test_the_id_field_is_marked_required_and_its_placeholder_is_only_a_hint(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        info = self.b.exec("var i = document.querySelectorAll('#modalbox input[type=text]')[0]; return [i.required || i.getAttribute('aria-required') === 'true', i.placeholder];")
        self.assertTrue(info[0], "the ID field must be marked as required")
        self.assertNotEqual(info[1], "T1", "the placeholder must not look like a default value")
        self.assertIn("32", self.text("#modalbox"), "a hint must say what an ID may contain")

    def test_a_session_with_the_name_of_another_one_is_refused(self):
        import time
        self.b.click(".pill.add")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the new session dialog")
        self.b.type("#modalbox input[type=text]", "  docs AGENT ")
        self.b.click_text("#modalbox button", "Create session")
        time.sleep(1.5)
        self.assertIn("already exists", self.text("#modalbox") + self.text("#toast"))
        self.assertEqual(len(json.loads(self.board.read_text("utf-8"))["lanes"]), 2)

    def test_the_session_filter_is_remembered_after_a_reload(self):
        self.b.click_text("#lanefilters button", "Docs agent")
        off = "[].filter.call(document.querySelectorAll('#lanefilters button'), function (x) { return x.textContent.indexOf('Docs agent') >= 0; })[0].className.indexOf('on') < 0"
        self.b.wait(off, what="the session switched off")
        self.open()
        self.b.wait("document.querySelectorAll('#now .lane-card').length === 1", what="one session on the page after the reload")
        self.assertTrue(self.b.exec("return " + off + ";"), "the switched-off session must stay off after a reload")

    def test_the_search_also_finds_the_tasks_of_a_session_by_its_name(self):
        self.b.type("#q", "Docs agent")
        self.b.wait("document.getElementById('flow').textContent.indexOf('Second') >= 0", what="the task of the Docs session")
        self.assertNotIn("First", self.text("#flow"))

    def test_search_treats_e_and_yo_alike_and_ignores_case(self):
        barid(self.board, "add", "YO1", "--lane", "a", "--title", "Ёжик тест", "--text", "x")
        barid(self.board, "add", "YO2", "--lane", "a", "--title", "Ежик два", "--text", "x")
        self.open()
        for query in ("ежик", "ЁЖИК", "ёжик"):
            self.b.exec("var q = document.getElementById('q'); q.value = arguments[0]; q.dispatchEvent(new Event('input'));", query)
            self.b.wait("document.getElementById('flow').textContent.indexOf('Ёжик тест') >= 0 && document.getElementById('flow').textContent.indexOf('Ежик два') >= 0", what=f"both titles for {query!r}")

    def test_a_second_click_on_mark_as_sent_is_ignored_while_the_first_one_works(self):
        import time
        self.install_toast_log()
        self.b.exec("var btn = [].slice.call(document.querySelectorAll('#now .lane-card button')).filter(function (x) { return x.textContent.indexOf('Mark as sent') >= 0; })[0]; btn.click(); btn.click();")
        time.sleep(3)
        self.assertNotIn("not possible", self.b.exec("return window.__toastlog.join(' | ');"))
        self.assertIn("sent", barid(self.board, "list", "--all"))

    def test_a_second_click_on_accept_report_is_ignored_while_the_first_one_works(self):
        import time
        barid(self.board, "claim", "T1", "--by", "w1", "--force")
        barid(self.board, "finish", "T1", "--report", "r.md", "--outcome", "complete", "--by", "w1")
        self.b.wait("document.getElementById('inboxwrap').textContent.indexOf('T1') >= 0", what="the review inbox")
        self.install_toast_log()
        self.b.exec("var btn = [].slice.call(document.querySelectorAll('#inboxwrap button')).filter(function (x) { return x.textContent.indexOf('Accept report') >= 0; })[0]; btn.click(); btn.click();")
        time.sleep(3)
        self.assertNotIn("not possible", self.b.exec("return window.__toastlog.join(' | ');"))
        self.assertIn("done", barid(self.board, "list", "--all"))

    # --- FIXSCN2: every way of closing a dialog or the drawer asks first when something was typed ---

    def test_the_drawer_close_button_asks_before_dropping_typed_text(self):
        """(1) with typed text ✕ asks and keeps the form, (2) Keep editing keeps everything, (3) ✕ then Discard closes without saving,
        (4) with nothing changed ✕ closes at once."""
        self.edit_task("Second", "CHANGED BY TYPING")
        self.b.click_text("#drawer button", "✕")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('Discard what you typed?') >= 0", what="the question before the drawer closes")
        self.assertTrue(self.b.exec("return document.getElementById('drawer').classList.contains('open');"), "the drawer must stay open while the question is shown")
        self.assertEqual(self.b.exec("return document.querySelectorAll('#drawer .formgrid input[type=text]')[1].value;"), "CHANGED BY TYPING")
        self.b.click_text("#drawer button", "Keep editing")
        self.assertNotIn("Discard what you typed?", self.text("#drawer"))
        self.assertTrue(self.b.exec("return document.getElementById('drawer').classList.contains('open');"), "Keep editing keeps the drawer open")
        self.assertEqual(self.b.exec("return document.querySelectorAll('#drawer .formgrid input[type=text]')[1].value;"), "CHANGED BY TYPING")
        self.b.click_text("#drawer button", "✕")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('Discard what you typed?') >= 0", what="the question again")
        self.b.click_text("#drawer button", "Discard")
        self.b.wait("!document.getElementById('drawer').classList.contains('open')", what="the drawer to close after Discard")
        self.assertEqual(self.board_title("T2"), "Second", "Discard must not save the typed text")
        self.b.click_text("#flow .item", "Second")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('Profile') >= 0", what="the drawer")
        self.b.click_text("#drawer button", "Edit")
        self.b.wait("document.querySelector('#drawer .formgrid')", what="the edit form")
        self.b.click_text("#drawer button", "✕")
        self.b.wait("!document.getElementById('drawer').classList.contains('open')", what="an unchanged drawer to close at once")
        self.assertTrue(self.b.exec("return document.getElementById('discardask') === null;"), "nothing was typed: no question")
        self.assertNoJsErrors()

    def test_the_close_button_of_the_new_task_dialog_asks_before_dropping_typed_text(self):
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form")
        self.type_nth("#modalbox input[type=text]", 1, "typed then closed")
        self.b.click_text("#modalbox button", "Close")
        self.b.wait("document.getElementById('modalbox').textContent.indexOf('Discard what you typed?') >= 0", what="the question before the dialog closes")
        self.assertTrue(self.b.exec("return document.getElementById('modal').classList.contains('open');"), "the dialog must stay open while the question is shown")
        self.b.click_text("#modalbox button", "Keep editing")
        self.assertTrue(self.b.exec("return document.getElementById('modal').classList.contains('open');"), "Keep editing keeps the dialog open")
        self.assertEqual(self.b.exec("return document.querySelectorAll('#modalbox input[type=text]')[1].value;"), "typed then closed")
        self.b.click_text("#modalbox button", "Close")
        self.b.wait("document.getElementById('modalbox').textContent.indexOf('Discard what you typed?') >= 0", what="the question again")
        self.b.click_text("#modalbox button", "Discard")
        self.b.wait("!document.getElementById('modal').classList.contains('open')", what="the dialog to close after Discard")
        self.assertNotIn("typed then closed", barid(self.board, "list"), "Discard must not create the task")
        self.b.click("#addbtn")
        self.b.wait("document.getElementById('modal').classList.contains('open')", what="the task form again")
        self.b.click_text("#modalbox button", "Close")
        self.b.wait("!document.getElementById('modal').classList.contains('open')", what="an empty dialog to close at once")
        self.assertTrue(self.b.exec("return document.getElementById('discardask') === null;"), "nothing was typed: no question")
        self.assertNoJsErrors()

    # --- FIXSCN3: clicking another task while an edit has typed text asks first; a flow with every session switched off says so ---

    def test_clicking_another_task_asks_before_dropping_an_unsaved_edit(self):
        """(1) with typed text, clicking another card asks and keeps the form, (2) Keep editing keeps the form and the text,
        (3) a second click on the other card, then Discard, opens the other task; the typed text is not saved."""
        self.edit_task("Second", "TYPED, NOT SAVED")
        self.b.click_text("#flow .item", "First")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('Discard what you typed?') >= 0", what="the question before the other task opens")
        self.assertEqual(self.b.exec("return document.querySelectorAll('#drawer .formgrid input[type=text]')[1].value;"), "TYPED, NOT SAVED")
        self.assertEqual(self.board_title("T2"), "Second")
        self.b.click_text("#drawer button", "Keep editing")
        self.assertNotIn("Discard what you typed?", self.text("#drawer"))
        self.assertEqual(self.b.exec("return document.querySelectorAll('#drawer .formgrid input[type=text]')[1].value;"), "TYPED, NOT SAVED", "Keep editing keeps the form and the text")
        self.b.click_text("#flow .item", "First")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('Discard what you typed?') >= 0", what="the question again")
        self.b.click_text("#drawer button", "Discard")
        self.b.wait("document.getElementById('drawer').textContent.indexOf('First') >= 0 && !document.querySelector('#drawer .formgrid')", what="the other task to open")
        self.assertEqual(self.board_title("T2"), "Second", "Discard must not save the typed text")
        self.assertNoJsErrors()

    def test_with_every_session_switched_off_the_flow_says_so_and_not_that_there_are_no_tasks(self):
        self.b.click_text("#lanefilters button", "Build agent")
        self.b.click_text("#lanefilters button", "Docs agent")
        self.b.wait("document.getElementById('flow').textContent.indexOf('All sessions are switched off') >= 0", what="the message for switched-off sessions")
        self.assertNotIn("No tasks yet", self.text("#flow"), "the message must not say there are no tasks")
        self.b.click_text("#lanefilters button", "Build agent")
        self.b.click_text("#lanefilters button", "Docs agent")
        self.b.wait("document.getElementById('flow').textContent.indexOf('All sessions') < 0", what="the message to go when the sessions are on again")
        self.assertIn("Second", self.text("#flow"))
        barid(self.board, "purge", "T1")
        barid(self.board, "purge", "T2")
        self.open()
        self.b.wait("document.getElementById('flow').textContent.indexOf('No tasks yet') >= 0", what="the message for a board with no tasks")
        self.assertNotIn("All sessions", self.text("#flow"))
        self.assertNoJsErrors()


if __name__ == "__main__":
    unittest.main()
