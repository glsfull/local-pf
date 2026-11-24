"""Yandex Search API in deferred mode: a captcha-free source of search results.

Deferred requests (``searchAsync``) cost 30.5 ₽ per 1000 in the daytime and 25.41 ₽ at night
versus 488/366 ₽ for synchronous ones, see https://aistudio.yandex.ru/ru/docs/search-api/pricing.
Each request is one billing unit, so one request with ``groupsOnPage=50`` covers the top 50.
The answer is ready in minutes to hours, so all phrases are submitted up front and polled.
"""
import base64
import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from dashboard.search import MAX_RANK, SearchError, site_host
from .clock import timestamp

SEARCH_ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_ENDPOINT = "https://operation.api.cloud.yandex.net/operations/"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
NO_RESULTS = "15"
log = logging.getLogger(__name__)


def parse_results(xml):
    """Return result URLs from a Yandex XML answer; error 15 means an empty result page."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise SearchError("Yandex Search API вернул некорректный XML.") from error
    failure = root.find("./response/error")
    if failure is not None:
        if failure.get("code") == NO_RESULTS:
            return []
        raise SearchError(f"Yandex Search API: {(failure.text or '').strip() or 'ошибка ' + str(failure.get('code'))}")
    urls = [node.text.strip() for node in root.findall("./response/results/grouping/group/doc/url") if node.text]
    if any(not site_host(url) for url in urls):
        raise SearchError("В ответе Yandex Search API есть результат без распознаваемого URL.")
    return urls[:MAX_RANK]


class YandexDeferredSearch:
    """``search_page(query, page, region)`` backed by deferred Yandex Search API requests."""

    def __init__(
        self, api_key, folder_id, journal, report, *, poll_interval=60, max_wait=4 * 3600,
        wait=None, clock=None, opener=None, save_progress=None,
    ):
        if not api_key or not folder_id:
            raise ValueError("Укажите YANDEX_SEARCH_API_KEY и YANDEX_SEARCH_FOLDER_ID для Yandex Search API.")
        self.api_key = api_key
        self.folder_id = folder_id
        self.journal = journal
        self.report = report
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        # Resolved lazily so tests can patch the module-level urlopen/time functions.
        self.wait = wait or (lambda seconds: time.sleep(seconds))
        self.clock = clock or (lambda: time.monotonic())
        self.opener = opener or (lambda request, timeout: urlopen(request, timeout=timeout))
        self.save_progress = save_progress
        self.operations = {}
        self.results = {}
        self.next_request = 0.0
        self.browser = None

    def call(self, url, body=None):
        # Space requests by one second: the default quota of the API is counted per second.
        delay = self.next_request - self.clock()
        if delay > 0:
            self.wait(delay)
        self.next_request = self.clock() + 1.0
        request = Request(
            url,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Api-Key {self.api_key}", "Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with self.opener(request, timeout=30) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise SearchError(f"Yandex Search API вернул HTTP {error.code}.") from error
        except (URLError, OSError) as error:
            raise SearchError("Не удалось подключиться к Yandex Search API.") from error
        if len(payload) > MAX_RESPONSE_BYTES:
            raise SearchError("Размер ответа Yandex Search API превышает 4 МБ.")
        try:
            return json.loads(payload)
        except ValueError as error:
            raise SearchError("Yandex Search API вернул некорректный JSON.") from error

    def submit(self, query, region):
        key = (query, region or None)
        if key in self.operations or key in self.results:
            return self.operations.get(key)
        body = {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": query,
                "page": "0",
                "fixTypoMode": "FIX_TYPO_MODE_OFF",
            },
            "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": str(MAX_RANK), "docsInGroup": "1"},
            "folderId": self.folder_id,
            "responseFormat": "FORMAT_XML",
        }
        if region:
            body["region"] = str(region)
        operation = self.call(SEARCH_ENDPOINT, body)
        operation_id = operation.get("id")
        if not operation_id:
            raise SearchError("Yandex Search API не вернул идентификатор отложенного запроса.")
        self.operations[key] = operation_id
        self.journal.record(
            "Отложенный запрос отправлен", SEARCH_ENDPOINT, query=query, region=region or None,
            operation_id=operation_id, status="PENDING",
        )
        if operation.get("done"):
            self.finish(key, operation)
        return operation_id

    def prepare(self, queries, region):
        """Submit every phrase before waiting so that the deferred answers are prepared in parallel."""
        try:
            for query in dict.fromkeys(queries):
                self.submit(query, region)
        except SearchError as error:
            # The measurement of this phrase repeats the request and fails the run in execute_run.
            log.warning("Не удалось отправить отложенные запросы заранее: %s", error)
        self.persist()

    def finish(self, key, operation):
        operation_id = self.operations.pop(key)
        url = OPERATION_ENDPOINT + operation_id
        if operation.get("error"):
            error = operation["error"]
            message = f"Yandex Search API: {error.get('message') or 'ошибка ' + str(error.get('code'))}"
            self.record_page(url, "API_ERROR", message)
            raise SearchError(message)
        try:
            xml = base64.b64decode(operation["response"]["rawData"], validate=True)
        except (KeyError, TypeError, ValueError) as error:
            self.record_page(url, "API_ERROR", "Ответ Yandex Search API без rawData.")
            raise SearchError("Ответ Yandex Search API без rawData.") from error
        try:
            urls = parse_results(xml)
        except SearchError as error:
            self.record_page(url, "API_ERROR", str(error))
            raise
        self.results[key] = urls
        self.journal.record(
            "Ответ API получен", url, query=key[0], operation_id=operation_id, status="OK",
            result_count=len(urls),
        )
        self.record_page(url, "OK", None)
        return urls

    def record_page(self, url, status, error):
        self.report["pages_checked"].append({
            "url": url, "exists": error is None, "status": status,
            "scroll_position": None, "error": error, "timestamp_msk": timestamp(),
        })
        if error:
            self.journal.record("Ошибка", url, status=status, error=error)

    def persist(self):
        if self.save_progress is not None:
            try:
                self.save_progress()
            except Exception:
                log.exception("Не удалось сохранить промежуточный отчёт")

    def __call__(self, query, page, region):
        # One request returns the whole top-50, the following pages are therefore empty.
        if page:
            return []
        key = (query, region or None)
        if key in self.results:
            return self.results[key]
        self.submit(query, region)
        if key in self.results:
            return self.results[key]
        operation_id = self.operations[key]
        url = OPERATION_ENDPOINT + operation_id
        started = self.clock()
        self.journal.record(
            "Ожидание ответа API", url, query=query, operation_id=operation_id, status="PENDING",
            poll_seconds=self.poll_interval, timeout_seconds=self.max_wait,
        )
        self.persist()
        while True:
            operation = self.call(url)
            if operation.get("done"):
                return self.finish(key, operation)
            waited = self.clock() - started
            if waited >= self.max_wait:
                self.operations.pop(key, None)
                message = f"Yandex Search API не подготовил ответ за {self.max_wait:g} с."
                self.record_page(url, "TIMEOUT", message)
                raise SearchError(message)
            self.wait(min(self.poll_interval, self.max_wait - waited))
