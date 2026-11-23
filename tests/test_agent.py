import json
import logging
import socket
import stat
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from DrissionPage.errors import PageDisconnectedError

from agent import run, verify
from modules.browser import matches_domain
from modules.config import TargetPage, read_config


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
        if url.path == "/results":
            if mode == "results_challenge":
                return '<input name="smart-token" type="hidden">', 200
            if mode == "challenge_path":
                return '<script>location.replace("/showcaptcha")</script>', 200
            attrs = 'target="_blank"' if mode == "new_tab" else ""
            decoy = '<a class="result" href="http://localhost.invalid">Другой сайт</a>'
            match = f'<a class="result" {attrs} href="{target}/landing">Цель</a>'
            return decoy + ("" if mode == "missing_site" else match), 200
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
            return """
                <h1 id="expected">Страница работает</h1><div style="height:2400px"></div>
                <script>
                    addEventListener('scroll', () => fetch('/scroll-event'), {once: true});
                </script>
            """, 200
        return "", 200

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
        )

    def check_run(self, mode, *targets):
        self.server.mode = mode
        self.server.requests = []
        with TemporaryDirectory() as output:
            config = replace(
                self.config, pages=targets or (TargetPage("/news", url="/news"),)
            )
            code = verify(config, Path(output), headless=True, search_home=self.search_home)
            report = json.loads((Path(output) / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["keyword"], self.config.keyword)
        self.assertEqual(report["target_domain"], self.config.domain)
        self.assertTrue(report["timestamp"].endswith("+00:00"))
        return code, report

    def assert_pages(self, report, *expected):
        self.assertEqual(
            report["pages_checked"],
            [{"url": url, "status": status, "exists": status == "OK"} for url, status in expected],
        )

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


class UnitTests(unittest.TestCase):
    def test_domain_matching_uses_hostname_boundaries(self):
        self.assertTrue(matches_domain("https://www.example.com:8080/path", "example.com"))
        self.assertFalse(matches_domain("https://example.com.invalid/path", "example.com"))
        self.assertFalse(matches_domain("https://other.invalid/?target=example.com", "example.com"))

    @patch("agent.run", return_value=0)
    def test_report_uses_normal_output_file_permissions(self, _):
        config = read_config(Path(__file__).resolve().parents[1] / "config.yaml")
        with TemporaryDirectory() as output:
            log_dir = Path(output)
            log_file = log_dir / "agent.log"
            log_file.touch()
            self.assertEqual(verify(config, log_dir), 0)
            self.assertEqual(
                stat.S_IMODE((log_dir / "report.json").stat().st_mode),
                stat.S_IMODE(log_file.stat().st_mode),
            )

    @patch("agent.open_browser", side_effect=PageDisconnectedError)
    def test_browser_restarts_at_most_twice(self, launch):
        config = read_config(Path(__file__).resolve().parents[1] / "config.yaml")
        report = {"search_result_found": True, "pages_checked": [{}]}
        with self.assertLogs(level=logging.ERROR):
            self.assertEqual(run(config, report), 1)
        self.assertEqual(launch.call_count, 3)
        self.assertEqual(report["pages_checked"], [])
