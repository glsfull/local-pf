from time import sleep
from urllib.parse import unquote, urljoin, urlsplit

from .browser import (
    AccessDenied,
    PageFailure,
    RunError,
    check_document,
    click_and_follow,
    loaded_document,
    matches_domain,
    wait_for,
)
from .clock import timestamp
from .search import SearchChallenge, check_challenge


def matches_path(url, expected):
    actual = urlsplit(url)
    wanted = urlsplit(expected)
    return (
        unquote(actual.path).rstrip("/") == unquote(wanted.path).rstrip("/")
        and (not wanted.query or actual.query == wanted.query)
        and (not wanted.fragment or actual.fragment == wanted.fragment)
    )


def find_link(tab, target, domain):
    selector = target.selector or "a[href]"
    text = " ".join(target.text.split()).casefold()
    for link in tab.eles(f"css:{selector}", timeout=0):
        if link.tag != "a":
            continue
        href = link.attr("href") or ""
        if not matches_domain(href, domain):
            continue
        if target.url and not matches_path(href, target.url):
            continue
        if text and " ".join(link.text.split()).casefold() != text:
            continue
        if link.states.is_displayed:
            return link
    return None


def check_page(browser, home, target, config, journal, result):
    link = wait_for(lambda: find_link(home, target, config.domain), config.timeout)
    selector = target.selector or "a[href]"
    journal.record(
        "Проверка ссылки", home.url, selector=selector, target=target.label,
        found=link is not None, status="OK" if link is not None else "NOT_FOUND",
    )
    if link is None:
        raise PageFailure("NOT_FOUND", "Ссылка отсутствует на входной странице")
    result["url"] = link.attr("href")
    journal.step("Переход по ссылке", home.url, destination=result["url"])
    tab, state = click_and_follow(browser, home, link, config.timeout)
    if not state["error"]:
        result["url"] = state["url"]
    journal.record("Переход на страницу", result["url"], http_status=state["status"])
    if matches_domain(state["url"], config.domain):
        check_challenge(tab, state["url"], config)
    check_document(state)
    if not matches_domain(state["url"], config.domain):
        raise PageFailure("UNEXPECTED_PAGE", "После клика открылся другой домен")
    if target.url and not matches_path(state["url"], target.url):
        raise PageFailure("UNEXPECTED_PAGE", "Открытый путь отличается от ожидаемого")
    found = bool(tab.wait.ele_displayed(
        f"css:{target.expected_selector}", timeout=config.timeout, raise_err=False
    ))
    journal.record(
        "Проверка элемента", state["url"], selector=target.expected_selector,
        found=found, status="OK" if found else "NOT_FOUND",
    )
    if not found:
        raise PageFailure("NOT_FOUND", "На странице отсутствует ожидаемый элемент")
    for step in range(1, 4):
        journal.step("Прокрутка", state["url"], step=step)
        tab.actions.scroll(delta_y=400)
        sleep(0.2)
        position = tab.run_js("""return (() => {
            const element = document.scrollingElement;
            const {scrollTop, scrollHeight, clientHeight} = element;
            const range = scrollHeight - clientHeight;
            return {
                scrollY: Math.round(scrollTop),
                scrollHeight,
                percent: range > 0 ? Math.round(100 * scrollTop / range) : 0
            };
        })();""")
        result["scroll_position"] = position
        journal.record("Позиция прокрутки", state["url"], scroll_position=position, step=step)


def history_entry(tab):
    history = tab.run_cdp("Page.getNavigationHistory", _timeout=2)
    return history["entries"][history["currentIndex"]]["id"]


def restore_home(browser, home, known_tabs, entry, landing_url, timeout, journal):
    for tab_id in browser.get_tabs(as_id=True):
        if tab_id not in known_tabs:
            browser.get_tab(tab_id).close()
    if history_entry(home) != entry:
        journal.step("Возврат на входную страницу", home.url, destination=landing_url)
        home.run_cdp("Page.navigateToHistoryEntry", entryId=entry, _timeout=2)
        if not wait_for(lambda: history_entry(home) == entry, timeout):
            raise RunError("Не удалось вернуться на входную страницу сайта")
        loaded_document(home, timeout, expected_url=landing_url)
        journal.record("Переход на входную страницу", landing_url)


def check_pages(browser, home, config, report, journal):
    entry = history_entry(home)
    landing_url = home.url
    known_tabs = set(browser.get_tabs(as_id=True))
    for index, target in enumerate(config.pages):
        result = {
            "target": target.label,
            "url": urljoin(landing_url, target.url) if target.url else None,
            "exists": False,
            "status": "ERROR",
            "scroll_position": None,
            "error": None,
        }
        try:
            check_page(browser, home, target, config, journal, result)
        except (AccessDenied, SearchChallenge, PageFailure) as error:
            result.update(status=error.status, error=str(error))
            if not isinstance(error, PageFailure):
                raise
            journal.record(
                "Ошибка", journal.url, status=error.status, error=str(error), target=target.label,
            )
        except Exception as error:
            result["error"] = str(error)
            raise
        else:
            result.update(status="OK", exists=True)
        finally:
            result["timestamp_msk"] = timestamp()
            report["pages_checked"].append(result)
        if index + 1 < len(config.pages):
            restore_home(browser, home, known_tabs, entry, landing_url, config.timeout, journal)
