import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from filelock import FileLock, Timeout

from . import create_app
from .search import YandexSearch


def main():
    parser = argparse.ArgumentParser(description="Локальная панель мониторинга позиций")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", type=Path, default=Path(".local-pf/dashboard.sqlite3"))
    parser.add_argument("--debug-log", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug_log else logging.INFO)
    load_dotenv()
    key = os.environ.get("YANDEX_SEARCH_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_SEARCH_FOLDER_ID", "").strip()
    source = YandexSearch(key, folder) if key and folder else None
    path = args.db.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(f"{path}.lock", timeout=0):
            create_app(path, source).run(host="127.0.0.1", port=args.port)
    except Timeout:
        parser.exit(1, f"Панель уже использует базу данных {path}.\n")


if __name__ == "__main__":
    main()
