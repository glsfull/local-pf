import base64
import json
import logging
import re
from string import ascii_letters, digits
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import tldextract

ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/search"
PAGE_SIZE = 10
MAX_PAGES = 5
MAX_QUERY_LENGTH = 400
MAX_RESPONSE_BYTES = 1024 * 1024
extract_domain = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
logger = logging.getLogger(__name__)


class SearchError(Exception):
    pass


def site_host(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.username is not None:
            return ""
        if parts.port == 0:
            return ""
        host = parts.hostname or ""
        return host.rstrip(".").encode("idna").decode("ascii").removeprefix("www.")
    except (ValueError, UnicodeError):
        return ""


def domain_hint(url):
    domain = extract_domain(site_host(url))
    return domain.domain if domain.suffix else ""


def page_key(url):
    parts = urlsplit(url)
    default_port = 443 if parts.scheme == "https" else 80
    port = None if parts.port == default_port else parts.port
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    path = re.sub(r"%[0-9a-fA-F]{2}", normalize_path_escape, path)
    return site_host(url), port, path.rstrip("/") or "/", parts.query


def normalize_path_escape(match):
    char = chr(int(match[0][1:], 16))
    return char if char in ascii_letters + digits + "-._~" else match[0].upper()


def match_position(search_page, query, project_url, landing_url, region):
    site = site_host(project_url)
    landing = page_key(landing_url) if landing_url else None
    for page in range(MAX_PAGES):
        urls = search_page(query, page, region)
        if not urls:
            break
        for index, url in enumerate(urls[:PAGE_SIZE], start=1):
            host = site_host(url)
            same_site = host and (host == site or host.endswith("." + site))
            if same_site and (landing is None or page_key(url) == landing):
                return page * PAGE_SIZE + index, url
    return None, None


class YandexSearch:
    def __init__(self, api_key, folder_id):
        self.api_key = api_key
        self.folder_id = folder_id
        self.next_request = 0.0

    def __call__(self, query, page, region):
        # The synchronous API's default quota is one call per second.
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request = time.monotonic() + 1.0
        logger.debug("Yandex Search API: page=%d region=%s", page, region or "default")
        body = {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": query,
                "page": str(page),
                "fixTypoMode": "FIX_TYPO_MODE_OFF",
            },
            "groupSpec": {
                "groupMode": "GROUP_MODE_FLAT",
                "groupsOnPage": str(PAGE_SIZE),
                "docsInGroup": "1",
            },
            "folderId": self.folder_id,
            "responseFormat": "FORMAT_XML",
        }
        if region:
            body["region"] = region
        request = Request(
            ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Api-Key {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise SearchError("Размер ответа Yandex Search API превышает 1 МБ.")
            xml = base64.b64decode(json.loads(payload)["rawData"], validate=True)
            root = ElementTree.fromstring(xml)
        except HTTPError as exc:
            raise SearchError(f"Yandex Search API вернул HTTP {exc.code}.") from exc
        except (URLError, OSError) as exc:
            raise SearchError("Не удалось подключиться к Yandex Search API.") from exc
        except (ValueError, KeyError, TypeError, ElementTree.ParseError) as exc:
            raise SearchError("Не удалось прочитать ответ Yandex Search API.") from exc

        error = root.find("./response/error")
        if error is not None:
            code = error.get("code", "")
            if code == "15":
                return []
            message = f"Yandex Search API вернул ошибку {code}." if code.isdecimal() else (
                "Yandex Search API вернул ошибку."
            )
            raise SearchError(message)
        grouping = root.find("./response/results/grouping")
        if grouping is None:
            raise SearchError("В ответе Yandex Search API отсутствует блок результатов.")
        return [doc.findtext("url", "").strip() for doc in grouping.findall("./group/doc")]
