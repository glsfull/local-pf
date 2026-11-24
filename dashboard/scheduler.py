"""Daily automatic position collection («Собирать позиции ежедневно автоматически»)."""
from datetime import datetime, time
import logging
from threading import Event, Thread

from modules.clock import MSK

from .history import moscow_date
from .storage import connect, project_settings

logger = logging.getLogger(__name__)


def collect_moment(now, collect_time):
    hours, minutes = map(int, collect_time.split(":"))
    return datetime.combine(now.date(), time(hours, minutes), tzinfo=MSK)


def due_projects(db, now=None):
    """Projects whose daily collection time has passed today without a run started after it."""
    now = (now or datetime.now(MSK)).astimezone(MSK)
    due = []
    for project in db.execute(
        "SELECT p.id FROM projects p WHERE EXISTS (SELECT 1 FROM keywords k WHERE k.project_id = p.id) ORDER BY p.id"
    ):
        settings = project_settings(db, project["id"])
        if not settings["collect_daily"] or settings["paused"]:
            continue
        moment = collect_moment(now, settings["collect_time"])
        if now < moment:
            continue
        latest = db.execute(
            "SELECT started_at FROM runs WHERE project_id = ? ORDER BY id DESC LIMIT 1", (project["id"],),
        ).fetchone()
        started = moscow_date(latest["started_at"]) if latest else None
        if started is None or started < moment:
            due.append(project["id"])
    return due


def scheduler_tick(path, launch, now=None):
    """Start the first due project. One check runs at a time, so the rest wait for the next tick."""
    with connect(path) as db:
        due = due_projects(db, now)
    for project_id in due:
        try:
            if launch(project_id, scheduled=True):
                return project_id
        except Exception:
            logger.exception("Не удалось запустить автоматический сбор позиций проекта %s", project_id)
    return None


def start_scheduler(path, launch, interval=60):
    stop = Event()

    def loop():
        while not stop.wait(interval):
            try:
                scheduler_tick(path, launch)
            except Exception:
                logger.exception("Ошибка планировщика сбора позиций")

    Thread(target=loop, name="positions-scheduler", daemon=True).start()
    return stop
