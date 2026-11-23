import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from dotenv import load_dotenv
from DrissionPage.errors import BrowserConnectError, PageDisconnectedError

from modules.browser import RunError, open_browser
from modules.checker import check_pages
from modules.config import read_config
from modules.search import SEARCH_HOME, SearchChallenge, find_target

ROOT = Path(__file__).resolve().parent
log = logging.getLogger(__name__)


def run(config, report, *, headless=False, search_home=SEARCH_HOME):
    for attempt in range(3):
        browser = None
        with TemporaryDirectory(prefix="local-pf-") as profile:
            try:
                browser = open_browser(config.timeout, headless=headless, profile=profile)
                home = find_target(browser, config, report, search_home)
                check_pages(browser, home, config, report)
                return 0
            except SearchChallenge as error:
                report["captcha_encountered"] = True
                log.warning("%s. Проверка остановлена.", error)
                return 1
            except (BrowserConnectError, PageDisconnectedError):
                log.exception("Chromium недоступен, попытка %d из 3", attempt + 1)
                if attempt < 2:
                    report["search_result_found"] = False
                    report["pages_checked"].clear()
            except RunError as error:
                log.error("%s", error)
                return 1
            except Exception:
                log.exception("Ошибка выполнения")
                return 1
            finally:
                if browser is not None:
                    try:
                        browser.quit(force=True)
                    except Exception:
                        log.exception("Не удалось закрыть Chromium")
    return 1


def verify(config, log_dir, *, headless=False, search_home=SEARCH_HOME):
    report = {
        "keyword": config.keyword,
        "target_domain": config.domain,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "search_result_found": False,
        "captcha_encountered": False,
        "pages_checked": [],
    }
    try:
        return run(config, report, headless=headless, search_home=search_home)
    finally:
        output = NamedTemporaryFile(mode="w", dir=log_dir, encoding="utf-8", delete=False)
        temporary = Path(output.name)
        try:
            with output:
                json.dump(report, output, ensure_ascii=False, indent=2)
                output.write("\n")
            temporary.replace(log_dir / "report.json")
        finally:
            temporary.unlink(missing_ok=True)
        log.info("Отчёт сохранён в %s", log_dir / "report.json")


def main():
    parser = argparse.ArgumentParser(description="Проверка ссылок через поиск Яндекса")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    log_dir = Path(os.getenv("LOG_DIR", ROOT / "logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.DEBUG if args.debug else logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            handlers=[
                logging.FileHandler(log_dir / "agent.log", encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )
        config = read_config(args.config)
        return verify(config, log_dir, headless=args.headless)
    except Exception as error:
        log.error("Не удалось выполнить проверку: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
