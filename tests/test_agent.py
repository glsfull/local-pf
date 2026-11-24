import json
import logging
import socket
import stat
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, urlsplit

from DrissionPage.errors import PageDisconnectedError

from agent import run, verify, verify_positions
from dashboard import create_app
from dashboard.search import SearchError
from dashboard.storage import connect, save_project
from modules.browser import (
    CaptchaHandler,
    CaptchaTimeout,
    close_browser,
    load_solver,
    matches_domain,
    open_browser,
    wait_for,
)
from modules.clock import MoscowFormatter
from modules.config import TargetPage, captcha_settings, read_config
from modules.journal import Journal
from modules.rank_parser import RankParser

CHECKBOX_PAGE = """
<form id="checkbox-captcha-form" action="/solve">
  <div class="CheckboxCaptcha"><div class="CheckboxCaptcha-Anchor">
    <input id="js-button" class="CheckboxCaptcha-Button" type="button" role="checkbox"
           aria-checked="false" style="width:56px;height:56px;margin:140px 0 0 220px">
  </div></div>
</form>
<script>
  let moves = 0;
  addEventListener('mousemove', event => { if (event.isTrusted) moves++; });
  document.getElementById('js-button').addEventListener('click', event => {
    location.replace('NEXT&trusted=' + event.isTrusted + '&moves=' + moves);
  });
</script>
"""
IMAGE_PAGE = """
<div class="AdvancedCaptcha">
  <div class="AdvancedCaptcha-FormLabel">Нажмите в таком порядке: красный, синий</div>
  <div class="AdvancedCaptcha-View" style="position:relative;width:300px;height:200px;background:#eee">
    <div style="position:absolute;left:40px;top:50px;width:20px;height:20px;background:red"></div>
    <div style="position:absolute;left:190px;top:110px;width:20px;height:20px;background:blue"></div>
  </div>
  <button data-testid="submit" class="CaptchaButton_view_action">Отправить</button>
</div>
<script>
  const clicks = [];
  const view = document.querySelector('.AdvancedCaptcha-View');
  view.addEventListener('click', event => {
    const box = view.getBoundingClientRect();
    clicks.push([event.clientX - box.left, event.clientY - box.top]);
  });
  document.querySelector('[data-testid=submit]').addEventListener('click', () => {
    const near = (point, x, y) => point && Math.hypot(point[0] - x, point[1] - y) < 12;
    const solved = clicks.length === 2 && near(clicks[0], 50, 60) && near(clicks[1], 200, 120);
    fetch('/captcha-submit?clicks=' + clicks.length).then(() => {
      if (solved) location.replace('SOLVE'); else location.reload();
    });
  });
</script>
"""
TEXT_PAGE = """
<div class="AdvancedCaptcha">
  <div class="AdvancedCaptcha-FormLabel">Введите текст с картинки</div>
  <div class="AdvancedCaptcha-View"><img class="AdvancedCaptcha-Image" alt="" width="200" height="60"></div>
  <span class="Textinput"><input class="Textinput-Control" name="rep"></span>
  <button data-testid="submit">Отправить</button>
</div>
"""
WIDGET_PAGE = """
<h1 id="expected">Страница с формой</h1>
<div class="smart-captcha" data-sitekey="test-key">
  <iframe title="SmartCaptcha checkbox widget" src="WIDGET" style="width:320px;height:120px;border:4px solid #ccc"></iframe>
  <input type="hidden" name="smart-token" value="">
</div>
<script>
  addEventListener('message', event => {
    document.querySelector('[name=smart-token]').value = event.data.token;
    fetch('/widget-solved?trusted=' + event.data.trusted);
  });
</script>
"""
WIDGET_FRAME = """
<div class="CheckboxCaptcha"><input id="js-button" class="CheckboxCaptcha-Button" type="button"
  style="width:40px;height:40px;margin:30px 0 0 60px"></div>
<script>
  document.getElementById('js-button').addEventListener('click', event => {
    parent.postMessage({token: 'token-' + Date.now(), trusted: event.isTrusted}, '*');
  });
</script>
"""


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        self.server.requests.append((url.path, url.query, self.headers.get("Referer")))
        if url.path == "/drop":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        host = f"http://127.0.0.1:{self.server.server_port}"
        html, status = self.fixture_response(url, host)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode())))
        self.end_headers()
        self.wfile.write(html.encode())

    def fixture_response(self, url, target):
        mode = self.server.mode
        if url.path == "/":
            if mode == "blocked":
                return "<h1>Forbidden</h1>", 403
            if mode == "home_challenge":
                return '<form id="checkbox-captcha-form">Проверка</form>', 403
            return """
                <form action="/results">
                    <input id="text" name="text">
                    <button id="search-button">Найти</button>
                </form>
            """, 200
        if url.path in ("/solve", "/showcaptcha", "/captcha-submit", "/widget", "/widget-solved"):
            return self.captcha_response(url)
        if url.path in ("/results", "/ranks") and mode.startswith("captcha_") and not self.server.solved:
                kind = mode.removeprefix("captcha_")
                return (
                    "<script>location.replace('/showcaptcha?kind=" + kind
                    + "&retpath=' + encodeURIComponent(location.href))</script>"
                ), 200
        if url.path == "/results":
            if mode == "results_challenge":
                return '<input name="smart-token" type="hidden">', 200
            if mode == "challenge_path":
                return '<script>location.replace("/showcaptcha")</script>', 200
            attrs = 'target="_blank"' if mode == "new_tab" else ""
            decoy = '<a class="result" href="http://localhost.invalid">Другой сайт</a>'
            match = f'<a class="result" {attrs} href="{target}/landing">Цель</a>'
            return decoy + ("" if mode == "missing_site" else match), 200
        if url.path == "/ranks":
            params = parse_qs(url.query)
            if mode == "rank_challenge":
                return '<input name="smart-token" type="hidden">', 403
            if mode == "rank_limit":
                return "<h1>Limit</h1>", 429
            if mode == "rank_empty":
                return '<div class="SearchEmpty">Нет результатов</div>', 200
            if mode == "rank_unknown":
                return "<h1>Unexpected layout</h1>", 200
            relevant = '<li class="serp-item"><h2><a href="https://example.org/target/">Цель</a></h2></li>'
            other = (
                '<li class="serp-item"><h2><a class="OrganicTitle-Link" href="https://other.example/">'
                'Другой сайт</a></h2><a class="OrganicTitle-Link" href="https://other.example/sub">Раздел</a></li>'
            )
            return other + (relevant if params["text"] == ["проверка Бренд"] and params["p"] == ["1"] else ""), 200
        if url.path == "/landing":
            return """
                <h1>Входная страница</h1>
                <a href="/news"> Новости </a>
                <a id="more" href="/more" target="_blank">Подробнее</a>
                <a href="/gone">Удалено</a>
                <a href="/broken">Ошибка</a>
                <a href="/empty">Заголовок отсутствует</a>
                <a href="/inactive" onclick="return false">Неактивная ссылка</a>
                <a href="/denied">Ограничено</a>
                <a href="/drop">Обрыв соединения</a>
            """, 200
        if url.path == "/gone":
            return "<h1>Страница отсутствует</h1>", 404
        if url.path == "/broken":
            return "<h1>Ошибка сервера</h1>", 503
        if url.path == "/denied":
            return "<h1>Лимит исчерпан</h1>", 429
        if url.path == "/empty":
            return "<main>Содержимое</main>", 200
        if url.path in ("/news", "/more"):
            if mode == "target_challenge":
                return '<input name="smart-token" type="hidden">', 403
            if mode == "target_widget":
                widget = f"http://localhost:{self.server.server_port}/widget"
                return WIDGET_PAGE.replace("WIDGET", widget), 200
            return """
                <h1 id="expected">Страница работает</h1><div style="height:2400px"></div>
                <script>
                    addEventListener('scroll', () => fetch('/scroll-event'), {once: true});
                </script>
            """, 200
        return "", 200

    def captcha_response(self, url):
        params = {key: values[0] for key, values in parse_qs(url.query).items()}
        self.server.captcha_log.append((url.path, params))
        retpath = params.get("retpath", "/")
        solve = "/solve?retpath=" + quote(retpath, safe="")
        if url.path == "/solve":
            self.server.solved = True
            return f"<script>location.replace({json.dumps(retpath)})</script>", 200
        if url.path == "/widget":
            return WIDGET_FRAME, 200
        if url.path in ("/captcha-submit", "/widget-solved"):
            return "", 200
        kind = params.get("kind")
        if kind in ("checkbox", "image") and params.get("step") != "advanced":
            following = solve if kind == "checkbox" else (
                "/showcaptcha?kind=image&step=advanced&retpath=" + quote(retpath, safe="")
            )
            return CHECKBOX_PAGE.replace("NEXT", following), 200
        if kind == "image":
            return IMAGE_PAGE.replace("SOLVE", solve), 200
        operator = ""
        if self.server.operator_delay is not None:
            # The fixture plays the operator who solves the captcha in noVNC.
            delay = int(self.server.operator_delay * 1000)
            operator = f"<script>setTimeout(() => location.replace({json.dumps(solve)}), {delay})</script>"
        return TEXT_PAGE + operator, 200

    def log_message(self, *_):
        pass


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.thread.join)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.search_home = f"http://localhost:{cls.server.server_port}"
        cls.config = replace(
            read_config(Path(__file__).resolve().parents[1] / "config.yaml"),
            keyword="проверочный запрос",
            domain="127.0.0.1",
            result_link="a.result",
            timeout=2,
            captcha_timeout=0,
            captcha_poll=0.2,
        )
        for name, value in (("CHECKBOX_WAIT", (0.3, 0.5)), ("CLICK_DELAY", (0.1, 0.2)), ("SUBMIT_WAIT", 0.5)):
            patcher = patch(f"modules.browser.{name}", value)
            patcher.start()
            cls.addClassCleanup(patcher.stop)

    def setUp(self):
        self.server.solved = False
        self.server.captcha_log = []
        self.server.operator_delay = None

    def check_run(self, mode, *targets, **settings):
        self.server.mode = mode
        self.server.requests = []
        with TemporaryDirectory() as output:
            config = replace(
                self.config, pages=targets or (TargetPage("/news", url="/news"),), **settings
            )
            code = verify(config, Path(output), headless=True, search_home=self.search_home)
            report = json.loads((Path(output) / "report.json").read_text(encoding="utf-8"))
            report["captcha_files"] = sorted(path.name for path in Path(output).glob("captcha/*.png"))
        self.assertEqual(report["keyword"], self.config.keyword)
        self.assertEqual(report["target_domain"], self.config.domain)
        self.assertTrue(report["timestamp"].endswith("+03:00"))
        self.assertTrue(report["events"])
        self.assertTrue(all(event["timestamp_msk"].endswith("+03:00") for event in report["events"]))
        return code, report

    def assert_pages(self, report, *expected):
        pages = report["pages_checked"]
        self.assertEqual([(page["target"], page["status"]) for page in pages], list(expected))
        for page in pages:
            self.assertEqual(page["exists"], page["status"] == "OK")
            self.assertEqual(page["exists"], page["error"] is None)
            self.assertTrue(page["timestamp_msk"].endswith("+03:00"))
            self.assertEqual(urlsplit(page["url"]).hostname, "127.0.0.1")
            self.assertIn("scroll_position", page)

    def test_search_and_click_in_same_tab(self):
        code, report = self.check_run("normal")
        self.assertEqual(code, 0)
        self.assertTrue(report["search_result_found"])
        self.assertFalse(report["captcha_encountered"])
        requests = [req for req in self.server.requests if req[0] != "/favicon.ico"]
        self.assertEqual(
            [path for path, _, _ in requests],
            ["/", "/results", "/landing", "/news", "/scroll-event"],
        )
        self.assertEqual(parse_qs(requests[1][1]), {"text": [self.config.keyword]})
        self.assertEqual(urlsplit(requests[2][2]).hostname, "localhost")
        self.assertEqual(urlsplit(requests[3][2]).path, "/landing")
        self.assert_pages(report, ("/news", "OK"))
        page = report["pages_checked"][0]
        events = report["events"]
        scrolls = [event for event in events if event["operation"] == "Позиция прокрутки"]
        self.assertEqual([event["step"] for event in scrolls], [1, 2, 3])
        self.assertTrue(all(event["url"] == page["url"] for event in scrolls))
        self.assertEqual(page["scroll_position"], scrolls[-1]["scroll_position"])
        self.assertEqual(urlsplit(page["url"]).path, "/news")
        self.assertGreater(page["scroll_position"]["scrollY"], 0)
        self.assertGreater(page["scroll_position"]["scrollHeight"], 2400)
        self.assertGreater(page["scroll_position"]["percent"], 0)

    def test_rank_agent_reads_database_and_stores_report(self):
        self.server.mode, self.server.requests = "normal", []
        with TemporaryDirectory() as output:
            root = Path(output)
            path = root / "dashboard.sqlite3"
            client = create_app(path).test_client()
            with connect(path) as db:
                project_id = save_project(db, None, ("Рейтинг", "https://example.org", "213", 0), [
                    ("проверка", "https://example.org/target/", "brand", "Бренд"),
                ])
            with (
                patch.dict("os.environ", {
                    "LOCAL_PF_RESULTS_URL": self.search_home + "/ranks",
                    "LOCAL_PF_OBSERVATION_DELAY": "0",
                }),
                patch("modules.rank_parser.open_browser", side_effect=lambda *args, **kwargs: open_browser(
                    *args, **kwargs, headless=True,
                )),
            ):
                self.assertEqual(verify_positions(path, root, project_id=project_id), 0)
            with connect(path) as db:
                run = db.execute("SELECT * FROM runs").fetchone()
                row = db.execute("SELECT * FROM measurements").fetchone()
            self.assertIsNone(row["rank"])
            self.assertEqual(row["hint_rank"], 3)
            self.assertEqual(row["hint_found_url"], "https://example.org/target/")
            self.assertTrue(row["hinted_at"].endswith("+03:00"))
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["pages_checked"]), 7)
            self.assertTrue(all(page["timestamp_msk"].endswith("+03:00") for page in report["pages_checked"]))
            html = client.get(f"/projects/{project_id}/runs/{run['id']}/logs").get_data(as_text=True)
            self.assertIn("Разбор выдачи", html)
            self.assertIn("проверка Бренд", html)
            requests = [(path, parse_qs(query)) for path, query, _ in self.server.requests if path != "/favicon.ico"]
            self.assertTrue(all(path == "/ranks" and fields["lr"] == ["213"] for path, fields in requests))

    def test_rank_parser_distinguishes_empty_layout_challenge_and_rate_limit(self):
        self.server.requests = []
        report = {"events": [], "pages_checked": [], "captcha_encountered": False}
        with patch.dict("os.environ", {"LOCAL_PF_RESULTS_URL": self.search_home + "/ranks"}):
            parser = RankParser(Journal(report["events"], 0), report, timeout=2)
        parser.browser = open_browser(2, headless=True)
        self.addCleanup(close_browser, parser.browser)
        self.server.mode = "rank_empty"
        self.assertEqual(parser("проверка", 0, ""), [])
        for mode, status in (
            ("rank_unknown", "PARSE_ERROR"), ("rank_challenge", "CAPTCHA"), ("rank_limit", "ACCESS_DENIED"),
        ):
            with self.subTest(mode=mode):
                self.server.mode = mode
                with self.assertRaisesRegex(SearchError, status):
                    parser("проверка", 0, "")
                self.assertEqual(report["pages_checked"][-1]["status"], status)
        self.assertTrue(report["captcha_encountered"])

    def test_new_tabs_and_history(self):
        code, report = self.check_run(
            "new_tab",
            TargetPage("Новости", text="Новости", expected_selector="#expected"),
            TargetPage("#more", selector="#more"),
            TargetPage("/news", url="/news"),
        )
        self.assertEqual(code, 0)
        self.assert_pages(report, ("Новости", "OK"), ("#more", "OK"), ("/news", "OK"))

    def test_missing_site(self):
        code, report = self.check_run("missing_site")
        self.assertEqual(code, 1)
        self.assertFalse(report["search_result_found"])
        self.assertFalse(any(path == "/landing" for path, _, _ in self.server.requests))

    def test_home_challenge_stops_before_search(self):
        code, report = self.check_run("home_challenge")
        self.assertEqual(code, 1)
        self.assertTrue(report["captcha_encountered"])
        self.assertEqual(report["status"], "TIMEOUT")
        self.assertFalse(report["search_result_found"])
        self.assertFalse(any(path == "/results" for path, _, _ in self.server.requests))

    def test_results_challenge_stops_before_target(self):
        code, report = self.check_run("results_challenge")
        self.assertEqual(code, 1)
        self.assertTrue(report["captcha_encountered"])
        self.assertFalse(report["search_result_found"])
        self.assertFalse(any(path == "/landing" for path, _, _ in self.server.requests))

    def test_challenge_redirect_stops_before_target(self):
        code, report = self.check_run("challenge_path")
        self.assertEqual(code, 1)
        self.assertTrue(report["captcha_encountered"])
        self.assertFalse(report["search_result_found"])
        self.assertFalse(any(path == "/landing" for path, _, _ in self.server.requests))

    def test_missing_link_does_not_prevent_next_check(self):
        code, report = self.check_run(
            "normal", TargetPage("/missing", url="/missing"), TargetPage("/news", url="/news")
        )
        self.assertEqual(code, 0)
        self.assert_pages(report, ("/missing", "NOT_FOUND"), ("/news", "OK"))
        self.assertFalse(any(path == "/missing" for path, _, _ in self.server.requests))

    def test_http_errors_and_missing_marker(self):
        code, report = self.check_run(
            "normal",
            TargetPage("/gone", url="/gone"),
            TargetPage("/broken", url="/broken"),
            TargetPage("/empty", url="/empty"),
            TargetPage("/news", url="/news"),
        )
        self.assertEqual(code, 0)
        self.assert_pages(
            report, ("/gone", "NOT_FOUND"), ("/broken", "HTTP_ERROR"),
            ("/empty", "NOT_FOUND"), ("/news", "OK"),
        )
        errors = [event for event in report["events"] if event["operation"] == "Ошибка"]
        self.assertEqual([urlsplit(event["url"]).path for event in errors], ["/gone", "/broken", "/empty"])
        self.assertIn("HTTP 404", errors[0]["error"])
        self.assertIn("HTTP 503", errors[1]["error"])
        check = next(event for event in report["events"] if
                     event["operation"] == "Проверка элемента" and event["url"].endswith("/empty"))
        self.assertEqual((check["selector"], check["found"], check["status"]), ("h1", False, "NOT_FOUND"))

    def test_click_timeout_does_not_prevent_next_check(self):
        code, report = self.check_run(
            "normal", TargetPage("/inactive", url="/inactive"), TargetPage("/news", url="/news")
        )
        self.assertEqual(code, 0)
        self.assert_pages(report, ("/inactive", "TIMEOUT"), ("/news", "OK"))

    def test_network_failure_does_not_prevent_next_check(self):
        code, report = self.check_run(
            "normal", TargetPage("/drop", url="/drop"), TargetPage("/news", url="/news")
        )
        self.assertEqual(code, 0)
        self.assertTrue(any(path == "/drop" for path, _, _ in self.server.requests))
        self.assert_pages(report, ("/drop", "NETWORK_ERROR"), ("/news", "OK"))
        self.assertTrue(report["pages_checked"][0]["url"].endswith("/drop"))

    def test_search_service_refuses_access(self):
        code, report = self.check_run("blocked")
        self.assertEqual(code, 1)
        self.assertFalse(report["captcha_encountered"])
        self.assertFalse(report["search_result_found"])

    def test_target_limits_access(self):
        code, report = self.check_run(
            "normal", TargetPage("/denied", url="/denied"), TargetPage("/news", url="/news")
        )
        self.assertEqual(code, 1)
        self.assertTrue(report["search_result_found"])
        self.assert_pages(report, ("/denied", "ACCESS_DENIED"))
        self.assertFalse(any(path == "/news" for path, _, _ in self.server.requests))

    def test_target_challenge_is_linked_to_the_page_and_stops_checks(self):
        code, report = self.check_run(
            "target_challenge", TargetPage("/news", url="/news"), TargetPage("/more", url="/more")
        )
        self.assertEqual(code, 1)
        self.assertTrue(report["captcha_encountered"])
        self.assert_pages(report, ("/news", "TIMEOUT"))
        self.assertEqual(report["events"][-1]["status"], "TIMEOUT")
        self.assertEqual(report["status"], "TIMEOUT")
        self.assertTrue(report["events"][-1]["url"].endswith("/news"))
        self.assertFalse(any(path == "/more" for path, _, _ in self.server.requests))

    def operations(self, report):
        return [event["operation"] for event in report["events"]]

    def test_checkbox_captcha_is_clicked_with_smooth_mouse_movement(self):
        code, report = self.check_run("captcha_checkbox")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertTrue(report["captcha_encountered"])
        self.assert_pages(report, ("/news", "OK"))
        operations = self.operations(report)
        start = operations.index("Обнаружена капча")
        self.assertEqual(
            operations[start:start + 3],
            ["Обнаружена капча", "Попытка нажать чекбокс", "Капча решена автоматически"],
        )
        self.assertEqual(report["events"][start]["captcha_type"], "checkbox")
        solved = [params for path, params in self.server.captcha_log if path == "/solve"]
        self.assertEqual(solved[0]["trusted"], "true")
        self.assertGreaterEqual(int(solved[0]["moves"]), 10)
        self.assertEqual((report["captcha"]["attempts"], report["captcha"]["resolution"]), (1, "automatic"))

    def test_checkbox_widget_inside_cross_origin_frame(self):
        code, report = self.check_run("target_widget")
        self.assertEqual(code, 0)
        self.assert_pages(report, ("/news", "OK"))
        self.assertIn(("/widget-solved", {"trusted": "true"}), self.server.captcha_log)
        self.assertIn("Капча решена автоматически", self.operations(report))

    def test_image_captcha_is_solved_by_local_model(self):
        with patch.dict("os.environ", {
            "LOCAL_PF_CAPTCHA_SOLVER": "examples.captcha_solver_fixed:solve",
            "LOCAL_PF_CAPTCHA_POINTS": "50,60;200,120",
        }):
            code, report = self.check_run("captcha_image")
        self.assertEqual(code, 0)
        self.assert_pages(report, ("/news", "OK"))
        operations = self.operations(report)
        for operation in (
            "Попытка нажать чекбокс", "Капча не решена", "Тип капчи изменился",
            "Попытка решить задание с картинками", "Ответ локальной модели",
            "Нажатие кнопки «Отправить»", "Капча решена автоматически",
        ):
            self.assertIn(operation, operations)
        answer = next(event for event in report["events"] if event["operation"] == "Ответ локальной модели")
        self.assertEqual(answer["points"], 2)
        self.assertEqual(len(report["captcha_files"]), 1)
        self.assertEqual(report["captcha"]["attempts"], 2)
        self.assertIn(("/captcha-submit", {"clicks": "2"}), self.server.captcha_log)

    def test_wrong_model_answers_stop_after_three_attempts(self):
        with patch.dict("os.environ", {
            "LOCAL_PF_CAPTCHA_SOLVER": "examples.captcha_solver_fixed:solve",
            "LOCAL_PF_CAPTCHA_POINTS": "10,10;20,20",
        }):
            code, report = self.check_run("captcha_image", captcha_timeout=0.5)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "TIMEOUT")
        self.assertEqual(report["captcha"]["attempts"], 3)
        self.assertEqual(report["captcha"]["resolution"], "timeout")
        submits = [path for path, _ in self.server.captcha_log if path == "/captcha-submit"]
        self.assertEqual(len(submits), 2)
        self.assertFalse(any(path == "/landing" for path, _, _ in self.server.requests))

    def test_complex_captcha_waits_for_operator_and_continues(self):
        self.server.operator_delay = 1.5
        statuses = []
        real_write = __import__("agent").write_report

        def snapshot(path, report):
            statuses.append(report.get("status"))
            real_write(path, report)

        with patch("agent.write_report", side_effect=snapshot), self.assertLogs("modules.browser", "WARNING") as logs:
            code, report = self.check_run("captcha_text", captcha_timeout=15)
        self.assertEqual(code, 0)
        self.assert_pages(report, ("/news", "OK"))
        self.assertIn("CAPTCHA_MANUAL_REQUIRED: Требуется участие оператора.", "\n".join(logs.output))
        operations = self.operations(report)
        self.assertNotIn("Попытка нажать чекбокс", operations)
        start = operations.index("Обнаружена капча")
        self.assertEqual(operations[start + 1:start + 3], [
            "Капча не решена, ожидание оператора", "Оператор решил капчу, продолжаем",
        ])
        manual = report["events"][start + 1]
        self.assertEqual(
            (manual["status"], manual["captcha_type"], manual["operator_url"]),
            ("CAPTCHA_MANUAL_REQUIRED", "complex", "http://localhost:56565"),
        )
        self.assertGreaterEqual(report["events"][start + 2]["waited_seconds"], 1)
        self.assertEqual(report["captcha"]["resolution"], "operator")
        # report.json is rewritten while the agent waits so the dashboard shows the request.
        self.assertEqual(statuses[0], "CAPTCHA_MANUAL_REQUIRED")
        self.assertEqual(statuses[-1], "OK")

    def test_operator_timeout_saves_report_and_exits(self):
        code, report = self.check_run("captcha_text", captcha_timeout=1)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "TIMEOUT")
        self.assertTrue(report["captcha_encountered"])
        self.assertFalse(report["search_result_found"])
        operations = self.operations(report)
        self.assertEqual(operations[-3:], [
            "Капча не решена, ожидание оператора", "Время ожидания оператора истекло", "Ошибка",
        ])
        self.assertEqual(report["events"][-1]["status"], "TIMEOUT")
        self.assertIn("1 с", report["events"][-1]["error"])

    def test_rank_parser_continues_after_operator_solves_captcha(self):
        self.server.mode, self.server.operator_delay = "captcha_text", 1
        report = {"events": [], "pages_checked": [], "captcha_encountered": False}
        journal = Journal(report["events"], 0)
        handler = CaptchaHandler(journal, report, timeout=2, manual_timeout=15, poll_interval=0.2)
        with patch.dict("os.environ", {"LOCAL_PF_RESULTS_URL": self.search_home + "/ranks"}):
            parser = RankParser(journal, report, timeout=2, handler=handler)
        parser.browser = open_browser(2, headless=True)
        self.addCleanup(close_browser, parser.browser)
        results = parser("проверка", 0, "")
        self.assertEqual(results, ["https://other.example/"])
        self.assertTrue(report["captcha_encountered"])
        self.assertEqual(report["pages_checked"][-1]["status"], "OK")
        self.assertIn("Оператор решил капчу, продолжаем", self.operations(report))

        self.server.mode, self.server.solved, self.server.operator_delay = "captcha_text", False, None
        handler.manual_timeout = 0
        with self.assertRaisesRegex(SearchError, "TIMEOUT"):
            parser("проверка", 1, "")
        self.assertEqual(report["status"], "TIMEOUT")
        self.assertEqual(report["pages_checked"][-1]["status"], "TIMEOUT")


