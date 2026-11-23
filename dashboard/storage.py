from contextlib import contextmanager
import sqlite3

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
    PRIMARY KEY (run_id, ordinal)
);
"""


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
        columns = {row["name"] for row in db.execute("PRAGMA table_info(measurements)")}
        for column in ("found_url", "hint_found_url", "error", "hint_error"):
            if column not in columns:
                db.execute(f"ALTER TABLE measurements ADD COLUMN {column} TEXT")
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


def record_measurement(db, run_id, ordinal, hinted, rank, url, error):
    columns = ("hint_rank", "hint_found_url", "hint_error", "hinted_at") if hinted else (
        "rank", "found_url", "error", "checked_at"
    )
    assignments = ", ".join(f"{column} = ?" for column in columns)
    db.execute(
        f"UPDATE measurements SET {assignments} WHERE run_id = ? AND ordinal = ?",
        (rank, url, error, timestamp(), run_id, ordinal),
    )
