from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

import yaml


@dataclass(frozen=True)
class TargetPage:
    label: str
    url: str = ""
    text: str = ""
    selector: str = ""
    expected_selector: str = "h1"


@dataclass(frozen=True)
class Config:
    keyword: str
    domain: str
    pages: tuple[TargetPage, ...]
    search_input: str
    search_button: str
    result_link: str
    captcha: str
    timeout: float
    observation_delay: float = 0.7
    captcha_attempts: int = 3
    captcha_timeout: float = 300
    captcha_poll: float = 2


def read_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: требуется непустая строка")
    return value.strip()


def read_number(value, field, low, high, *, integer=False, allow_low=True):
    kinds = (int,) if integer else (float, int)
    if (
        type(value) not in kinds
        or not isfinite(value)
        or value > high
        or value < low
        or (not allow_low and value == low)
    ):
        bracket = "[" if allow_low else "("
        kind = "целое число" if integer else "число"
        raise ValueError(f"{field}: требуется {kind} в диапазоне {bracket}{low:g}, {high:g}]")
    return value


def captcha_settings(values, names=None):
    """Read SmartCaptcha limits from YAML values or environment strings."""
    names = names or {
        "attempts": "CAPTCHA_SOLVE_ATTEMPTS",
        "timeout": "CAPTCHA_MANUAL_TIMEOUT_SECONDS",
        "poll": "CAPTCHA_POLL_SECONDS",
    }
    defaults = {"attempts": 3, "timeout": 300, "poll": 2}
    result = {}
    for key, name in names.items():
        value = values.get(name, defaults[key])
        if isinstance(value, str):
            try:
                value = int(value) if key == "attempts" else float(value)
            except ValueError:
                pass
        limits = {"attempts": (0, 10), "timeout": (0, 3600), "poll": (0, 60)}[key]
        result[key] = read_number(
            value, name, *limits, integer=key == "attempts", allow_low=key != "poll",
        )
    return result


def read_page(value):
    if isinstance(value, str):
        value = {"url" if value.startswith("/") else "text": value}
    if not isinstance(value, dict):
        raise ValueError("TARGET_PAGES: ожидается путь, текст или объект")
    fields = ("url", "text", "selector")
    unknown = value.keys() - {*fields, "expected_selector"}
    if unknown:
        raise ValueError(f"TARGET_PAGES: неизвестные поля {sorted(map(str, unknown))}")
    matches = [field for field in fields if field in value]
    if len(matches) != 1:
        raise ValueError("TARGET_PAGES: задайте одно из полей url, text, selector")
    field = matches[0]
    label = read_text(value[field], f"TARGET_PAGES.{field}")
    if field == "url" and (
        not label.startswith("/") or label.startswith("//")
        or urlsplit(label).netloc or "\\" in label
    ):
        raise ValueError("TARGET_PAGES.url: требуется путь внутри сайта")
    expected = read_text(value.get("expected_selector", "h1"), "expected_selector")
    return TargetPage(label, **{field: label}, expected_selector=expected)


def read_config(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Конфигурация должна быть объектом YAML")
    domain = read_text(data.get("TARGET_DOMAIN"), "TARGET_DOMAIN")
    try:
        domain = domain.rstrip(".").encode("idna").decode("ascii").lower()
        labels = domain.split(".")
        if any(
            not label or label.startswith("-") or label.endswith("-")
            or any(not (char.isalnum() or char == "-") for char in label)
            for label in labels
        ):
            raise ValueError
    except (UnicodeError, ValueError):
        raise ValueError("TARGET_DOMAIN: требуется имя хоста без схемы и пути") from None
    pages = data.get("TARGET_PAGES")
    if not isinstance(pages, list) or not pages:
        raise ValueError("TARGET_PAGES: требуется непустой список")
    timeout = data.get("TIMEOUT_SECONDS", 15)
    if (
        type(timeout) not in (float, int)
        or not isfinite(timeout)
        or not 0 < timeout <= 120
    ):
        raise ValueError("TIMEOUT_SECONDS: требуется число в диапазоне (0, 120]")
    delay = data.get("OBSERVATION_DELAY_SECONDS", 0.7)
    if type(delay) not in (float, int) or not 0 <= delay <= 5:
        raise ValueError("OBSERVATION_DELAY_SECONDS: требуется число от 0 до 5")
    captcha = captcha_settings(data)
    return Config(
        keyword=read_text(data.get("SEARCH_KEYWORD"), "SEARCH_KEYWORD"),
        domain=domain,
        pages=tuple(read_page(page) for page in pages),
        search_input=read_text(data.get("SEARCH_INPUT_SELECTOR", "#text"), "SEARCH_INPUT_SELECTOR"),
        search_button=read_text(
            data.get("SEARCH_BUTTON_SELECTOR", "button[type='submit']"), "SEARCH_BUTTON_SELECTOR"
        ),
        result_link=read_text(
            data.get("RESULT_LINK_SELECTOR", "a.OrganicTitle-Link"), "RESULT_LINK_SELECTOR"
        ),
        captcha=read_text(
            data.get("CAPTCHA_SELECTOR", "[data-sitekey], input[name='smart-token'], #checkbox-captcha-form"),
            "CAPTCHA_SELECTOR",
        ),
        timeout=timeout,
        observation_delay=delay,
        captcha_attempts=captcha["attempts"],
        captcha_timeout=captcha["timeout"],
        captcha_poll=captcha["poll"],
    )