class UnitTests(unittest.TestCase):
    @patch("modules.browser.time.sleep")
    def test_wait_for_page_to_reattach_after_navigation(self, _):
        probe = Mock(side_effect=[RuntimeError("Not attached to an active page"), "loaded"])
        self.assertEqual(wait_for(probe, 1), "loaded")
        with self.assertRaisesRegex(RuntimeError, "Unknown browser error"):
            wait_for(Mock(side_effect=RuntimeError("Unknown browser error")), 1)

    def test_domain_matching_uses_hostname_boundaries(self):
        self.assertTrue(matches_domain("https://www.example.com:8080/path", "example.com"))
        self.assertFalse(matches_domain("https://example.com.invalid/path", "example.com"))
        self.assertFalse(matches_domain("https://other.invalid/?target=example.com", "example.com"))

    def test_captcha_settings_from_environment(self):
        names = {"attempts": "A", "timeout": "T", "poll": "P"}
        self.assertEqual(
            captcha_settings({"A": "5", "T": "0", "P": "0.5"}, names),
            {"attempts": 5, "timeout": 0, "poll": 0.5},
        )
        self.assertEqual(captcha_settings({}), {"attempts": 3, "timeout": 300, "poll": 2})
        for values, field in (({"A": "1.5"}, "A"), ({"T": "-1"}, "T"), ({"P": "0"}, "P"), ({"T": "x"}, "T")):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, field):
                captcha_settings(values, names)

    def test_solver_is_loaded_from_module_and_function(self):
        self.assertIsNone(load_solver(""))
        solve = load_solver("examples.captcha_solver_fixed:solve")
        with patch.dict("os.environ", {"LOCAL_PF_CAPTCHA_POINTS": "1,2; 3.5,4"}):
            self.assertEqual(solve({}), [(1.0, 2.0), (3.5, 4.0)])
        with self.assertRaisesRegex(ValueError, "module:function"):
            load_solver("examples.captcha_solver_fixed")

    def test_manual_wait_polls_until_timeout(self):
        report, saved, now = {"events": []}, [], [0.0]
        handler = CaptchaHandler(
            Journal(report["events"], 0), report, manual_timeout=6, poll_interval=2,
            save_progress=lambda: saved.append(report["status"]),
        )
        tab = Mock(url="https://ya.ru/showcaptcha")
        with (
            patch.object(CaptchaHandler, "present", return_value=True) as present,
            patch("modules.browser.time.monotonic", side_effect=lambda: now[0]),
            patch("modules.browser.time.sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)),
            self.assertRaises(CaptchaTimeout),
        ):
            handler.wait_for_captcha_resolution(tab)
        self.assertEqual(present.call_count, 3)
        self.assertEqual(saved, ["CAPTCHA_MANUAL_REQUIRED", "TIMEOUT"])
        self.assertTrue(report["captcha_encountered"])
        self.assertEqual(
            [event["operation"] for event in report["events"]],
            ["Капча не решена, ожидание оператора", "Время ожидания оператора истекло"],
        )

    @patch("agent.check_pages")
    @patch("agent.find_target")
    @patch("agent.open_browser")
    def test_report_is_readable_before_browser_closes(self, launch, *_):
        config = read_config(Path(__file__).resolve().parents[1] / "config.yaml")
        with TemporaryDirectory() as output:
            log_dir = Path(output)
            log_file = log_dir / "agent.log"
            log_file.touch()
            report_path = log_dir / "report.json"
            observed = []

            def check_report(**_):
                observed.append((
                    json.loads(report_path.read_text(encoding="utf-8"))["keyword"],
                    stat.S_IMODE(report_path.stat().st_mode),
                ))

            checked = Mock(side_effect=check_report)
            launch.return_value.process_id = None
            launch.return_value.quit.side_effect = checked
            self.assertEqual(verify(config, log_dir), 0)
            checked.assert_called_once_with(force=True)
            self.assertEqual(observed, [(config.keyword, stat.S_IMODE(log_file.stat().st_mode))])

    @patch("modules.browser.Process")
    def test_browser_shutdown_waits_for_profile_writes(self, process):
        closed = Mock()
        browser = Mock(process_id=42)
        closed.attach_mock(browser.quit, "quit")
        closed.attach_mock(process.return_value.wait, "wait")
        close_browser(browser)
        process.assert_called_once_with(42)
        self.assertEqual([call[0] for call in closed.mock_calls], ["quit", "wait"])

    @patch("modules.journal.sleep")
    def test_observation_step_and_log_use_moscow_time(self, sleep):
        events = []
        Journal(events, 0.7).step("Прокрутка", "https://example.org/", step=1)
        sleep.assert_called_once_with(0.7)
        self.assertEqual(events[0]["delay_seconds"], 0.7)
        self.assertTrue(events[0]["timestamp_msk"].endswith("+03:00"))
        record = logging.LogRecord("agent", logging.INFO, "", 0, "step", (), None)
        record.created = datetime.fromisoformat("2026-11-23T22:24:30+00:00").timestamp()
        self.assertEqual(MoscowFormatter("%(asctime)s").format(record), "2026-11-24 01:24:30 MSK")

    @patch("agent.open_browser", side_effect=PageDisconnectedError)
    def test_browser_restarts_at_most_twice(self, launch):
        config = read_config(Path(__file__).resolve().parents[1] / "config.yaml")
        report = {"search_result_found": True, "pages_checked": [{}]}
        with self.assertLogs(level=logging.ERROR):
            self.assertEqual(run(config, report), 1)
        self.assertEqual(launch.call_count, 3)
        self.assertEqual(report["pages_checked"], [])
