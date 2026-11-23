from contextlib import chdir
import json
from pathlib import Path
import re
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from werkzeug.datastructures import MultiDict

from dashboard import create_app
from dashboard.__main__ import MoscowRequestHandler, main
from dashboard.agent_process import execute_agent
from dashboard.runner import execute_run
from dashboard.search import SearchError, domain_hint, match_position
from dashboard.storage import SCHEMA, connect, finish_run, run_lock

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
        self.assertIn(LANDING, self.client.get(project + "/keywords").get_data(as_text=True))
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
        for heading in (
            "Ключевая фраза", "Позиция без подсказки", "Уточнённый запрос", "Позиция с подсказкой",
            "URL найденной страницы", "Дата и время (МСК)", "Статус",
        ):
            self.assertIn(f"<th>{heading}</th>", history)
        with connect(self.path) as db:
            ranks = db.execute(
                "SELECT * FROM measurements WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        self.assertEqual([(r["rank"], r["hint_rank"]) for r in ranks], [
            (None, 50), (None, 13), (50, None), (None, None),
        ])
        self.assertIsNone(ranks[2]["hinted_at"])
        self.assertIsNotNone(ranks[3]["hinted_at"])
        self.assertEqual(
            [(row["found_url"], row["hint_found_url"]) for row in ranks],
            [(None, LANDING), (None, PROJECT_URL), (PROJECT_URL, None), (None, None)],
        )
        self.assertTrue(ranks[0]["checked_at"].endswith("+03:00"))
        self.assertTrue(ranks[0]["hinted_at"].endswith("+03:00"))
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
        self.mock_search.side_effect = [[LANDING], [], SearchError("Выдача вернула HTTP 503.")]
        with self.assertLogs("dashboard.runner", level="WARNING"):
            execute_run(self.path, run_id, self.mock_search, wait=Mock())
        with connect(self.path) as db:
            run = db.execute("SELECT state, error FROM runs WHERE id = ?", (run_id,)).fetchone()
            rows = db.execute("SELECT * FROM measurements WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()
        self.assertEqual(tuple(run), ("failed", "Выдача вернула HTTP 503."))
        self.assertEqual(rows[0]["found_url"], LANDING)
        self.assertIsNotNone(rows[0]["checked_at"])
        self.assertIsNotNone(rows[1]["checked_at"])
        self.assertIsNotNone(rows[1]["hinted_at"])
        self.assertIsNone(rows[1]["error"])
        self.assertEqual(rows[1]["hint_error"], run["error"])
        self.assertIsNone(rows[2]["checked_at"])
        html = self.client.get(project).get_data(as_text=True)
        self.assertIn("NOT_FOUND", html)
        self.assertIn("HTTP 503", html)
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

    def test_keyword_and_cluster_metrics_are_optional_and_persist(self):
        form = self.form(self.csrf("/projects/new"))
        form.update({"region_name": "Москва", "spent": "35.50"})
        for name, values in {
            "iw": ["120", "80", "", ""], "demand": ["-124", "", "", ""],
            "cluster": ["Потолки", "Потолки", "", ""], "chance": ["62", "", "", ""],
            "power": ["50", "30", "", ""], "visits": ["12", "8", "", ""],
            "competition": ["high", "", "", ""], "budget": ["10", "20", "", ""],
        }.items():
            form.setlist(name, values)
        project = self.client.post("/projects/new", data=form).location
        self.assertEqual(project, "/projects/1")
        home = self.client.get("/").get_data(as_text=True)
        for expected in ("Москва", "40%", "35,5 ₽", "20", "30 ₽"):
            self.assertIn(expected, home)
        keywords = self.client.get(project + "/keywords").get_data(as_text=True)
        for expected in ("120", "-124", "62%", "Высокая", "Шанс топа", "Бюджет в день"):
            self.assertIn(expected, keywords)
        clusters_url = project + "/clusters"
        csrf = self.csrf(clusters_url)
        response = self.client.post(clusters_url, data={
            "csrf": csrf, "cluster": "Потолки", "url": LANDING,
            "commercial": "75", "has_meta": "1", "has_text": "0",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        for expected in ("Потолки (2)", LANDING, "200", "75", ">Есть<", ">Нет<"):
            self.assertIn(expected, response.get_data(as_text=True))
        self.assertEqual(self.client.post(clusters_url, data={
            "csrf": csrf, "cluster": "Потолки", "url": OTHER,
        }).status_code, 422)

    def test_date_matrix_keeps_latest_original_and_hinted_samples_separate(self):
        project = self.create_project()
        first = self.start_run(project)
        with connect(self.path) as db:
            db.execute(
                """UPDATE measurements SET rank=4, found_url=?, checked_at='2026-11-22T22:00:00+00:00'
                   WHERE run_id=? AND ordinal=0""",
                (LANDING, first),
            )
            finish_run(db, first, "complete")
        second = self.start_run(project)
        with connect(self.path) as db:
            db.execute(
                """UPDATE measurements SET checked_at='2026-11-23T23:00:00+03:00', hint_rank=2,
                   hint_found_url=?, hinted_at='2026-11-24T00:00:00+03:00' WHERE run_id=? AND ordinal=0""",
                (LANDING, second),
            )
            db.execute(
                """UPDATE measurements SET rank=1, checked_at='2026-11-24T01:00:00+03:00'
                   WHERE run_id=? AND ordinal=1""",
                (second,),
            )
            finish_run(db, second, "complete")
        response = self.client.get(project)
        matrix = response.get_data(as_text=True)
        self.assertIn('datetime="2026-11-24"', matrix)
        self.assertIn('datetime="2026-11-23"', matrix)
        self.assertIn('class="matrix-cell top-1"', matrix)
        self.assertIn('class="matrix-cell outside"', matrix)
        self.assertIn('class="matrix-cell top-3"', matrix)
        self.assertNotIn('class="matrix-cell top-10"', matrix)
        self.assertIn("Проверка не проводилась", matrix)
        self.assertIn("Натяжные потолки Бобр", matrix)
        self.assertIn('data-count="1">1 · 100%', matrix)
        hinted = self.client.get(project + "?query=hint").get_data(as_text=True)
        self.assertIn('data-count="1">1 · 100%', hinted)
        self.assertIn('aria-current="true">С подсказкой', hinted)

    def test_panel_restart_keeps_run_while_agent_owns_lock(self):
        project = self.create_project()
        run_id = self.start_run(project)
        with run_lock(self.path):
            create_app(self.path, self.mock_search)
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0], "running")

    def test_docker_agent_uses_shared_paths_and_preserves_worker_error(self):
        project = self.create_project()
        run_id = self.start_run(project)
        root = Path(__file__).resolve().parents[1]
        options = SimpleNamespace(
            local_agent=False, agent_container="dp-agent", container_db="/shared/dashboard.sqlite3",
            container_log_dir=None, log_dir=root / "logs", debug_log=False,
        )
        command = [
            "docker", "exec", "-e", "DISPLAY=:1", "dp-agent", "/opt/local-pf/bin/python", "/app/agent.py",
            "--db", "/shared/dashboard.sqlite3", "--log-dir", "/app/logs", "--run-id", str(run_id),
        ]

        def report_failure(*args, **kwargs):
            with connect(self.path) as db:
                finish_run(db, run_id, "failed", "CAPTCHA")
            raise CalledProcessError(1, command)

        with patch("dashboard.agent_process.subprocess.run", side_effect=report_failure) as run:
            execute_agent(self.path, run_id, options)
        run.assert_called_once_with(command, env=None, check=True)
        with connect(self.path) as db:
            row = db.execute("SELECT state, error FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(tuple(row), ("failed", "CAPTCHA"))

    def test_legacy_positions_keep_ranks_and_show_moscow_dates(self):
        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        prefix, separator, measurements = SCHEMA.partition("CREATE TABLE IF NOT EXISTS measurements")
        for column in ("found_url", "hint_found_url", "error", "hint_error"):
            measurements = measurements.replace(f"    {column} TEXT,\n", "")
        with connect(legacy_path) as db:
            db.executescript(prefix + separator + measurements)
            db.execute("INSERT INTO projects VALUES (1, 'Старый проект', ?, '', 0)", (PROJECT_URL,))
            db.execute(
                "INSERT INTO runs VALUES (1, 1, ?, 0, '', 'Yandex Search API', 'complete', NULL, ?, ?)",
                (PROJECT_URL, "2026-11-23T22:24:30+00:00", "2026-11-23T22:25:00+00:00"),
            )
            db.execute(
                """INSERT INTO measurements(run_id, ordinal, phrase, landing_url, hint_query, rank, checked_at)
                   VALUES (1, 0, 'Старая фраза', ?, 'Старая фраза Бобр', 4, '2026-11-23T22:25:00+00:00')""",
                (LANDING,),
            )
        client = create_app(legacy_path).test_client()
        response = client.get("/projects/1")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("24.11.2026, 01:25:00 MSK", html)
        self.assertIn("Старая фраза", html)
        self.assertIn("Задан " + LANDING, html)
        with connect(legacy_path) as db:
            row = db.execute("SELECT rank, found_url, hint_error FROM measurements").fetchone()
        self.assertEqual(tuple(row), (4, None, None))

    def test_working_entrypoint_starts_empty_and_reads_agent_report(self):
        directory = Path(self.directory.name)
        report_path = directory / "report.json"
        clients = []

        def capture(app, **options):
            self.assertEqual(options, {
                "host": "127.0.0.1", "port": 8080, "request_handler": MoscowRequestHandler,
            })
            clients.append(app.test_client())

        with (
            chdir(directory), patch("sys.argv", ["dashboard", "--agent-log-dir", str(directory)]),
            patch("dashboard.__main__.load_dotenv"), patch("flask.Flask.run", autospec=True, side_effect=capture),
        ):
            main()
        with connect(directory / ".local-pf/dashboard.sqlite3") as db:
            for table in ("projects", "keywords", "runs", "measurements"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        client = clients[0]
        self.assertNotIn("Мастер Бобр", client.get("/").get_data(as_text=True))
        self.assertIn("Отчёт появится", client.get("/logs").get_data(as_text=True))
        report_path.write_text(json.dumps({
            "keyword": "Проверка", "target_domain": "example.org",
            "timestamp": "2026-11-23T22:24:30+00:00",
            "events": [{
                "operation": "Позиция прокрутки", "url": "https://example.org/page",
                "timestamp_msk": "2026-11-24T01:24:30+03:00",
                "scroll_position": {"scrollY": 1200, "scrollHeight": 4800, "percent": 30},
            }, {
                "operation": "Проверка элемента", "url": "https://example.org/missing",
                "timestamp_msk": "2026-11-24T01:24:31+03:00",
                "selector": "#expected", "found": False, "status": "NOT_FOUND",
                "http_status": 404, "error": "<script>broken()</script>",
            }],
        }), encoding="utf-8")
        html = client.get("/logs").get_data(as_text=True)
        for content in (
            "24.11.2026, 01:24:30 MSK", "scrollY=1200 / scrollHeight=4800 (30%)",
            "https://example.org/page", "https://example.org/missing", "#expected",
            "Не найден", "HTTP 404", "NOT_FOUND", "&lt;script&gt;broken()&lt;/script&gt;",
        ):
            self.assertIn(content, html)


class SearchRankTest(unittest.TestCase):
    def test_variable_page_sizes_and_cutoff(self):
        read_page = Mock(side_effect=[[OTHER, MISMATCH], [OTHER, LANDING]])
        self.assertEqual(match_position(read_page, "фраза", PROJECT_URL, LANDING, "213"), (4, LANDING))
        self.assertEqual([call.args for call in read_page.call_args_list], [("фраза", 0, "213"), ("фраза", 1, "213")])
        read_page = Mock(return_value=[OTHER] * 50 + [LANDING])
        self.assertEqual(match_position(read_page, "фраза", PROJECT_URL, LANDING, ""), (None, None))
        read_page.assert_called_once()


if __name__ == "__main__":
    unittest.main()
