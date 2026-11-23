"""Проверка запуска Chromium внутри контейнера."""

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from DrissionPage.errors import BrowserConnectError, PageDisconnectedError

from modules.browser import RunError, loaded_document, open_browser
from modules.config import read_config
from modules.search import open_search


def check_local_browser(browser, timeout):
    title = "local-pf Chromium check"
    html = f"<title>{title}</title><h1>{title}</h1>"
    url = "data:text/html;charset=utf-8," + quote(html, safe="")
    browser.get(url, retry=0, timeout=timeout)
    loaded_document(browser, timeout)
    if browser.title != title or not browser.ele("css:h1", timeout=0):
        raise RunError("Chromium не отобразил контрольную страницу")
    print(title)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="открыть контрольную страницу")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    try:
        config = None if args.local else read_config(root / "config.yaml")
        timeout = config.timeout if config else 15
        with TemporaryDirectory(prefix="local-pf-") as profile:
            browser = open_browser(timeout, profile=profile)
            try:
                if config is None:
                    check_local_browser(browser, timeout)
                else:
                    open_search(browser, config)
                    print(browser.title, browser.url)
            finally:
                browser.quit(force=True)
    except (RunError, BrowserConnectError, PageDisconnectedError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
