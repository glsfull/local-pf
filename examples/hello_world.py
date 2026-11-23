"""Проверка запуска Chromium и открытия ya.ru внутри контейнера."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from modules.browser import RunError, open_browser
from modules.config import read_config
from modules.search import open_search


def main():
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    config = read_config(root / "config.yaml")
    with TemporaryDirectory(prefix="local-pf-") as profile:
        browser = open_browser(config.timeout, profile=profile)
        try:
            open_search(browser, config)
            print(browser.title, browser.url)
            return 0
        except RunError as error:
            print(error, file=sys.stderr)
            return 1
        finally:
            browser.quit(force=True)


if __name__ == "__main__":
    raise SystemExit(main())
