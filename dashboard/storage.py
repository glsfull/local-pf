from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3

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
    PRIMARY KEY (run_id, ordinal)
);
"""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def initialize(path):
    with connect(path) as db:
        db.executescript(SCHEMA)
        db.execute(
            "UPDATE runs SET state = 'interrupted', finished_at = ? WHERE state = 'running'",
            (timestamp(),),
        )


def save_project(db, project_id, project, keywords):
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
        [(project_id, index, *keyword) for index, keyword in enumerate(keywords)],
    )
    return project_id


def start_run(db, project, keywords, source, domain):
    run_id = db.execute(
        """INSERT INTO runs(project_id, project_url, interval_seconds, region, source, state, started_at)
        VALUES (?, ?, ?, ?, ?, 'running', ?)""",
        (project["id"], project["url"], project["interval_seconds"], project["region"], source, timestamp()),
    ).lastrowid
    for word in keywords:
        hint = word["brand"] if word["hint_type"] == "brand" else domain
        db.execute(
            """INSERT INTO measurements(run_id, ordinal, phrase, landing_url, hint_query)
            VALUES (?, ?, ?, ?, ?)""",
            (run_id, word["ordinal"], word["phrase"], word["landing_url"], f"{word['phrase']} {hint}"),
        )
    return run_id


def finish_run(db, run_id, state, error=None):
    db.execute(
        "UPDATE runs SET state = ?, error = ?, finished_at = ? WHERE id = ?",
        (state, error, timestamp(), run_id),
    )
