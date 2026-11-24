import csv
from datetime import date
import re

from .search import MAX_QUERY_LENGTH, domain_hint, site_host

KEYWORD_FIELDS = ("phrase", "landing_url", "hint_type", "brand")
KEYWORD_ATTRIBUTES = ("cluster", "iw", "demand", "chance", "power", "visits", "competition", "budget")
COMPETITION = {"low": "Низкая", "medium": "Средняя", "high": "Высокая"}
MAX_KEYWORDS = 100
# Yandex region code, menu label, city used for position collection and its code.
REGIONS = (
    ("1", "Москва и область", "Москва", "213"),
    ("213", "Москва", "Москва", "213"),
    ("10174", "Санкт-Петербург и область", "Санкт-Петербург", "2"),
    ("2", "Санкт-Петербург", "Санкт-Петербург", "2"),
    ("225", "Россия", "Москва", "213"),
    ("54", "Екатеринбург", "Екатеринбург", "54"),
    ("65", "Новосибирск", "Новосибирск", "65"),
    ("43", "Казань", "Казань", "43"),
    ("47", "Нижний Новгород", "Нижний Новгород", "47"),
    ("35", "Краснодар", "Краснодар", "35"),
    ("39", "Ростов-на-Дону", "Ростов-на-Дону", "39"),
    ("51", "Самара", "Самара", "51"),
    ("56", "Челябинск", "Челябинск", "56"),
    ("172", "Уфа", "Уфа", "172"),
)
REGION_INFO = {code: (label, city, city_code) for code, label, city, city_code in REGIONS}
QUERY_REGIONS = {"cities": "Распределять по городам", "as_is": "Подставлять «как есть»"}
FREQUENCY_TYPES = {"plain": '"ключевая фраза"', "exact": '"!ключевая !фраза"', "quoted": '"[!ключевая !фраза]"'}
THEMES = {
    "": "Определить автоматически по запросам", "services": "Услуги", "shop": "Интернет-магазин",
    "info": "Информационный сайт", "saas": "Сервисы и программы", "realty": "Недвижимость",
}
SCHEDULE_MODES = {
    "uniform_day": "Равномерно в течение дня",
    "uniform_hours": "Равномерно по часам",
    "random_hours": "Случайно распределить по часам",
    "random_minutes": "Случайно распределить по минутам (турбо-наливы по расписанию)",
}
NOT_FOUND_MODES = {"domain": "Добавить домен без", "brand": "Добавить брендовый запрос"}
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN = re.compile(rf"{LABEL}(?:\.{LABEL})+")


def validate_url(value):
    if len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f\\]", value):
        return False
    return DOMAIN.fullmatch(site_host(value)) is not None


def position_region(region):
    """Return the Yandex region code used for position checks."""
    return REGION_INFO[region][2] if region in REGION_INFO else region


def region_label(region, name=""):
    if name:
        return name
    return REGION_INFO[region][0] if region in REGION_INFO else region


def normalize_url(value):
    value = value.strip()
    if value and "://" not in value and re.fullmatch(r"[^\s/?#]+(?:[/?#].*)?", value):
        return "https://" + value
    return value


def read_project(form):
    fields = {
        key: form.get(key, "").strip()
        for key in ("name", "url", "region", "interval_seconds", "region_name", "spent")
    }
    fields["url"] = normalize_url(fields["url"])
    if fields["region"] == "custom":
        fields["region"] = form.get("region_custom", "").strip()
    elif fields["region"] in REGION_INFO:
        fields["region_name"] = region_label(fields["region"])
    if not fields["name"]:
        fields["name"] = site_host(fields["url"])[:100]
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


def validate_project(project, keywords, *, default_brand=""):
    if not validate_url(project["url"]):
        raise ValueError("Введите адрес сайта, например pfult.ru или https://pfult.ru/.")
    if not 1 <= len(project["name"]) <= 100:
        raise ValueError("Введите название проекта длиной до 100 символов.")
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
        if hint_type == "brand" and not brand:
            brand = default_brand
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


