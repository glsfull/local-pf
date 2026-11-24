from contextlib import contextmanager
import sqlite3

from filelock import FileLock, Timeout

from dashboard import identities
from modules.clock import timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    region TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS keywords (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    ordinal INTEGER NOT NULL,
    phrase TEXT NOT NULL,
    landing_url TEXT NOT NULL,
    hint_type TEXT NOT NULL,
    brand TEXT NOT NULL,
    PRIMARY KEY (project_id, ordinal)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    project_url TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    region TEXT NOT NULL,
    source TEXT NOT NULL,
    state TEXT NOT NULL,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS active_run ON runs((1)) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS project_runs ON runs(project_id, id DESC);
CREATE TABLE IF NOT EXISTS measurements (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ordinal INTEGER NOT NULL,
    phrase TEXT NOT NULL,
    landing_url TEXT NOT NULL,
    hint_query TEXT NOT NULL,
    rank INTEGER,
    hint_rank INTEGER,
    checked_at TEXT,
    hinted_at TEXT,
    found_url TEXT,
    hint_found_url TEXT,
    error TEXT,
    hint_error TEXT,
    iw INTEGER,
    PRIMARY KEY (run_id, ordinal)
);
CREATE TABLE IF NOT EXISTS run_reports (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id),
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_attributes (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id),
    region_name TEXT NOT NULL,
    spent REAL
);
CREATE TABLE IF NOT EXISTS keyword_attributes (
    project_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    cluster TEXT NOT NULL,
    iw INTEGER,
    demand INTEGER,
    chance REAL,
    power REAL,
    visits INTEGER,
    competition TEXT NOT NULL,
    budget REAL,
    PRIMARY KEY (project_id, ordinal),
    FOREIGN KEY (project_id, ordinal) REFERENCES keywords(project_id, ordinal) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cluster_attributes (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    commercial INTEGER,
    has_meta INTEGER,
    has_text INTEGER,
    PRIMARY KEY (project_id, name)
);
CREATE TABLE IF NOT EXISTS project_settings (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id),
    query_region TEXT NOT NULL DEFAULT 'cities',
    frequency_type TEXT NOT NULL DEFAULT 'exact',
    theme TEXT NOT NULL DEFAULT '',
    schedule_mode TEXT NOT NULL DEFAULT 'uniform_day',
    schedule TEXT NOT NULL DEFAULT '',
    not_found TEXT NOT NULL DEFAULT 'domain',
    brand_queries TEXT NOT NULL DEFAULT '',
    limit_budget INTEGER NOT NULL DEFAULT 0,
    daily_budget REAL,
    collect_daily INTEGER NOT NULL DEFAULT 1,
    collect_time TEXT NOT NULL DEFAULT '21:00',
    rare_lowfreq INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS visit_stats (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    phrase TEXT NOT NULL,
    day TEXT NOT NULL,
    done INTEGER NOT NULL,
    planned INTEGER NOT NULL,
    cost REAL NOT NULL,
    depth REAL,
    PRIMARY KEY (project_id, phrase, day)
);
CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    ordinal INTEGER,
    state TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    resume INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    report_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS active_agent ON agent_sessions((1)) WHERE state = 'running';
CREATE TABLE IF NOT EXISTS panel_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""
SCHEDULE_SLOTS = 7 * 24
DEFAULT_SETTINGS = {
    "query_region": "cities", "frequency_type": "exact", "theme": "", "schedule_mode": "uniform_day",
    "schedule": "1" * SCHEDULE_SLOTS, "not_found": "domain", "brand_queries": "", "limit_budget": 0,
    "daily_budget": None, "collect_daily": 1, "collect_time": "21:00", "rare_lowfreq": 0, "paused": 0,
}


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def run_lock(path):
    return FileLock(f"{path}.run.lock", timeout=0)


def agent_lock(path):
    """Held by agent.py while it drives Chromium for an agent session."""
    return FileLock(f"{path}.agent.lock", timeout=0)


def initialize(path):
    with connect(path) as db:
        db.executescript(SCHEMA)
        identities.initialize(db)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(measurements)")}
        for column in ("found_url", "hint_found_url", "error", "hint_error"):
            if column not in columns:
                db.execute(f"ALTER TABLE measurements ADD COLUMN {column} TEXT")
        if "iw" not in columns:
            db.execute("ALTER TABLE measurements ADD COLUMN iw INTEGER")
    try:
        with run_lock(path), connect(path) as db:
            db.execute(
                "UPDATE runs SET state = 'interrupted', finished_at = ? WHERE state = 'running'",
                (timestamp(),),
            )
    except Timeout:
        # A browser agent can still be working after the dashboard process stops.
        pass
    try:
        with agent_lock(path), connect(path) as db:
            db.execute(
                """UPDATE agent_sessions SET state = 'interrupted', status = 'ERROR',
                   message = 'Панель была перезапущена во время работы агента.', finished_at = ?
                   WHERE state = 'running'""",
                (timestamp(),),
            )
    except Timeout:
        pass


def project_keywords(db, project_id):
    return db.execute(
        """SELECT k.*, a.cluster, a.iw, a.demand, a.chance, a.power, a.visits, a.competition, a.budget
           FROM keywords k LEFT JOIN keyword_attributes a USING (project_id, ordinal)
           WHERE k.project_id = ? ORDER BY k.ordinal""",
        (project_id,),
    ).fetchall()


def save_project(db, project_id, project, keywords, *, attributes=None):
    if project_id is None:
        project_id = db.execute(
            "INSERT INTO projects(name, url, region, interval_seconds) VALUES (?, ?, ?, ?)",
            project,
        ).lastrowid
    else:
        db.execute(
            "UPDATE projects SET name = ?, url = ?, region = ?, interval_seconds = ? WHERE id = ?",
            (*project, project_id),
        )
        db.execute("DELETE FROM keywords WHERE project_id = ?", (project_id,))
    db.executemany(
        "INSERT INTO keywords VALUES (?, ?, ?, ?, ?, ?)",
        [(project_id, index, *keyword[:4]) for index, keyword in enumerate(keywords)],
    )
    db.executemany(
        "INSERT INTO keyword_attributes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(project_id, index, *keyword[4]) for index, keyword in enumerate(keywords) if len(keyword) == 5],
    )
    if attributes is not None:
        db.execute(
            """INSERT INTO project_attributes VALUES (?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET region_name=excluded.region_name, spent=excluded.spent""",
            (project_id, *attributes),
        )
    db.execute(
        """DELETE FROM cluster_attributes WHERE project_id = ? AND NOT EXISTS (
               SELECT 1 FROM keyword_attributes a WHERE a.project_id = ? AND a.cluster = cluster_attributes.name
           )""",
        (project_id, project_id),
    )
    return project_id


def project_settings(db, project_id):
    row = db.execute("SELECT * FROM project_settings WHERE project_id = ?", (project_id,)).fetchone()
    settings = dict(DEFAULT_SETTINGS)
    if row is None:
        # Projects created before the settings table existed never asked for automatic collection.
        settings["collect_daily"] = 0
    else:
        settings.update({key: row[key] for key in DEFAULT_SETTINGS})
    if len(settings["schedule"]) != SCHEDULE_SLOTS:
        settings["schedule"] = DEFAULT_SETTINGS["schedule"]
    return settings


def save_project_settings(db, project_id, settings):
    columns = tuple(DEFAULT_SETTINGS)
    values = {**project_settings(db, project_id), **settings}
    db.execute(
        f"""INSERT INTO project_settings(project_id, {', '.join(columns)})
            VALUES (?, {', '.join('?' for _ in columns)})
            ON CONFLICT(project_id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in columns)}""",
        (project_id, *(values[column] for column in columns)),
    )


def panel_setting(db, key, default=None):
    row = db.execute("SELECT value FROM panel_settings WHERE key = ?", (key,)).fetchone()
    return default if row is None or row["value"] is None else row["value"]


def save_panel_setting(db, key, value):
    db.execute(
        "INSERT INTO panel_settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def start_run(db, project, keywords, source, domain, *, region=None):
    run_id = db.execute(
        """INSERT INTO runs(project_id, project_url, interval_seconds, region, source, state, started_at)
        VALUES (?, ?, ?, ?, ?, 'running', ?)""",
        (project["id"], project["url"], project["interval_seconds"],
         project["region"] if region is None else region, source, timestamp()),
    ).lastrowid
    frequencies = dict(db.execute(
        "SELECT ordinal, iw FROM keyword_attributes WHERE project_id = ?", (project["id"],),
    ))
    for word in keywords:
        hint = word["brand"] if word["hint_type"] == "brand" else domain
        db.execute(
            """INSERT INTO measurements(run_id, ordinal, phrase, landing_url, hint_query, iw)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, word["ordinal"], word["phrase"], word["landing_url"], f"{word['phrase']} {hint}",
             frequencies.get(word["ordinal"])),
        )
    return run_id


def finish_run(db, run_id, state, error=None):
    db.execute(
        "UPDATE runs SET state = ?, error = ?, finished_at = ? WHERE id = ?",
        (state, error, timestamp(), run_id),
    )


def record_measurement(db, run_id, ordinal, hinted, rank, url, error):
    columns = ("hint_rank", "hint_found_url", "hint_error", "hinted_at") if hinted else (
        "rank", "found_url", "error", "checked_at"
    )
    assignments = ", ".join(f"{column} = ?" for column in columns)
    db.execute(
        f"UPDATE measurements SET {assignments} WHERE run_id = ? AND ordinal = ?",
        (rank, url, error, timestamp(), run_id, ordinal),
    )


AGENT_STATUSES = {
    "STARTING": "Запуск",
    "SEARCH": "Идёт поиск",
    "FOUND": "Сайт найден",
    "NOT_FOUND": "Сайт не найден",
    "CAPTCHA": "Капча — ожидание оператора",
    "ERROR": "Ошибка",
    "DONE": "Завершено",
}


def start_agent_session(db, project_id, ordinal=None):
    return db.execute(
        """INSERT INTO agent_sessions(project_id, ordinal, state, status, message, started_at)
           VALUES (?, ?, 'running', 'STARTING', 'Запуск агента в контейнере.', ?)""",
        (project_id, ordinal, timestamp()),
    ).lastrowid


def update_agent_session(db, session_id, status, message=None, *, report_json=None):
    db.execute(
        """UPDATE agent_sessions SET status = ?, message = ?, report_json = COALESCE(?, report_json)
           WHERE id = ? AND state = 'running'""",
        (status, message, report_json, session_id),
    )


def finish_agent_session(db, session_id, state, status, message=None, *, report_json=None):
    db.execute(
        """UPDATE agent_sessions SET state = ?, status = ?, message = ?, finished_at = ?,
           report_json = COALESCE(?, report_json), resume = 0 WHERE id = ? AND state = 'running'""",
        (state, status, message, timestamp(), report_json, session_id),
    )
