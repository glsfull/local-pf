"""Run an offline example with: python -m examples.dashboard_demo."""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from dashboard import create_app
from dashboard.runner import execute_run
from dashboard.storage import connect, save_project, start_run
from modules.clock import MSK

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


def seed(path):
    with connect(path) as db:
        save_project(db, None, ("Студия ремонта", "https://example.com/", "", 30), [
            ("Ремонт квартир", "", "brand", "Студия ремонта"),
            ("Дизайн интерьера", "", "domain", ""),
        ])
        project_id = save_project(db, None, ("Мастер Бобр", SITE, "213", 2), WORDS, attributes=("Москва", 260))
        db.execute("INSERT INTO cluster_attributes VALUES (?, ?, ?, ?, ?, ?)", (project_id, "Потолки", LANDING, 75, 1, 1))
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        words = db.execute("SELECT * FROM keywords WHERE project_id = ? ORDER BY ordinal", (project_id,)).fetchall()
    now = datetime.now(MSK)
    for days_ago, changes in [
        (7, {"Матовые потолки": (24, f"{SITE}/catalog/matovye/"), "Монтаж потолков": None}),
        (4, {"Матовые потолки": (15, f"{SITE}/catalog/matovye/"), "Ремонт потолков": (33, SITE)}),
        (2, {"Матовые потолки": (3, f"{SITE}/catalog/matovye/"), "Монтаж потолков": (1, f"{SITE}/montazh/")}),
        (0, {}),
    ]:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="local-pf-demo-") as directory:
        path = Path(directory) / "demo.sqlite3"
        app = create_app(path, demo_search, demo=True)
        seed(path)
        app.run(host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
