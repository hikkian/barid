"""Scenario sweep of the Barid panel: many boards (1 to 10 sessions, odd texts, every status, big boards) x window widths x themes x languages,
each page audited by a script for what a person would call broken: a page that scrolls sideways, a card or button outside the window, text cut off
in a button or chip, cards on top of each other, a card that is not under its own session. It needs Firefox (the same driver as test_ui.py).

    python3 tests/scenario_sweep.py            # the whole sweep (about 10 minutes), a table of problems, screenshots of the bad pages in /dev/shm/sweep
    python3 tests/scenario_sweep.py --quick    # a small subset (used by tests/test_scenarios.py)

Not named test_*.py: unittest does not pick it up by itself.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RB_PATH = ROOT / "skills" / "barid" / "scripts" / "barid.py"
sys.path.insert(0, str(HERE))
import ui_driver  # noqa: E402

spec = importlib.util.spec_from_file_location("rb_sweep", RB_PATH)
rb = importlib.util.module_from_spec(spec)
sys.modules["rb_sweep"] = rb
spec.loader.exec_module(rb)
AGENT = rb.Actor("agent", "worker")
UNTRUSTED = rb.Actor("agent", "planner")

AUDIT = r"""
var out = [], vw = window.innerWidth;
function name(e) { return (e.id ? "#" + e.id : "") + (e.className && e.className.baseVal === undefined ? "." + String(e.className).trim().split(/\s+/).join(".") : e.tagName.toLowerCase()); }
function scrolls(e) { for (var p = e; p && p !== document.body; p = p.parentElement) { var o = getComputedStyle(p).overflowX; if (o === "auto" || o === "scroll") return true; } return false; }
if (document.documentElement.scrollWidth > vw + 1) out.push(["page-overflow", "scrollWidth " + document.documentElement.scrollWidth + " > " + vw]);
var watch = document.querySelectorAll("main .lane-card, main .item, main .ib, main .together, main .btn, main .pill, main .tag, main .chip, header .pill, header .btn, header input");
[].forEach.call(watch, function (e) {
  var r = e.getBoundingClientRect(); if (!r.width || !r.height || e.closest("[hidden]")) return;
  if (!scrolls(e) && (r.right > vw + 1 || r.left < -1)) out.push(["outside-window", name(e) + " " + Math.round(r.left) + ".." + Math.round(r.right) + " of " + vw]);
});
[].forEach.call(document.querySelectorAll("main .btn, main .pill, main .tag, main .chip, header .btn, header .pill"), function (e) {
  if (e.closest("[hidden]")) return;
  var cs = getComputedStyle(e);
  if ((cs.overflow === "hidden" || cs.overflowX === "hidden") && cs.textOverflow !== "ellipsis" && e.scrollWidth > e.clientWidth + 1) out.push(["text-cut", name(e) + " " + (e.textContent || "").slice(0, 30)]);
});
var cards = [].slice.call(document.querySelectorAll("#now .lane-card"));
for (var i = 0; i < cards.length; i++) for (var j = i + 1; j < cards.length; j++) {
  var a = cards[i].getBoundingClientRect(), b = cards[j].getBoundingClientRect();
  if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) out.push(["cards-overlap", i + " and " + j]);
}
var heads = {}; [].forEach.call(document.querySelectorAll("#flow .lanehead"), function (e) { heads[e.dataset.lane] = Math.round(e.getBoundingClientRect().left); });
if (Object.keys(heads).length && vw > 760) [].forEach.call(document.querySelectorAll("#flow .step .item"), function (e) {
  if (heads[e.dataset.lane] !== undefined && Math.abs(Math.round(e.getBoundingClientRect().left) - heads[e.dataset.lane]) > 1) out.push(["card-not-under-its-session", e.querySelector(".id").textContent]);
});
[].forEach.call(document.querySelectorAll("main .lane-card, main .item"), function (e) { if (e.scrollHeight > e.clientHeight + 2 && getComputedStyle(e).overflowY === "hidden") out.push(["content-cut-vertically", name(e)]); });
return JSON.stringify(out);
"""


# ------------------------------------------------------------------------------------------------ boards
def new_board(lanes):
    d = rb.new_board("sweep", [(lid, title) for lid, title in lanes])
    d["_root"] = tempfile.gettempdir()
    return d


def add(d, iid, lane, title="", text="do the thing", **kw):
    f = {"id": iid, "lane": lane, "title": title or f"Task {iid}", "text": text, "profile": kw.pop("profile", "light")}
    f.update(kw)
    return rb.op_add(d, kw.get("_actor", rb.HUMAN), {k: v for k, v in f.items() if k != "_actor"})


def run(d, iid, actor=AGENT):
    rb.op_claim(d, actor, iid, True, 0, snapshot={})        # force: the scenarios build states, they do not obey the scheduler


def finish(d, iid, outcome="complete", actor=AGENT):
    rb.op_finish(d, actor, iid, "/tmp/report.md", "", False, outcome)


def lanes_n(n, long_titles=False):
    return [(f"s{k}", (f"Session number {k} with a rather long descriptive name that keeps going" if long_titles else f"Session {k}")) for k in range(1, n + 1)]


def b_empty():
    return new_board(lanes_n(1))


def b_n(n, long_titles=False, per_lane=4):
    d = new_board(lanes_n(n, long_titles))
    for k in range(1, n + 1):
        for t in range(per_lane):
            add(d, f"S{k}T{t}", f"s{k}", needs=[f"S{k}T{t - 1}"] if t else None, uses=["gpu"] if (k + t) % 3 == 0 else None, profile="dev" if t % 2 else "light")
    run(d, "S1T0")
    if n > 1:
        run(d, "S2T0")
    return d


def b_long_text():
    d = new_board([("alpha", "A lane with a very long title that goes on and on and on and on and on"), ("beta", "Beta")])
    url = "https://example.com/" + "very-long-path-segment-" * 12
    add(d, "A" * 32, "alpha", title="T" * 200)
    add(d, "B-" + "x" * 29, "beta", title=url, text=url)
    add(d, "C1", "alpha", title="Unbroken " + "W" * 120, needs=["A" * 32])
    run(d, "B-" + "x" * 29)
    for k in range(12):
        rb.op_note(d, AGENT, "B-" + "x" * 29, f"note {k} " + url)
    add(d, "R1", "alpha", title="Report path " + "p" * 100)
    run(d, "R1", rb.Actor("agent", "other"))
    rb.op_finish(d, rb.Actor("agent", "other"), "R1", "/home/user/" + "very-long-folder/" * 10 + "report-" + "z" * 80 + ".md", "", False, "complete")
    return d


def b_many(n=300):
    d = new_board(lanes_n(4))
    for k in range(n):
        add(d, f"T{k}", f"s{k % 4 + 1}", needs=[f"T{k - 4}"] if k >= 4 and k % 3 == 0 else None, uses=["gpu"] if k % 7 == 0 else None)
    run(d, "T0")
    return d


def b_every_status():
    d = new_board(lanes_n(4))
    add(d, "DR1", "s1", text="", title="A draft")
    add(d, "PR1", "s1", title="A proposal", _actor=UNTRUSTED)
    add(d, "Q1", "s2", title="Queued and ready")
    add(d, "W1", "s2", title="Waits for a task", needs=["Q1"])
    add(d, "W2", "s3", title="Waits for a start time", not_before="+5h")
    add(d, "W3", "s3", title="Waits for a decision", needs=["DR1"])
    add(d, "SE1", "s4", title="Sent to a session")
    rb.op_sent(d, rb.HUMAN, "SE1")
    add(d, "RU1", "s1", title="Running")
    run(d, "RU1")
    add(d, "ST1", "s3", title="Running with an expired lease", uses=["gpu"])
    run(d, "ST1", rb.Actor("agent", "gone"))
    next(i for i in d["items"] if i["id"] == "ST1")["claim"]["lease_until"] = "2020-01-01T00:00:00+00:00"
    add(d, "RV1", "s2", title="Report to accept")
    run(d, "RV1", rb.Actor("agent", "b"))
    finish(d, "RV1", "complete", rb.Actor("agent", "b"))
    add(d, "RV2", "s4", title="Report with warnings")
    run(d, "RV2", rb.Actor("agent", "c"))
    finish(d, "RV2", "partial", rb.Actor("agent", "c"))
    next(i for i in d["items"] if i["id"] == "RV2")["violations"] = [{"type": "outside_scope", "path": "src/a.py"}, {"type": "collision", "path": "src/b.py", "with": "RU1"}]
    add(d, "DN1", "s1", title="Accepted")
    run(d, "DN1", rb.Actor("agent", "d"))
    finish(d, "DN1", "complete", rb.Actor("agent", "d"))
    rb.op_accept(d, rb.HUMAN, "DN1")
    add(d, "CA1", "s1", title="Cancelled")
    rb.op_cancel(d, rb.HUMAN, "CA1")
    add(d, "NV1", "s4", title="Needs a cancelled task", needs=["CA1"])
    return d


def b_night():
    d = b_every_status()
    for lane, st, task, why in (("s1", "working", "RU1", ""), ("s2", "waiting", "", "Q1: cannot run next to RU1 (lane)"), ("s3", "idle", "", "nothing queued"), ("s4", "idle", "", "needs the person")):
        rb.op_seen(d, lane, "night-agent", st, task, why)
    return d


def b_unicode():
    d = new_board([("ru", "Основная сессия"), ("zh", "主要会话"), ("ar", "الجلسة الرئيسية"), ("em", "🚀 Launch 🧪")])
    add(d, "U1", "ru", title="Проверка длинного русского названия задачи, которое не помещается в одну строку")
    add(d, "U2", "zh", title="用中文写的很长的任务标题，需要自动换行而不是溢出窗口边界")
    add(d, "U3", "ar", title="عنوان مهمة طويل جدا باللغة العربية يجب أن يلتف داخل البطاقة")
    add(d, "U4", "em", title="🚀🧪🔥 emoji only title 🚀🧪🔥🚀🧪🔥🚀🧪🔥🚀🧪🔥🚀🧪🔥")
    run(d, "U1")
    return d


SCENARIOS = [("empty", b_empty), ("1-session", lambda: b_n(1)), ("2-sessions", lambda: b_n(2)), ("3-sessions", lambda: b_n(3)), ("4-sessions", lambda: b_n(4)),
             ("6-sessions", lambda: b_n(6)), ("10-sessions-long-names", lambda: b_n(10, True, 3)), ("long-text", b_long_text), ("300-tasks", b_many),
             ("every-status", b_every_status), ("night", b_night), ("unicode", b_unicode)]
WIDTHS = [320, 390, 600, 768, 1024, 1280, 1600, 1920]
QUICK = {"scenarios": {"4-sessions", "long-text", "every-status"}, "widths": [390, 900, 1600], "modes": ["dark", "light"], "langs": ["en"]}


# ------------------------------------------------------------------------------------------------ driver
def serve(path):
    proc = subprocess.Popen([sys.executable, str(RB_PATH), "--board", str(path), "serve", "--port", "0"], stdout=subprocess.PIPE, text=True)
    box = []
    threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True).start()
    for _ in range(300):
        if box:
            break
        time.sleep(0.1)
    if not box:
        proc.kill()
        raise RuntimeError("the panel server did not start")
    return proc, box[0].split("Barid panel:")[1].split()[0]


def sweep(quick=False, shots="/dev/shm/sweep", log=print):
    only = QUICK if quick else {"scenarios": {n for n, _ in SCENARIOS}, "widths": WIDTHS, "modes": ["dark", "light"], "langs": ["en", "ru"]}
    shutil.rmtree(shots, ignore_errors=True)
    os.makedirs(shots)
    browser = ui_driver.Browser()
    problems, pages = [], 0
    try:
        for name, build in SCENARIOS:
            if name not in only["scenarios"]:
                continue
            tmp = tempfile.mkdtemp(prefix="sweep-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
            path = Path(tmp) / ".barid" / "board.json"
            path.parent.mkdir()
            rb.save(path, build())
            proc, url = serve(path)
            try:
                for mode in only["modes"]:
                    for lang in only["langs"]:
                        for width in only["widths"]:
                            browser.cmd("WebDriver:SetWindowRect", {"width": width, "height": 900})
                            browser.goto(f"{url}?lang={lang}&mode={mode}")
                            try:
                                browser.wait("document.getElementById('proj').textContent.length > 0 && document.querySelectorAll('#now .lane-card, #flow .empty, #now .empty').length > 0", timeout=30, what="the page")
                            except ui_driver.UiError as e:
                                problems.append((name, width, mode, lang, "did-not-render", str(e)[:80]))
                                continue
                            time.sleep(0.25)
                            pages += 1
                            found = json.loads(browser.exec(AUDIT)) + [("js-error", m) for m in browser.errors()]
                            seen = set()
                            for kind, detail in found:
                                if (kind, detail) in seen:
                                    continue
                                seen.add((kind, detail))
                                problems.append((name, width, mode, lang, kind, detail))
                            if found:
                                try:
                                    png = browser.cmd("WebDriver:TakeScreenshot", {"full": True, "hash": False})["value"]
                                    import base64
                                    Path(shots, f"{name}-{width}-{mode}-{lang}.png").write_bytes(base64.b64decode(png))
                                except Exception:
                                    pass
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                shutil.rmtree(tmp, ignore_errors=True)
            log(f"{name}: done")
    finally:
        browser.close()
    return pages, problems


def report(pages, problems):
    lines = [f"{pages} pages audited, {len(problems)} problems"]
    by = {}
    for name, width, mode, lang, kind, detail in problems:
        by.setdefault((kind, name), []).append((width, mode, lang, detail))
    for (kind, name), rows in sorted(by.items()):
        widths = sorted({r[0] for r in rows})
        lines.append(f"  {kind:<28} {name:<24} widths {widths}  e.g. {rows[0][3][:90]}")
    return "\n".join(lines)


if __name__ == "__main__":
    pages, problems = sweep(quick="--quick" in sys.argv)
    print(report(pages, problems))
    sys.exit(1 if problems else 0)
