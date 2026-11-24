"""Issue #12: Yandex Search API positions, settings, agent sessions with live status and captcha handoff."""
import base64
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from xml.sax.saxutils import escape

from dashboard import create_app
from dashboard.__main__ import main, open_panel
from dashboard.agent_process import agent_command, ensure_container, execute_agent_session
from dashboard.config_import import import_config, read_config as read_panel_config
from dashboard.search import SearchError
from dashboard.search_api import (
    DAY_PRICE, NIGHT_PRICE, OPERATION_ENDPOINT, SEARCH_ENDPOINT, YandexDeferredSearch, parse_results, request_price,
)
from dashboard.storage import connect, panel_setting, project_keywords, project_settings, start_agent_session
from modules.clock import MSK
from modules.config import read_config
from modules.journal import Journal
from modules.search import SearchChallenge, TargetNotFound
from modules.session import AgentSession

from tests.test_dashboard import DashboardCase, PROJECT_URL

ROOT = Path(__file__).resolve().parents[1]
NO_ENV = {"YANDEX_API_KEY": "", "YANDEX_FOLDER_ID": "", "LOCAL_PF_NOVNC_URL": ""}


def xml_answer(urls=(), error=None):
    if error:
        body = f'<error code="{error[0]}">{escape(error[1])}</error>'
    else:
        groups = "".join(f"<group><doc><url>{escape(url)}</url></doc></group>" for url in urls)
        body = f"<results><grouping>{groups}</grouping></results>"
    return f'<?xml version="1.0" encoding="utf-8"?><yandexsearch><response>{body}</response></yandexsearch>'


class FakeApi:
    """Search API double: ``searchAsync`` answers with an operation, the operation is done on the second poll."""

    def __init__(self, answers, polls_before_done=1):
        self.answers = answers
        self.polls_before_done = polls_before_done
        self.requests = []
        self.operations = {}

    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), request.full_url, body, request.get_header("Authorization")))
        if request.full_url == SEARCH_ENDPOINT:
            operation_id = f"op{len(self.operations) + 1}"
            self.operations[operation_id] = [body["query"]["queryText"], 0]
            return BytesIO(json.dumps({"id": operation_id, "done": False}).encode())
        operation_id = request.full_url.removeprefix(OPERATION_ENDPOINT)
        state = self.operations[operation_id]
        state[1] += 1
        if state[1] <= self.polls_before_done:
            return BytesIO(json.dumps({"id": operation_id, "done": False}).encode())
        xml = xml_answer(self.answers.get(state[0], ()))
        raw = base64.b64encode(xml.encode()).decode()
        return BytesIO(json.dumps({"id": operation_id, "done": True, "response": {"rawData": raw}}).encode())

    def queries(self):
        return [body["query"]["queryText"] for method, _, body, _ in self.requests if method == "POST"]


