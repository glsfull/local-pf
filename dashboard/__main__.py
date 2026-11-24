import argparse
from datetime import datetime
from functools import partial
import logging
import os
from pathlib import Path
from threading import Timer
import webbrowser

from dotenv import load_dotenv
from filelock import FileLock, Timeout
from werkzeug.serving import WSGIRequestHandler

from modules.clock import MSK, MoscowFormatter

from . import create_app
from .agent_process import execute_agent, execute_agent_session
from .config_import import import_config
from .scheduler import start_scheduler

ROOT = Path(__file__).resolve().parents[1]


def open_panel(url, opener=webbrowser.open):
    """Open the panel in the default browser; a missing browser is not an error."""
    try:
        opener(url)
    except webbrowser.Error:
        logging.getLogger(__name__).info("Откройте панель вручную: %s", url)


class MoscowRequestHandler(WSGIRequestHandler):
    def log_date_time_string(self):
        return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S %Z")


def main():
    parser = argparse.ArgumentParser(description="Локальная панель мониторинга позиций")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", type=Path, default=Path(".local-pf/dashboard.sqlite3"))
    parser.add_argument("--agent-log-dir", type=Path, help="Папка с отчётом браузерного агента")
    parser.add_argument("--agent-container", default="dp-agent", help="Имя контейнера Chromium/noVNC")
    parser.add_argument("--container-db", help="Путь к общей SQLite-базе внутри контейнера")
    parser.add_argument("--container-log-dir", help="Путь к общей папке логов внутри контейнера")
    parser.add_argument("--local-agent", action="store_true", help="Chromium на локальном DISPLAY=:1")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать панель в браузере")
    parser.add_argument(
        "--config", type=Path, help="YAML с ключами Search API, проектами и фразами (examples/panel.example.yaml)",
    )
    parser.add_argument("--debug-log", action="store_true")
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.setFormatter(MoscowFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.DEBUG if args.debug_log else logging.INFO, handlers=[handler])
    load_dotenv(ROOT / ".env")
    path = args.db.resolve()
    args.log_dir = (args.agent_log_dir or Path(os.getenv("LOG_DIR", ROOT / "logs"))).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(f"{path}.lock", timeout=0):
            app = create_app(
                path, report_path=args.log_dir / "report.json",
                agent_runner=partial(execute_agent, options=args),
                agent_session_runner=partial(execute_agent_session, options=args),
            )
            if args.config:
                try:
                    logging.getLogger(__name__).info("%s", import_config(path, args.config))
                except (OSError, ValueError) as exc:
                    parser.exit(2, f"Не удалось загрузить {args.config}: {exc}\n")
            start_scheduler(path, app.launch_run)
            url = f"http://127.0.0.1:{args.port}/"
            logging.getLogger(__name__).info("Панель: %s", url)
            if not args.no_browser:
                Timer(1.0, open_panel, (url,)).start()
            app.run(
                host="127.0.0.1", port=args.port, request_handler=MoscowRequestHandler,
            )
    except Timeout:
        parser.exit(1, f"Панель уже использует базу данных {path}.\n")


if __name__ == "__main__":
    main()
