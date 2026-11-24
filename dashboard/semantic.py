"""Storage of the «Сбор ядра (ИИ)» tab: DeepSeek answers and adding chosen phrases to the project."""
import json

from modules.clock import timestamp

from .forms import MAX_KEYWORDS, MAX_QUERY_LENGTH, brand_list
from .search import domain_hint
from .storage import project_keywords, project_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_runs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    seeds TEXT NOT NULL,
    extra TEXT NOT NULL,
    count INTEGER NOT NULL,
    keywords_json TEXT NOT NULL,
    usage_json TEXT NOT NULL DEFAULT '{}'
);
"""


def initialize(db):
    db.executescript(SCHEMA)


def save_run(db, project_id, model, form, keywords, usage):
    return db.execute(
        "INSERT INTO semantic_runs(project_id, created_at, model, seeds, extra, count, keywords_json, usage_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, timestamp(), model, form["seeds"], form["extra"], form["count"],
         json.dumps(keywords, ensure_ascii=False), json.dumps(usage)),
    ).lastrowid


def latest_run(db, project_id, run_id=None):
    """The chosen or the latest run of the project with its decoded phrases, or None."""
    query = "SELECT * FROM semantic_runs WHERE project_id = ?"
    params = [project_id]
    if run_id is not None:
        query += " AND id = ?"
        params.append(run_id)
    row = db.execute(query + " ORDER BY id DESC LIMIT 1", params).fetchone()
    if row is None:
        return None
    run = dict(row)
    run["keywords"] = json.loads(row["keywords_json"])
    run["usage"] = json.loads(row["usage_json"] or "{}")
    return run


def run_history(db, project_id, limit=10):
    return db.execute(
        "SELECT id, created_at, model, count, json_array_length(keywords_json) AS found "
        "FROM semantic_runs WHERE project_id = ? ORDER BY id DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()


def refinement(settings, url):
    """(hint_type, brand) for new phrases from the project setting «если сайт не найден в топ-50»."""
    brands = brand_list(settings)
    if settings["not_found"] == "brand" and brands:
        return "brand", brands[0]
    if domain_hint(url):
        return "domain", ""
    if brands:
        return "brand", brands[0]
    raise ValueError("У сайта не распознана доменная зона: укажите бренд в настройках проекта.")


def append_keywords(db, project, chosen):
    """Append chosen {"phrase", "cluster"} items to the project keywords.

    Phrases already in the project are skipped; the 100-phrase limit of a project is respected.
    Returns (added, skipped_existing, skipped_limit).
    """
    words = project_keywords(db, project["id"])
    seen = {word["phrase"].casefold() for word in words}
    hint_type, brand = refinement(project_settings(db, project["id"]), project["url"])
    domain = domain_hint(project["url"])
    ordinal = max((word["ordinal"] for word in words), default=-1) + 1
    added = existing = over = 0
    for item in chosen:
        phrase = item["phrase"]
        if phrase.casefold() in seen:
            existing += 1
            continue
        if len(words) + added >= MAX_KEYWORDS:
            over += 1
            continue
        if len(f"{phrase} {brand or domain}") > MAX_QUERY_LENGTH:
            existing += 1
            continue
        seen.add(phrase.casefold())
        db.execute("INSERT INTO keywords VALUES (?, ?, ?, '', ?, ?)", (project["id"], ordinal, phrase, hint_type, brand))
        db.execute(
            "INSERT INTO keyword_attributes(project_id, ordinal, cluster, competition) VALUES (?, ?, ?, '')",
            (project["id"], ordinal, item.get("cluster", "")[:100]),
        )
        ordinal += 1
        added += 1
    return added, existing, over
