from datetime import datetime
import json
import logging
from pathlib import Path
import secrets
import sqlite3
from threading import Thread

from flask import Flask, abort, redirect, render_template, request, session, url_for

from .forms import COMPETITION, optional_number, read_project, validate_project, validate_url
from .history import TIER_LABELS, moscow_date, rank_tier, read_history, visibility
from .runner import execute_run
from .search import domain_hint, site_host
from .storage import connect, finish_run, initialize, project_keywords, save_project, start_run

RUN_STATES = {
    "running": "Выполняется",
    "complete": "Завершена",
    "failed": "Ошибка",
    "interrupted": "Прервана",
}
logger = logging.getLogger(__name__)


def create_app(path, search_page=None, *, demo=False, report_path=None, agent_runner=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_SAMESITE="Strict",
        TRUSTED_HOSTS=["127.0.0.1", "localhost"],
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    path = Path(path)
    if report_path is not None:
        report_path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize(path)
    source = "Демо-данные" if demo else "Парсер выдачи"
    configured = agent_runner is not None or search_page is not None

    @app.context_processor
    def template_context():
        return {
            "source": source,
            "configured": configured,
            "demo": demo,
            "agent_logs_enabled": report_path is not None,
            "states": RUN_STATES,
            "competition_labels": COMPETITION,
            "csrf_token": lambda: session.setdefault("csrf", secrets.token_hex(32)),
        }

    @app.template_filter("display_date")
    def display_date(value):
        date = value if isinstance(value, datetime) else moscow_date(value)
        return date.strftime("%d.%m.%Y, %H:%M:%S %Z") if date else "—"

    @app.template_filter("metric")
    def metric(value, suffix=""):
        if value is None:
            return "—"
        formatted = f"{value:,.2f}".rstrip("0").rstrip(".")
        return formatted.replace(",", " ").replace(".", ",") + suffix

    app.add_template_filter(rank_tier)

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
        project = db.execute(
            """SELECT p.*, a.region_name, a.spent FROM projects p
               LEFT JOIN project_attributes a ON a.project_id = p.id WHERE p.id = ?""",
            (project_id,),
        ).fetchone()
        if project is None:
            abort(404)
        return project

    @app.get("/")
    def projects():
        with connect(path) as db:
            rows = db.execute(
                """SELECT p.*, a.region_name, a.spent, r.started_at, r.state,
                          COUNT(k.ordinal) AS keyword_count, AVG(ka.power) AS power,
                          SUM(ka.visits) AS visits, SUM(ka.budget) AS budget,
                          COUNT(CASE WHEN m.checked_at IS NOT NULL AND m.error IS NULL THEN 1 END) AS measured,
                          COUNT(CASE WHEN m.rank BETWEEN 1 AND 50 THEN 1 END) AS in_top
                   FROM projects p LEFT JOIN runs r ON r.id = (
                       SELECT id FROM runs WHERE project_id = p.id ORDER BY id DESC LIMIT 1
                   )
                   LEFT JOIN project_attributes a ON a.project_id = p.id
                   LEFT JOIN keywords k ON k.project_id = p.id
                   LEFT JOIN keyword_attributes ka ON ka.project_id = k.project_id AND ka.ordinal = k.ordinal
                   LEFT JOIN measurements m ON m.run_id = r.id AND m.phrase = k.phrase AND m.landing_url = k.landing_url
                   GROUP BY p.id
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
                keywords = project_keywords(db, project_id)
            error = None
            if request.method == "POST":
                try:
                    project, keywords = read_project(request.form)
                    values, words, attributes = validate_project(project, keywords)
                except ValueError as exc:
                    error = str(exc)
                else:
                    project_id = save_project(db, project_id, values, words, attributes=attributes)
                    return redirect(url_for("project", project_id=project_id), code=303)

        return render_template(
            "edit.html", project=project, keywords=keywords, project_id=project_id, error=error
        ), 422 if error else 200

    @app.get("/projects/<int:project_id>")
    @app.get("/projects/<int:project_id>/<any(keywords,clusters):view>")
    @app.get("/projects/<int:project_id>/runs/<int:run_id>")
    def project(project_id, run_id=None, view="positions"):
        with connect(path) as db:
            db.execute("BEGIN")
            item = get_project(db, project_id)
            words = project_keywords(db, project_id)
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
            has_report = run and db.execute(
                "SELECT 1 FROM run_reports WHERE run_id = ?", (run["id"],),
            ).fetchone() is not None
            days, history, latest = read_history(db, project_id) if view != "clusters" else ([], [], {})
            clusters, attributes = {}, {}
            if view == "clusters":
                attributes = {
                    row["name"]: row for row in db.execute(
                        "SELECT * FROM cluster_attributes WHERE project_id = ?", (project_id,),
                    )
                }
                for word in words:
                    clusters.setdefault(word["cluster"] or "", []).append(word)
        hinted_chart = request.args.get("query") == "hint"
        return render_template(
            "project.html", project=item, keywords=words, domain=domain_hint(item["url"]),
            runs=runs, run=run, measurements=measurements, active=active, has_report=has_report,
            view=view, days=days, history=history, latest=latest, tiers=TIER_LABELS,
            chart=visibility(days, history, hinted_chart) if view == "positions" else [],
            hinted_chart=hinted_chart, clusters=clusters, cluster_details=attributes,
        )

    @app.post("/projects/<int:project_id>/clusters")
    def edit_cluster(project_id):
        name = request.form.get("cluster", "")
        url = request.form.get("url", "").strip()
        with connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            item = get_project(db, project_id)
            if not name or not db.execute(
                "SELECT 1 FROM keyword_attributes WHERE project_id = ? AND cluster = ?", (project_id, name),
            ).fetchone():
                abort(404)
            host, site = site_host(url), site_host(item["url"])
            if url and (not validate_url(url) or (host != site and not host.endswith("." + site))):
                abort(422, "Укажите URL проекта или его поддомена.")
            try:
                commercial = optional_number(request.form.get("commercial"), "Коммерция", 0, 100)
                meta = optional_number(request.form.get("has_meta"), "Мета", 0, 1)
                content = optional_number(request.form.get("has_text"), "Текст", 0, 1)
            except ValueError as exc:
                abort(422, str(exc))
            db.execute(
                """INSERT INTO cluster_attributes VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, name) DO UPDATE SET url=excluded.url, commercial=excluded.commercial,
                       has_meta=excluded.has_meta, has_text=excluded.has_text""",
                (project_id, name, url, commercial, meta, content),
            )
        return redirect(url_for("project", project_id=project_id, view="clusters"), code=303)

    @app.post("/projects/<int:project_id>/runs")
    def new_run(project_id):
        if not configured:
            abort(409, "Подключите браузерный агент для проверки позиций.")
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
            if agent_runner is not None:
                Thread(target=agent_runner, args=(path, run_id), daemon=True).start()
            else:
                Thread(target=execute_run, args=(path, run_id, search_page), daemon=True).start()
        except RuntimeError:
            logger.exception("Не удалось запустить проверку %s", run_id)
            with connect(path) as db:
                finish_run(db, run_id, "failed", "Не удалось запустить проверку.")
        return redirect(url_for("project", project_id=project_id), code=303)

    @app.get("/settings")
    def settings():
        return render_template("settings.html")

    @app.get("/logs")
    def agent_logs():
        if report_path is None:
            abort(404)
        report, error = None, None
        try:
            with report_path.open(encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                raise ValueError("Invalid report")
            for key in ("events", "pages_checked"):
                rows = data.get(key, [])
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError("Invalid report entries")
            report = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.exception("Не удалось прочитать отчёт агента")
            error = "Не удалось прочитать report.json. Проверьте файл и права доступа."
        return render_template("logs.html", report=report, error=error)

    @app.get("/projects/<int:project_id>/runs/<int:run_id>/logs")
    def run_logs(project_id, run_id):
        with connect(path) as db:
            row = db.execute(
                """SELECT report_json, p.name FROM run_reports
                   JOIN runs r ON r.id = run_id JOIN projects p ON p.id = r.project_id
                   WHERE r.project_id = ? AND r.id = ?""",
                (project_id, run_id),
            ).fetchone()
        if row is None:
            abort(404)
        return render_template(
            "logs.html", report=json.loads(row["report_json"]), project_name=row["name"],
            project_id=project_id, run_id=run_id,
        )

    return app
