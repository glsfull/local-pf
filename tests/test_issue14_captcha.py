"""Issue #14: does the agent solve Yandex SmartCaptcha? Ten checks with different approaches.

The agent deliberately does NOT solve captchas: passing SmartCaptcha automatically means bypassing
Yandex bot protection, so the agent detects the captcha, stops touching the page, switches the
session to the CAPTCHA status and hands the browser over to the operator (noVNC or «Продолжить»).
These tests prove that behaviour against a local fake Yandex in real headless Chromium:

1-4. four SmartCaptcha variants are detected (checkbox form, hidden smart-token, /showcaptcha redirect
     with an image task, data-sitekey widget) and the page is never clicked, typed into or submitted;
5.   the operator solves the captcha in the browser — the agent notices it and finishes the search;
6.   the operator presses «Продолжить» in the panel — the agent repeats the search;
7.   nobody solves it — the run stops after the waiting time;
8.   the captcha comes back every time — the run stops after three rounds instead of looping;
9.   the panel shows the CAPTCHA status and the «Продолжить» button only while the agent waits;
10.  static audit: no captcha-solving services, OCR or automatic clicks in the code and requirements.
"""
import json
import os
from pathlib import Path
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlsplit

from dashboard import create_app
from dashboard.storage import connect, save_project, start_agent_session
from modules.browser import close_browser, open_identity, wait_for
from modules.search import SearchChallenge
from modules.session import AgentSession, captcha_visible, session_config

from tests.test_dashboard import DashboardCase

ROOT = Path(__file__).resolve().parents[1]
CHROME = os.getenv("CHROME_PATH") or shutil.which("google-chrome") or shutil.which("chromium")
NO_ENV = {"YANDEX_API_KEY": "", "YANDEX_FOLDER_ID": "", "LOCAL_PF_NOVNC_URL": "", "DEEPSEEK_API_KEY": ""}
PHRASE = "натяжные потолки"

# Any click, key press, input or form submit on a captcha page is reported to /checkcaptcha.
SPY = """<script>
for (const type of ["click", "mousedown", "keydown", "input", "change", "submit"]) {
  document.addEventListener(type, () => navigator.sendBeacon("/checkcaptcha?event=" + type), true);
}
</script>"""
CAPTCHAS = {
    "checkbox": """<title>Вы не робот?</title><form id="checkbox-captcha-form" method="post" action="/checkcaptcha">
        <input type="checkbox" class="CheckboxCaptcha-Button" id="js-button"><label for="js-button">Я не робот</label>
        <button type="submit">Отправить</button></form>""",
    "token": """<title>Проверка</title><form method="post" action="/checkcaptcha">
        <input name="smart-token" type="hidden"><button type="submit">Я не робот</button></form>""",
    "image": """<title>Ой!</title><form method="post" action="/checkcaptcha">
        <p>Нажмите в таком порядке:</p><img src="/captcha.png" alt="" width="300" height="200">
        <input name="rep" placeholder="Введите символы"><button type="submit">Отправить</button></form>""",
    "sitekey": """<title>Проверка</title><div class="smart-captcha" data-sitekey="ysc1_local_test"></div>
        <button type="button" onclick="this.textContent='OK'">Проверить</button>""",
}


