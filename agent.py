import argparse
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from dotenv import load_dotenv
from DrissionPage.errors import BrowserConnectError, PageDisconnectedError

from modules.browser import RunError, close_browser, open_browser
from modules.checker import check_pages
from modules.clock import MoscowFormatter, timestamp
from modules.config import read_config
from modules.journal import Journal
from modules.search import SEARCH_HOME, SearchChallenge, find_target

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


def run(config, report, *, headless=False, search_home=SEARCH_HOME, report_path=None):
    journal = Journal(report.setdefault("events", []), 0 if headless else config.observation_delay)
    for attempt in range(3):
        browser = None
        retry = False
        with TemporaryDirectory(prefix="local-pf-") as profile:
            try:
                browser = open_browser(config.timeout, headless=headless, profile=profile)
                home = find_target(browser, config, report, journal, search_home)
                check_pages(browser, home, config, report, journal)
                return 0
            except SearchChallenge as error:
                report["captcha_encountered"] = True
                journal.record("Ошибка", error.url, status=error.status, error=str(error))
                return 1
            except (BrowserConnectError, PageDisconnectedError):
                log.exception("Chromium недоступен, попытка %d из 3", attempt + 1)
                retry = attempt < 2
                journal.record(
                    "Ошибка", journal.url, status="BROWSER_ERROR",
                    error=f"Chromium недоступен, попытка {attempt + 1} из 3",
                )
                if retry:
                    report["search_result_found"] = False
                    report["pages_checked"].clear()
            except RunError as error:
                journal.record("Ошибка", journal.url, status=getattr(error, "status", "ERROR"), error=str(error))
                log.error("%s", error)
                return 1
            except Exception as error:
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


def main():
    parser = argparse.ArgumentParser(description="Проверка ссылок через поиск Яндекса")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if not args.headless and Path("/.dockerenv").exists():
        os.environ["DISPLAY"] = ":1"
    log_dir = Path(os.getenv("LOG_DIR", ROOT / "logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        formatter = MoscowFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        handlers = [logging.FileHandler(log_dir / "agent.log", encoding="utf-8"), logging.StreamHandler()]
        for handler in handlers:
            handler.setFormatter(formatter)
        logging.basicConfig(level=logging.INFO, handlers=handlers)
        if args.debug:
            logging.getLogger("modules").setLevel(logging.DEBUG)
        config = read_config(args.config)
        return verify(config, log_dir, headless=args.headless)
    except Exception as error:
        log.error("Не удалось выполнить проверку: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
