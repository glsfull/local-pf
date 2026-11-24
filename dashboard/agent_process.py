import logging
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

from .storage import connect, finish_run

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
CONTAINER_ROOT = PurePosixPath("/app")


def docker_path(path, override):
    if override:
        return override
    try:
        return str(CONTAINER_ROOT / path.relative_to(ROOT).as_posix())
    except ValueError:
        raise ValueError(f"Для пути вне папки проекта укажите путь в контейнере: {path}") from None


def execute_agent(path, run_id, options):
    try:
        env = None
        if options.local_agent:
            command = [sys.executable, str(ROOT / "agent.py")]
            db_path, log_dir = str(path), str(options.log_dir)
            env = {**os.environ, "DISPLAY": ":1"}
        else:
            command = [
                "docker", "exec", "-e", "DISPLAY=:1", options.agent_container,
                "/opt/local-pf/bin/python", str(CONTAINER_ROOT / "agent.py"),
            ]
            db_path = docker_path(path, options.container_db)
            log_dir = docker_path(options.log_dir, options.container_log_dir)
        command.extend(["--db", db_path, "--log-dir", log_dir, "--run-id", str(run_id)])
        if options.debug_log:
            command.append("--debug")
        if getattr(options, "search_api", False):
            command.extend(["--source", "api"])
        subprocess.run(command, env=env, check=True)
        error = "Агент завершился без записи результатов. Проверьте общий путь к SQLite."
    except subprocess.CalledProcessError as exc:
        error = f"Агент завершился с кодом {exc.returncode}. Проверьте контейнер и журнал."
        logger.warning("%s", error)
    except (OSError, ValueError) as exc:
        error = f"Не удалось запустить агент: {exc}"
        logger.warning("%s", error)

    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()["state"] == "running":
            finish_run(db, run_id, "failed", error)