class FakeYandex(BaseHTTPRequestHandler):
    """ya.ru with a search form, results and SmartCaptcha shown ``server.captchas`` times at ``server.place``."""

    def do_GET(self):
        url = urlsplit(self.path)
        server = self.server
        server.requests.append(url.path)
        site = f"http://127.0.0.1:{server.server_port}"
        if url.path == "/solve":  # The operator solved the captcha in noVNC.
            server.captchas = 0
            return self.reply("<h1>Спасибо!</h1>")
        if url.path == "/showcaptcha":
            server.shown.append(url.path)
            return self.reply(CAPTCHAS["image"] + SPY)
        if url.path in ("/", "/search") and server.captchas and server.place == url.path:
            server.captchas -= 1
            if server.kind == "image":
                return self.reply("", 302, location=f"/showcaptcha?retpath={quote(self.path, safe='')}")
            server.shown.append(url.path)
            return self.reply(CAPTCHAS[server.kind] + SPY)
        if url.path == "/":
            return self.reply('<form action="/search"><input id="text" name="text"><button type="submit">Найти</button></form>')
        if url.path == "/search":
            return self.reply(
                '<ul><li class="serp-item"><h2><a href="https://other.example/">Другой сайт</a></h2></li>'
                f'<li class="serp-item"><h2><a href="{site}/landing">Натяжные потолки — сайт проекта</a></h2></li></ul>'
            )
        if url.path == "/landing":
            return self.reply("<h1>Сайт проекта</h1>")
        return self.reply("", 404)

    def do_POST(self):
        """Beacons of the spy and captcha form submits."""
        self.server.interactions.append("POST " + self.path)
        self.reply("", 403)

    def reply(self, html, status=200, *, location=None):
        body = html.encode()
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@unittest.skipUnless(CHROME, "Chromium не найден")
class CaptchaTest(DashboardCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeYandex)
        thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(thread.join)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.home = f"http://localhost:{cls.server.server_port}/"

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, NO_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.server.requests, self.server.interactions, self.server.shown = [], [], []
        self.server.captchas, self.server.kind, self.server.place = 0, "checkbox", "/search"
        self.profiles = TemporaryDirectory()
        self.addCleanup(self.profiles.cleanup)
        self.browser = None
        self.clock = 0.0

    def captcha(self, kind, place="/search", times=1):
        self.server.kind, self.server.place, self.server.captchas = kind, place, times

    def session(self, operator=lambda seconds: None, **options):
        """Agent session for one phrase of a project whose site is the local fixture (127.0.0.1)."""
        with connect(self.path) as db:
            # Stored directly: the form only accepts public domains, the fixture site is 127.0.0.1.
            project_id = save_project(
                db, None, ("Проверка капчи", f"http://127.0.0.1:{self.server.server_port}/", "213", 0),
                [(PHRASE, "", "domain", "")],
            )
            session_id = start_agent_session(db, project_id, 0)

        def wait(seconds):
            self.clock += seconds
            operator(seconds)

        session = AgentSession(
            self.path, session_id, search_home=self.home, delay=0, wait=wait, clock=lambda: self.clock, poll=5,
            **options,
        )
        return session_id, session

    def execute(self, session):
        def open_browser(identity):
            self.browser = open_identity(5, identity, profile=self.profiles.name, headless=True)
            return self.browser

        code = session.execute(open_browser, close_browser)
        with connect(self.path) as db:
            row = db.execute("SELECT state, status, message, report_json FROM agent_sessions").fetchone()
        report = json.loads(row["report_json"])
        operations = [event["operation"] for event in report["events"]]
        return code, row, report, operations

    def assert_untouched(self):
        """The agent never clicked, typed into or submitted the captcha page."""
        self.assertEqual(self.server.interactions, [])
        self.assertNotIn("/solve", self.server.requests)

    def assert_handed_to_operator(self, kind, place):
        self.captcha(kind, place, times=100)
        code, row, report, operations = self.execute(self.session(captcha_wait=60)[1])
        self.assertEqual(code, 1)
        self.assertEqual(tuple(row)[:3], ("failed", "ERROR", "Капча не решена за 1 мин."))
        self.assertTrue(report["captcha_encountered"])
        captcha = next(event for event in report["events"] if event["operation"] == "Капча")
        self.assertEqual(captcha["status"], "CAPTCHA")
        self.assertEqual(captcha["error"], "Требуется решение оператора в noVNC")
        self.assertNotIn("/landing", self.server.requests)
        self.assertTrue(self.server.shown)
        self.assert_untouched()
        return captcha

    # 1-4: every SmartCaptcha variant is detected and left to the operator.
    def test_1_checkbox_captcha_on_ya_ru_is_not_clicked(self):
        self.assert_handed_to_operator("checkbox", "/")
        self.assertNotIn("/search", self.server.requests)
        # Control: the spy does catch a real click and typing on the captcha page.
        self.captcha("checkbox", "/")
        browser = open_identity(5, None, profile=self.profiles.name, headless=True)
        try:
            browser.get(self.home)
            browser.ele("#js-button").click()
            browser.actions.type("x")
            self.assertTrue(wait_for(lambda: len(self.server.interactions) >= 2, 5))
        finally:
            close_browser(browser)
        self.assertIn("POST /checkcaptcha?event=click", self.server.interactions)

    def test_2_hidden_smart_token_on_results_is_not_submitted(self):
        captcha = self.assert_handed_to_operator("token", "/search")
        self.assertIn("/search?text=", captcha["url"])

    def test_3_showcaptcha_redirect_with_image_task_is_not_typed_into(self):
        captcha = self.assert_handed_to_operator("image", "/search")
        self.assertIn("/showcaptcha?retpath=", captcha["url"])

    def test_4_sitekey_widget_is_detected_without_pressing_its_button(self):
        self.assert_handed_to_operator("sitekey", "/search")

    # 5-8: what happens after the captcha.
    def test_5_operator_solves_captcha_in_novnc_and_agent_finishes_search(self):
        self.captcha("checkbox", "/search", times=1)

        def operator(seconds):
            self.assertTrue(captcha_visible(self.browser, session_config(PHRASE, "127.0.0.1")))
            self.browser.get(self.home + "solve")  # The operator's action in the shared browser.

        code, row, report, operations = self.execute(self.session(operator)[1])
        self.assertEqual(code, 0, row["message"])
        self.assertEqual(row["status"], "DONE")
        self.assertIn("Капча решена оператором", operations)
        self.assertEqual(operations.index("Капча") + 1, operations.index("Капча решена оператором"))
        self.assertEqual(report["keywords"][0]["found"], True)
        self.assertEqual(report["keywords"][0]["position"], 2)
        self.assertEqual(self.server.requests.count("/landing"), 1)
        self.assertEqual(self.server.interactions, [])

    def test_6_operator_presses_continue_in_panel_and_agent_repeats_search(self):
        self.captcha("token", "/search", times=1)
        clicks = []

        def operator(seconds):
            page = self.client.get(f"/agent?session={session_id}").get_data(as_text=True)
            self.assertIn("Капча — ожидание оператора", page)
            response = self.client.post(f"/agent/{session_id}/resume", data={"csrf": self.csrf("/agent")})
            clicks.append(response.status_code)

        session_id, session = self.session(operator)
        code, row, report, operations = self.execute(session)
        self.assertEqual(code, 0, row["message"])
        self.assertEqual(clicks, [303])
        self.assertIn("Оператор нажал «Продолжить»", operations)
        self.assertEqual(self.server.requests.count("/search"), 2)
        self.assertEqual(report["keywords"][0]["found"], True)
        self.assert_untouched()

    def test_7_unsolved_captcha_stops_the_run_after_the_waiting_time(self):
        self.captcha("checkbox", "/search", times=100)
        polls = []
        code, row, report, operations = self.execute(self.session(polls.append, captcha_wait=15 * 60)[1])
        self.assertEqual(code, 1)
        self.assertEqual(row["message"], "Капча не решена за 15 мин.")
        self.assertEqual(len(polls), 15 * 60 // 5)
        self.assertEqual(operations[-1], "Ошибка")
        self.assert_untouched()

    def test_8_captcha_that_comes_back_stops_after_three_rounds(self):
        self.captcha("sitekey", "/search", times=100)

        def operator(seconds):
            self.client.post(f"/agent/{session_id}/resume", data={"csrf": self.csrf("/agent")})

        session_id, session = self.session(operator)
        code, row, report, operations = self.execute(session)
        self.assertEqual(code, 1)
        self.assertEqual(row["message"], "Капча появляется повторно. Запуск остановлен.")
        self.assertEqual(operations.count("Капча"), 3)
        self.assertEqual(operations.count("Оператор нажал «Продолжить»"), 3)
        self.assertEqual(self.server.requests.count("/search"), 3)
        self.assert_untouched()

    def test_9_panel_offers_continue_only_while_agent_waits(self):
        self.captcha("checkbox", "/", times=1)
        seen = []

        def operator(seconds):
            page = self.client.get(f"/agent?session={session_id}").get_data(as_text=True)
            status = self.client.get(f"/agent/{session_id}/status.json").get_json()
            seen.append((status["status"], status["message"], re.search(r"data-agent-resume\s*(hidden)?>", page).group(1)))
            self.browser.get(self.home + "solve")

        session_id, session = self.session(operator)
        page = self.client.get(f"/agent?session={session_id}").get_data(as_text=True)
        self.assertRegex(page, r"data-agent-resume\s+hidden>")
        self.assertEqual(self.client.post(f"/agent/{session_id}/resume", data={"csrf": self.csrf("/agent")}).status_code, 409)
        code, *_ = self.execute(session)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("CAPTCHA", "Решите капчу в окне noVNC или нажмите «Продолжить».", None)])
        self.assertEqual(self.client.post(f"/agent/{session_id}/resume", data={"csrf": self.csrf("/agent")}).status_code, 409)


