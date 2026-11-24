import logging

from .browser import (
    CaptchaTimeout,
    PageFailure,
    RunError,
    SearchChallenge,
    captcha_present,
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


MAX_CHALLENGES = 3


def check_challenge(tab, url, config, captcha=None):
    """Return ``None`` without a captcha, or the document state after the handler solved it."""
    if not captcha_present(tab, url, config.captcha):
        return None
    if captcha is None:
        raise SearchChallenge(url)
    return captcha.handle_captcha(tab, url)


def wait_past_challenge(probe, tab, timeout, captcha=None):
    """Run ``wait_for`` and hand a captcha raised by ``probe`` to the handler."""
    for _ in range(MAX_CHALLENGES):
        try:
            return wait_for(probe, timeout)
        except CaptchaTimeout:
            raise
        except SearchChallenge as error:
            if captcha is None:
                raise
            captcha.handle_captcha(tab, error.url)
    return wait_for(probe, timeout)


def open_search(tab, config, search_home=SEARCH_HOME, *, journal=None, captcha=None):
    log.info("Открытие поисковой страницы")
    loaded = tab.get(search_home, retry=0, timeout=config.timeout)
    state = loaded_document(tab, config.timeout, validate=False)
    if journal is not None:
        journal.record("Переход на страницу поиска", state["url"], http_status=state["status"])
    state = check_challenge(tab, state["url"], config, captcha) or state
    check_document(state)
    if not loaded and state["url"] == "about:blank":
        raise PageFailure("NETWORK_ERROR", "Поисковая страница недоступна")


def search_form(browser, config, journal, captcha=None):
    for _ in range(MAX_CHALLENGES):
        field = browser.ele(f"css:{config.search_input}", timeout=config.timeout)
        button = browser.ele(f"css:{config.search_button}", timeout=config.timeout)
        for selector, element in ((config.search_input, field), (config.search_button, button)):
            journal.record(
                "Проверка элемента", browser.url, selector=selector,
                found=bool(element), status="OK" if element else "NOT_FOUND",
            )
        if field and button:
            return field, button
        if check_challenge(browser, browser.url, config, captcha) is None:
            break
    raise RunError("Форма поиска отсутствует. Проверьте селекторы.")


def find_target(browser, config, report, journal, search_home=SEARCH_HOME, *, captcha=None):
    journal.step("Открытие поиска", search_home)
    open_search(browser, config, search_home, journal=journal, captcha=captcha)

    field, button = search_form(browser, config, journal, captcha)

    journal.step("Ввод запроса", browser.url, selector=config.search_input)
    click_element(browser, field, config.timeout).input(config.keyword)
    journal.step("Отправка запроса", browser.url, selector=config.search_button)
    results, state = click_and_follow(browser, browser, button, config.timeout)
    journal.record("Переход к выдаче", state["url"], http_status=state["status"])
    state = check_challenge(results, state["url"], config, captcha) or state
    check_document(state)

    def result_links():
        check_challenge(results, results.url, config)
        return results.eles(f"css:{config.result_link}", timeout=0)

    links = wait_past_challenge(result_links, results, config.timeout, captcha) or []
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
            if any(matches_domain(state["url"], domain) for domain in (config.domain, "ya.ru", "yandex.ru")):
                state = check_challenge(landing, state["url"], config, captcha) or state
            check_document(state)
            if matches_domain(state["url"], config.domain):
                return landing
            raise RunError("После клика открылся другой домен")

    raise RunError(f"Целевой домен {config.domain} отсутствует в доступной выдаче")
