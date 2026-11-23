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
    pass


def check_challenge(tab, url, config):
    if "showcaptcha" in urlsplit(url).path.lower() or tab.ele(
        f"css:{config.captcha}", timeout=0
    ):
        raise SearchChallenge("Поисковая система запросила SmartCaptcha")


def open_search(tab, config, search_home=SEARCH_HOME):
    log.info("Открытие поисковой страницы")
    loaded = tab.get(search_home, retry=0, timeout=config.timeout)
    state = loaded_document(tab, config.timeout, validate=False)
    check_challenge(tab, state["url"], config)
    check_document(state)
    if not loaded and state["url"] == "about:blank":
        raise PageFailure("NETWORK_ERROR", "Поисковая страница недоступна")


def find_target(browser, config, report, search_home=SEARCH_HOME):
    open_search(browser, config, search_home)

    field = browser.ele(f"css:{config.search_input}", timeout=config.timeout)
    button = browser.ele(f"css:{config.search_button}", timeout=config.timeout)
    if not field or not button:
        check_challenge(browser, browser.url, config)
        raise RunError("Форма поиска отсутствует. Проверьте селекторы.")

    log.info("Отправка поискового запроса")
    click_element(browser, field, config.timeout).input(config.keyword)
    results, state = click_and_follow(browser, browser, button, config.timeout)
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
            log.info("Клик по результату для %s", config.domain)
            landing, state = click_and_follow(browser, results, link, config.timeout)
            if matches_domain(state["url"], config.domain):
                check_document(state)
                return landing
            if matches_domain(state["url"], "ya.ru") or matches_domain(state["url"], "yandex.ru"):
                check_challenge(landing, state["url"], config)
            check_document(state)
            raise RunError("После клика открылся другой домен")

    raise RunError(f"Целевой домен {config.domain} отсутствует в доступной выдаче")
