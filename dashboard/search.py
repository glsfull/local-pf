import re
from string import ascii_letters, digits
from urllib.parse import quote, urlsplit

import tldextract

MAX_PAGES = 5
MAX_RANK = 50
MAX_QUERY_LENGTH = 400
extract_domain = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


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
    rank = 0
    for page in range(MAX_PAGES):
        urls = search_page(query, page, region)
        if not urls:
            break
        for url in urls:
            rank += 1
            host = site_host(url)
            same_site = host and (host == site or host.endswith("." + site))
            if same_site and (landing is None or page_key(url) == landing):
                return rank, url
            if rank == MAX_RANK:
                return None, None
    return None, None