def deferred(api, report=None, **options):
    report = {"pages_checked": [], "events": []} if report is None else report
    clock = [0.0]
    options.setdefault("wait", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    options.setdefault("clock", lambda: clock[0])
    return YandexDeferredSearch("AQVN-secret", "b1gfolder", Journal(report["events"], 0), report, opener=api, **options)


class SearchApiTest(unittest.TestCase):
    def test_parse_results_takes_one_document_per_group_and_handles_errors(self):
        urls = [f"https://site{index}.ru/" for index in range(60)]
        self.assertEqual(parse_results(xml_answer(urls)), urls[:50])
        self.assertEqual(parse_results(xml_answer(error=("15", "Искомая комбинация слов нигде не встречается"))), [])
        with self.assertRaisesRegex(SearchError, "Лимит"):
            parse_results(xml_answer(error=("32", "Лимит запросов")))
        with self.assertRaisesRegex(SearchError, "некорректный XML"):
            parse_results("<yandexsearch>")

    def test_request_body_auth_polling_and_cost(self):
        api = FakeApi({"натяжные потолки": ["https://other.ru/", PROJECT_URL]}, polls_before_done=2)
        search = deferred(api, poll_interval=60)
        self.assertEqual(search("натяжные потолки", 0, "213"), ["https://other.ru/", PROJECT_URL])
        self.assertEqual(search("натяжные потолки", 1, "213"), [])
        method, url, body, auth = api.requests[0]
        self.assertEqual((method, url, auth), ("POST", SEARCH_ENDPOINT, "Api-Key AQVN-secret"))
        self.assertEqual(body["groupSpec"], {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": "50", "docsInGroup": "1"})
        self.assertEqual((body["folderId"], body["region"], body["responseFormat"]), ("b1gfolder", "213", "FORMAT_XML"))
        self.assertEqual(body["query"]["searchType"], "SEARCH_TYPE_RU")
        self.assertEqual([method for method, *_ in api.requests], ["POST", "GET", "GET", "GET"])
        summary = search.summary()
        self.assertEqual(summary["api_requests"], 1)
        self.assertIn(summary["api_cost_rub"], (round(DAY_PRICE / 1000, 4), round(NIGHT_PRICE / 1000, 4)))
        self.assertEqual(search.report["pages_checked"][0]["status"], "OK")

    def test_price_depends_on_moscow_time(self):
        self.assertEqual(request_price(datetime(2026, 11, 24, 7, 59, tzinfo=MSK)), NIGHT_PRICE / 1000)
        self.assertEqual(request_price(datetime(2026, 11, 24, 8, 0, tzinfo=MSK)), DAY_PRICE / 1000)

    def test_http_errors_timeout_and_missing_credentials(self):
        def forbidden(request, timeout):
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

        with self.assertRaisesRegex(SearchError, "search-api.webSearch.user"):
            deferred(forbidden)("запрос", 0, None)
        search = deferred(FakeApi({}, polls_before_done=10**6), poll_interval=600, max_wait=3600)
        with self.assertRaisesRegex(SearchError, "за 1 ч"):
            search("запрос", 0, None)
        self.assertEqual(search.report["pages_checked"][-1]["status"], "TIMEOUT")
        with self.assertRaisesRegex(ValueError, "YANDEX_FOLDER_ID"):
            YandexDeferredSearch("key", "", Journal([], 0), {})


@patch.dict(os.environ, NO_ENV)
class ApiPanelTest(DashboardCase):
    def setUp(self):
        super().setUp()
        self.api = FakeApi({
            # Main phrase finds the landing page: the refined query of this phrase is never sent.
            "Натяжные потолки": ["https://other.ru/", "https://master-bobr.ru/potolki/"],
            "Ремонт": [],
            "Ремонт master-bobr": [f"https://site{index}.ru/" for index in range(6)] + [PROJECT_URL],
        })

        def factory(key, folder, journal, report):
            self.credentials = (key, folder)
            return YandexDeferredSearch(key, folder, journal, report, opener=self.api, wait=lambda _: None)

        self.app = create_app(self.path, Mock(), api_factory=factory)
        self.client = self.app.test_client()

    def save_settings(self, **values):
        data = {"csrf": self.csrf("/settings"), "balance": "", "user_name": "", "telegram_url": "", **values}
        return self.client.post("/settings", data=data)

    def test_settings_keep_mask_and_clear_the_key(self):
        response = self.save_settings(yandex_api_key="bad key", yandex_folder_id="b1gfolder")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.save_settings(yandex_folder_id="B1G/../x").status_code, 422)
        self.assertEqual(self.save_settings(position_source="webmaster").status_code, 422)
        self.assertEqual(self.save_settings(novnc_url="javascript:alert(1)").status_code, 422)
        self.assertEqual(self.save_settings(
            yandex_api_key="AQVNsecretvalue1234", yandex_folder_id="b1gfolder", position_source="api",
            novnc_url="http://localhost:56565/vnc.html",
        ).status_code, 303)
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertNotIn("AQVNsecretvalue1234", page)
        self.assertIn("••••1234", page)
        self.assertIn('value="b1gfolder"', page)
        self.assertIn("http://localhost:56565/vnc.html", page)
        self.assertIn("Yandex Search API", self.client.get("/").get_data(as_text=True))
        # An empty field keeps the saved key.
        self.assertEqual(self.save_settings(yandex_folder_id="b1gfolder").status_code, 303)
        self.assertIn("••••1234", self.client.get("/settings").get_data(as_text=True))
        self.assertEqual(self.save_settings(yandex_folder_id="b1gfolder", clear_api_key="1").status_code, 303)
        self.assertIn("Ключ не задан.", self.client.get("/settings").get_data(as_text=True))

    def test_positions_button_runs_deferred_api_and_refines_only_missing_phrases(self):
        self.save_settings(yandex_api_key="AQVNsecretvalue1234", yandex_folder_id="b1gfolder")
        project = self.create_project()
        page = self.client.get(project + "/positions").get_data(as_text=True)
        self.assertIn("📸 Снять позиции", page)
        with patch("dashboard.Thread") as thread:
            self.assertEqual(self.client.post(project + "/runs", data={"csrf": self.csrf(project)}).status_code, 303)
        target = thread.call_args.kwargs["target"]
        args = thread.call_args.kwargs["args"]
        self.assertEqual(args[2], ("AQVNsecretvalue1234", "b1gfolder"))
        self.assertEqual(target(*args), "complete")
        queries = self.api.queries()
        main_phrases = ["Натяжные потолки", "Ремонт", "Монтаж", "Комнаты"]
        self.assertEqual(queries[:4], main_phrases)
        self.assertNotIn("Натяжные потолки Бобр", queries)
        self.assertEqual(sorted(queries[4:]), sorted(["Ремонт master-bobr", "Монтаж master-bobr", "Комнаты Бобр"]))
        with connect(self.path) as db:
            run = db.execute("SELECT * FROM runs WHERE id = ?", (args[1],)).fetchone()
            rows = db.execute("SELECT * FROM measurements WHERE run_id = ? ORDER BY ordinal", (args[1],)).fetchall()
            report = json.loads(db.execute("SELECT report_json FROM run_reports").fetchone()[0])
        self.assertEqual(run["source"], "Yandex Search API")
        self.assertEqual([(row["rank"], row["hint_rank"]) for row in rows], [(2, None), (None, 7), (None, None), (None, None)])
        self.assertEqual(report["api_requests"], 7)
        self.assertGreater(report["api_cost_rub"], 0)
        self.assertNotIn("AQVNsecretvalue1234", json.dumps(report))
        page = self.client.get(project + "/positions").get_data(as_text=True)
        self.assertIn("Yandex Search API: запросов 7 · 0,", page)

    def test_missing_credentials_fall_back_to_browser_parser(self):
        project = self.create_project()
        with patch("dashboard.Thread") as thread:
            self.client.post(project + "/runs", data={"csrf": self.csrf(project)})
        self.assertEqual(thread.call_args.kwargs["target"].__name__, "execute_run")
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT source FROM runs").fetchone()[0], "Парсер выдачи")
        self.assertEqual(self.api.requests, [])

    def test_api_without_browser_requires_credentials(self):
        self.app = create_app(self.path)
        self.client = self.app.test_client()
        project = self.create_project()
        self.assertIn("Для съёма позиций укажите YANDEX_API_KEY", self.client.get(project).get_data(as_text=True))
        response = self.client.post(project + "/runs", data={"csrf": self.csrf(project)})
        self.assertEqual(response.status_code, 409)
        self.assertIn("YANDEX_API_KEY", response.get_data(as_text=True))
        with patch.dict(os.environ, {"YANDEX_API_KEY": "AQVNenvkey0000", "YANDEX_FOLDER_ID": "b1genv"}):
            with patch("dashboard.Thread") as thread:
                self.assertEqual(self.client.post(project + "/runs", data={"csrf": self.csrf(project)}).status_code, 303)
            self.assertEqual(thread.call_args.kwargs["args"][2], ("AQVNenvkey0000", "b1genv"))


@patch.dict(os.environ, NO_ENV)
class AgentRoutesTest(DashboardCase):
    def setUp(self):
        super().setUp()
        self.runner = Mock()
        self.app = create_app(self.path, Mock(), agent_session_runner=self.runner)
        self.client = self.app.test_client()

    def test_start_status_resume_and_single_active_session(self):
        project = self.create_project()
        project_id = int(project.rstrip("/").split("/")[-1])
        page = self.client.get(project + "/positions").get_data(as_text=True)
        self.assertIn("🚀 Запустить агента", page)
        self.assertIn(f'value="{project_id}:2"', page)
        self.assertIn("noVNC", self.client.get("/agent").get_data(as_text=True))
        csrf = self.csrf("/agent")
        self.assertEqual(self.client.post("/agent/start", data={"csrf": csrf, "target": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/agent/start", data={"csrf": csrf, "target": f"{project_id}:99"}).status_code, 409)
        with patch("dashboard.Thread") as thread:
            response = self.client.post("/agent/start", data={"csrf": csrf, "target": f"{project_id}:2"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(thread.call_args.kwargs["target"], self.runner)
        session_id = thread.call_args.kwargs["args"][1]
        self.assertTrue(response.location.endswith(f"/agent?session={session_id}"))
        with patch("dashboard.Thread"):
            busy = self.client.post("/agent/start", data={"csrf": csrf, "target": f"{project_id}:"})
        self.assertEqual(busy.status_code, 409)
        status = self.client.get(f"/agent/{session_id}/status.json").get_json()
        self.assertEqual((status["state"], status["status"], status["label"]), ("running", "STARTING", "Запуск"))
        # «Продолжить» is accepted only while the agent waits for the operator.
        self.assertEqual(self.client.post(f"/agent/{session_id}/resume", data={"csrf": csrf}).status_code, 409)
        with connect(self.path) as db:
            db.execute("UPDATE agent_sessions SET status = 'CAPTCHA' WHERE id = ?", (session_id,))
        page = self.client.get(f"/agent?session={session_id}").get_data(as_text=True)
        self.assertIn("Капча — ожидание оператора", page)
        self.assertIn("Продолжить", page)
        self.assertEqual(self.client.post(f"/agent/{session_id}/resume", data={"csrf": csrf}).status_code, 303)
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT resume FROM agent_sessions").fetchone()[0], 1)
        self.assertEqual(self.client.get("/agent/999/status.json").status_code, 404)

    def test_restart_marks_abandoned_session_interrupted(self):
        project = self.create_project()
        project_id = int(project.rstrip("/").split("/")[-1])
        with connect(self.path) as db:
            session_id = start_agent_session(db, project_id)
        create_app(self.path, Mock())
        with connect(self.path) as db:
            row = db.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        self.assertEqual((row["state"], row["status"]), ("interrupted", "ERROR"))

    def test_session_command_runs_agent_in_container_and_reports_failure(self):
        project = self.create_project()
        project_id = int(project.rstrip("/").split("/")[-1])
        with connect(self.path) as db:
            session_id = start_agent_session(db, project_id)
        options = SimpleNamespace(
            local_agent=False, agent_container="dp-agent", container_db="/data/panel.sqlite3",
            container_log_dir="/data/logs", log_dir=Path(self.directory.name), debug_log=False,
        )
        command, _ = agent_command(self.path, options, "--session-id", str(session_id))
        self.assertEqual(command[:2], ["docker", "exec"])
        self.assertIn("dp-agent", command)
        self.assertEqual(command[-2:], ["--session-id", str(session_id)])
        failure = subprocess.CalledProcessError(125, command)
        with patch("dashboard.agent_process.subprocess.run", side_effect=failure):
            execute_agent_session(self.path, session_id, options)
        with connect(self.path) as db:
            row = db.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        self.assertEqual((row["state"], row["status"]), ("failed", "ERROR"))


class FakeBrowser:
    tab_id = "main"

    def __init__(self, captcha=False):
        self.captcha = captcha

    def get_tabs(self, as_id=False):
        return ["main"]

    def get_tab(self, tab_id):
        return SimpleNamespace(url="https://ya.ru/showcaptcha" if self.captcha else "https://ya.ru/", ele=lambda *a, **k: None)


@patch.dict(os.environ, NO_ENV)
class AgentSessionTest(DashboardCase):
    def setUp(self):
        super().setUp()
        project = self.create_project()
        self.project_id = int(project.rstrip("/").split("/")[-1])
        self.clock = [0.0]

    def session(self, search, ordinal=None, **options):
        with connect(self.path) as db:
            session_id = start_agent_session(db, self.project_id, ordinal)
        session = AgentSession(
            self.path, session_id, search=search, delay=0, wait=lambda seconds: self.clock.__setitem__(0, self.clock[0] + seconds),
            clock=lambda: self.clock[0], **options,
        )
        return session_id, session

    def row(self, session_id):
        with connect(self.path) as db:
            return db.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()

    def test_main_query_then_refined_query_and_live_report(self):
        queries = []

        def search(browser, config, result, journal, home):
            queries.append((config.keyword, config.domain, config.max_pages))
            if config.keyword == "Ремонт":
                raise TargetNotFound("Целевой домен отсутствует в выдаче (страниц: 5, позиций: 50)")
            result.update(search_result_found=True, search_position=14, search_page=2)
            journal.record("Клик по результату", PROJECT_URL)
            return SimpleNamespace(url=PROJECT_URL)

        session_id, session = self.session(search)
        self.assertEqual(session.execute(lambda identity: FakeBrowser(), lambda browser: None), 0)
        self.assertEqual(queries[:3], [
            ("Натяжные потолки", "master-bobr.ru", 5), ("Ремонт", "master-bobr.ru", 5),
            ("Ремонт master-bobr", "master-bobr.ru", 5),
        ])
        # The refined query is used only when the main phrase did not find the site.
        self.assertNotIn("Натяжные потолки Бобр", [query for query, *_ in queries])
        row = self.row(session_id)
        self.assertEqual((row["state"], row["status"], row["message"]), ("complete", "DONE", "Найдено 4 из 4 фраз."))
        report = json.loads(row["report_json"])
        self.assertEqual(report["keywords"][1], {
            "phrase": "Ремонт", "query": "Ремонт master-bobr", "found": True, "position": 14, "page": 2,
            "landing_url": PROJECT_URL, "refined": True, "profile": "временный", "proxy": None,
        })
        self.assertEqual(self.clock[0], 45)  # Project interval between phrases: 3 × 15 s.
        status = self.client.get(f"/agent/{session_id}/status.json").get_json()
        self.assertEqual(status["label"], "Завершено")
        self.assertTrue(status["events"])

    def test_captcha_waits_for_operator_and_resumes(self):
        attempts = []

        def search(browser, config, result, journal, home):
            attempts.append(config.keyword)
            if len(attempts) == 1:
                raise SearchChallenge("https://ya.ru/showcaptcha")
            result.update(search_position=3, search_page=1)
            return SimpleNamespace(url=PROJECT_URL)

        session_id, session = self.session(search, ordinal=0)
        original = session.wait

        def wait(seconds):
            original(seconds)
            if self.row(session_id)["status"] == "CAPTCHA":
                with connect(self.path) as db:
                    db.execute("UPDATE agent_sessions SET resume = 1 WHERE id = ?", (session_id,))

        session.wait = wait
        self.assertEqual(session.execute(lambda identity: FakeBrowser(captcha=True), lambda browser: None), 0)
        self.assertEqual(attempts, ["Натяжные потолки", "Натяжные потолки"])
        report = json.loads(self.row(session_id)["report_json"])
        self.assertTrue(report["captcha_encountered"])
        self.assertIn("Оператор нажал «Продолжить»", [event["operation"] for event in report["events"]])

    def test_unsolved_captcha_times_out(self):
        def search(browser, config, result, journal, home):
            raise SearchChallenge("https://ya.ru/showcaptcha")

        session_id, session = self.session(search, ordinal=0, captcha_wait=60, poll=10)
        self.assertEqual(session.execute(lambda identity: FakeBrowser(captcha=True), lambda browser: None), 1)
        row = self.row(session_id)
        self.assertEqual((row["state"], row["status"]), ("failed", "ERROR"))
        self.assertIn("Капча не решена", row["message"])


class ConfigImportTest(DashboardCase):
    EXAMPLE = ROOT / "tests" / "fixtures" / "panel.yaml"
    TEMPLATE = ROOT / "examples" / "panel.example.yaml"

    def write(self, text):
        config = Path(self.directory.name) / "panel.yaml"
        config.write_text(text, encoding="utf-8")
        return config

    def projects(self):
        with connect(self.path) as db:
            return [
                (row["name"], row["url"], row["region"], [
                    (word["phrase"], word["hint_type"], word["brand"]) for word in project_keywords(db, row["id"])
                ], project_settings(db, row["id"]))
                for row in db.execute("SELECT * FROM projects ORDER BY id")
            ]

    def test_example_config_creates_then_updates_projects(self):
        self.assertEqual(import_config(self.path, self.EXAMPLE), "Конфигурация загружена: проектов создано 2, обновлено 0.")
        shop, service = self.projects()
        self.assertEqual(shop[:3], ("Пример — интернет-магазин", "https://example-shop.ru/", "213"))
        self.assertEqual(shop[3], [
            ("купить диван", "domain", ""), ("угловой диван недорого", "brand", "Пример Шоп"),
            ("диван кровать с доставкой", "brand", "ПримерШоп"), ("диваны москва", "domain", ""),
        ])
        self.assertEqual(shop[4]["brand_queries"], "Пример Шоп")
        self.assertEqual(shop[4]["collect_daily"], 1)
        self.assertEqual(service[:3], ("Пример — услуги", "https://example-service.ru", "2"))
        with connect(self.path) as db:
            self.assertEqual(panel_setting(db, "novnc_url"), "http://localhost:56565/")
            self.assertEqual(panel_setting(db, "position_source"), "api")
            self.assertIsNone(panel_setting(db, "yandex_api_key"))

        config = self.write(
            "settings:\n  yandex_api_key: AQVN0123456789abcdef\n  yandex_folder_id: b1gexample\n"
            "projects:\n  - url: https://example-shop.ru/\n    name: Магазин\n    region: 213\n"
            "    keywords: [купить диван, диван недорого]\n"
        )
        self.assertEqual(import_config(self.path, config), "Конфигурация загружена: проектов создано 0, обновлено 1.")
        shop, service = self.projects()
        self.assertEqual(shop[0], "Магазин")
        self.assertEqual([word[0] for word in shop[3]], ["купить диван", "диван недорого"])
        self.assertEqual(shop[4]["brand_queries"], "Пример Шоп")
        with connect(self.path) as db:
            self.assertEqual(panel_setting(db, "yandex_api_key"), "AQVN0123456789abcdef")
            self.assertEqual(panel_setting(db, "yandex_folder_id"), "b1gexample")
        with patch.dict(os.environ, NO_ENV):
            page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("••••cdef", page)
        self.assertNotIn("AQVN0123456789abcdef", page)

    def test_invalid_config_is_rejected_without_changes(self):
        cases = {
            "conversions:\n  cart: true\n": "Конверсии (корзина, формы, регистрация",
            "settings:\n  yandex_folder_id: Bad Folder\n": "YANDEX_FOLDER_ID: латинские буквы и цифры",
            "settings:\n  novnc_url: ftp://host/\n": "Адрес noVNC должен начинаться",
            "projects:\n  - url: shop.ru\n    keywords:\n      - phrase: диван\n        hint_type: zone\n":
                "Проект 1: Выберите тип подсказки в строке 1.",
            "projects:\n  - url: shop.ru\n    keywords: [диван, Диван]\n":
                "Проект 1: Поисковая фраза в строке 2 уже есть в проекте.",
            "projects:\n  - url: shop.ru\n    keywords: [диван]\n    clicks: 5\n": "Проект 1: неизвестный ключ «clicks»",
            "projects:\n  - url: shop.ru\n    keywords: [диван]\n  - url: bad url\n    keywords: [диван]\n":
                "Проект 2: Введите адрес сайта",
            "projects: [\n": "не является корректным YAML",
        }
        for text, message in cases.items():
            with self.subTest(message):
                with self.assertRaises(ValueError) as caught:
                    import_config(self.path, self.write(text))
                self.assertIn(message, str(caught.exception))
                self.assertEqual(self.projects(), [])
        self.assertEqual(read_panel_config(""), (None, []))

    def test_entrypoint_loads_config(self):
        argv = ["dashboard", "--no-browser", "--db", str(self.path)]
        with patch("dashboard.__main__.load_dotenv"), patch("flask.Flask.run") as run, \
                patch("dashboard.__main__.start_scheduler"), patch("dashboard.__main__.ensure_container"):
            with patch("sys.argv", argv + ["--config", str(self.EXAMPLE)]):
                main()
            self.assertEqual(len(self.projects()), 2)
            run.reset_mock()
            with patch("sys.argv", argv + ["--config", str(self.write("conversions: {}\n"))]), \
                    self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()


class EntrypointTest(unittest.TestCase):
    def test_panel_opens_in_browser_unless_disabled(self):
        opener = Mock()
        open_panel("http://127.0.0.1:8080/", opener)
        opener.assert_called_once_with("http://127.0.0.1:8080/")
        with TemporaryDirectory() as directory:
            for argv, opened in (
                (["dashboard", "--port", "8091"], True), (["dashboard", "--no-browser", "--no-container"], False),
            ):
                with (
                    patch("sys.argv", argv + ["--db", str(Path(directory) / "panel.sqlite3")]),
                    patch("dashboard.__main__.load_dotenv"), patch("flask.Flask.run"),
                    patch("dashboard.__main__.start_scheduler"), patch("dashboard.__main__.Timer") as timer,
                    patch("dashboard.__main__.ensure_container") as container,
                ):
                    main()
                self.assertEqual(timer.called, opened)
                self.assertEqual(container.called, opened)
                if opened:
                    self.assertEqual(timer.call_args.args[2], ("http://127.0.0.1:8091/",))

    def test_one_command_starts_stopped_agent_container(self):
        def docker(state, start=0, error=None):
            calls = []

            def run(command, **options):
                calls.append(command[:2])
                if error:
                    raise error
                if command[1] == "inspect":
                    return SimpleNamespace(returncode=0 if state else 1, stdout=f"{state}\n", stderr="")
                return SimpleNamespace(returncode=start, stdout="", stderr="no such image" if start else "")
            return run, calls

        run, calls = docker("false")
        self.assertEqual(ensure_container("dp-agent", run), "Контейнер dp-agent запущен.")
        self.assertEqual(calls, [["docker", "inspect"], ["docker", "start"]])
        run, calls = docker("true")
        self.assertEqual(ensure_container("dp-agent", run), "Контейнер dp-agent уже запущен.")
        self.assertEqual(calls, [["docker", "inspect"]])
        self.assertIn("не найден", ensure_container("dp-agent", docker("")[0]))
        self.assertIn("no such image", ensure_container("dp-agent", docker("false", start=1)[0]))
        self.assertIn("Docker недоступен", ensure_container("dp-agent", docker("", error=FileNotFoundError("docker"))[0]))

    def test_chromium_flags_for_container(self):
        from modules import browser

        for in_docker, expected in ((True, True), (False, False)):
            with (
                patch.dict(os.environ, {"CHROME_PATH": "/usr/bin/chromium"}),
                patch("modules.browser.ChromiumPage", side_effect=lambda options: options),
                patch("modules.browser.os.geteuid", return_value=1000, create=True),
                patch("modules.browser.os.path.exists", return_value=in_docker),
            ):
                options = browser.open_browser(5)
            self.assertIn("--disable-dev-shm-usage", options.arguments)
            self.assertEqual("--no-sandbox" in options.arguments, expected)

    def test_config_search_depth(self):
        config = read_config(ROOT / "tests" / "fixtures" / "agent.yaml")
        self.assertEqual(config.max_pages, 5)
        self.assertIn("Pager-Item_type_next", config.next_page)
