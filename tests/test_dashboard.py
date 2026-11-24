from contextlib import chdir
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
from subprocess import CalledProcessError, run
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from werkzeug.datastructures import MultiDict

from dashboard import create_app
from dashboard.history import rank_tier
from dashboard.scheduler import due_projects, scheduler_tick
from dashboard.__main__ import MoscowRequestHandler, main
from dashboard.agent_process import execute_agent
from dashboard.runner import execute_run
from dashboard.search import SearchError, domain_hint, match_position
from dashboard.storage import SCHEMA, connect, finish_run, run_lock
from modules.clock import MSK

PROJECT_URL = "https://www.master-bobr.ru/"
LANDING = "https://master-bobr.ru/potolki/"
MISMATCH = "https://master-bobr.ru/catalog/"
OTHER = "https://master-bobr.ru.example.org/potolki/"


class DashboardCase(unittest.TestCase):
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


class PanelTest(DashboardCase):
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
        for expected in ("Москва", "40%", "20", "30 ₽"):
            self.assertIn(expected, home)
        self.assertIn("35,5 ₽", self.client.get(project + "/expenses").get_data(as_text=True))
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
            patch("dashboard.__main__.start_scheduler") as scheduler, patch("dashboard.__main__.Timer") as timer,
        ):
            main()
        scheduler.assert_called_once()
        timer.assert_called_once()
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


