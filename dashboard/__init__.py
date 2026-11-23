import logging
from datetime import datetime
from pathlib import Path
import secrets
import sqlite3
from threading import Thread

from flask import Flask, abort, redirect, render_template, request, session, url_for

from .forms import read_project, validate_project
from .runner import execute_run
from .search import domain_hint
from .storage import connect, finish_run, initialize, save_project, start_run

RUN_STATES = {
    "running": "Выполняется",
    "complete": "Завершена",
    "failed": "Ошибка",
    "interrupted": "Прервана",
}
logger = logging.getLogger(__name__)


def create_app(path, search_page=None, *, demo=False):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_SAMESITE="Strict",
        TRUSTED_HOSTS=["127.0.0.1", "localhost"],
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize(path)
    source = "Демо-данные" if demo else "Yandex Search API"

    @app.context_processor
    def template_context():
        return {
            "source": source,
            "configured": search_page is not None,
            "demo": demo,
            "states": RUN_STATES,
            "csrf_token": lambda: session.setdefault("csrf", secrets.token_hex(32)),
        }

    @app.template_filter("display_date")
    def display_date(value):
        if not value:
            return "—"
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y, %H:%M")

    @app.before_request
    def verify_csrf():
        if request.method == "POST":
            expected = session.get("csrf")
            if not expected or not secrets.compare_digest(request.form.get("csrf", "").encode(), expected.encode()):
                abort(400, "Срок действия формы истёк. Обновите страницу и повторите действие.")

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_project(db, project_id):
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            abort(404)
        return project

    @app.get("/")
    def projects():
        with connect(path) as db:
            rows = db.execute(
                """SELECT p.*, r.started_at, r.state,
                          (SELECT COUNT(*) FROM keywords WHERE project_id = p.id) AS keyword_count
                   FROM projects p LEFT JOIN runs r ON r.id = (
                       SELECT id FROM runs WHERE project_id = p.id ORDER BY id DESC LIMIT 1
                   )
                   ORDER BY p.id DESC"""
            ).fetchall()
        return render_template("projects.html", projects=rows)

    @app.route("/projects/new", methods=["GET", "POST"])
    @app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
    def edit_project(project_id=None):
        with connect(path) as db:
            project, keywords = {"interval_seconds": 10}, []
            if project_id is not None:
                project = get_project(db, project_id)
                keywords = db.execute(
                    "SELECT * FROM keywords WHERE project_id = ? ORDER BY ordinal", (project_id,)
                ).fetchall()
            error = None
            if request.method == "POST":
                try:
                    project, keywords = read_project(request.form)
                    values, words = validate_project(project, keywords)
                except ValueError as exc:
                    error = str(exc)
                else:
                    project_id = save_project(db, project_id, values, words)
                    return redirect(url_for("project", project_id=project_id), code=303)

        return render_template(
            "edit.html", project=project, keywords=keywords, project_id=project_id, error=error
        ), 422 if error else 200

    @app.get("/projects/<int:project_id>")
    @app.get("/projects/<int:project_id>/runs/<int:run_id>")
    def project(project_id, run_id=None):
        with connect(path) as db:
            db.execute("BEGIN")
            item = get_project(db, project_id)
            words = db.execute(
                "SELECT * FROM keywords WHERE project_id = ? ORDER BY ordinal", (project_id,)
            ).fetchall()
            runs = db.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY id DESC", (project_id,)
            ).fetchall()
            active = db.execute("SELECT project_id FROM runs WHERE state = 'running'").fetchone()
            run = next((r for r in runs if r["id"] == run_id), None) if run_id is not None else (
                runs[0] if runs else None
            )
            if run_id is not None and run is None:
                abort(404)
            measurements = db.execute(
                "SELECT * FROM measurements WHERE run_id = ? ORDER BY ordinal", (run["id"],)
            ).fetchall() if run else []
        return render_template(
            "project.html", project=item, keywords=words, domain=domain_hint(item["url"]),
            runs=runs, run=run, measurements=measurements, active=active,
        )

    @app.post("/projects/<int:project_id>/runs")
    def new_run(project_id):
        if search_page is None:
            abort(409, "Укажите реквизиты Yandex Search API в настройках окружения.")
        try:
            with connect(path) as db:
                db.execute("BEGIN IMMEDIATE")
                item = get_project(db, project_id)
                words = db.execute(
                    "SELECT * FROM keywords WHERE project_id = ? ORDER BY ordinal", (project_id,)
                ).fetchall()
                if not words:
                    abort(409, "Добавьте поисковые фразы.")
                run_id = start_run(db, item, words, source, domain_hint(item["url"]))
        except sqlite3.IntegrityError as exc:
            if "active_run" not in str(exc):
                raise
            abort(409, "Дождитесь окончания текущей проверки.")
        try:
            Thread(target=execute_run, args=(path, run_id, search_page), daemon=True).start()
        except RuntimeError:
            logger.exception("Не удалось запустить проверку %s", run_id)
            with connect(path) as db:
                finish_run(db, run_id, "failed", "Не удалось запустить проверку.")
        return redirect(url_for("project", project_id=project_id), code=303)

    @app.get("/settings")
    def settings():
        return render_template("settings.html")

    return app
