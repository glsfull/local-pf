"""Panel connection settings: Yandex Search API credentials, position source, the noVNC address
and the DeepSeek key and model used to collect the semantic core.

Shared by the «Настройки» page and by the YAML configuration import (``py -m dashboard --config``).
"""
import re
from urllib.parse import urlsplit

from . import deepseek
from .storage import save_panel_setting

POSITION_SOURCES = {"api": "Yandex Search API (отложенные запросы)", "browser": "Браузерный парсер выдачи"}
FOLDER_ID = re.compile(r"[a-z0-9]{1,50}")


def local_url(value):
    """http(s) URL with a host, for example http://localhost:56565/ — the noVNC address of the agent."""
    if len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f\\]", value):
        return False
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def text(form, name, default=""):
    value = form.get(name, default)
    return default if value is None else str(value).strip()


def read_connection(form):
    """Validated connection settings from a form or a mapping; raises ValueError with a message for the user."""
    connection = {
        "yandex_api_key": text(form, "yandex_api_key"),
        "yandex_folder_id": text(form, "yandex_folder_id"),
        "position_source": text(form, "position_source", "api") or "api",
        "novnc_url": text(form, "novnc_url"),
        "deepseek_api_key": deepseek.read_api_key(text(form, "deepseek_api_key")),
        "deepseek_model": text(form, "deepseek_model"),
    }
    if connection["deepseek_model"]:
        connection["deepseek_model"] = deepseek.read_model(connection["deepseek_model"])
    api_key = connection["yandex_api_key"]
    if api_key and (len(api_key) > 200 or any(char.isspace() for char in api_key)):
        raise ValueError("YANDEX_API_KEY: ключ без пробелов, до 200 символов.")
    if connection["yandex_folder_id"] and not FOLDER_ID.fullmatch(connection["yandex_folder_id"]):
        raise ValueError("YANDEX_FOLDER_ID: латинские буквы и цифры, до 50 символов.")
    if connection["position_source"] not in POSITION_SOURCES:
        raise ValueError("Выберите источник позиций.")
    if connection["novnc_url"] and not local_url(connection["novnc_url"]):
        raise ValueError("Адрес noVNC должен начинаться с http:// или https://.")
    return connection


def save_connection(db, connection):
    """An empty key keeps the saved one: the key is never sent back to the browser."""
    if connection["yandex_api_key"]:
        save_panel_setting(db, "yandex_api_key", connection["yandex_api_key"])
    if connection["deepseek_api_key"]:
        save_panel_setting(db, "deepseek_api_key", connection["deepseek_api_key"])
    for name in ("yandex_folder_id", "position_source", "novnc_url", "deepseek_model"):
        save_panel_setting(db, name, connection[name])
