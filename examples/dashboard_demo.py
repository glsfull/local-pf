"""Run an offline example with: python -m examples.dashboard_demo."""
import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from dashboard import create_app
from dashboard.runner import execute_run
from dashboard.search import domain_hint
from dashboard.storage import (
    connect, finish_agent_session, project_keywords, save_panel_setting, save_project, save_project_settings,
    start_agent_session, start_run, update_agent_session,
)
from modules.clock import MSK, timestamp

SITE = "https://example.org"
LANDING = f"{SITE}/catalog/potolki/"
SEARCH_RESULTS = {
    "Натяжные потолки Бобр": (4, LANDING),
    "Матовые потолки": (8, f"{SITE}/catalog/matovye/"),
    "Монтаж потолков": (23, f"{SITE}/montazh/"),
    "Потолки для кухни example": (18, f"{SITE}/catalog/kuhnya/"),
}
WORDS = [
    ("Натяжные потолки", LANDING, "brand", "Бобр", ("Потолки", 430, -24, 18, 46, 12, "high", 180)),
    ("Матовые потолки", f"{SITE}/catalog/matovye/", "domain", "", ("Потолки", 260, 46, 64, 62, 8, "medium", 95)),
    ("Монтаж потолков", "", "domain", "", ("Услуги", 164, 18, 31, 37, 4, "medium", 60)),
    ("Потолки для кухни", f"{SITE}/catalog/kuhnya/", "domain", "", ("Потолки", 92, 11, 27, 34, 3, "low", 35)),
    ("Ремонт потолков", "", "brand", "Бобр", ("Услуги", 75, None, None, None, None, "", None)),
]


def demo_search(query, page, region, results=SEARCH_RESULTS):
    urls = [f"https://results.example.net/{page * 10 + index}" for index in range(1, 11)]
    result = results.get(query)
    if result:
        rank, url = result
        result_page, index = divmod(rank - 1, 10)
        if page == result_page:
            urls[index] = url
    return urls


# (days ago, rank changes) — a two-week history for the positions chart and matrix.
HISTORY = [
    (13, {"Матовые потолки": (31, f"{SITE}/catalog/matovye/"), "Монтаж потолков": None, "Натяжные потолки Бобр": (9, LANDING)}),
    (11, {"Матовые потолки": (27, f"{SITE}/catalog/matovye/"), "Монтаж потолков": None, "Натяжные потолки Бобр": (7, LANDING)}),
    (9, {"Матовые потолки": (24, f"{SITE}/catalog/matovye/"), "Монтаж потолков": (41, f"{SITE}/montazh/")}),
    (7, {"Матовые потолки": (24, f"{SITE}/catalog/matovye/"), "Монтаж потолков": None}),
    (6, {"Матовые потолки": (19, f"{SITE}/catalog/matovye/"), "Монтаж потолков": (28, f"{SITE}/montazh/")}),
    (4, {"Матовые потолки": (15, f"{SITE}/catalog/matovye/"), "Ремонт потолков": (33, SITE)}),
    (3, {"Матовые потолки": (9, f"{SITE}/catalog/matovye/"), "Ремонт потолков": (12, SITE)}),
    (2, {"Матовые потолки": (3, f"{SITE}/catalog/matovye/"), "Монтаж потолков": (1, f"{SITE}/montazh/")}),
    (1, {"Матовые потолки": (5, f"{SITE}/catalog/matovye/"), "Монтаж потолков": (2, f"{SITE}/montazh/")}),
    (0, {}),
]
# phrase → (planned visits per day, price of one visit, base depth)
VISITS = {
    "Натяжные потолки": (12, 6, 2.1),
    "Матовые потолки": (8, 6, 3.2),
    "Монтаж потолков": (4, 5, 1.6),
    "Потолки для кухни": (3, 5, 2.4),
    "Ремонт потолков": (2, 4, 1.2),
    "Потолки в ванную": (3, 5, 1.8),  # removed from the project: shown with «С удаленными»
}


def visit_rows(project_id, today, days=14):
    """Deterministic daily visit statistics: some days miss visits, some reach high depth."""
    rows = []
    for days_ago in range(days):
        day = (today - timedelta(days=days_ago)).isoformat()
        for index, (phrase, (planned, price, depth)) in enumerate(VISITS.items()):
            if phrase == "Потолки в ванную" and days_ago < 7:
                continue
            missed = (days_ago + index) % 6 == 0
            done = 0 if missed else planned - (days_ago + index) % 3
            depth_value = None if not done else round(depth + ((days_ago * 7 + index) % 5) / 4, 1)
            rows.append((project_id, phrase, day, done, planned, done * price, depth_value))
    return rows


def seed(path, balance="0"):
    with connect(path) as db:
        save_project(db, None, ("Студия ремонта", "https://example.com/", "", 30), [
            ("Ремонт квартир", "", "brand", "Студия ремонта"),
            ("Дизайн интерьера", "", "domain", ""),
        ])
        project_id = save_project(db, None, ("Мастер Бобр", SITE, "213", 2), WORDS, attributes=("Москва", 260))
        db.execute("INSERT INTO cluster_attributes VALUES (?, ?, ?, ?, ?, ?)", (project_id, "Потолки", LANDING, 75, 1, 1))
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        words = db.execute("SELECT * FROM keywords WHERE project_id = ? ORDER BY ordinal", (project_id,)).fetchall()
        save_project_settings(db, project_id, {
            "brand_queries": "бобр", "limit_budget": 1, "daily_budget": 150, "theme": "services",
            # Visits from 8:00 to 22:00 MSK on weekdays and from 10:00 on weekends.
            "schedule_mode": "uniform_hours",
            "schedule": "".join(
                "1" if (8 if day < 5 else 10) <= hour < 22 else "0" for day in range(7) for hour in range(24)
            ),
        })
        db.executemany(
            "INSERT INTO visit_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
            visit_rows(project_id, datetime.now(MSK).date()),
        )
        save_panel_setting(db, "balance", balance)
        save_panel_setting(db, "user_name", "Демо")
    now = datetime.now(MSK)
    for days_ago, changes in HISTORY:
        results = {**SEARCH_RESULTS, **changes}
        with connect(path) as db:
            run_id = start_run(db, project, words, "Демо-данные", "example")
        execute_run(path, run_id, lambda query, page, region: demo_search(query, page, region, results), wait=lambda _: None)
        at = (now - timedelta(days=days_ago)).isoformat(timespec="seconds")
        with connect(path) as db:
            db.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?", (at, at, run_id))
            db.execute(
                """UPDATE measurements SET checked_at=?, hinted_at=CASE WHEN hinted_at IS NOT NULL THEN ? END
                   WHERE run_id=?""",
                (at, at, run_id),
            )


