"""Yandex profiles and proxies for the browser agent: one search phrase = one profile + one proxy.

The operator adds profiles and proxies in «Настройки панели» one by one or in bulk (a list or files).
Before every phrase the agent takes the least recently used profile and the least recently used proxy;
the main query and the refined query of the phrase share them, the next phrase gets the next pair.

A profile is a persistent Chromium user data directory (``<db dir>/profiles/<id>``) with an optional
set of cookies imported on its first use (JSON export of Cookie-Editor / EditThisCookie). The panel
never shows proxy passwords or cookie values back: only masked values are rendered.
"""
import ipaddress
import json
import re
import shutil
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from modules.clock import timestamp

PROXY_SCHEMES = ("http", "https", "socks4", "socks5")
MAX_PROFILES = 5000
MAX_PROXIES = 5000
MAX_COOKIES = 1000
MAX_NAME = 100
MAX_LINE = 200_000
HOSTNAME = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", re.I)
SAME_SITE = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS yandex_profiles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    cookies TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    uses INTEGER NOT NULL DEFAULT 0,
    turn INTEGER
);
CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    uses INTEGER NOT NULL DEFAULT 0,
    turn INTEGER
);
"""


class Proxy:
    """A validated proxy address: scheme://[user:password@]host:port."""

    def __init__(self, scheme, host, port, username="", password=""):
        self.scheme, self.host, self.port = scheme, host, port
        self.username, self.password = username, password

    @property
    def authenticated(self):
        return bool(self.username)

    @property
    def netloc(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def server(self):
        """Value for Chromium --proxy-server: credentials are not supported there."""
        return f"{self.scheme}://{self.netloc}"

    @property
    def url(self):
        if not self.authenticated:
            return self.server
        return f"{self.scheme}://{quote(self.username, safe='')}:{quote(self.password, safe='')}@{self.netloc}"

    @property
    def masked(self):
        if not self.authenticated:
            return self.server
        return f"{self.scheme}://{self.username}:••••@{self.netloc}"

    def __repr__(self):
        return f"Proxy({self.masked})"


def parse_host(value):
    host = value.strip("[]")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    host = host.rstrip(".")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("некорректный адрес хоста") from None
    if not HOSTNAME.fullmatch(host):
        raise ValueError("некорректный адрес хоста")
    return host


def parse_port(value):
    if not re.fullmatch(r"[0-9]{1,5}", value or "") or not 1 <= int(value) <= 65535:
        raise ValueError("порт — число от 1 до 65535")
    return int(value)


def parse_proxy(line):
    """Proxy from one line in a common format; raises ValueError with the reason.

    Accepted: scheme://user:pass@host:port, scheme://host:port, user:pass@host:port, host:port,
    host:port:user:pass. Without a scheme the proxy is HTTP.
    """
    line = line.strip()
    if not line or len(line) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", line):
        raise ValueError("пустая строка или пробелы внутри адреса")
    if "://" in line:
        parts = urlsplit(line)
        scheme = parts.scheme.lower()
        if scheme == "socks5h":
            scheme = "socks5"
        if scheme not in PROXY_SCHEMES:
            raise ValueError("схема прокси — http, https, socks4 или socks5")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("у адреса прокси не должно быть пути и параметров")
        try:
            port = parts.port
        except ValueError:
            raise ValueError("порт — число от 1 до 65535") from None
        if port is None or not parts.hostname:
            raise ValueError("укажите хост и порт")
        host = parse_host(parts.hostname)
        username, password = unquote(parts.username or ""), unquote(parts.password or "")
    else:
        scheme, credentials = "http", ""
        if "@" in line:
            credentials, line = line.rsplit("@", 1)
        fields = line.split(":")
        if line.startswith("["):
            address, _, rest = line[1:].partition("]")
            fields = [address, *rest.lstrip(":").split(":")]
        if len(fields) == 4 and not credentials:
            host, port, username, password = fields
        elif len(fields) == 2:
            host, port = fields
            username, _, password = credentials.partition(":")
        else:
            raise ValueError("формат — host:port, host:port:user:pass или scheme://user:pass@host:port")
        host, port = parse_host(host), parse_port(port)
    if password and not username:
        raise ValueError("пароль без логина")
    if len(username) > 255 or len(password) > 255:
        raise ValueError("логин и пароль — до 255 символов")
    if scheme == "socks4" and username:
        raise ValueError("SOCKS4 не поддерживает логин и пароль")
    return Proxy(scheme, host, port, username, password)


def normalize_cookie(item, index):
    if not isinstance(item, dict):
        raise ValueError(f"cookie {index} — ожидается объект")
    name, value, domain = item.get("name"), item.get("value"), item.get("domain")
    if not isinstance(name, str) or not name or not isinstance(value, str):
        raise ValueError(f"cookie {index} — нужны строки name и value")
    if not isinstance(domain, str) or not domain.strip("."):
        raise ValueError(f"cookie {index} — нужен domain")
    cookie = {"name": name, "value": value, "domain": domain, "path": item.get("path") or "/"}
    for key in ("secure", "httpOnly"):
        if isinstance(item.get(key), bool):
            cookie[key] = item[key]
    expires = item.get("expirationDate", item.get("expires"))
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
        cookie["expires"] = float(expires)
    same_site = SAME_SITE.get(str(item.get("sameSite", "")).lower())
    if same_site:
        cookie["sameSite"] = same_site
    return cookie


def parse_cookies(text):
    """Cookies of a profile: a JSON array exported by Cookie-Editor or EditThisCookie (may be empty)."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("cookies — JSON-массив (экспорт Cookie-Editor)") from None
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if not isinstance(data, list):
        raise ValueError("cookies — JSON-массив (экспорт Cookie-Editor)")
    if len(data) > MAX_COOKIES:
        raise ValueError(f"не больше {MAX_COOKIES} cookies в профиле")
    return [normalize_cookie(item, index) for index, item in enumerate(data, start=1)]


def parse_profile_name(name):
    name = (name or "").strip()
    if not 1 <= len(name) <= MAX_NAME or re.search(r"[\x00-\x1f\x7f]", name):
        raise ValueError(f"название профиля — от 1 до {MAX_NAME} символов")
    return name


def parse_profile_line(line):
    """«название» or «название;[cookies JSON]» (a tab also separates the name from the cookies)."""
    match = re.fullmatch(r"([^\t;]*)[\t;]\s*(\[.*\]|\{.*\})", line.strip(), re.S)
    name, cookies = (match[1], match[2]) if match else (line, "")
    return parse_profile_name(name), parse_cookies(cookies)


def bulk_lines(text):
    """Non-empty lines without comments («#»)."""
    for number, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip().lstrip("﻿")
        if line and not line.startswith("#"):
            if len(line) > MAX_LINE:
                raise ValueError(f"Строка {number}: слишком длинная строка.")
            yield number, line


def read_bulk(text, parse, source="Строка"):
    """Parse every line; one wrong line rejects the whole list with its number and reason."""
    items = []
    for number, line in bulk_lines(text):
        try:
            items.append(parse(line))
        except ValueError as exc:
            raise ValueError(f"{source} {number}: {exc}.") from None
    return items


def initialize(db):
    db.executescript(SCHEMA)


def profile_count(db):
    return db.execute("SELECT COUNT(*) FROM yandex_profiles").fetchone()[0]


def proxy_count(db):
    return db.execute("SELECT COUNT(*) FROM proxies").fetchone()[0]


def add_profiles(db, profiles):
    """Insert (name, cookies) pairs; returns (added, skipped duplicates)."""
    added = skipped = 0
    if profile_count(db) + len(profiles) > MAX_PROFILES:
        raise ValueError(f"В панели может быть не больше {MAX_PROFILES} профилей.")
    for name, cookies in profiles:
        cursor = db.execute(
            "INSERT OR IGNORE INTO yandex_profiles(name, cookies, created_at) VALUES (?, ?, ?)",
            (name, json.dumps(cookies, ensure_ascii=False), timestamp()),
        )
        added += cursor.rowcount
        skipped += 1 - cursor.rowcount
    return added, skipped


def add_proxies(db, proxies):
    """Insert proxies; returns (added, skipped duplicates)."""
    added = skipped = 0
    if proxy_count(db) + len(proxies) > MAX_PROXIES:
        raise ValueError(f"В панели может быть не больше {MAX_PROXIES} прокси.")
    for proxy in proxies:
        cursor = db.execute(
            "INSERT OR IGNORE INTO proxies(url, created_at) VALUES (?, ?)", (proxy.url, timestamp()),
        )
        added += cursor.rowcount
        skipped += 1 - cursor.rowcount
    return added, skipped


def list_profiles(db):
    return [
        {"id": row["id"], "name": row["name"], "cookies": len(json.loads(row["cookies"] or "[]")),
         "uses": row["uses"], "last_used_at": row["last_used_at"]}
        for row in db.execute("SELECT * FROM yandex_profiles ORDER BY id")
    ]


def list_proxies(db):
    return [
        {"id": row["id"], "masked": parse_proxy(row["url"]).masked, "uses": row["uses"],
         "last_used_at": row["last_used_at"]}
        for row in db.execute("SELECT * FROM proxies ORDER BY id")
    ]


def take_least_used(db, table):
    """Never used entries first (in the order they were added), then the one used longest ago."""
    row = db.execute(f"SELECT * FROM {table} ORDER BY turn IS NOT NULL, turn, id LIMIT 1").fetchone()
    if row is not None:
        db.execute(
            f"""UPDATE {table} SET uses = uses + 1, last_used_at = ?,
                turn = (SELECT COALESCE(MAX(turn), 0) + 1 FROM {table}) WHERE id = ?""",
            (timestamp(), row["id"]),
        )
    return row


def take_identity(db, profiles_root):
    """The next (profile, proxy) pair for one search phrase; either part is None when none is configured."""
    db.execute("BEGIN IMMEDIATE")
    profile = take_least_used(db, "yandex_profiles")
    proxy = take_least_used(db, "proxies")
    return Identity(
        profile_id=profile["id"] if profile else None,
        profile_name=profile["name"] if profile else None,
        profile_dir=Path(profiles_root) / str(profile["id"]) if profile else None,
        cookies=json.loads(profile["cookies"] or "[]") if profile else [],
        proxy=parse_proxy(proxy["url"]) if proxy else None,
    )


class Identity:
    """Profile and proxy used for one search phrase."""

    def __init__(self, *, profile_id=None, profile_name=None, profile_dir=None, cookies=(), proxy=None):
        self.profile_id = profile_id
        self.profile_name = profile_name
        self.profile_dir = profile_dir
        self.cookies = list(cookies)
        self.proxy = proxy

    def describe(self):
        profile = f"профиль «{self.profile_name}»" if self.profile_name else "временный профиль"
        proxy = f"прокси {self.proxy.masked}" if self.proxy else "без прокси"
        return f"{profile}, {proxy}"

    def journal_details(self):
        return {"profile": self.profile_name or "временный", "proxy": self.proxy.masked if self.proxy else None}


def profiles_root(path):
    """Persistent Chromium profiles live next to the panel database: <db dir>/profiles/<id>."""
    return Path(path).resolve().parent / "profiles"


def remove_profile_dir(root, profile_id):
    """Delete the Chromium data of a removed profile (best effort: a running browser may hold files)."""
    shutil.rmtree(Path(root) / str(int(profile_id)), ignore_errors=True)
