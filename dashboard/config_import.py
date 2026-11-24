"""YAML configuration import: `py -m dashboard --config panel.yaml`.

The file fills the same fields as the «Настройки» page and the project form, with the same validation:
connection settings (YANDEX_API_KEY, YANDEX_FOLDER_ID, position source, noVNC address) and projects
with their keywords and refinement type. A project with the same site address is updated, not duplicated.
See examples/panel.example.yaml.
"""
from pathlib import Path

import yaml
from werkzeug.datastructures import MultiDict

from .connection import read_connection, save_connection
from .forms import KEYWORD_FIELDS, normalize_url, read_project, validate_project
from .storage import DEFAULT_SETTINGS, connect, project_settings, save_project, save_project_settings

SECTIONS = {"settings", "projects"}
PROJECT_KEYS = {"name", "url", "region", "region_name", "interval_seconds", "brand", "keywords"}
KEYWORD_KEYS = set(KEYWORD_FIELDS)
DECLINED = {
    "conversions": "Конверсии (корзина, формы, регистрация со случайными данными) не поддерживаются: "
    "агент не заполняет формы фиктивными данными. См. docs/issue12-spec.md.",
}


def scalar(value):
    return "" if value is None else str(value).strip()


def check_keys(data, allowed, where):
    if not isinstance(data, dict):
        raise ValueError(f"{where}: ожидается словарь «ключ: значение».")
    for key in data:
        if key in DECLINED:
            raise ValueError(f"{where}: {DECLINED[key]}")
        if key not in allowed:
            raise ValueError(f"{where}: неизвестный ключ «{key}». Допустимо: {', '.join(sorted(allowed))}.")


def project_form(item, where):
    check_keys(item, PROJECT_KEYS, where)
    keywords = item.get("keywords") or []
    if not isinstance(keywords, list):
        raise ValueError(f"{where}: keywords — список фраз.")
    form = MultiDict({
        "name": scalar(item.get("name")), "url": scalar(item.get("url")),
        "region": scalar(item.get("region", "1")), "region_name": scalar(item.get("region_name")),
        "interval_seconds": scalar(item.get("interval_seconds", 10)), "spent": "",
    })
    for index, word in enumerate(keywords, start=1):
        if isinstance(word, str):
            word = {"phrase": word}
        check_keys(word, KEYWORD_KEYS, f"{where}, фраза {index}")
        word = {"hint_type": "domain", **word}
        for field in KEYWORD_FIELDS:
            form.add(field, scalar(word.get(field)))
    return form


def read_config(text):
    """Validated settings and projects; raises ValueError naming the wrong place in the file."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Файл конфигурации не является корректным YAML: {exc}") from None
    check_keys(data, SECTIONS, "Конфигурация")
    settings = data.get("settings")
    connection = None
    if settings is not None:
        check_keys(settings, {"yandex_api_key", "yandex_folder_id", "position_source", "novnc_url"}, "settings")
        connection = read_connection({key: scalar(value) for key, value in settings.items()})
    projects = []
    items = data.get("projects") or []
    if not isinstance(items, list):
        raise ValueError("projects: ожидается список проектов.")
    for number, item in enumerate(items, start=1):
        where = f"Проект {number}"
        try:
            project, keywords = read_project(project_form(item, where))
            brand = scalar(item.get("brand"))
            if len(brand) > 100:
                raise ValueError("Бренд — до 100 символов.")
            values, words, attributes = validate_project(project, keywords, default_brand=brand)
        except ValueError as exc:
            message = str(exc)
            raise ValueError(message if message.startswith(where) else f"{where}: {message}") from None
        projects.append((values, words, attributes, brand))
    return connection, projects


def import_config(path, config_path):
    """Apply the file to the panel database in one transaction; returns a short summary."""
    connection, projects = read_config(Path(config_path).read_text(encoding="utf-8-sig"))
    created = updated = 0
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if connection is not None:
            save_connection(db, connection)
        for values, words, attributes, brand in projects:
            url = normalize_url(values[1])
            row = db.execute("SELECT id FROM projects WHERE url = ? ORDER BY id LIMIT 1", (url,)).fetchone()
            project_id = save_project(db, row["id"] if row else None, values, words, attributes=attributes)
            # A new project gets the defaults of the project form; an existing one keeps its settings.
            settings = project_settings(db, project_id) if row else dict(DEFAULT_SETTINGS)
            if brand:
                brands = [line for line in settings["brand_queries"].splitlines() if line and line != brand]
                settings["brand_queries"] = "\n".join([brand, *brands][:20])
            save_project_settings(db, project_id, settings)
            if row:
                updated += 1
            else:
                created += 1
    return f"Конфигурация загружена: проектов создано {created}, обновлено {updated}."