class DemoAgent:
    """Scripted «🚀 Запустить агента» without a browser: search → captcha → «Продолжить» → site found.

    The statuses, phrases and journal have the same shape as the ones published by modules/session.py.
    """

    def __init__(self, step=1.5, captcha_wait=300):
        self.step, self.captcha_wait = step, captcha_wait

    def __call__(self, path, session_id):
        with connect(path) as db:
            row = db.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
            project = db.execute("SELECT * FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
            words = [
                word for word in project_keywords(db, project["id"])
                if row["ordinal"] is None or word["ordinal"] == row["ordinal"]
            ]
        self.path, self.session_id = path, session_id
        self.report = {"events": [], "keywords": []}
        for index, word in enumerate(words):
            hint = word["brand"] if word["hint_type"] == "brand" else domain_hint(project["url"])
            outcome = {"phrase": word["phrase"], "query": None, "found": False, "position": None, "page": None,
                       "refined": False}
            self.report["keywords"].append(outcome)
            for refined, query in enumerate((word["phrase"], f"{word['phrase']} {hint}")):
                outcome.update(query=query, refined=bool(refined))
                if refined:
                    self.event("Уточнённый запрос", query=query)
                self.status("SEARCH", f"Поиск «{query}» в Яндексе.", "Ввод фразы и «Найти»", query=query)
                if index == 0 and not refined and not self.captcha(query):
                    return self.finish("failed", "ERROR", "Капча не решена за 5 мин.")
                rank, _ = SEARCH_RESULTS.get(query, (None, None))
                if rank:
                    page = (rank - 1) // 10 + 1
                    for number in range(1, page):
                        self.event("Следующая страница выдачи", query=query, page=number + 1)
                    outcome.update(found=True, position=rank, page=page)
                    self.event("Клик по результату", query=query, page=page)
                    self.status("FOUND", f"«{query}»: сайт найден на позиции {rank} (страница {page}) и открыт.")
                    break
                self.event("Сайт не найден", query=query, error="Целевой домен не найден (страниц: 5, позиций: 50)")
                self.status("NOT_FOUND", f"«{query}»: сайт не найден в топ-50.")
        found = sum(item["found"] for item in self.report["keywords"])
        return self.finish("complete", "DONE", f"Найдено {found} из {len(words)} фраз.")

    def event(self, operation, **details):
        self.report["events"].append({"timestamp_msk": timestamp(), "operation": operation, **details})

    def status(self, status, message, operation=None, **details):
        if operation:
            self.event(operation, **details)
        with connect(self.path) as db:
            update_agent_session(db, self.session_id, status, message, report_json=self.json())
        time.sleep(self.step)

    def captcha(self, query):
        self.event("Капча", query=query, error="Требуется решение оператора в noVNC")
        self.status("CAPTCHA", "Решите капчу в окне noVNC или нажмите «Продолжить».")
        deadline = time.monotonic() + self.captcha_wait
        while time.monotonic() < deadline:
            with connect(self.path) as db:
                if db.execute(
                    "UPDATE agent_sessions SET resume = 0 WHERE id = ? AND resume = 1", (self.session_id,),
                ).rowcount:
                    self.event("Оператор нажал «Продолжить»", query=query)
                    return True
            time.sleep(0.5)
        return False

    def finish(self, state, status, message):
        with connect(self.path) as db:
            finish_agent_session(db, self.session_id, state, status, message, report_json=self.json())
        return state

    def json(self):
        return json.dumps(self.report, ensure_ascii=False)


def seed_agent(path):
    """One finished agent run for the «История запусков» table."""
    with connect(path) as db:
        project_id = db.execute("SELECT id FROM projects WHERE url = ?", (SITE,)).fetchone()["id"]
        session_id = start_agent_session(db, project_id, 1)
    report = {
        "events": [
            {"timestamp_msk": timestamp(), "operation": "Ввод фразы и «Найти»", "query": "Матовые потолки"},
            {"timestamp_msk": timestamp(), "operation": "Клик по результату", "query": "Матовые потолки", "page": 1},
        ],
        "keywords": [{"phrase": "Матовые потолки", "query": "Матовые потолки", "found": True, "position": 8,
                      "page": 1, "refined": False}],
    }
    with connect(path) as db:
        finish_agent_session(
            db, session_id, "complete", "DONE", "Найдено 1 из 1 фраз.", report_json=json.dumps(report, ensure_ascii=False),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--balance", default="0", help="panel balance in ₽; 0 shows the low-balance warning")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="local-pf-demo-") as directory:
        path = Path(directory) / "demo.sqlite3"
        app = create_app(path, demo_search, demo=True, agent_session_runner=DemoAgent())
        seed(path, args.balance)
        seed_agent(path)
        app.run(host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
