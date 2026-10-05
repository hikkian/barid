"""A tiny browser driver for the panel tests: Firefox headless over Marionette, standard library only (no Selenium, no downloads)."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time

ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"


class UiError(Exception):
    pass


class Browser:
    def __init__(self, port=0, width=1280, height=900):
        self.firefox = shutil.which("firefox")
        if not self.firefox:
            raise UiError("firefox is not installed")
        self.profile = tempfile.mkdtemp(prefix="barid-ui-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        with open(os.path.join(self.profile, "user.js"), "w") as f:
            f.write(f'user_pref("marionette.port", {self.port});\nuser_pref("browser.shell.checkDefaultBrowser", false);\n'
                    'user_pref("datareporting.policy.dataSubmissionEnabled", false);\nuser_pref("app.update.enabled", false);\n'
                    'user_pref("browser.startup.homepage_override.mstone", "ignore");\nuser_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\n')
        cmd = ["nice", "-n", "19", self.firefox, "-profile", self.profile, "-no-remote", "-headless", "-marionette", "about:blank"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sock = None
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                break
            except OSError:
                time.sleep(0.3)
        if not self.sock:
            self.close()
            raise UiError("could not reach Firefox Marionette")
        self.sock.settimeout(60)
        self._id = 0
        self._read()  # hello
        self.cmd("WebDriver:NewSession", {"capabilities": {}})
        self.cmd("WebDriver:SetWindowRect", {"width": width, "height": height})

    def _read(self):
        head = b""
        while not head.endswith(b":"):
            ch = self.sock.recv(1)
            if not ch:
                raise UiError("Firefox closed the connection")
            head += ch
        n = int(head[:-1])
        body = b""
        while len(body) < n:
            body += self.sock.recv(n - len(body))
        return json.loads(body.decode("utf-8"))

    def cmd(self, name, params=None):
        self._id += 1
        payload = json.dumps([0, self._id, name, params or {}]).encode("utf-8")
        self.sock.sendall(str(len(payload)).encode() + b":" + payload)
        while True:
            msg = self._read()
            if isinstance(msg, list) and msg[0] == 1 and msg[1] == self._id:
                if msg[2]:
                    raise UiError(f"{name}: {msg[2].get('error')}: {msg[2].get('message')}")
                return msg[3]

    def goto(self, url):
        self.cmd("WebDriver:Navigate", {"url": url})
        self.exec("window.__errs = []; window.addEventListener('error', function (e) { window.__errs.push(e.message); });"
                  "window.addEventListener('unhandledrejection', function (e) { window.__errs.push('rejection: ' + e.reason); });")

    def exec(self, script, *args):
        r = self.cmd("WebDriver:ExecuteScript", {"script": script, "args": list(args)})
        return r.get("value") if isinstance(r, dict) else r

    def wait(self, script, timeout=10, what=""):
        """Wait until the JS expression is truthy; return its value."""
        end, last = time.time() + timeout, None
        while time.time() < end:
            last = self.exec("return (" + script + ");")
            if last:
                return last
            time.sleep(0.15)
        raise UiError(f"timed out waiting for {what or script} (last value {last!r})")

    def element(self, css, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            try:
                r = self.cmd("WebDriver:FindElement", {"using": "css selector", "value": css})
                return r["value"][ELEMENT_KEY]
            except UiError:
                time.sleep(0.15)
        raise UiError(f"no element {css}")

    def click(self, css):
        end, err = time.time() + 10, None
        while time.time() < end:
            try:
                self.cmd("WebDriver:ElementClick", {"id": self.element(css)})
                return
            except UiError as e:
                err = e
                time.sleep(0.2)
        raise UiError(f"could not click {css}: {err}")

    def click_text(self, css, text):
        """Click the first element matching css whose text contains `text` (a real click, so a hidden or covered element fails)."""
        n = self.exec("var css = arguments[0], want = arguments[1];"
                      "var a = [].slice.call(document.querySelectorAll(css)).filter(function (x) { return x.textContent.indexOf(want) >= 0; });"
                      "if (!a.length) return 0; a[0].setAttribute('data-ui-target', '1'); return a.length;", css, text)
        if not n:
            raise UiError(f"no {css} with text {text!r}")
        try:
            self.click('[data-ui-target="1"]')
        finally:
            self.exec("var t = document.querySelector('[data-ui-target]'); if (t) t.removeAttribute('data-ui-target');")

    def type(self, css, text):
        self.cmd("WebDriver:ElementSendKeys", {"id": self.element(css), "text": text})

    def errors(self):
        return self.exec("return window.__errs || [];")

    def close(self):
        try:
            if self.sock:
                self.cmd("Marionette:Quit", {"flags": ["eForceQuit"]})
        except Exception:
            pass
        try:
            self.proc.wait(timeout=8)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)
