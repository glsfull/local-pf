import logging
from time import sleep
from urllib.parse import unquote, urlsplit

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

log = logging.getLogger(__name__)


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


def check_page(browser, home, target, config):
    link = wait_for(lambda: find_link(home, target, config.domain), config.timeout)
    if link is None:
        raise PageFailure("NOT_FOUND", "Ссылка отсутствует на входной странице")
    log.info("Клик по ссылке на %s", target.label)
    tab, state = click_and_follow(browser, home, link, config.timeout)
    check_document(state)
    if not matches_domain(state["url"], config.domain):
        raise PageFailure("UNEXPECTED_PAGE", "После клика открылся другой домен")
    if target.url and not matches_path(state["url"], target.url):
        raise PageFailure("UNEXPECTED_PAGE", "Открытый путь отличается от ожидаемого")
    if not tab.wait.ele_displayed(
        f"css:{target.expected_selector}", timeout=config.timeout, raise_err=False
    ):
        raise PageFailure("NOT_FOUND", "На странице отсутствует ожидаемый элемент")
    log.info("Пролистывание %s", target.label)
    for _ in range(3):
        tab.actions.scroll(delta_y=400)
        sleep(0.2)


def history_entry(tab):
    history = tab.run_cdp("Page.getNavigationHistory", _timeout=2)
    return history["entries"][history["currentIndex"]]["id"]


def restore_home(browser, home, known_tabs, entry, landing_url, timeout):
    for tab_id in browser.get_tabs(as_id=True):
        if tab_id not in known_tabs:
            browser.get_tab(tab_id).close()
    if history_entry(home) != entry:
        log.info("Возврат на входную страницу сайта через историю браузера")
        home.run_cdp("Page.navigateToHistoryEntry", entryId=entry, _timeout=2)
        if not wait_for(lambda: history_entry(home) == entry, timeout):
            raise RunError("Не удалось вернуться на входную страницу сайта")
        loaded_document(home, timeout, expected_url=landing_url)


def check_pages(browser, home, config, report):
    entry = history_entry(home)
    landing_url = home.url
    known_tabs = set(browser.get_tabs(as_id=True))
    for index, target in enumerate(config.pages):
        log.info("Проверка %s", target.label)
        try:
            check_page(browser, home, target, config)
        except AccessDenied:
            report["pages_checked"].append(
                {"url": target.label, "exists": False, "status": "ACCESS_DENIED"}
            )
            raise
        except PageFailure as error:
            log.warning("%s: %s", target.label, error)
            status = error.status
        else:
            status = "OK"
        report["pages_checked"].append(
            {"url": target.label, "exists": status == "OK", "status": status}
        )
        if index + 1 < len(config.pages):
            restore_home(browser, home, known_tabs, entry, landing_url, config.timeout)
