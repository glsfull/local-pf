import logging
import os
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from dashboard.search import MAX_RANK, SearchError, site_host
from .browser import CAPTCHA_SELECTOR, RunError, SearchChallenge, check_document, loaded_document, open_browser
from .clock import timestamp
from .search import check_challenge, wait_past_challenge

DEFAULT_RESULTS_URL = "https://ya.ru/search/"
RESULT_LINK_SELECTOR = "li.serp-item a.OrganicTitle-Link, li.serp-item h2 > a"
EMPTY_SELECTOR = ".EmptySearchResults, .SearchEmpty"
logger = logging.getLogger(__name__)


class RankParser:
    def __init__(self, journal, report, *, timeout=15, profile=None, handler=None):
        self.journal = journal
        self.handler = handler
        self.report = report
        self.timeout = timeout
        self.profile = profile
        self.captcha = CAPTCHA_SELECTOR
        self.browser = None
        self.source = urlsplit(os.getenv("LOCAL_PF_RESULTS_URL", DEFAULT_RESULTS_URL))
        if self.source.scheme not in ("http", "https") or not self.source.hostname or self.source.username:
            raise ValueError("LOCAL_PF_RESULTS_URL: укажите HTTP(S) URL страницы выдачи.")
        self.link_selector = os.getenv("LOCAL_PF_RESULT_LINK_SELECTOR", RESULT_LINK_SELECTOR)
        self.empty_selector = os.getenv("LOCAL_PF_EMPTY_RESULTS_SELECTOR", EMPTY_SELECTOR)

    def page_url(self, query, page, region):
        fields = parse_qs(self.source.query)
        fields.update(text=[query], p=[page])
        if region:
            fields["lr"] = [region]
        return urlunsplit(self.source._replace(query=urlencode(fields, doseq=True), fragment=""))

    def read_results(self, url, query, page):
        if self.browser is None:
            self.browser = open_browser(self.timeout, profile=self.profile)
        self.browser.get(url, retry=0, timeout=self.timeout)
        state = loaded_document(self.browser, self.timeout, validate=False)
        self.journal.record("Переход к выдаче", state["url"], http_status=state["status"])
        state = check_challenge(self.browser, state["url"], self, self.handler) or state
        actual_url = state["url"]
        check_document(state)
        actual = urlsplit(actual_url)
        fields = parse_qs(actual.query)
        if fields.get("p", ["0"]) != [str(page)] or fields.get("text") != [query]:
            raise SearchError("Источник открыл выдачу для другой страницы или фразы.")

        def parse():
            check_challenge(self.browser, self.browser.url, self)
            urls = self.browser.run_js("""
                const links = document.querySelectorAll(arguments[0]);
                const results = new Set();
                const urls = [];
                for (const link of links) {
                    const result = link.closest('li.serp-item') || link;
                    if (!result.getClientRects().length || results.has(result)) continue;
                    results.add(result);
                    urls.push(link.href || '');
                    if (urls.length >= arguments[1]) break;
                }
                return urls;
            """, self.link_selector, MAX_RANK)
            if urls:
                return (urls,)
            if self.browser.ele(f"css:{self.empty_selector}", timeout=0):
                return ([],)
            return None

        # An explicit empty page is valid. An unknown layout is an error.
        result = wait_past_challenge(parse, self.browser, self.timeout, self.handler)
        if result is None:
            raise SearchError("Не распознана поисковая выдача. Проверьте селекторы парсера.")
        urls, = result
        if any(not site_host(link) for link in urls):
            raise SearchError("В выдаче есть результат без распознаваемого URL.")
        return actual_url, urls

    def __call__(self, query, page, region):
        url = self.page_url(query, page, region)
        self.journal.step("Открытие выдачи", url, query=query, page=page + 1)
        error, status = None, "OK"
        try:
            url, results = self.read_results(url, query, page)
        except SearchChallenge as exc:
            url, status, error = exc.url, exc.status, str(exc)
            self.report["captcha_encountered"] = True
        except (RunError, SearchError) as exc:
            url = self.journal.url or url
            status, error = getattr(exc, "status", "PARSE_ERROR"), str(exc)
        except Exception:
            url = self.journal.url or url
            status, error = "BROWSER_ERROR", "Ошибка Chromium при чтении выдачи."
            logger.debug("Ошибка браузера при чтении %s", url, exc_info=True)
        else:
            self.journal.record("Разбор выдачи", url, status=status, result_count=len(results))
        finally:
            self.report["pages_checked"].append({
                "url": url, "exists": error is None, "status": status,
                "scroll_position": None, "error": error, "timestamp_msk": timestamp(),
            })
        if error:
            self.journal.record("Ошибка", url, status=status, error=error)
            raise SearchError(f"{status}: {error}")
        return results
