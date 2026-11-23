import os
import shutil
import time
from urllib.parse import urlsplit

from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.errors import ContextLostError
from psutil import NoSuchProcess, Process, TimeoutExpired


class RunError(Exception):
    pass


class PageFailure(RunError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class AccessDenied(RunError):
    status = "ACCESS_DENIED"


def open_browser(timeout, *, headless=False, profile=None):
    chrome = os.getenv("CHROME_PATH") or shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        raise RunError("Chromium не найден. Укажите CHROME_PATH.")
    options = ChromiumOptions(read_file=False).set_browser_path(chrome).auto_port()
    if profile is not None:
        options.set_tmp_path(str(profile))
    options.set_argument("--disable-dev-shm-usage")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        options.set_argument("--no-sandbox")
    options.set_retry(0).set_timeouts(base=timeout, page_load=timeout, script=timeout)
    options.headless(headless)
    return ChromiumPage(options)


def close_browser(browser):
    try:
        process = Process(browser.process_id) if browser.process_id else None
    except NoSuchProcess:
        process = None
    try:
        browser.quit(force=True)
    finally:
        # Chrome can still be flushing its profile after the CDP connection closes.
        if process is not None:
            try:
                process.wait(timeout=5)
            except TimeoutExpired:
                try:
                    process.kill()
                except NoSuchProcess:
                    pass
                else:
                    process.wait(timeout=5)


def wait_for(probe, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = probe()
        except ContextLostError:
            result = None
        except RuntimeError as error:
            # Chrome temporarily detaches the page during a history navigation.
            if "Not attached to an active page" not in str(error):
                raise
            result = None
        if result:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.1, remaining))


def document(tab):
    response = tab.run_cdp(
        "Runtime.evaluate",
        expression="""({
            url: location.href,
            error: document.documentURI.startsWith('chrome-error:'),
            complete: document.readyState === 'complete',
            started: performance.timeOrigin,
            status: performance.getEntriesByType('navigation')[0]?.responseStatus || 0
        })""",
        returnByValue=True,
        _timeout=2,
    )
    return response["result"].get("value")


def check_document(state):
    if state["error"]:
        raise PageFailure("NETWORK_ERROR", "Chromium показал страницу ошибки сети")
    code = state["status"]
    if code in (401, 403, 429):
        raise AccessDenied(f"Сервис ограничил доступ (HTTP {code})")
    if code in (404, 410):
        raise PageFailure("NOT_FOUND", f"Страница отсутствует (HTTP {code})")
    if code >= 400:
        raise PageFailure("HTTP_ERROR", f"Ошибка HTTP {code}")


def loaded_document(tab, timeout, *, validate=True, expected_url=None):
    def probe():
        state = document(tab)
        if state and state["complete"] and (expected_url is None or state["url"] == expected_url):
            return state
        return None

    state = wait_for(probe, timeout)
    if state is None:
        raise PageFailure("TIMEOUT", "Документ не загрузился за отведённое время")
    if validate:
        check_document(state)
    return state


def click_element(tab, element, timeout):
    tab.scroll.to_see(element, center=True)
    # Multiline links can have a bounding-box center outside every rendered line.
    point = wait_for(
        lambda: element.run_js("""
            for (const rect of this.getClientRects()) {
                const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
                const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
                if (left >= right || top >= bottom) continue;
                const x = (left + right) / 2, y = (top + bottom) / 2;
                if (this.contains(document.elementFromPoint(x, y))) {
                    return [x + scrollX, y + scrollY];
                }
            }
            return null;
        """, timeout=min(timeout, 2)),
        timeout,
    )
    if point is None:
        raise PageFailure("CLICK_UNAVAILABLE", "Элемент недоступен для клика")
    return tab.actions.move_to(point, duration=0.2).click()


def click_and_follow(browser, source, link, timeout):
    before = document(source)
    known_tabs = set(browser.get_tabs(as_id=True))
    click_element(source, link, timeout)

    def probe():
        tabs = browser.get_tabs(as_id=True)
        new_tab = next((tab_id for tab_id in tabs if tab_id not in known_tabs), None)
        tab = browser.get_tab(new_tab) if new_tab else source
        state = document(tab)
        if not state or not state["complete"]:
            return None
        if tab is source and all(state[key] == before[key] for key in ("url", "started", "error")):
            return None
        if state["url"] == "about:blank":
            return None
        return tab, state

    result = wait_for(probe, timeout)
    if not result:
        raise PageFailure("TIMEOUT", "После клика документ не загрузился")
    return result


def hostname(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            return parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        pass
    return ""


def matches_domain(url, domain):
    host = hostname(url)
    return host == domain or host.endswith("." + domain)
