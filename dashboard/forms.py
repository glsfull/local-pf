import re

from .search import MAX_QUERY_LENGTH, domain_hint, site_host

KEYWORD_FIELDS = ("phrase", "landing_url", "hint_type", "brand")
MAX_KEYWORDS = 100
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN = re.compile(rf"{LABEL}(?:\.{LABEL})+")


def validate_url(value):
    if len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f\\]", value):
        return False
    return DOMAIN.fullmatch(site_host(value)) is not None


def read_project(form):
    fields = {key: form.get(key, "").strip() for key in ("name", "url", "region", "interval_seconds")}
    columns = [form.getlist(field) for field in KEYWORD_FIELDS]
    if len({len(column) for column in columns}) != 1 or len(columns[0]) > MAX_KEYWORDS:
        raise ValueError("Проверьте список фраз. Максимум 100 фраз на проект.")
    rows = [
        dict(zip(KEYWORD_FIELDS, (value.strip() for value in values)))
        for values in zip(*columns)
    ]
    return fields, rows


def validate_project(project, keywords):
    if not 1 <= len(project["name"]) <= 100:
        raise ValueError("Введите название проекта длиной до 100 символов.")
    if not validate_url(project["url"]):
        raise ValueError("Введите полный адрес сайта, начиная с https:// или http://.")
    if project["region"] and not re.fullmatch(r"[0-9]{1,10}", project["region"]):
        raise ValueError("Код региона должен содержать до 10 цифр.")
    value = project["interval_seconds"]
    if not re.fullmatch(r"[0-9]{1,4}", value) or int(value) > 3600:
        raise ValueError("Укажите интервал от 0 до 3600 секунд.")

    domain = domain_hint(project["url"])
    site = site_host(project["url"])
    prepared = []
    seen = set()
    for index, row in enumerate(keywords, start=1):
        phrase, url, hint_type, brand = (row[key] for key in KEYWORD_FIELDS)
        brand = brand if hint_type == "brand" else ""
        if not phrase and not url and not brand:
            continue
        if not phrase:
            raise ValueError(f"Введите поисковую фразу в строке {index}.")
        if url:
            host = site_host(url)
            if not validate_url(url) or (host != site and not host.endswith("." + site)):
                raise ValueError(f"Укажите URL проекта или его поддомена в строке {index}.")
        if hint_type not in ("domain", "brand"):
            raise ValueError(f"Выберите тип подсказки в строке {index}.")
        if hint_type == "brand" and not brand:
            raise ValueError(f"Введите бренд для фразы в строке {index}.")
        if hint_type == "domain" and not domain:
            raise ValueError(f"Укажите бренд в строке {index}. У сайта не распознана доменная зона.")
        if len(f"{phrase} {brand or domain}") > MAX_QUERY_LENGTH:
            raise ValueError(f"Длина фразы с подсказкой в строке {index} превышает 400 символов.")
        if phrase.casefold() in seen:
            raise ValueError(f"Поисковая фраза в строке {index} уже есть в проекте.")
        seen.add(phrase.casefold())
        prepared.append((phrase, url, hint_type, brand))
    if not prepared:
        raise ValueError("Добавьте хотя бы одну поисковую фразу.")
    return (project["name"], project["url"], project["region"], int(value)), prepared
