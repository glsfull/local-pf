"""Issue #14: Yandex profiles and proxies (manual and bulk upload), one phrase = one profile + one proxy."""
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dashboard import identities
from dashboard.identities import parse_cookies, parse_profile_line, parse_proxy, take_identity
from dashboard.runner import execute_run
from dashboard.storage import connect, start_agent_session, start_run
from modules.proxy_relay import ProxyRelay
from modules.session import AgentSession

from tests.test_dashboard import DashboardCase, PROJECT_URL

NO_ENV = {"YANDEX_API_KEY": "", "YANDEX_FOLDER_ID": "", "LOCAL_PF_NOVNC_URL": "", "DEEPSEEK_API_KEY": ""}
COOKIES = [{"name": "yandexuid", "value": "123", "domain": ".yandex.ru", "path": "/", "secure": True,
            "expirationDate": 1893456000, "sameSite": "no_restriction"}]


class ParseTest(unittest.TestCase):
    def test_proxy_formats(self):
        cases = {
            "http://user:pa%40ss@203.0.113.10:8000": ("http", "203.0.113.10", 8000, "user", "pa@ss"),
            "socks5://203.0.113.11:1080": ("socks5", "203.0.113.11", 1080, "", ""),
            "socks5h://u:p@proxy.example:1080": ("socks5", "proxy.example", 1080, "u", "p"),
            "HTTPS://Proxy.Example:443": ("https", "proxy.example", 443, "", ""),
            "203.0.113.12:3128": ("http", "203.0.113.12", 3128, "", ""),
            "203.0.113.12:3128:login:secret": ("http", "203.0.113.12", 3128, "login", "secret"),
            "login:secret@203.0.113.12:3128": ("http", "203.0.113.12", 3128, "login", "secret"),
            "[2001:db8::1]:8080": ("http", "2001:db8::1", 8080, "", ""),
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                proxy = parse_proxy(line)
                self.assertEqual((proxy.scheme, proxy.host, proxy.port, proxy.username, proxy.password), expected)
                # The stored form parses back to the same proxy.
                again = parse_proxy(proxy.url)
                self.assertEqual((again.scheme, again.host, again.port, again.username, again.password), expected)
        proxy = parse_proxy("203.0.113.12:3128:login:secret")
        self.assertEqual(proxy.masked, "http://login:••••@203.0.113.12:3128")
        self.assertEqual(proxy.server, "http://203.0.113.12:3128")
        self.assertEqual(parse_proxy("[2001:db8::1]:8080").server, "http://[2001:db8::1]:8080")

    def test_proxy_errors(self):
        for line in (
            "", "203.0.113.1", "203.0.113.1:0", "203.0.113.1:70000", "ftp://203.0.113.1:21", "a b:80",
            "http://203.0.113.1:80/path", "socks4://u:p@203.0.113.1:1080", "bad_host!:80", "h:1:2:3:4:5",
            "http://:p@203.0.113.1:80",
        ):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_proxy(line)

    def test_cookies_and_profile_lines(self):
        cookies = parse_cookies(json.dumps(COOKIES))
        self.assertEqual(cookies, [{
            "name": "yandexuid", "value": "123", "domain": ".yandex.ru", "path": "/", "secure": True,
            "expires": 1893456000.0, "sameSite": "None",
        }])
        self.assertEqual(parse_cookies(json.dumps({"cookies": COOKIES})), cookies)
        self.assertEqual(parse_cookies(""), [])
        for text in ("{", "[1]", '[{"name": "a", "value": "b"}]', '"text"'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_cookies(text)
        self.assertEqual(parse_profile_line("yandex-01"), ("yandex-01", []))
        self.assertEqual(parse_profile_line(f"yandex-02;{json.dumps(COOKIES)}"), ("yandex-02", cookies))
        self.assertEqual(parse_profile_line(f"yandex-03\t{json.dumps(COOKIES)}"), ("yandex-03", cookies))
        with self.assertRaises(ValueError):
            parse_profile_line("x" * 101)


@patch.dict(os.environ, NO_ENV)
class SettingsUploadTest(DashboardCase):
    def post(self, url, data, **kwargs):
        data = {"csrf": self.csrf("/settings"), **data}
        return self.client.post(url, data=data, content_type="multipart/form-data", **kwargs)

    def test_manual_bulk_and_file_upload_of_profiles(self):
        self.assertIn("Профили не добавлены.", self.client.get("/settings").get_data(as_text=True))
        response = self.post("/settings/profiles", {
            "name": "yandex-01", "cookies": json.dumps(COOKIES),
            "bulk": "# список\nyandex-02\n\nyandex-03;" + json.dumps(COOKIES) + "\nYANDEX-01",
            "files": [
                (BytesIO(json.dumps(COOKIES).encode()), "yandex-04.json"),
                (BytesIO("\ufeffyandex-05\nyandex-06\n".encode()), "list.txt"),
            ],
        })
        self.assertEqual(response.status_code, 303)
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Профили Яндекса: добавлено 6. Пропущено повторов: 1.", html)
        self.assertNotIn('"value": "123"', html)
        with connect(self.path) as db:
            rows = db.execute("SELECT name, cookies FROM yandex_profiles ORDER BY id").fetchall()
        self.assertEqual([row["name"] for row in rows], [f"yandex-0{number}" for number in range(1, 7)])
        self.assertEqual([len(json.loads(row["cookies"])) for row in rows], [1, 0, 1, 1, 0, 0])

    def test_profile_upload_errors_keep_the_list_unchanged(self):
        for data, message in (
            ({}, "Укажите название профиля, список профилей или файлы."),
            ({"bulk": "ok\n" + "x" * 101}, "Строка 2: название профиля — от 1 до 100 символов."),
            ({"name": "a", "cookies": "{"}, "Профиль: cookies — JSON-массив (экспорт Cookie-Editor)."),
            ({"files": [(BytesIO(b"\xff\xfe\x00"), "bad.txt")]}, "Файл bad.txt: нужен текст в кодировке UTF-8."),
            ({"files": [(BytesIO(b"[1]"), "acc.json")]}, "Файл acc.json: cookie 1 — ожидается объект."),
        ):
            with self.subTest(message=message):
                response = self.post("/settings/profiles", data)
                self.assertEqual(response.status_code, 422)
                self.assertIn(message, response.get_data(as_text=True))
        with connect(self.path) as db:
            self.assertEqual(identities.profile_count(db), 0)
        self.assertEqual(self.client.post("/settings/profiles", data={"bulk": "a"}).status_code, 400)

    def test_manual_and_bulk_upload_of_proxies_hides_passwords(self):
        response = self.post("/settings/proxies", {"bulk": "203.0.113.10:8000:login:secret-password"})
        self.assertEqual(response.status_code, 303)
        response = self.post("/settings/proxies", {
            "bulk": "socks5://203.0.113.11:1080\n203.0.113.10:8000:login:secret-password",
            "files": [(BytesIO(b"https://u:p2@proxy.example:443\n203.0.113.12:3128\n"), "proxies.txt")],
        })
        self.assertEqual(response.status_code, 303)
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Прокси: добавлено 3. Пропущено повторов: 1.", html)
        self.assertIn("http://login:••••@203.0.113.10:8000", html)
        self.assertIn("https://u:••••@proxy.example:443", html)
        self.assertNotIn("secret-password", html)
        response = self.post("/settings/proxies", {"bulk": "203.0.113.13:80\nftp://203.0.113.14:21"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Строка 2: схема прокси — http, https, socks4 или socks5.", response.get_data(as_text=True))
        with connect(self.path) as db:
            self.assertEqual(identities.proxy_count(db), 4)

    def test_delete_one_and_all(self):
        self.post("/settings/profiles", {"bulk": "a\nb"})
        self.post("/settings/proxies", {"bulk": "203.0.113.1:80\n203.0.113.2:80"})
        with connect(self.path) as db:
            profile_id = db.execute("SELECT id FROM yandex_profiles WHERE name = 'a'").fetchone()[0]
            proxy_id = db.execute("SELECT id FROM proxies ORDER BY id").fetchone()[0]
        profile_dir = identities.profiles_root(self.path) / str(profile_id)
        profile_dir.mkdir(parents=True)
        self.assertEqual(self.post(f"/settings/profiles/{profile_id}/delete", {}).status_code, 303)
        self.assertFalse(profile_dir.exists())
        self.assertEqual(self.post(f"/settings/proxies/{proxy_id}/delete", {}).status_code, 303)
        self.assertEqual(self.post(f"/settings/proxies/{proxy_id}/delete", {}).status_code, 404)
        with connect(self.path) as db:
            self.assertEqual((identities.profile_count(db), identities.proxy_count(db)), (1, 1))
        self.post("/settings/profiles/delete", {})
        self.post("/settings/proxies/delete", {})
        with connect(self.path) as db:
            self.assertEqual((identities.profile_count(db), identities.proxy_count(db)), (0, 0))

    def test_limits(self):
        with patch.object(identities, "MAX_PROXIES", 2):
            response = self.post("/settings/proxies", {"bulk": "203.0.113.1:80\n203.0.113.2:80\n203.0.113.3:80"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("не больше 2 прокси", response.get_data(as_text=True))


class RotationTest(DashboardCase):
    def add(self, profiles=(), proxies=()):
        with connect(self.path) as db:
            identities.add_profiles(db, [(name, []) for name in profiles])
            identities.add_proxies(db, [parse_proxy(line) for line in proxies])

    def take(self):
        with connect(self.path) as db:
            return take_identity(db, identities.profiles_root(self.path))

    def test_least_recently_used_round_robin(self):
        self.add(["p1", "p2"], ["203.0.113.1:80", "u:p@203.0.113.2:80", "203.0.113.3:80"])
        pairs = [(identity.profile_name, identity.proxy.masked) for identity in (self.take() for _ in range(4))]
        self.assertEqual(pairs, [
            ("p1", "http://203.0.113.1:80"), ("p2", "http://u:••••@203.0.113.2:80"),
            ("p1", "http://203.0.113.3:80"), ("p2", "http://203.0.113.1:80"),
        ])
        # A new profile is used before the ones that already searched, then the rotation goes on.
        self.add(["p3"])
        self.assertEqual([self.take().profile_name for _ in range(4)], ["p3", "p1", "p2", "p3"])
        identity = self.take()
        self.assertEqual(identity.profile_dir, identities.profiles_root(self.path) / "1")
        self.assertIn("профиль «p1»", identity.describe())

    def test_without_profiles_and_proxies(self):
        identity = self.take()
        self.assertIsNone(identity.profile_dir)
        self.assertIsNone(identity.proxy)
        self.assertEqual(identity.describe(), "временный профиль, без прокси")

    @patch.dict(os.environ, NO_ENV)
    def test_agent_session_uses_new_profile_and_proxy_for_every_phrase(self):
        """One phrase = one profile + one proxy; the refined query keeps the pair of its phrase."""
        self.add(["p1", "p2", "p3"], ["203.0.113.1:80", "203.0.113.2:80"])
        project = self.create_project()
        project_id = int(project.rstrip("/").split("/")[-1])
        with connect(self.path) as db:
            session_id = start_agent_session(db, project_id)
        timeline = []

        def search(browser, config, result, journal, home):
            timeline.append(("search", browser.identity.profile_name, config.keyword))
            if config.keyword == "Ремонт":
                from modules.search import TargetNotFound
                raise TargetNotFound("нет в выдаче")
            return SimpleNamespace(url=PROJECT_URL)

        def open_browser(identity):
            timeline.append(("open", identity.profile_name, identity.proxy.masked))
            return SimpleNamespace(identity=identity, get_tabs=lambda as_id=False: ["main"], tab_id="main")

        session = AgentSession(
            self.path, session_id, search=search, delay=0, wait=lambda seconds: timeline.append(("wait", seconds)),
        )
        self.assertEqual(session.execute(open_browser, lambda browser: timeline.append(("close", browser.identity.profile_name))), 0)
        self.assertEqual(timeline, [
            ("open", "p1", "http://203.0.113.1:80"), ("search", "p1", "Натяжные потолки"), ("close", "p1"),
            ("wait", 15),
            ("open", "p2", "http://203.0.113.2:80"), ("search", "p2", "Ремонт"), ("search", "p2", "Ремонт master-bobr"),
            ("close", "p2"),
            ("wait", 15),
            ("open", "p3", "http://203.0.113.1:80"), ("search", "p3", "Монтаж"), ("close", "p3"),
            ("wait", 15),
            ("open", "p1", "http://203.0.113.2:80"), ("search", "p1", "Комнаты"), ("close", "p1"),
        ])
        with connect(self.path) as db:
            report = json.loads(db.execute("SELECT report_json FROM agent_sessions").fetchone()[0])
        self.assertEqual([(item["profile"], item["proxy"]) for item in report["keywords"]], [
            ("p1", "http://203.0.113.1:80"), ("p2", "http://203.0.113.2:80"),
            ("p3", "http://203.0.113.1:80"), ("p1", "http://203.0.113.2:80"),
        ])
        events = [event for event in report["events"] if event["operation"] == "Профиль и прокси"]
        self.assertEqual([event["query"] for event in events], ["Натяжные потолки", "Ремонт", "Монтаж", "Комнаты"])

    def test_browser_is_closed_when_the_phrase_fails(self):
        project = self.create_project()
        project_id = int(project.rstrip("/").split("/")[-1])
        with connect(self.path) as db:
            session_id = start_agent_session(db, project_id, 1)
        closed = []

        def search(*args):
            raise RuntimeError("сбой")

        session = AgentSession(self.path, session_id, search=search, delay=0, wait=lambda seconds: None)
        with self.assertLogs("modules.session", "ERROR"):
            self.assertEqual(session.execute(lambda identity: SimpleNamespace(get_tabs=lambda as_id=False: []), closed.append), 1)
        self.assertEqual(len(closed), 1)

    def test_position_check_by_browser_takes_identity_per_phrase(self):
        """The browser position source (RankParser) also switches the pair before every phrase."""
        self.add(["p1", "p2"], ["203.0.113.1:80"])
        project = self.create_project()
        project_id = int(project.rstrip("/").split("/")[-1])

        class Source:
            def __init__(self):
                self.calls = []

            def begin_phrase(self):
                with connect(path) as db:
                    self.identity = take_identity(db, identities.profiles_root(path))

            def __call__(self, query, page, region):
                self.calls.append((self.identity.profile_name, query))
                return [] if query == "Ремонт" else [PROJECT_URL]

        path = self.path
        with connect(self.path) as db:
            project_row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            words = db.execute("SELECT * FROM keywords WHERE project_id = ? ORDER BY ordinal", (project_id,)).fetchall()
            run_id = start_run(db, project_row, words, "Парсер выдачи", "master-bobr")
        source = Source()
        self.assertEqual(execute_run(self.path, run_id, source, wait=lambda seconds: None), "complete")
        self.assertEqual([profile for profile, _ in source.calls if _ in ("Ремонт", "Ремонт master-bobr")], ["p2", "p2"])
        self.assertEqual(source.calls[0][0], "p1")


class QuietServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """Chromium drops idle background connections: resets are expected here."""


class AuthProxy(QuietServer):
    """Upstream HTTP proxy for tests: requires Basic credentials, serves absolute-URI GETs, echoes CONNECT tunnels."""

    def __init__(self, credentials):
        self.expected = "Basic " + base64.b64encode(credentials.encode()).decode()
        self.seen = []
        super().__init__(("127.0.0.1", 0), self.Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n")[0].decode()
            line, *headers = head.split("\r\n")
            fields = {key.lower(): value.strip() for key, _, value in (h.partition(":") for h in headers)}
            self.server.seen.append((line, fields.get("proxy-authorization")))
            if fields.get("proxy-authorization") != self.server.expected:
                self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                return
            if line.startswith("CONNECT"):
                self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                while chunk := self.request.recv(4096):
                    self.request.sendall(chunk.upper())
                return
            body = f"<html><head><title>via proxy</title></head><body>{line.split()[1]}</body></html>".encode()
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body
            )


class Socks5Proxy(QuietServer):
    """SOCKS5 server for tests: username/password auth, records the requested host, echoes the stream."""

    def __init__(self, user, password):
        self.user, self.password, self.targets = user.encode(), password.encode(), []
        super().__init__(("127.0.0.1", 0), self.Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    class Handler(socketserver.BaseRequestHandler):
        def recv(self, size):
            data = b""
            while len(data) < size:
                data += self.request.recv(size - len(data))
            return data

        def handle(self):
            _, count = self.recv(2)
            if 2 not in self.recv(count):
                self.request.sendall(b"\x05\xff")
                return
            self.request.sendall(b"\x05\x02")
            _, size = self.recv(2)
            user = self.recv(size)
            password = self.recv(self.recv(1)[0])
            if (user, password) != (self.server.user, self.server.password):
                self.request.sendall(b"\x01\x01")
                return
            self.request.sendall(b"\x01\x00")
            _, command, _, kind = self.recv(4)
            host = self.recv(self.recv(1)[0]).decode() if kind == 3 else socket.inet_ntoa(self.recv(4))
            port = struct.unpack("!H", self.recv(2))[0]
            self.server.targets.append((command, host, port))
            self.request.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", 1))
            while chunk := self.request.recv(4096):
                self.request.sendall(chunk.upper())


def through(relay, payload):
    host, port = relay.address.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(payload)
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
            if b"HELLO" in data:
                break
        return data


class ProxyRelayTest(unittest.TestCase):
    def test_http_proxy_gets_credentials_for_connect_and_plain_requests(self):
        upstream = AuthProxy("login:p@ss")
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        proxy = parse_proxy(f"http://login:p%40ss@127.0.0.1:{upstream.server_address[1]}")
        with ProxyRelay(proxy) as relay:
            data = through(relay, b"CONNECT yandex.ru:443 HTTP/1.1\r\nHost: yandex.ru:443\r\n\r\nhello")
            self.assertTrue(data.startswith(b"HTTP/1.1 200"))
            self.assertIn(b"HELLO", data)
            data = through(relay, b"GET http://ya.test/search?text=1 HTTP/1.1\r\nHost: ya.test\r\nProxy-Connection: keep-alive\r\n\r\n")
            self.assertIn(b"/search?text=1", data)
        self.assertEqual(upstream.seen[0], ("CONNECT yandex.ru:443 HTTP/1.1", upstream.expected))
        self.assertEqual(upstream.seen[1], ("GET http://ya.test/search?text=1 HTTP/1.1", upstream.expected))

    def test_wrong_password_reaches_chromium_as_proxy_error(self):
        upstream = AuthProxy("login:right")
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        proxy = parse_proxy(f"127.0.0.1:{upstream.server_address[1]}:login:wrong")
        with ProxyRelay(proxy) as relay:
            data = through(relay, b"CONNECT yandex.ru:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(data.startswith(b"HTTP/1.1 407"))

    def test_socks5_proxy_with_password_resolves_names_remotely(self):
        upstream = Socks5Proxy("user", "secret")
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        proxy = parse_proxy(f"socks5://user:secret@127.0.0.1:{upstream.server_address[1]}")
        with ProxyRelay(proxy) as relay:
            data = through(relay, b"CONNECT yandex.ru:443 HTTP/1.1\r\n\r\nhello")
        self.assertTrue(data.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"HELLO", data)
        self.assertEqual(upstream.targets, [(1, "yandex.ru", 443)])
        wrong = parse_proxy(f"socks5://user:bad@127.0.0.1:{upstream.server_address[1]}")
        with ProxyRelay(wrong) as relay:
            self.assertTrue(through(relay, b"CONNECT yandex.ru:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 407"))


CHROME = os.getenv("CHROME_PATH") or shutil.which("google-chrome") or shutil.which("chromium")


@unittest.skipUnless(CHROME, "Chromium не найден")
class BrowserIdentityTest(unittest.TestCase):
    """Real Chromium: authenticated proxy through the relay and a persistent profile with imported cookies."""

    def setUp(self):
        from tempfile import TemporaryDirectory

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_chromium_uses_authenticated_proxy_and_profile_cookies(self):
        from modules.browser import COOKIES_MARKER, close_browser, open_identity

        upstream = AuthProxy("login:secret")
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        identity = identities.Identity(
            profile_id=1, profile_name="p1", profile_dir=self.root / "profiles" / "1",
            cookies=[{"name": "yandexuid", "value": "42", "domain": "ya.test", "path": "/", "expires": 1893456000.0}],
            proxy=parse_proxy(f"127.0.0.1:{upstream.server_address[1]}:login:secret"),
        )
        browser = open_identity(10, identity, profile=self.root, headless=True)
        try:
            browser.get("http://ya.test/first", retry=0, timeout=10)
            self.assertEqual(browser.title, "via proxy")
            self.assertEqual(browser.run_js("return document.cookie"), "yandexuid=42")
        finally:
            close_browser(browser)
        self.assertTrue((identity.profile_dir / COOKIES_MARKER).exists())
        # Chromium also sends its own background requests through the proxy: all of them carry the credentials.
        self.assertIn(("GET http://ya.test/first HTTP/1.1", upstream.expected), upstream.seen)
        self.assertTrue(all(auth == upstream.expected for _, auth in upstream.seen))
        # The second launch of the same profile keeps the cookies without importing them again.
        identity.cookies = []
        browser = open_identity(10, identity, profile=self.root, headless=True)
        try:
            browser.get("http://ya.test/second", retry=0, timeout=10)
            self.assertEqual(browser.run_js("return document.cookie"), "yandexuid=42")
        finally:
            close_browser(browser)
        self.assertTrue(identity.profile_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
