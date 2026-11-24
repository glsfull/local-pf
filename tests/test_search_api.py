import base64
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from xml.sax.saxutils import escape

from agent import API_SOURCE_LABEL, verify_positions
from dashboard import create_app
from dashboard.agent_process import execute_agent
from dashboard.search import SearchError
from dashboard.storage import connect, save_project
from modules.journal import Journal
from modules.search_api import OPERATION_ENDPOINT, SEARCH_ENDPOINT, YandexDeferredSearch, parse_results


def xml_answer(urls=None, error=None):
    if error is not None:
        body = f'<error code="{error}">Искомая комбинация слов нигде не встречается</error>'
    else:
        docs = "".join(f"<group><doc><url>{escape(url)}</url></doc></group>" for url in urls)
        body = f"<results><grouping>{docs}</grouping></results>"
    return f'<?xml version="1.0" encoding="utf-8"?><yandexsearch><response>{body}</response></yandexsearch>'


class FakeApi:
    """Plays searchAsync and the operations endpoint: every answer needs `pending` extra polls."""

    def __init__(self, answers, pending=1):
        self.answers = answers
        self.pending = pending
        self.requests = []
        self.operations = {}

    @contextmanager
    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), request.full_url, body, request.get_header("Authorization")))
        if request.full_url == SEARCH_ENDPOINT:
            operation_id = f"op{len(self.operations) + 1}"
            self.operations[operation_id] = [body["query"]["queryText"], self.pending]
            payload = {"id": operation_id, "done": False}
        else:
            operation_id = request.full_url.removeprefix(OPERATION_ENDPOINT)
            query, left = self.operations[operation_id]
            self.operations[operation_id][1] -= 1
            answer = self.answers[query]
            if left > 0:
                payload = {"id": operation_id, "done": False}
            elif "code" in answer:
                payload = {"id": operation_id, "done": True, "error": answer}
            else:
                xml = xml_answer(**answer).encode("utf-8")
                payload = {"id": operation_id, "done": True, "response": {"rawData": base64.b64encode(xml).decode()}}
        yield SimpleNamespace(read=lambda size: json.dumps(payload).encode("utf-8"))

    def polls(self):
        return [url for method, url, _, _ in self.requests if method == "GET"]


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class DeferredSearchTest(unittest.TestCase):
    def source(self, api, **options):
        self.report = {"events": [], "pages_checked": []}
        self.clock = Clock()
        return YandexDeferredSearch(
            "secret", "folder", Journal(self.report["events"], 0), self.report,
            poll_interval=60, max_wait=600, wait=self.clock.wait, clock=self.clock, opener=api, **options,
        )

    def test_deferred_request_is_polled_until_done(self):
        api = FakeApi({"окна": {"urls": ["https://a.example/", "https://www.target.ru/page"]}}, pending=2)
        source = self.source(api)
        self.assertEqual(source("окна", 0, "213"), ["https://a.example/", "https://www.target.ru/page"])
        self.assertEqual(source("окна", 1, "213"), [])
        self.assertEqual(source("окна", 0, "213"), ["https://a.example/", "https://www.target.ru/page"])
        method, url, body, auth = api.requests[0]
        self.assertEqual((method, url, auth), ("POST", SEARCH_ENDPOINT, "Api-Key secret"))
        self.assertEqual(body["groupSpec"]["groupsOnPage"], "50")
        self.assertEqual((body["folderId"], body["region"], body["responseFormat"]), ("folder", "213", "FORMAT_XML"))
        self.assertEqual(api.polls(), [OPERATION_ENDPOINT + "op1"] * 3)
        self.assertIn(60, self.clock.waits)
        self.assertEqual(
            [event["operation"] for event in self.report["events"]],
            ["Отложенный запрос отправлен", "Ожидание ответа API", "Ответ API получен"],
        )
        self.assertEqual(self.report["events"][-1]["result_count"], 2)
        self.assertEqual([page["status"] for page in self.report["pages_checked"]], ["OK"])

    def test_prepare_submits_every_phrase_once_before_polling(self):
        api = FakeApi({"a": {"urls": ["https://a.example/"]}, "b": {"error": 15}})
        source = self.source(api)
        source.prepare(["a", "b", "a"], None)
        self.assertEqual([method for method, *_ in api.requests], ["POST", "POST"])
        self.assertNotIn("region", api.requests[0][2])
        self.assertEqual(source("b", 0, None), [])
        self.assertEqual(source("a", 0, None), ["https://a.example/"])
        self.assertEqual(len([method for method, *_ in api.requests if method == "POST"]), 2)

    def test_operation_error_and_timeout_raise_search_error(self):
        source = self.source(FakeApi({"a": {"code": 3, "message": "Invalid region"}}, pending=0))
        with self.assertRaisesRegex(SearchError, "Invalid region"):
            source("a", 0, None)
        self.assertEqual(self.report["pages_checked"][0]["status"], "API_ERROR")
        source = self.source(FakeApi({"a": {"urls": []}}, pending=100))
        with self.assertRaisesRegex(SearchError, "600"):
            source("a", 0, None)
        self.assertEqual(self.report["pages_checked"][0]["status"], "TIMEOUT")
        self.assertLessEqual(self.clock.now, 601 + 11)

    def test_http_error_and_bad_xml(self):
        def refuse(request, timeout):
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

        with self.assertRaisesRegex(SearchError, "HTTP 403"):
            self.source(refuse)("a", 0, None)
        with self.assertRaisesRegex(SearchError, "XML"):
            parse_results(b"<oops")
        with self.assertRaisesRegex(SearchError, "Yandex Search API"):
            parse_results(xml_answer(error=55).encode())
        with self.assertRaisesRegex(ValueError, "YANDEX_SEARCH_API_KEY"):
            YandexDeferredSearch("", "folder", None, {})

    def test_agent_measures_positions_through_deferred_api(self):
        api = FakeApi({
            "окна": {"error": 15},
            "окна Бренд": {"urls": ["https://other.example/", "https://www.example.org/target/"]},
        })
        with TemporaryDirectory() as output:
            root = Path(output)
            path = root / "dashboard.sqlite3"
            client = create_app(path).test_client()
            with connect(path) as db:
                project_id = save_project(db, None, ("API", "https://example.org", "213", 0), [
                    ("окна", "https://example.org/target/", "brand", "Бренд"),
                ])
            with (
                patch.dict("os.environ", {
                    "YANDEX_SEARCH_API_KEY": "secret", "YANDEX_SEARCH_FOLDER_ID": "folder",
                    "LOCAL_PF_OBSERVATION_DELAY": "0", "LOCAL_PF_API_POLL_SECONDS": "1",
                }),
                patch("modules.search_api.urlopen", api),
                patch("modules.search_api.time.sleep"),
                patch("modules.rank_parser.open_browser") as browser,
            ):
                self.assertEqual(verify_positions(path, root, project_id=project_id, source_kind="api"), 0)
            browser.assert_not_called()
            with connect(path) as db:
                run = db.execute("SELECT * FROM runs").fetchone()
                row = db.execute("SELECT * FROM measurements").fetchone()
            self.assertEqual(run["source"], API_SOURCE_LABEL)
            self.assertEqual((row["rank"], row["hint_rank"]), (None, 2))
            self.assertEqual(row["hint_found_url"], "https://www.example.org/target/")
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual((report["status"], report["source"]), ("OK", "api"))
            self.assertFalse(report["captcha_encountered"])
            html = client.get(f"/projects/{project_id}/runs/{run['id']}/logs").get_data(as_text=True)
            self.assertIn("Отложенный запрос отправлен", html)
            self.assertIn("Ответ API получен", html)

    def test_dashboard_passes_api_source_to_agent(self):
        options = SimpleNamespace(
            local_agent=True, log_dir=Path("/tmp/logs"), debug_log=False, search_api=True,
        )
        with TemporaryDirectory() as output:
            path = Path(output) / "dashboard.sqlite3"
            create_app(path)
            with connect(path) as db:
                db.execute("INSERT INTO projects(name, url, region, interval_seconds) VALUES ('p', 'https://e.org', '', 0)")
                run_id = db.execute(
                    "INSERT INTO runs(project_id, project_url, interval_seconds, region, source, state, started_at)"
                    " VALUES (1, 'https://e.org', 0, '', 'x', 'complete', 'now')"
                ).lastrowid
            with patch("dashboard.agent_process.subprocess.run") as run:
                execute_agent(path, run_id, options)
        self.assertEqual(run.call_args.args[0][-2:], ["--source", "api"])


if __name__ == "__main__":
    unittest.main()
