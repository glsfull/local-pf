"""Run an offline example with: python -m examples.dashboard_demo."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from dashboard import create_app
from dashboard.runner import execute_run
from dashboard.storage import connect, save_project, start_run

SITE = "https://example.org"
LANDING = f"{SITE}/catalog/potolki/"
SEARCH_RESULTS = {
    "Натяжные потолки Бобр": (4, LANDING),
    "Матовые потолки": (8, f"{SITE}/catalog/matovye/"),
    "Монтаж потолков": (23, f"{SITE}/montazh/"),
    "Потолки для кухни example": (18, f"{SITE}/catalog/kuhnya/"),
}
WORDS = [
    ("Натяжные потолки", LANDING, "brand", "Бобр"),
    ("Матовые потолки", f"{SITE}/catalog/matovye/", "domain", ""),
    ("Монтаж потолков", "", "domain", ""),
    ("Потолки для кухни", f"{SITE}/catalog/kuhnya/", "domain", ""),
    ("Ремонт потолков", "", "brand", "Бобр"),
]


def demo_search(query, page, region):
    urls = [f"https://results.example.net/{page * 10 + index}" for index in range(1, 11)]
    result = SEARCH_RESULTS.get(query)
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
        project_id = save_project(db, None, ("Мастер Бобр", SITE, "213", 2), WORDS)
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        words = db.execute("SELECT * FROM keywords WHERE project_id = ? ORDER BY ordinal", (project_id,)).fetchall()
        run_id = start_run(db, project, words, "Демо-данные", "example")
    execute_run(path, run_id, demo_search, wait=lambda seconds: None)


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
