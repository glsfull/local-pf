import argparse
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from dotenv import load_dotenv
from DrissionPage.errors import BrowserConnectError, PageDisconnectedError
from filelock import Timeout

from modules.browser import (
    CAPTCHA_SELECTOR,
    CaptchaHandler,
    RunError,
    SearchChallenge,
    close_browser,
    load_solver,
    open_browser,
)
from modules.checker import check_pages
from modules.clock import MoscowFormatter, timestamp
from modules.config import captcha_settings, read_config
from modules.journal import Journal
from modules.search import SEARCH_HOME, find_target

ROOT = Path(__file__).resolve().parent
log = logging.getLogger(__name__)


def write_report(path, report):
    temporary = path.with_name(f".report-{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    log.info("Отчёт сохранён в %s", path)


def captcha_handler(journal, report, *, selector, timeout, attempts, manual_timeout, poll, report_path):
    """Build the SmartCaptcha handler that keeps report.json current while it waits."""
    def save_progress():
        write_report(report_path, report)

    return CaptchaHandler(
        journal, report, selector=selector, timeout=timeout, attempts=attempts,
        manual_timeout=manual_timeout, poll_interval=poll,
        solver=load_solver(os.getenv("LOCAL_PF_CAPTCHA_SOLVER")),
        screenshot_dir=report_path.parent / "captcha" if report_path is not None else None,
        save_progress=save_progress if report_path is not None else None,
        operator_url=os.getenv("LOCAL_PF_OPERATOR_URL", "http://localhost:56565"),
    )


def run(config, report, *, headless=False, search_home=SEARCH_HOME, report_path=None):
    journal = Journal(report.setdefault("events", []), 0 if headless else config.observation_delay)
    captcha = captcha_handler(
        journal, report, selector=config.captcha, timeout=config.timeout,
        attempts=config.captcha_attempts, manual_timeout=config.captcha_timeout,
        poll=config.captcha_poll, report_path=report_path,
    )
    report["status"] = "RUNNING"
    for attempt in range(3):
        browser = None
        retry = False
        with TemporaryDirectory(prefix="local-pf-") as profile:
            try:
                browser = open_browser(config.timeout, headless=headless, profile=profile)
                home = find_target(browser, config, report, journal, search_home, captcha=captcha)
                check_pages(browser, home, config, report, journal, captcha=captcha)
                report["status"] = "OK"
                return 0
            except SearchChallenge as error:
                report["captcha_encountered"] = True
                report["status"] = error.status
                journal.record("Ошибка", error.url, status=error.status, error=str(error))
                log.error("%s", error)
                return 1
            except (BrowserConnectError, PageDisconnectedError):
                log.exception("Chromium недоступен, попытка %d из 3", attempt + 1)
                retry = attempt < 2
                journal.record(
                    "Ошибка", journal.url, status="BROWSER_ERROR",
                    error=f"Chromium недоступен, попытка {attempt + 1} из 3",
                )
                report["status"] = "BROWSER_ERROR"
                if retry:
                    report["search_result_found"] = False
                    report["pages_checked"].clear()
            except RunError as error:
                report["status"] = getattr(error, "status", "ERROR")
                journal.record("Ошибка", journal.url, status=report["status"], error=str(error))
                log.error("%s", error)
                return 1
            except Exception as error:
                report["status"] = "ERROR"
                journal.record("Ошибка", journal.url, status="ERROR", error=str(error))
                log.exception("Ошибка выполнения")
                return 1
            finally:
                try:
                    if report_path is not None and not retry:
                        write_report(report_path, report)
                finally:
                    if browser is not None:
                        try:
                            close_browser(browser)
                        except Exception:
                            log.exception("Не удалось закрыть Chromium")
    return 1


def verify(config, log_dir, *, headless=False, search_home=SEARCH_HOME):
    report = {
        "keyword": config.keyword,
        "target_domain": config.domain,
        "timestamp": timestamp(),
        "search_result_found": False,
        "captcha_encountered": False,
        "pages_checked": [],
    }
    return run(config, report, headless=headless, search_home=search_home, report_path=log_dir / "report.json")


def verify_positions(path, log_dir, *, run_id=None, project_id=None):
    from dashboard.runner import execute_run
    from dashboard.search import domain_hint, site_host
    from dashboard.storage import connect, run_lock, start_run
    from modules.rank_parser import RankParser

    if not path.is_file():
        raise ValueError(f"База данных панели не найдена: {path}")
    delay = float(os.getenv("LOCAL_PF_OBSERVATION_DELAY", "1.0"))
    if not 0 <= delay <= 5:
        raise ValueError("LOCAL_PF_OBSERVATION_DELAY: укажите паузу от 0 до 5 секунд.")
    report = {
        "timestamp": timestamp(),
        "captcha_encountered": False,
        "pages_checked": [],
        "events": [],
    }
    captcha = captcha_settings(os.environ, {
        "attempts": "LOCAL_PF_CAPTCHA_ATTEMPTS",
        "timeout": "LOCAL_PF_CAPTCHA_TIMEOUT",
        "poll": "LOCAL_PF_CAPTCHA_POLL",
    })
    journal = Journal(report["events"], delay)
    with run_lock(path), TemporaryDirectory(prefix="local-pf-rank-") as profile:
        handler = captcha_handler(
            journal, report, selector=CAPTCHA_SELECTOR, timeout=15,
            attempts=captcha["attempts"], manual_timeout=captcha["timeout"], poll=captcha["poll"],
            report_path=log_dir / "report.json",
        )
        source = RankParser(journal, report, profile=profile, handler=handler)
        with connect(path) as db:
            if project_id is not None:
                db.execute("BEGIN IMMEDIATE")
                project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                if project is None:
                    raise ValueError("Проект не найден.")
                words = db.execute(
                    "SELECT * FROM keywords WHERE project_id = ? ORDER BY ordinal", (project_id,),
                ).fetchall()
                if not words:
                    raise ValueError("В проекте нет поисковых фраз.")
                run_id = start_run(db, project, words, "Парсер выдачи", domain_hint(project["url"]))
            run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None or run["state"] != "running":
                raise ValueError("Активная проверка не найдена.")
        report.update(run_id=run_id, target_domain=site_host(run["project_url"]), status="RUNNING")
        try:
            state = execute_run(path, run_id, source, report=report)
            if report["status"] != "TIMEOUT":
                report["status"] = "OK" if state == "complete" else "ERROR"
            write_report(log_dir / "report.json", report)
        finally:
            if source.browser is not None:
                try:
                    close_browser(source.browser)
                except Exception:
                    log.exception("Не удалось закрыть Chromium")
    return 0 if state == "complete" else 1


def main():
    parser = argparse.ArgumentParser(description="Проверка ссылок через поиск Яндекса")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--db", type=Path, default=ROOT / ".local-pf/dashboard.sqlite3")
    parser.add_argument("--log-dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-id", type=int, help="Выполнить проверку позиций, созданную панелью")
    mode.add_argument("--project-id", type=int, help="Создать и выполнить проверку позиций проекта из SQLite")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if not args.headless and Path("/.dockerenv").exists():
        os.environ["DISPLAY"] = ":1"
    log_dir = args.log_dir or Path(os.getenv("LOG_DIR", ROOT / "logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        formatter = MoscowFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        handlers = [logging.FileHandler(log_dir / "agent.log", encoding="utf-8"), logging.StreamHandler()]
        for handler in handlers:
            handler.setFormatter(formatter)
        logging.basicConfig(level=logging.INFO, handlers=handlers)
        if args.debug:
            logging.getLogger("modules").setLevel(logging.DEBUG)
        if args.run_id is not None or args.project_id is not None:
            if args.headless or os.environ.get("DISPLAY") != ":1":
                parser.error("Для проверки позиций требуется видимый Chromium на DISPLAY=:1.")
            return verify_positions(args.db, log_dir, run_id=args.run_id, project_id=args.project_id)
        config = read_config(args.config)
        return verify(config, log_dir, headless=args.headless)
    except Timeout:
        log.error("Дождитесь окончания текущей проверки позиций.")
        return 1
    except Exception as error:
        log.error("Не удалось выполнить проверку: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
