import logging
import time

from .search import SearchError, match_position
from .storage import connect, finish_run, record_measurement

logger = logging.getLogger(__name__)


def execute_run(path, run_id, search_page, wait=time.sleep):
    with connect(path) as db:
        run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        measurements = db.execute(
            "SELECT * FROM measurements WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall()

    state, error = "complete", None
    try:
        for index, row in enumerate(measurements):
            if index and run["interval_seconds"]:
                wait(run["interval_seconds"])
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
        finish_run(db, run_id, state, error)


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
