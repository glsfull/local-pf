import re

from .search import MAX_QUERY_LENGTH, domain_hint, site_host

KEYWORD_FIELDS = ("phrase", "landing_url", "hint_type", "brand")
KEYWORD_ATTRIBUTES = ("cluster", "iw", "demand", "chance", "power", "visits", "competition", "budget")
COMPETITION = {"low": "Низкая", "medium": "Средняя", "high": "Высокая"}
MAX_KEYWORDS = 100
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN = re.compile(rf"{LABEL}(?:\.{LABEL})+")


def validate_url(value):
    if len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f\\]", value):
        return False
    return DOMAIN.fullmatch(site_host(value)) is not None


def read_project(form):
    fields = {
        key: form.get(key, "").strip()
        for key in ("name", "url", "region", "interval_seconds", "region_name", "spent")
    }
    columns = [form.getlist(field) for field in KEYWORD_FIELDS]
    count = len(columns[0])
    columns += [form.getlist(field) or [""] * count for field in KEYWORD_ATTRIBUTES]
    if len({len(column) for column in columns}) != 1 or len(columns[0]) > MAX_KEYWORDS:
        raise ValueError("Проверьте список фраз. Максимум 100 фраз на проект.")
    rows = [
        dict(zip(KEYWORD_FIELDS + KEYWORD_ATTRIBUTES, (value.strip() for value in values)))
        for values in zip(*columns)
    ]
    return fields, rows


def optional_number(value, label, lower, upper, *, decimal=False):
    if not value:
        return None
    pattern = r"-?[0-9]{1,9}(?:[.,][0-9]{1,2})?" if decimal else r"-?[0-9]{1,9}"
    if re.fullmatch(pattern, value):
        number = float(value.replace(",", ".")) if decimal else int(value)
        if lower <= number <= upper:
            return number
    raise ValueError(f"{label} — укажите число от {lower} до {upper}.")


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
    if len(project["region_name"]) > 100:
        raise ValueError("Название региона должно содержать до 100 символов.")
    spent = optional_number(project["spent"], "Расход", 0, 999999999, decimal=True)

    domain = domain_hint(project["url"])
    site = site_host(project["url"])
    prepared = []
    seen = set()
    for index, row in enumerate(keywords, start=1):
        phrase, url, hint_type, brand = (row[key] for key in KEYWORD_FIELDS)
        brand = brand if hint_type == "brand" else ""
        if not phrase and not url and not brand and not any(row[key] for key in KEYWORD_ATTRIBUTES):
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
        cluster, iw, demand, chance, power, visits, competition, budget = (row[key] for key in KEYWORD_ATTRIBUTES)
        if len(cluster) > 100 or (competition and competition not in COMPETITION):
            raise ValueError(f"Проверьте кластер и конкуренцию в строке {index}.")
        try:
            attributes = (
                cluster,
                optional_number(iw, "IW", 0, 999999999),
                optional_number(demand, "Спрос", -999999999, 999999999),
                optional_number(chance, "Шанс топа", 0, 100, decimal=True),
                optional_number(power, "Мощность", 0, 100, decimal=True),
                optional_number(visits, "Переходы в день", 0, 999999999),
                competition,
                optional_number(budget, "Бюджет в день", 0, 999999999, decimal=True),
            )
        except ValueError as exc:
            raise ValueError(f"Строка {index}. {exc}") from exc
        prepared.append((phrase, url, hint_type, brand, attributes))
    if not prepared:
        raise ValueError("Добавьте хотя бы одну поисковую фразу.")
    values = (project["name"], project["url"], project["region"], int(value))
    return values, prepared, (project["region_name"], spent)
