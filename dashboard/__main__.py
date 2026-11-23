import argparse
from datetime import datetime
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from filelock import FileLock, Timeout
from werkzeug.serving import WSGIRequestHandler

from modules.clock import MSK, MoscowFormatter

from . import create_app
from .search import YandexSearch

ROOT = Path(__file__).resolve().parents[1]


class MoscowRequestHandler(WSGIRequestHandler):
    def log_date_time_string(self):
        return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S %Z")


def main():
    parser = argparse.ArgumentParser(description="Локальная панель мониторинга позиций")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", type=Path, default=Path(".local-pf/dashboard.sqlite3"))
    parser.add_argument("--agent-log-dir", type=Path, help="Папка с отчётом браузерного агента")
    parser.add_argument("--debug-log", action="store_true")
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.setFormatter(MoscowFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.DEBUG if args.debug_log else logging.INFO, handlers=[handler])
    load_dotenv(ROOT / ".env")
    key = os.environ.get("YANDEX_SEARCH_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_SEARCH_FOLDER_ID", "").strip()
    source = YandexSearch(key, folder) if key and folder else None
    path = args.db.resolve()
    log_dir = args.agent_log_dir or Path(os.getenv("LOG_DIR", ROOT / "logs"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(f"{path}.lock", timeout=0):
            create_app(path, source, report_path=log_dir / "report.json").run(
                host="127.0.0.1", port=args.port, request_handler=MoscowRequestHandler,
            )
    except Timeout:
        parser.exit(1, f"Панель уже использует базу данных {path}.\n")


if __name__ == "__main__":
    main()
