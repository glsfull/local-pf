import logging
import time

from .search import SearchError, match_position
from .storage import connect, finish_run, timestamp

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
            rank = match_position(search_page, row["phrase"], *args)
            with connect(path) as db:
                db.execute(
                    "UPDATE measurements SET rank = ?, checked_at = ? WHERE run_id = ? AND ordinal = ?",
                    (rank, timestamp(), run_id, row["ordinal"]),
                )
            if rank is None:
                rank = match_position(search_page, row["hint_query"], *args)
                with connect(path) as db:
                    db.execute(
                        """UPDATE measurements SET hint_rank = ?, hinted_at = ?
                        WHERE run_id = ? AND ordinal = ?""",
                        (rank, timestamp(), run_id, row["ordinal"]),
                    )
    except SearchError as exc:
        state, error = "failed", str(exc)
        logger.warning("Проверка %s остановлена. %s", run_id, error)
    except Exception:
        logger.exception("Проверка %s остановлена из-за внутренней ошибки", run_id)
        state, error = "failed", "Внутренняя ошибка проверки. Подробнее в журнале панели."
    with connect(path) as db:
        finish_run(db, run_id, state, error)
