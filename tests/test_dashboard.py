import base64
from io import BytesIO
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from werkzeug.datastructures import MultiDict

from dashboard import create_app
from dashboard.runner import execute_run
from dashboard.search import ENDPOINT, SearchError, YandexSearch, domain_hint
from dashboard.storage import connect

PROJECT_URL = "https://www.master-bobr.ru/"
LANDING = "https://master-bobr.ru/potolki/"
MISMATCH = "https://master-bobr.ru/catalog/"
OTHER = "https://master-bobr.ru.example.org/potolki/"


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "panel.sqlite3"
        self.mock_search = Mock()
        self.app = create_app(self.path, self.mock_search)
        self.client = self.app.test_client()

    def csrf(self, path):
        response = self.client.get(path)
        return re.search(r'name="csrf" value="([a-f0-9]+)"', response.get_data(as_text=True))[1]

    def form(self, csrf):
        return MultiDict({
            "csrf": csrf, "name": "Мастер Бобр", "url": PROJECT_URL,
            "interval_seconds": "15", "region": "213",
            "phrase": ["Натяжные потолки", "Ремонт", "Монтаж", "Комнаты"],
            "landing_url": [LANDING, "", "", ""],
            "hint_type": ["brand", "domain", "domain", "brand"],
            "brand": ["Бобр", "", "", "Бобр"],
        })

    def create_project(self):
        response = self.client.post("/projects/new", data=self.form(self.csrf("/projects/new")))
        self.assertEqual(response.status_code, 303)
        return response.location

    def start_run(self, project):
        with patch("dashboard.Thread") as thread:
            response = self.client.post(project + "/runs", data={"csrf": self.csrf(project)})
        self.assertEqual(response.status_code, 303)
        thread.return_value.start.assert_called_once()
        return thread.call_args.kwargs["args"][1]

    def test_project_routes_validation_and_csrf(self):
        self.assertEqual(self.client.post("/projects/new").status_code, 400)
        form = self.form(self.csrf("/projects/new"))
        form.setlist("brand", ["", "", "", "Бобр"])
        response = self.client.post("/projects/new", data=form)
        self.assertEqual(response.status_code, 422)
        self.assertIn("Введите бренд для фразы в строке 1.", response.get_data(as_text=True))
        self.assertIn("Натяжные потолки", response.get_data(as_text=True))
        form.setlist("brand", ["Бобр", "", "", "Бобр"])
        form.setlist("landing_url", [OTHER, "", "", ""])
        self.assertEqual(self.client.post("/projects/new", data=form).status_code, 422)

        project = self.create_project()
        form = self.form(self.csrf(project + "/edit"))
        form["name"] = "Проверка после редактирования"
        response = self.client.post(project + "/edit", data=form, follow_redirects=True)
        self.assertIn("Проверка после редактирования", response.get_data(as_text=True))
        self.assertIn(LANDING, response.get_data(as_text=True))
        self.assertEqual(self.client.get(project + "/runs/999").status_code, 404)

    def test_run_paginates_uses_both_hints_and_preserves_history(self):
        project = self.create_project()
        run_id = self.start_run(project)
        duplicate = self.client.post(project + "/runs", data={"csrf": self.csrf(project)})
        self.assertEqual(duplicate.status_code, 409)

        def result(query, page, region):
            urls = [MISMATCH, OTHER] + ["https://example.net/"] * 8
            replacements = {
                ("Натяжные потолки Бобр", 4): (9, LANDING),
                ("Ремонт master-bobr", 1): (2, PROJECT_URL),
                ("Монтаж", 4): (9, PROJECT_URL),
            }
            if (query, page) in replacements:
                index, url = replacements[query, page]
                urls[index] = url
            if query != "Натяжные потолки" and query != "Натяжные потолки Бобр":
                urls[0] = "https://example.net/"
            return urls

        self.mock_search.side_effect = result
        wait = Mock()
        execute_run(self.path, run_id, self.mock_search, wait=wait)
        run_url = f"{project}/runs/{run_id}"
        history = self.client.get(run_url).get_data(as_text=True)
        self.assertIn("Натяжные потолки Бобр", history)
        self.assertIn("Ремонт master-bobr", history)
        self.assertIn("Вне топ‑50", history)
        with connect(self.path) as db:
            ranks = db.execute(
                "SELECT rank, hint_rank, hinted_at FROM measurements WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        self.assertEqual([(r["rank"], r["hint_rank"]) for r in ranks], [
            (None, 50), (None, 13), (50, None), (None, None),
        ])
        self.assertIsNone(ranks[2]["hinted_at"])
        self.assertIsNotNone(ranks[3]["hinted_at"])
        calls = [call.args for call in self.mock_search.call_args_list]
        self.assertEqual(calls[:5], [("Натяжные потолки", page, "213") for page in range(5)])
        self.assertLessEqual(max(page for _, page, _ in calls), 4)
        self.assertNotIn("Монтаж master-bobr", [query for query, _, _ in calls])
        self.assertEqual([call.args for call in wait.call_args_list], [(15,), (15,), (15,)])

        form = self.form(self.csrf(project + "/edit"))
        form.setlist("phrase", ["Новое название", "Ремонт", "Монтаж", "Комнаты"])
        self.client.post(project + "/edit", data=form)
        self.assertIn("Натяжные потолки Бобр", self.client.get(run_url).get_data(as_text=True))

    def test_search_error_and_restart_keep_completed_data(self):
        project = self.create_project()
        run_id = self.start_run(project)
        self.mock_search.side_effect = [[LANDING], SearchError("Проверка подключения")]
        with self.assertLogs("dashboard.runner", level="WARNING"):
            execute_run(self.path, run_id, self.mock_search, wait=Mock())
        with connect(self.path) as db:
            run = db.execute("SELECT state, error FROM runs WHERE id = ?", (run_id,)).fetchone()
            rows = db.execute("SELECT checked_at FROM measurements WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()
        self.assertEqual(tuple(run), ("failed", "Проверка подключения"))
        self.assertIsNotNone(rows[0]["checked_at"])
        self.assertIsNone(rows[1]["checked_at"])
        self.start_run(project)
        restarted = create_app(self.path, self.mock_search).test_client()
        self.assertIn("Прервана", restarted.get(project).get_data(as_text=True))

    def test_domain_suffix_and_unconfigured_source(self):
        self.assertEqual(domain_hint("https://www.shop.example.co.uk/"), "example")
        project = self.create_project()
        client = create_app(self.path).test_client()
        response = client.get(project)
        self.assertIn("Настройки подключения", response.get_data(as_text=True))
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', response.get_data(as_text=True))[1]
        self.assertEqual(client.post(project + "/runs", data={"csrf": csrf}).status_code, 409)


class YandexSearchTest(unittest.TestCase):
    def response(self, document):
        payload = base64.b64encode(document.encode()).decode()
        return BytesIO(json.dumps({"rawData": payload}).encode())

    @patch("dashboard.search.urlopen")
    def test_request_and_xml(self, open_url):
        open_url.return_value = self.response(f"""
            <yandexsearch><response><results><grouping>
            <group><doc><url>{LANDING}</url></doc></group>
            </grouping></results></response></yandexsearch>
        """)
        self.assertEqual(YandexSearch("test-key", "test-folder")("поиск", 4, "213"), [LANDING])
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.headers["Authorization"], "Api-Key test-key")
        body = json.loads(request.data)
        self.assertEqual(body["query"]["page"], "4")
        self.assertEqual(body["query"]["queryText"], "поиск")
        self.assertEqual(body["region"], "213")
        self.assertEqual(body["groupSpec"]["groupsOnPage"], "10")
        self.assertEqual(body["folderId"], "test-folder")

    @patch("dashboard.search.urlopen")
    def test_empty_page_and_access_failure(self, open_url):
        open_url.return_value = self.response('<yandexsearch><response><error code="15"/></response></yandexsearch>')
        self.assertEqual(YandexSearch("key", "folder")("поиск", 0, ""), [])
        for code in (403, 429):
            with self.subTest(code=code):
                open_url.side_effect = HTTPError(ENDPOINT, code, "error", {}, None)
                with self.assertRaisesRegex(SearchError, str(code)):
                    YandexSearch("key", "folder")("поиск", 0, "")


if __name__ == "__main__":
    unittest.main()
