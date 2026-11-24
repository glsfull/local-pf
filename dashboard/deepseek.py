"""Semantic core collection with DeepSeek (issue #14).

DeepSeek has an OpenAI-compatible API (https://api-docs.deepseek.com/): ``POST /chat/completions``
with ``Authorization: Bearer <DEEPSEEK_API_KEY>``. JSON output (``response_format: json_object``)
requires the word "json" in the prompt and an example of the expected structure; the answer may
come back blank, so a blank answer is retried once.

The panel ships a ready prompt (``SYSTEM_PROMPT`` + ``user_prompt``): the user enters the site, the
seed phrases or topic, the region and the number of phrases, and gets phrases grouped into clusters
with a search intent. Selected phrases are appended to the project keywords.
"""
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENDPOINT = "https://api.deepseek.com/chat/completions"
MODELS = ("deepseek-flash", "deepseek-v4-pro")
DEFAULT_MODEL = MODELS[0]
MODEL_NAME = re.compile(r"[a-z0-9][a-z0-9.\-]{0,63}")
MIN_COUNT, MAX_COUNT, DEFAULT_COUNT = 10, 200, 50
MAX_SEEDS = 2000
MAX_EXTRA = 1000
MAX_PHRASE = 200
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT = 180
INTENTS = {
    "commercial": "Коммерческий", "informational": "Информационный",
    "navigational": "Навигационный", "local": "Локальный",
}
log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — SEO-специалист по Яндексу, собираешь семантическое ядро для продвижения сайта в Яндексе.
Правила:
1. Только реальные поисковые запросы на русском языке, как их вводят люди в Яндекс: 2–7 слов, без кавычек, операторов и спецсимволов, в нижнем регистре.
2. Только запросы, по которым продвигается указанный сайт: его товары, услуги и регион. Без запросов конкурентов и чужих брендов.
3. Без дублей, перестановок слов и запросов, отличающихся только окончанием.
4. Смешивай высокочастотные, среднечастотные и низкочастотные запросы; добавляй коммерческие хвосты (купить, цена, заказать, недорого, под ключ), если они уместны.
5. Каждый запрос отнеси к кластеру — короткому названию группы запросов с одним интентом, которые можно продвигать одной страницей.
6. Для каждого запроса укажи интент: commercial, informational, navigational или local.
Ответ — только json-объект такой структуры, без пояснений:
{"keywords": [{"phrase": "натяжные потолки цена за м2", "cluster": "Цены на натяжные потолки", "intent": "commercial"}]}"""


class DeepSeekError(Exception):
    """A user-facing error of the DeepSeek request."""


def user_prompt(site, seeds, region, count, extra=""):
    """The user message of the built-in prompt."""
    lines = [
        f"Сайт: {site}",
        f"Регион продвижения: {region}",
        "Базовые фразы и тематика:",
        seeds.strip(),
        f"Собери {count} поисковых запросов для семантического ядра этого сайта и верни json.",
    ]
    if extra.strip():
        lines.append(f"Дополнительные требования: {extra.strip()}")
    return "\n".join(lines)


def read_request(form):
    """Validated fields of the «Сбор ядра» form; raises ValueError with a message for the user."""
    seeds = (form.get("seeds") or "").strip()
    extra = (form.get("extra") or "").strip()
    count = (form.get("count") or str(DEFAULT_COUNT)).strip()
    if not seeds:
        raise ValueError("Введите базовые фразы или тематику сайта.")
    if len(seeds) > MAX_SEEDS:
        raise ValueError(f"Базовые фразы — до {MAX_SEEDS} символов.")
    if len(extra) > MAX_EXTRA:
        raise ValueError(f"Дополнительные требования — до {MAX_EXTRA} символов.")
    if not count.isdigit() or not MIN_COUNT <= int(count) <= MAX_COUNT:
        raise ValueError(f"Количество фраз — число от {MIN_COUNT} до {MAX_COUNT}.")
    return {"seeds": seeds, "extra": extra, "count": int(count)}


def read_model(value):
    model = (value or "").strip() or DEFAULT_MODEL
    if not MODEL_NAME.fullmatch(model):
        raise ValueError("Модель DeepSeek — латинские буквы, цифры, точка и дефис, например deepseek-flash.")
    return model


def read_api_key(value):
    key = (value or "").strip()
    if key and (len(key) > 200 or any(char.isspace() for char in key)):
        raise ValueError("DEEPSEEK_API_KEY: ключ без пробелов, до 200 символов.")
    return key


def clean_phrase(value):
    phrase = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'«»").strip()
    return phrase.lower()[:MAX_PHRASE]


def parse_keywords(content):
    """[{"phrase", "cluster", "intent"}] from the model answer; duplicates and blanks are dropped."""
    text = (content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except ValueError:
        raise DeepSeekError("DeepSeek вернул ответ не в формате JSON. Повторите сбор.") from None
    items = data.get("keywords") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise DeepSeekError("В ответе DeepSeek нет списка keywords. Повторите сбор.")
    keywords, seen = [], set()
    for item in items:
        if isinstance(item, str):
            item = {"phrase": item}
        if not isinstance(item, dict):
            continue
        phrase = clean_phrase(item.get("phrase"))
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        intent = str(item.get("intent") or "").strip().lower()
        keywords.append({
            "phrase": phrase,
            "cluster": re.sub(r"\s+", " ", str(item.get("cluster") or "")).strip()[:100],
            "intent": intent if intent in INTENTS else "",
        })
    if not keywords:
        raise DeepSeekError("DeepSeek не вернул ни одной фразы. Уточните базовые фразы и повторите сбор.")
    return keywords


class DeepSeekClient:
    """Chat completion with the built-in prompt; ``opener`` is replaceable in tests."""

    def __init__(self, api_key, model=DEFAULT_MODEL, *, opener=None, endpoint=ENDPOINT):
        if not api_key:
            raise DeepSeekError("Укажите DEEPSEEK_API_KEY в настройках панели.")
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.opener = opener or (lambda request, timeout: urlopen(request, timeout=timeout))

    def body(self, prompt, count):
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            # About 40 tokens per phrase with its cluster; the cap keeps the JSON from being cut off.
            "max_tokens": min(8192, 600 + count * 45),
            "temperature": 1.0,
            "stream": False,
        }

    def complete(self, prompt, count):
        request = Request(
            self.endpoint, data=json.dumps(self.body(prompt, count), ensure_ascii=False).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise DeepSeekError(http_message(error)) from None
        except (URLError, TimeoutError, OSError) as error:
            raise DeepSeekError(f"DeepSeek недоступен: {getattr(error, 'reason', error)}.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DeepSeekError("Ответ DeepSeek слишком большой.")
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            return choice["message"].get("content") or "", choice.get("finish_reason"), data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise DeepSeekError("DeepSeek вернул ответ неизвестного формата.") from None

    def collect(self, site, seeds, region, count, extra=""):
        """(keywords, usage, prompt): a blank answer is retried once, as DeepSeek recommends for JSON output."""
        prompt = user_prompt(site, seeds, region, count, extra)
        for attempt in (1, 2):
            content, finish, usage = self.complete(prompt, count)
            if content.strip():
                break
            log.info("DeepSeek вернул пустой ответ, попытка %s", attempt)
        if finish == "length" and not content.strip().endswith("}"):
            raise DeepSeekError("Ответ DeepSeek обрезан по длине. Уменьшите количество фраз.")
        return parse_keywords(content), usage, prompt


def http_message(error):
    detail = ""
    try:
        detail = json.loads(error.read(65536)).get("error", {}).get("message", "")
    except (ValueError, AttributeError, OSError):
        pass
    known = {
        400: "некорректный запрос", 401: "неверный DEEPSEEK_API_KEY", 402: "на балансе DeepSeek недостаточно средств",
        422: "некорректные параметры запроса", 429: "слишком много запросов, повторите позже",
        500: "ошибка сервера DeepSeek", 503: "сервер DeepSeek перегружен, повторите позже",
    }
    message = f"DeepSeek: {known.get(error.code, f'ошибка HTTP {error.code}')}"
    return message + (f" ({detail[:200]})." if detail else ".")