class TopPultInterfaceTest(DashboardCase):
    """Issue #9: TopPult-style screens backed by SQLite."""

    def post(self, path, data, page=None, **options):
        return self.client.post(path, data={"csrf": self.csrf(page or path.rsplit("/", 1)[0]), **data}, **options)

    def html(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def import_visits(self, project, text):
        return self.client.post(project + "/visits", data={
            "csrf": self.csrf(project + "/visits"), "visits": (BytesIO(text.encode("utf-8-sig")), "visits.csv"),
        }, content_type="multipart/form-data")

    def test_layout_header_footer_and_project_list(self):
        project = self.create_project()
        home = self.html("/")
        for expected in (
            "Топ<b>Пульт</b>", "Мои проекты", "Инструменты", "Помощь", 'class="balance"', "— ₽",
            "Создать проект", "Настроить группы", "Название</th>", "Переходов сегодня", "Итого",
            '<span class="pill running">Запущен</span>', "© ТопПульт, 2026. Все права защищены",
            "Документация", "Телеграм-группа", "Оферта", "Приватность",
        ):
            self.assertIn(expected, home)
        self.assertNotIn("Переходы по проектам приостановлены", home)
        self.post("/settings", {"balance": "0", "user_name": "seo-user"}, page="/settings")
        home = self.html("/")
        for expected in ("Переходы по проектам приостановлены", '<span class="user-name">seo-user</span>', "0 ₽"):
            self.assertIn(expected, home)
        positions = self.html(project + "/positions")
        self.assertIn("Сбор позиций приостановлен", positions)
        self.assertIn('<button class="button primary" disabled', positions)
        with patch("dashboard.Thread") as thread:
            response = self.post(project + "/runs", {}, page=project)
        self.assertEqual(response.status_code, 409)
        thread.assert_not_called()

    def test_settings_validation_and_help(self):
        response = self.post("/settings", {"balance": "abc"}, page="/settings")
        self.assertEqual(response.status_code, 422)
        response = self.post("/settings", {"telegram_url": "http://t.me/group"}, page="/settings")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.post("/settings", {
            "balance": "1250.5", "user_name": "", "telegram_url": "https://t.me/example_group",
        }, page="/settings").status_code, 303)
        self.assertIn("1 250,5 ₽", self.html("/"))
        self.assertIn("https://t.me/example_group", self.html("/help"))

    def test_create_form_and_extra_settings_persist(self):
        page = self.html("/projects/new")
        for expected in (
            "Создание проекта", "Вернуться к списку проектов", 'placeholder="Например, pfult.ru"',
            "Позиции будут собираться из города: Москва (213).", "Регион поисковых запросов",
            'placeholder="По желанию"', "Дополнительные настройки", "Тип запроса", "Тематика", "Расписание",
            "Если сайт не нашелся по запросу", "Ограничить дневной бюджет",
            "Временно доступна только лайв-выдача", "Собирать позиции ежедневно автоматически",
            "Планировать низкочастотники реже чем раз в день",
        ):
            self.assertIn(expected, page)
        form = self.form(self.csrf("/projects/new"))
        for key, value in {
            "settings_form": "1", "url": "pfult.ru", "name": "", "query_region": "as_is", "frequency_type": "quoted",
            "theme": "", "schedule_mode": "uniform_hours", "not_found": "brand", "brand_queries": "пульт\nтоп пульт",
            "limit_budget": "1", "daily_budget": "500", "collect_daily": "1", "collect_time": "09:30",
        }.items():
            form[key] = value
        form.setlist("schedule", ["0-9", "0-10", "6-23"])
        form.setlist("landing_url", ["", "", "", ""])
        response = self.client.post("/projects/new", data=form)
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        project = response.location
        with connect(self.path) as db:
            row = db.execute("SELECT * FROM projects").fetchone()
            settings = dict(db.execute("SELECT * FROM project_settings").fetchone())
        self.assertEqual((row["url"], row["name"]), ("https://pfult.ru", "pfult.ru"))
        self.assertEqual(settings["query_region"], "as_is")
        self.assertEqual(settings["brand_queries"], "пульт\nтоп пульт")
        self.assertEqual((settings["limit_budget"], settings["daily_budget"]), (1, 500))
        self.assertEqual((settings["collect_time"], settings["rare_lowfreq"]), ("09:30", 0))
        self.assertEqual({i for i, slot in enumerate(settings["schedule"]) if slot == "1"}, {9, 10, 6 * 24 + 23})
        self.assertIn("Собираются в 09:30", self.html(project + "/positions"))
        edit = self.html(project + "/edit")
        self.assertIn('value="as_is" checked', edit)
        self.assertIn('name="schedule" value="6-23" checked', edit)
        form["not_found"], form["brand_queries"] = "brand", ""
        form["csrf"] = self.csrf(project + "/edit")
        self.assertEqual(self.client.post(project + "/edit", data=form).status_code, 422)

    def test_project_tabs_pause_and_schedule(self):
        project = self.create_project()
        for view, title in (
            ("keywords", "Добавить ключевики"), ("clusters", "Перекластеризовать"), ("positions", "Гостевая ссылка"),
            ("visits", "Что означают числа в таблице"), ("schedule", "Сохранить расписание"),
            ("expenses", "Фактический расход"), ("edit", "Сохранить"),
        ):
            page = self.html(f"{project}/{view}")
            self.assertIn(title, page)
            self.assertIn('aria-current="page">', page)
            for tab in ("Ключевики", "Кластеры", "Позиции", "Переходы", "Расписание", "Расходы", "Настройки"):
                self.assertIn(f">{tab}</a>", page)
        response = self.post(project + "/pause", {"return_to": project + "/keywords"}, page=project)
        self.assertEqual(response.location, project + "/keywords")
        self.assertIn("Остановлен", self.html("/"))
        self.assertIn("Запустить", self.html(project))
        self.assertEqual(self.post(project + "/pause", {"return_to": "https://evil.example/"}, page=project).location,
                         project + "/positions")
        self.assertIn("Запущен", self.html("/"))
        self.assertEqual(self.post(project + "/schedule", {
            "schedule_mode": "uniform_hours", "schedule": "1-12", "collect_time": "07:05",
        }, page=project).status_code, 303)
        with connect(self.path) as db:
            settings = db.execute("SELECT * FROM project_settings").fetchone()
        self.assertEqual((settings["schedule"].index("1"), settings["collect_daily"], settings["collect_time"]),
                         (36, 0, "07:05"))
        self.assertEqual(self.post(project + "/schedule", {"schedule_mode": "uniform_hours", "collect_time": "25:00"},
                                   page=project).status_code, 422)

    def test_keywords_tab_edits_and_auto_strategy(self):
        form = self.form(self.csrf("/projects/new"))
        form.setlist("competition", ["high", "low", "", ""])
        form.setlist("iw", ["1200", "5", "", ""])
        project = self.client.post("/projects/new", data=form).location
        page = self.html(project + "/keywords")
        for expected in (
            "Настроить колонки", "Авто-стратегия", "Экспорт", "Без фильтров", "На странице", "Всего",
            'class="flame on"', 'data-column-toggle="power"',
        ):
            self.assertIn(expected, page)
        self.assertEqual(self.post(project + "/keywords/0", {"power": "55.5", "visits": "7"}, page=project).status_code, 303)
        self.assertEqual(self.post(project + "/keywords/1", {"power": "101"}, page=project).status_code, 422)
        self.assertEqual(self.post(project + "/keywords/9", {"power": "1"}, page=project).status_code, 404)
        self.post(project + "/keywords/1", {"auto": "1"}, page=project)
        with connect(self.path) as db:
            values = db.execute("SELECT ordinal, power, visits FROM keyword_attributes ORDER BY ordinal").fetchall()
        self.assertEqual([tuple(row) for row in values][:2], [(0, 55.5, 7), (1, 10, None)])
        self.post(project + "/keywords/auto", {}, page=project)
        with connect(self.path) as db:
            powers = [row[0] for row in db.execute("SELECT power FROM keyword_attributes ORDER BY ordinal")]
        self.assertEqual(powers, [30, 10, 10, 10])
        self.assertIn("В топ-10", self.html(project + "/keywords?filter=top10"))

    def test_clusters_tree_and_recluster(self):
        project = self.create_project()
        page = self.html(project + "/clusters")
        self.assertIn("(не кластеризовано)", page)
        self.assertIn("0 кластеров / 4 ключевиков", page)
        run_id = self.start_run(project)
        with connect(self.path) as db:
            db.execute("UPDATE measurements SET rank=3, found_url=? WHERE run_id=? AND ordinal IN (0, 1)", (LANDING, run_id))
            db.execute("UPDATE measurements SET rank=8, found_url=? WHERE run_id=? AND ordinal=2", (MISMATCH, run_id))
            db.execute("UPDATE measurements SET checked_at='2026-11-24T10:00:00+03:00' WHERE run_id=?", (run_id,))
            finish_run(db, run_id, "complete")
        self.assertEqual(self.post(project + "/clusters/recluster", {}, page=project).status_code, 303)
        page = self.html(project + "/clusters")
        for expected in ("Натяжные потолки (2)", "Монтаж (1)", "(не кластеризовано) (1)", LANDING, MISMATCH, "data-tree-toggle"):
            self.assertIn(expected, page)
        exported = self.client.get(project + "/export/clusters.csv")
        self.assertEqual(exported.mimetype, "text/csv")
        self.assertIn("Натяжные потолки;Ремонт", exported.get_data(as_text=True))

    def test_visits_import_matrix_filters_and_expenses(self):
        project = self.create_project()
        self.assertEqual(self.import_visits(project, "2026-11-24;Ремонт;1\n").status_code, 422)
        self.assertEqual(self.import_visits(project, "\n".join((
            "Дата;Запрос;Выполнено;План;Расход;Глубина",
            "2026-11-24;Натяжные потолки;10;12;120;3.5",
            "2026-11-24;Ремонт;0;5;0;",
            "23.11.2026;Ремонт;4;5;48;1.5",
            "2026-11-24;Старая фраза;2;2;24;2",
        ))).status_code, 303)
        page = self.html(project + "/visits")
        for expected in (
            "14/22 · 168 ₽", 'class="visit-cell visit-high"', 'class="visit-cell visit-miss"',
            'class="visit-cell visit-ok"', "Без брендовых", "Только с брендовыми", "С удаленными",
        ):
            self.assertIn(expected, page)
        self.assertNotIn("Старая фраза", page)
        self.assertIn("Старая фраза", self.html(project + "/visits?deleted=1"))
        with connect(self.path) as db:
            db.execute("UPDATE project_settings SET brand_queries = 'ремонт'")
        brand = self.html(project + "/visits?brand=brand")
        self.assertIn("Ремонт", brand)
        self.assertNotIn("Натяжные потолки\n", brand)
        nobrand = self.html(project + "/visits?brand=nobrand")
        self.assertIn("Натяжные потолки", nobrand)
        self.assertNotIn("Ремонт\n", nobrand)
        expenses = self.html(project + "/expenses")
        for expected in ("192 ₽", "48 ₽", "24.11.2026", "23.11.2026"):
            self.assertIn(expected, expenses)
        csv = self.client.get(project + "/export/visits.csv").get_data(as_text=True)
        self.assertTrue(csv.startswith("﻿Запрос;"))
        self.assertIn("10/12 · 120 ₽", csv)

    def test_positions_tab_chart_matrix_and_exports(self):
        project = self.create_project()
        run_id = self.start_run(project)
        with connect(self.path) as db:
            db.execute("UPDATE measurements SET rank=5, checked_at='2026-11-24T10:00:00+03:00' WHERE run_id=?", (run_id,))
            finish_run(db, run_id, "complete")
        page = self.html(project + "/positions")
        for expected in (
            "Собираются в 21:00", "2 недели", "Месяц", "Лайв-выдача", "Вебмастер", "Экспортировать",
            'id="visibility-chart"', "vendor/chart.umd.min.js", 'class="sticky-1">Запрос',
            '"!W"', "Найденный URL", 'class="matrix-cell top-6"', "Показаны: 1 - 4 из 4", "По 15",
            "Топ 1-3", "Топ 4-6", "Топ 7-10", "Топ 11-20", "Топ 21+",
        ):
            self.assertIn(expected, page)
        self.assertNotIn("vendor/chart.umd.min.js", self.html(project + "/keywords"))
        self.assertIn("Показаны: 3 - 4 из 4", self.html(project + "/positions?per_page=15&page=9").replace("1 - 4", "3 - 4"))
        for kind in ("keywords", "positions"):
            response = self.client.get(f"{project}/export/{kind}.csv")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Натяжные потолки", response.get_data(as_text=True))
        self.assertIn("Ремонт;;;5\r\n", self.client.get(project + "/export/positions.csv").get_data(as_text=True))
        self.assertEqual(self.client.get(project + "/export/runs.csv").status_code, 404)

    def test_position_class_filter_and_js_agree(self):
        ranks = [1, 3, 4, 6, 7, 10, 11, 20, 21, 51, None]
        python = [rank_tier(rank) for rank in ranks]
        self.assertEqual(python, ["top-3", "top-3", "top-6", "top-6", "top-10", "top-10", "top-20", "top-20",
                                  "outside", "outside", "outside"])
        template = self.app.jinja_env.from_string("{{ 4|position_class }}")
        self.assertEqual(template.render(), "top-6")
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js не установлен")
        script = Path(__file__).parents[1] / "dashboard/static/positions.js"
        output = run(
            [node, "-e", f"const {{positionClass}} = require({json.dumps(str(script))});"
             f"console.log(JSON.stringify({json.dumps(ranks)}.map(positionClass)))"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        self.assertEqual(json.loads(output), python)

    def test_scheduler_starts_daily_collection_once(self):
        project = self.create_project()
        project_id = int(project.rsplit("/", 1)[1])
        before = datetime(2026, 11, 24, 20, 59, tzinfo=MSK)
        after = datetime(2026, 11, 24, 21, 1, tzinfo=MSK)
        with connect(self.path) as db:
            self.assertEqual(due_projects(db, before), [])
            self.assertEqual(due_projects(db, after), [project_id])
            db.execute(
                "INSERT INTO runs(project_id, project_url, interval_seconds, region, source, state, started_at) "
                "VALUES (?, ?, 10, '213', 'test', 'complete', '2026-11-24T21:00:30+03:00')",
                (project_id, PROJECT_URL),
            )
            self.assertEqual(due_projects(db, after), [])
            self.assertEqual(due_projects(db, datetime(2026, 11, 25, 21, 0, tzinfo=MSK)), [project_id])
            db.execute("UPDATE project_settings SET paused = 1")
            self.assertEqual(due_projects(db, datetime(2026, 11, 25, 21, 0, tzinfo=MSK)), [])
            db.execute("UPDATE project_settings SET paused = 0, collect_daily = 0")
            self.assertEqual(due_projects(db, datetime(2026, 11, 25, 21, 0, tzinfo=MSK)), [])
            db.execute("UPDATE project_settings SET collect_daily = 1")
        launch = Mock(return_value=None)
        self.assertIsNone(scheduler_tick(self.path, launch, datetime(2026, 11, 25, 21, 0, tzinfo=MSK)))
        launch.assert_called_once_with(project_id, scheduled=True)
        with patch("dashboard.Thread"):
            self.assertIsNotNone(self.app.launch_run(project_id, scheduled=True))
            self.assertIsNone(self.app.launch_run(project_id, scheduled=True))

    def test_rare_low_frequency_phrases_are_skipped_by_scheduled_runs(self):
        form = self.form(self.csrf("/projects/new"))
        form.setlist("iw", ["5", "500", "", ""])
        project = self.client.post("/projects/new", data=form).location
        project_id = int(project.rsplit("/", 1)[1])
        run_id = self.start_run(project)
        with connect(self.path) as db:
            db.execute("UPDATE measurements SET rank=4, checked_at=? WHERE run_id=?",
                       (datetime.now(MSK).isoformat(), run_id))
            finish_run(db, run_id, "complete")
            db.execute("UPDATE project_settings SET rare_lowfreq = 1")
        with patch("dashboard.Thread"):
            scheduled = self.app.launch_run(project_id, scheduled=True)
        with connect(self.path) as db:
            ordinals = [row[0] for row in db.execute("SELECT ordinal FROM measurements WHERE run_id=?", (scheduled,))]
        self.assertEqual(ordinals, [1, 2, 3])


    def test_visits_export_has_every_row_and_import_rejects_huge_dates(self):
        project = self.create_project()
        rows = [f"2026-11-24;Фраза {index};1;2;10" for index in range(20)]
        self.assertEqual(self.import_visits(project, "\n".join([*rows, "2026-11-24;=HYPERLINK(1);1;1;1"])).status_code, 303)
        self.assertIn("Показаны: 1 - 15 из 25", self.html(project + "/visits?deleted=1"))
        exported = self.client.get(project + "/export/visits.csv?deleted=1&page=2").get_data(as_text=True)
        self.assertEqual(len(exported.strip().splitlines()), 26)
        self.assertIn("\n'=HYPERLINK(1);", exported)
        self.assertEqual(self.import_visits(project, "01.01.99999999999999999999;Ремонт;1;1;1").status_code, 422)

    def test_recluster_keeps_phrases_missing_from_the_latest_day(self):
        project = self.create_project()
        first = self.start_run(project)
        with connect(self.path) as db:
            db.execute("UPDATE measurements SET rank=3, found_url=?, checked_at='2026-11-23T10:00:00+03:00' "
                       "WHERE run_id=? AND ordinal IN (0, 1)", (LANDING, first))
            finish_run(db, first, "complete")
        second = self.start_run(project)
        with connect(self.path) as db:
            db.execute("UPDATE measurements SET rank=2, found_url=?, checked_at='2026-11-24T10:00:00+03:00' "
                       "WHERE run_id=? AND ordinal = 0", (LANDING, second))
            finish_run(db, second, "complete")
        self.post(project + "/clusters/recluster", {}, page=project)
        with connect(self.path) as db:
            clusters = [row[0] for row in db.execute(
                "SELECT cluster FROM keyword_attributes WHERE ordinal IN (0, 1) ORDER BY ordinal")]
        self.assertEqual(clusters, ["Натяжные потолки", "Натяжные потолки"])

    def test_upgraded_projects_keep_daily_collection_off(self):
        project_id = int(self.create_project().rsplit("/", 1)[1])
        with connect(self.path) as db:
            db.execute("DELETE FROM project_settings")
            self.assertEqual(due_projects(db, datetime(2026, 11, 24, 22, 0, tzinfo=MSK)), [])
        self.assertIn("Автосбор выключен", self.html(f"/projects/{project_id}/positions"))

    def test_deferred_scheduled_run_waits_before_retrying(self):
        project_id = int(self.create_project().rsplit("/", 1)[1])
        with connect(self.path) as db:
            db.execute("INSERT INTO panel_settings VALUES ('balance', '0')")
        with patch("dashboard.Thread"), patch("dashboard.monotonic") as clock:
            clock.return_value = 1000
            self.assertIsNone(self.app.launch_run(project_id, scheduled=True))
            with connect(self.path) as db:
                db.execute("UPDATE panel_settings SET value = '100' WHERE key = 'balance'")
            clock.return_value = 1060
            self.assertIsNone(self.app.launch_run(project_id, scheduled=True))
            clock.return_value = 1000 + 30 * 60
            self.assertIsNotNone(self.app.launch_run(project_id, scheduled=True))

    def test_keyword_power_is_not_prefilled(self):
        project = self.create_project()
        self.assertIn('name="power" inputmode="decimal" size="3" value="" placeholder="10"', self.html(project + "/keywords"))
        self.post(project + "/keywords/0", {"power": "", "visits": "7"}, page=project)
        with connect(self.path) as db:
            self.assertEqual(tuple(db.execute("SELECT power, visits FROM keyword_attributes WHERE ordinal = 0").fetchone()),
                             (None, 7))

class DemoDataTest(unittest.TestCase):
    def test_demo_seed_fills_every_project_tab(self):
        from examples.dashboard_demo import demo_search, seed

        with TemporaryDirectory() as directory:
            path = Path(directory) / "demo.sqlite3"
            app = create_app(path, demo_search, demo=True)
            seed(path)
            client = app.test_client()
            self.assertIn("На балансе недостаточно средств", client.get("/").get_data(as_text=True))
            for view in ("keywords", "clusters", "positions", "visits", "schedule", "expenses"):
                with self.subTest(view=view):
                    response = client.get(f"/projects/2/{view}")
                    self.assertEqual(response.status_code, 200)
            visits = client.get("/projects/2/visits?deleted=1").get_data(as_text=True)
            self.assertIn("Потолки в ванную", visits)
            self.assertIn("visit-miss", visits)
            self.assertIn("visit-high", visits)
            positions = client.get("/projects/2/positions").get_data(as_text=True)
            self.assertEqual(positions.count("data-day="), 10)


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
