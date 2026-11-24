"""Yandex Search API v2 in deferred (``searchAsync``) mode — positions without a browser or captcha.

Pricing per 1000 requests (https://aistudio.yandex.ru/docs/ru/search-api/pricing.html): deferred requests
cost 30.5 ₽ in the daytime and 25.41 ₽ at night (00:00–07:59 MSK) versus 488/366 ₽ for synchronous ones.
One request is one billing unit, and one request with ``groupsOnPage=50`` covers the whole top 50,
so a phrase costs one request (two when the refined query is needed).

The answer is prepared in minutes to hours, therefore every phrase is submitted first and polled afterwards.
Authentication: an API key of a service account with the ``search-api.webSearch.user`` role and the folder ID.
"""
import base64
from datetime import datetime
import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from modules.clock import MSK, timestamp

from .search import MAX_RANK, SearchError, site_host

SEARCH_ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_ENDPOINT = "https://operation.api.cloud.yandex.net/operations/"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
NO_RESULTS = "15"
DAY_PRICE, NIGHT_PRICE = 30.5, 25.41
SOURCE = "Yandex Search API"
log = logging.getLogger(__name__)


def request_price(moment=None):
    """Price of one deferred request in rubles at ``moment`` (MSK)."""
    moment = (moment or datetime.now(MSK)).astimezone(MSK)
    return (NIGHT_PRICE if moment.hour < 8 else DAY_PRICE) / 1000


def parse_results(xml):
    """Return result URLs from a Yandex XML answer: the first document of every group.

    Error 15 means that the query has no results, which is an empty top rather than a failure.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise SearchError("Yandex Search API вернул некорректный XML.") from error
    failure = root.find("./response/error")
    if failure is not None:
        if failure.get("code") == NO_RESULTS:
            return []
        text = (failure.text or "").strip() or f"ошибка {failure.get('code')}"
        raise SearchError(f"Yandex Search API: {text}")
    urls = []
    for group in root.findall("./response/results/grouping/group"):
        node = group.find("doc/url")
        if node is not None and node.text:
            urls.append(node.text.strip())
    if any(not site_host(url) for url in urls):
        raise SearchError("В ответе Yandex Search API есть результат без распознаваемого URL.")
    return urls[:MAX_RANK]


class YandexDeferredSearch:
    """``search_page(query, page, region)`` backed by deferred Yandex Search API requests.

    ``prepare(queries, region)`` submits a batch before the first wait, so that Yandex prepares
    the answers in parallel; ``__call__`` then polls the operation of one phrase.
    """

    def __init__(
        self, api_key, folder_id, journal, report, *, poll_interval=60, max_wait=6 * 3600,
        wait=None, clock=None, opener=None, save_progress=None,
    ):
        if not api_key or not folder_id:
            raise ValueError("Укажите YANDEX_API_KEY и YANDEX_FOLDER_ID в настройках панели.")
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
        self.requests = 0
        self.cost = 0.0
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
            hint = {
                401: " Проверьте YANDEX_API_KEY.",
                403: " Проверьте YANDEX_FOLDER_ID и роль search-api.webSearch.user.",
                429: " Превышена квота запросов.",
            }.get(error.code, "")
            raise SearchError(f"Yandex Search API вернул HTTP {error.code}.{hint}") from error
        except (URLError, OSError) as error:
            raise SearchError("Не удалось подключиться к Yandex Search API.") from error
        if len(payload) > MAX_RESPONSE_BYTES:
            raise SearchError("Размер ответа Yandex Search API превышает 4 МБ.")
        try:
            return json.loads(payload)
        except ValueError as error:
            raise SearchError("Yandex Search API вернул некорректный JSON.") from error

    def body(self, query, region):
        body = {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": query,
                "page": "0",
                "fixTypoMode": "FIX_TYPO_MODE_OFF",
            },
            # GROUP_MODE_DEEP groups documents by site like the search results page: one site — one position.
            "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(MAX_RANK), "docsInGroup": "1"},
            "maxPassages": "1",
            "l10n": "LOCALIZATION_RU",
            "folderId": self.folder_id,
            "responseFormat": "FORMAT_XML",
        }
        if region:
            body["region"] = str(region)
        return body

    def submit(self, query, region):
        key = (query, region or None)
        if key in self.operations or key in self.results:
            return self.operations.get(key)
        operation = self.call(SEARCH_ENDPOINT, self.body(query, region))
        operation_id = operation.get("id")
        if not operation_id:
            raise SearchError("Yandex Search API не вернул идентификатор отложенного запроса.")
        self.operations[key] = operation_id
        self.requests += 1
        price = request_price()
        self.cost += price
        self.journal.record(
            "Отложенный запрос отправлен", SEARCH_ENDPOINT, query=query, page=1, region=region or None,
            operation_id=operation_id, status="PENDING", cost_rub=round(price, 5),
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
            "Ответ API получен", url, query=key[0], page=1, operation_id=operation_id, status="OK",
            result_count=len(urls),
        )
        self.record_page(url, "OK", None)
        return urls

    def record_page(self, url, status, error):
        self.report.setdefault("pages_checked", []).append({
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

    def summary(self):
        return {"api_requests": self.requests, "api_cost_rub": round(self.cost, 4)}

    def __call__(self, query, page, region):
        # One request returns the whole top 50, the following pages are therefore empty.
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
            "Ожидание ответа API", url, query=query, page=1, operation_id=operation_id, status="PENDING",
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
                message = f"Yandex Search API не подготовил ответ за {self.max_wait / 3600:g} ч."
                self.record_page(url, "TIMEOUT", message)
                raise SearchError(message)
            self.wait(min(self.poll_interval, self.max_wait - waited))
