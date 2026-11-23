import logging
from urllib.parse import urlsplit

from .browser import (
    PageFailure,
    RunError,
    check_document,
    click_and_follow,
    click_element,
    hostname,
    loaded_document,
    matches_domain,
    wait_for,
)

SEARCH_HOME = "https://ya.ru"
log = logging.getLogger(__name__)


class SearchChallenge(RunError):
    status = "CAPTCHA"

    def __init__(self, url):
        super().__init__("На странице обнаружена SmartCaptcha")
        self.url = url


def check_challenge(tab, url, config):
    if "showcaptcha" in urlsplit(url).path.lower() or tab.ele(
        f"css:{config.captcha}", timeout=0
    ):
        raise SearchChallenge(url)


def open_search(tab, config, search_home=SEARCH_HOME, *, journal=None):
    log.info("Открытие поисковой страницы")
    loaded = tab.get(search_home, retry=0, timeout=config.timeout)
    state = loaded_document(tab, config.timeout, validate=False)
    if journal is not None:
        journal.record("Переход на страницу поиска", state["url"], http_status=state["status"])
    check_challenge(tab, state["url"], config)
    check_document(state)
    if not loaded and state["url"] == "about:blank":
        raise PageFailure("NETWORK_ERROR", "Поисковая страница недоступна")


def find_target(browser, config, report, journal, search_home=SEARCH_HOME):
    journal.step("Открытие поиска", search_home)
    open_search(browser, config, search_home, journal=journal)

    field = browser.ele(f"css:{config.search_input}", timeout=config.timeout)
    button = browser.ele(f"css:{config.search_button}", timeout=config.timeout)
    for selector, element in ((config.search_input, field), (config.search_button, button)):
        journal.record(
            "Проверка элемента", browser.url, selector=selector,
            found=bool(element), status="OK" if element else "NOT_FOUND",
        )
    if not field or not button:
        check_challenge(browser, browser.url, config)
        raise RunError("Форма поиска отсутствует. Проверьте селекторы.")

    journal.step("Ввод запроса", browser.url, selector=config.search_input)
    click_element(browser, field, config.timeout).input(config.keyword)
    journal.step("Отправка запроса", browser.url, selector=config.search_button)
    results, state = click_and_follow(browser, browser, button, config.timeout)
    journal.record("Переход к выдаче", state["url"], http_status=state["status"])
    check_challenge(results, state["url"], config)
    check_document(state)

    def result_links():
        check_challenge(results, results.url, config)
        return results.eles(f"css:{config.result_link}", timeout=0)

    links = wait_for(result_links, config.timeout) or []
    log.info("Найдено ссылок в выдаче: %d", len(links))
    for link in links:
        href = link.attr("href") or ""
        host = hostname(href)
        log.debug("Домен ссылки из выдачи: %s", host)
        if link.states.is_displayed and matches_domain(href, config.domain):
            report["search_result_found"] = True
            journal.step("Клик по результату", results.url, destination=href)
            landing, state = click_and_follow(browser, results, link, config.timeout)
            journal.record("Переход на целевой сайт", state["url"], http_status=state["status"])
            if matches_domain(state["url"], config.domain):
                check_challenge(landing, state["url"], config)
                check_document(state)
                return landing
            if matches_domain(state["url"], "ya.ru") or matches_domain(state["url"], "yandex.ru"):
                check_challenge(landing, state["url"], config)
            check_document(state)
            raise RunError("После клика открылся другой домен")

    raise RunError(f"Целевой домен {config.domain} отсутствует в доступной выдаче")
