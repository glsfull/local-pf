import json
import logging
import time

from .search import SearchError, match_position
from .storage import connect, finish_run, record_measurement

logger = logging.getLogger(__name__)


def batched(search_page, method):
    """Optional method of the search source, checked on the class so that mocks do not match."""
    return callable(getattr(type(search_page), method, None))


def execute_run(path, run_id, search_page, wait=time.sleep, *, report=None):
    with connect(path) as db:
        run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        measurements = db.execute(
            "SELECT * FROM measurements WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall()

    if run is None or run["state"] != "running":
        return "interrupted"

    state, error = "complete", None
    try:
        if batched(search_page, "prepare"):
            execute_batch(path, run_id, run, measurements, search_page)
        else:
            for index, row in enumerate(measurements):
                if index and run["interval_seconds"]:
                    wait(run["interval_seconds"])
                if batched(search_page, "begin_phrase"):
                    # One phrase = one profile + one proxy: the main and the refined query share them.
                    search_page.begin_phrase()
                args = (run["project_url"], row["landing_url"], run["region"])
                rank = measure(path, run_id, row, search_page, args)
                if rank is None:
                    measure(path, run_id, row, search_page, args, hinted=True)
    except SearchError as exc:
        state, error = "failed", str(exc)
        logger.warning("Проверка %s остановлена. %s", run_id, error)
    except Exception:
        state, error = "failed", "Внутренняя ошибка проверки. Подробнее в журнале панели."
        logger.exception("Проверка %s остановлена из-за внутренней ошибки", run_id)
    with connect(path) as db:
        if report is not None:
            report.update(state=state, error=error)
            if batched(search_page, "summary"):
                report.update(search_page.summary())
            db.execute(
                "INSERT INTO run_reports VALUES (?, ?)", (run_id, json.dumps(report, ensure_ascii=False)),
            )
        finish_run(db, run_id, state, error)
    return state


def execute_batch(path, run_id, run, measurements, search_page):
    """Two passes for deferred sources: submit every main phrase, collect the answers,
    then submit the refined queries of the phrases that were not found and collect them too.

    The refined query is not used when the main phrase is found (one paid request per phrase).
    """
    region = run["region"]
    search_page.prepare([row["phrase"] for row in measurements], region)
    missing = []
    for row in measurements:
        args = (run["project_url"], row["landing_url"], region)
        if measure(path, run_id, row, search_page, args) is None:
            missing.append((row, args))
    search_page.prepare([row["hint_query"] for row, _ in missing], region)
    for row, args in missing:
        measure(path, run_id, row, search_page, args, hinted=True)


def measure(path, run_id, row, search_page, args, *, hinted=False):
    query = row["hint_query" if hinted else "phrase"]
    try:
        rank, url = match_position(search_page, query, *args)
    except Exception as exc:
        error = exc
        if not isinstance(exc, SearchError):
            logger.exception("Ошибка при измерении позиции в проверке %s", run_id)
            error = SearchError("Внутренняя ошибка проверки. Подробнее в журнале панели.")
        with connect(path) as db:
            record_measurement(db, run_id, row["ordinal"], hinted, None, None, str(error))
        raise error
    with connect(path) as db:
        record_measurement(db, run_id, row["ordinal"], hinted, rank, url, None)
    return rank