class CaptchaAuditTest(unittest.TestCase):
    """10: the code has no way to solve a captcha by itself — no solver services, OCR or clicks on it."""

    SOLVERS = re.compile(
        r"2captcha|anti-?captcha|rucaptcha|capmonster|capsolver|deathbycaptcha|nopecha|azcaptcha|"
        r"pytesseract|easyocr|checkcaptcha|smart-?token.*\.(click|input)|CheckboxCaptcha",
        re.I,
    )

    def test_10_no_captcha_solver_in_code_or_dependencies(self):
        sources = [ROOT / "agent.py", ROOT / "requirements.txt", *sorted((ROOT / "modules").glob("*.py")),
                   *sorted((ROOT / "dashboard").glob("*.py"))]
        found = [f"{path.name}: {match.group(0)}" for path in sources
                 for match in self.SOLVERS.finditer(path.read_text(encoding="utf-8"))]
        self.assertEqual(found, [])
        # On a captcha the search raises SearchChallenge before any element of the page is used.
        search = (ROOT / "modules" / "search.py").read_text(encoding="utf-8")
        self.assertIn("raise SearchChallenge(url)", search)
        self.assertTrue(issubclass(SearchChallenge, Exception))
        session = (ROOT / "modules" / "session.py").read_text(encoding="utf-8")
        self.assertIn("On a SmartCaptcha the agent does not try to pass it", session)


if __name__ == "__main__":
    unittest.main()