def read_settings(form):
    """Read the TopPult-style project settings. Returns None when the form has no settings block."""
    if form.get("settings_form") != "1":
        return None
    choices = {
        "query_region": QUERY_REGIONS, "frequency_type": FREQUENCY_TYPES, "theme": THEMES,
        "not_found": NOT_FOUND_MODES,
    }
    settings = {}
    for key, options in choices.items():
        value = form.get(key, "")
        if value not in options:
            raise ValueError("Проверьте дополнительные настройки проекта.")
        settings[key] = value
    brands = [line.strip() for line in form.get("brand_queries", "").splitlines() if line.strip()]
    if len(brands) > 20 or any(len(line) > 100 for line in brands):
        raise ValueError("Укажите до 20 брендовых запросов длиной до 100 символов.")
    if settings["not_found"] == "brand" and not brands:
        raise ValueError("Добавьте брендовый запрос или выберите «Добавить домен без зоны».")
    settings["brand_queries"] = "\n".join(brands)
    settings["limit_budget"] = int(form.get("limit_budget") == "1")
    settings["daily_budget"] = optional_number(
        form.get("daily_budget", "").strip(), "Дневной бюджет", 0, 999999999, decimal=True,
    )
    if settings["limit_budget"] and settings["daily_budget"] is None:
        raise ValueError("Укажите дневной бюджет или выключите ограничение.")
    return {**settings, **read_schedule_settings(form)}


def read_schedule_settings(form):
    """Read the schedule block shared by the project form and the «Расписание» tab."""
    mode = form.get("schedule_mode", "")
    if mode not in SCHEDULE_MODES:
        raise ValueError("Выберите расписание переходов.")
    settings = {"schedule_mode": mode, "schedule": read_schedule(form)}
    for key in ("collect_daily", "rare_lowfreq"):
        settings[key] = int(form.get(key) == "1")
    collect_time = form.get("collect_time", "21:00").strip()
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", collect_time):
        raise ValueError("Укажите время сбора позиций в формате ЧЧ:ММ.")
    settings["collect_time"] = collect_time
    return settings


def read_schedule(form):
    slots = ["0"] * (len(WEEKDAYS) * 24)
    for value in form.getlist("schedule"):
        match = re.fullmatch(r"([0-6])-([0-9]{1,2})", value)
        if not match or int(match[2]) > 23:
            raise ValueError("Проверьте расписание переходов.")
        slots[int(match[1]) * 24 + int(match[2])] = "1"
    return "".join(slots)


def brand_list(settings):
    return [line for line in settings["brand_queries"].splitlines() if line]


VISIT_COLUMNS = ("date", "phrase", "done", "planned", "cost", "depth")


def read_visits(text):
    """Parse a visits CSV: date;phrase;done;planned;cost[;depth]. Returns rows for visit_stats."""
    sample = text[:2048]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows = []
    for index, values in enumerate(csv.reader(text.splitlines(), delimiter=delimiter), start=1):
        if not any(value.strip() for value in values):
            continue
        values = [value.strip() for value in values]
        if index == 1 and values[0].casefold() in ("date", "дата"):
            continue
        if len(values) not in (5, 6):
            raise ValueError(f"Строка {index}: ожидается дата, запрос, выполнено, план, расход и глубина.")
        try:
            day = date.fromisoformat(values[0]) if "-" in values[0] else date(*map(int, reversed(values[0].split("."))))
        except (TypeError, ValueError):
            raise ValueError(f"Строка {index}: укажите дату в формате ГГГГ-ММ-ДД или ДД.ММ.ГГГГ.") from None
        phrase = values[1]
        if not 1 <= len(phrase) <= 399:
            raise ValueError(f"Строка {index}: укажите запрос.")
        try:
            done = optional_number(values[2], "Выполнено", 0, 999999)
            planned = optional_number(values[3], "План", 0, 999999)
            cost = optional_number(values[4], "Расход", 0, 999999999, decimal=True)
            depth = optional_number(values[5], "Глубина", 0, 100, decimal=True) if len(values) == 6 else None
        except ValueError as exc:
            raise ValueError(f"Строка {index}. {exc}") from None
        if done is None or planned is None or cost is None:
            raise ValueError(f"Строка {index}: заполните выполнено, план и расход.")
        rows.append((phrase, day.isoformat(), done, planned, cost, depth))
        if len(rows) > 50000:
            raise ValueError("Файл слишком большой: до 50 000 строк.")
    if not rows:
        raise ValueError("Файл не содержит строк с переходами.")
    return rows
