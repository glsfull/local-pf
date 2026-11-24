import csv
from datetime import datetime
import getpass
import io
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
from threading import Thread
from time import monotonic

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for

from modules.clock import MSK, timestamp
from modules.journal import Journal

from .forms import (
    COMPETITION, FREQUENCY_TYPES, NOT_FOUND_MODES, QUERY_REGIONS, REGIONS, SCHEDULE_MODES, THEMES, WEEKDAYS,
    brand_list, optional_number, position_region, read_project, read_schedule_settings, read_settings,
    read_visits, region_label, validate_project, validate_url,
)
from .connection import POSITION_SOURCES, read_connection, save_connection
from .history import TIER_LABELS, moscow_date, rank_tier, read_history, visibility
from .runner import execute_run
from .search import domain_hint, site_host
from .search_api import DAY_PRICE, NIGHT_PRICE, SOURCE as API_SOURCE, YandexDeferredSearch
from .storage import (
    AGENT_STATUSES, DEFAULT_SETTINGS, connect, finish_agent_session, finish_run, initialize, panel_setting,
    project_keywords, project_settings, save_panel_setting, save_project, save_project_settings, start_agent_session,
    start_run,
)
from . import views

BRAND = "ТопПульт"
RUN_STATES = {
    "running": "Выполняется",
    "complete": "Завершена",
    "failed": "Ошибка",
    "interrupted": "Прервана",
}
PROJECT_TABS = (
    ("keywords", "Ключевики"), ("clusters", "Кластеры"), ("positions", "Позиции"), ("visits", "Переходы"),
    ("schedule", "Расписание"), ("expenses", "Расходы"), ("settings", "Настройки"),
)
EXPORTS = ("keywords", "clusters", "positions", "visits")
DEFAULT_NOVNC_URL = "http://localhost:56565/"
AGENT_EVENTS_SHOWN = 15
logger = logging.getLogger(__name__)


class Conflict(Exception):
    """A position check cannot start right now."""


SCHEDULE_RETRY_SECONDS = 30 * 60


def today_msk():
    return datetime.now(MSK).date()


def plain(value):
    """Number for CSV cells: 120.0 → 120, 35.5 → 35.5."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def csv_cell(value):
    """Keep spreadsheet apps from treating a phrase or URL like «=HYPERLINK(...)» as a formula."""
    return "'" + value if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def csv_response(name, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(header)
    writer.writerows([csv_cell(value) for value in row] for row in rows)
    return Response(
        "﻿" + buffer.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


def mask_secret(value):
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def create_app(
    path, search_page=None, *, demo=False, report_path=None, agent_runner=None, agent_session_runner=None,
    api_factory=YandexDeferredSearch,
):
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
    browser_source = "Демо-данные" if demo else "Парсер выдачи"
    browser_configured = agent_runner is not None or search_page is not None

    def read_balance(db):
        value = panel_setting(db, "balance")
        return None if value in (None, "") else float(value)

    def api_credentials(db):
        """YANDEX_API_KEY and YANDEX_FOLDER_ID from the panel settings, with the environment as a fallback."""
        key = panel_setting(db, "yandex_api_key") or os.getenv("YANDEX_API_KEY", "")
        folder = panel_setting(db, "yandex_folder_id") or os.getenv("YANDEX_FOLDER_ID", "")
        return key.strip(), folder.strip()

    def position_source(db):
        """(label, uses_api): the API is used when it is selected and both credentials are present."""
        if demo:
            return browser_source, False
        wanted = panel_setting(db, "position_source", "api")
        if wanted == "api" and all(api_credentials(db)):
            return API_SOURCE, True
        if wanted == "api" and not browser_configured:
            return API_SOURCE, False
        return browser_source, False

    def novnc_url(db):
        return panel_setting(db, "novnc_url") or os.getenv("LOCAL_PF_NOVNC_URL", DEFAULT_NOVNC_URL)

    @app.context_processor
    def template_context():
        with connect(path) as db:
            balance = read_balance(db)
            user_name = panel_setting(db, "user_name") or getpass.getuser()
            source, uses_api = position_source(db)
            novnc = novnc_url(db)
        return {
            "brand": BRAND,
            "year": datetime.now(MSK).year,
            "balance": balance,
            "user_name": user_name,
            "source": source,
            "uses_api": uses_api,
            "configured": uses_api or browser_configured,
            "agent_enabled": agent_session_runner is not None,
            "novnc_url": novnc,
            "demo": demo,
            "agent_logs_enabled": report_path is not None,
            "states": RUN_STATES,
            "competition_labels": COMPETITION,
            "project_tabs": PROJECT_TABS,
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
        return formatted.replace(",", " ").replace(".", ",") + suffix

    @app.template_filter("signed")
    def signed(value, suffix=""):
        if value is None:
            return ""
        return ("+" if value > 0 else "") + metric(value, suffix)

    app.add_template_filter(rank_tier)
    # Jinja counterpart of positionClass() in static/positions.js.
    app.add_template_filter(rank_tier, "position_class")
    app.add_template_filter(views.visit_class)

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

    def get_project_safe(project_id):
        if project_id is None:
            return None
        with connect(path) as db:
            return get_project(db, project_id)

    def back_to(project_id, view):
        target = request.form.get("return_to", "")
        prefix = f"/projects/{project_id}/"
        if target.startswith(prefix) and "//" not in target and "\\" not in target:
            return redirect(target, code=303)
        return redirect(url_for("project", project_id=project_id, view=view), code=303)

    deferred = {}

    def launch_run(project_id, *, scheduled=False):
        """Start a position check. Manual starts raise Conflict, scheduled ones return None instead.

        A scheduled start that cannot proceed for a reason other than a running check (no money, no phrases)
        is retried after SCHEDULE_RETRY_SECONDS instead of on every scheduler tick.
        """
        if scheduled and project_id in deferred and monotonic() - deferred[project_id] < SCHEDULE_RETRY_SECONDS:
            return None
        busy = False
        try:
            try:
                with connect(path) as db:
                    db.execute("BEGIN IMMEDIATE")
                    source, uses_api = position_source(db)
                    credentials = api_credentials(db)
                    if not uses_api and not browser_configured:
                        raise Conflict(
                            "Укажите YANDEX_API_KEY и YANDEX_FOLDER_ID в настройках панели "
                            "или подключите браузерный агент."
                        )
                    item = get_project(db, project_id)
                    balance = read_balance(db)
                    if balance is not None and balance <= 0:
                        raise Conflict("На балансе недостаточно средств. Сбор позиций приостановлен.")
                    words = db.execute(
                        "SELECT * FROM keywords WHERE project_id = ? ORDER BY ordinal", (project_id,)
                    ).fetchall()
                    if scheduled and project_settings(db, project_id)["rare_lowfreq"]:
                        _, lines, _ = read_history(db, project_id)
                        skip = views.low_frequency_skip(project_keywords(db, project_id), lines, today_msk())
                        words = [word for word in words if word["ordinal"] not in skip]
                    if not words:
                        raise Conflict("Добавьте поисковые фразы.")
                    run_id = start_run(
                        db, item, words, source, domain_hint(item["url"]), region=position_region(item["region"]),
                    )
            except sqlite3.IntegrityError as exc:
                if "active_run" not in str(exc):
                    raise
                busy = True
                raise Conflict("Дождитесь окончания текущей проверки.") from None
        except Conflict as exc:
            if scheduled:
                if not busy:
                    deferred[project_id] = monotonic()
                logger.info("Автоматический сбор позиций проекта %s отложен. %s", project_id, exc)
                return None
            raise
        deferred.pop(project_id, None)
        try:
            if uses_api:
                Thread(target=execute_api_run, args=(path, run_id, credentials), daemon=True).start()
            elif agent_runner is not None:
                Thread(target=agent_runner, args=(path, run_id), daemon=True).start()
            else:
                Thread(target=execute_run, args=(path, run_id, search_page), daemon=True).start()
        except RuntimeError:
            logger.exception("Не удалось запустить проверку %s", run_id)
            with connect(path) as db:
                finish_run(db, run_id, "failed", "Не удалось запустить проверку.")
        return run_id

    app.launch_run = launch_run

    def execute_api_run(path, run_id, credentials):
        """Positions through deferred Yandex Search API requests: runs on the host, no browser needed."""
        report = {"timestamp": timestamp(), "run_id": run_id, "captcha_encountered": False,
                  "pages_checked": [], "events": []}
        try:
            with connect(path) as db:
                url = db.execute("SELECT project_url FROM runs WHERE id = ?", (run_id,)).fetchone()["project_url"]
            report["target_domain"] = site_host(url)
            search = api_factory(*credentials, Journal(report["events"], 0), report)
        except Exception as exc:
            logger.exception("Не удалось подготовить Yandex Search API")
            with connect(path) as db:
                finish_run(db, run_id, "failed", f"Yandex Search API: {exc}")
            return "failed"
        return execute_run(path, run_id, search, report=report)

    app.execute_api_run = execute_api_run

    @app.get("/")
    def projects():
        today = today_msk().isoformat()
        with connect(path) as db:
            db.execute("BEGIN")
            rows = db.execute(
                """SELECT p.*, a.region_name, a.spent, r.started_at, r.state, r.error AS run_error,
                          COUNT(k.ordinal) AS keyword_count, AVG(ka.power) AS power,
                          SUM(ka.visits) AS visits, SUM(ka.budget) AS budget, SUM(ka.demand) AS demand,
                          AVG(ka.chance) AS chance,
                          (SELECT SUM(done) FROM visit_stats v WHERE v.project_id = p.id AND v.day = ?) AS visits_today,
                          (SELECT SUM(cost) FROM visit_stats v WHERE v.project_id = p.id AND v.day = ?) AS cost_today
                   FROM projects p LEFT JOIN runs r ON r.id = (
                       SELECT id FROM runs WHERE project_id = p.id ORDER BY id DESC LIMIT 1
                   )
                   LEFT JOIN project_attributes a ON a.project_id = p.id
                   LEFT JOIN keywords k ON k.project_id = p.id
                   LEFT JOIN keyword_attributes ka ON ka.project_id = k.project_id AND ka.ordinal = k.ordinal
                   GROUP BY p.id
                   ORDER BY p.id DESC""",
                (today, today),
            ).fetchall()
            items = []
            for row in rows:
                days, lines, _ = read_history(db, row["id"], limit=2)
                current, change = views.project_visibility(days, lines)
                items.append({
                    "row": row, "visibility": current, "change": change,
                    "settings": project_settings(db, row["id"]),
                    "region": region_label(row["region"], row["region_name"]),
                    "attention": row["state"] == "failed" or (row["chance"] is not None and row["chance"] < 20),
                })
            balance = read_balance(db)

        def total(key):
            values = [item["row"][key] for item in items if item["row"][key] is not None]
            return sum(values) if values else None

        totals = {key: total(key) for key in ("keyword_count", "visits", "budget", "visits_today", "cost_today")}
        powers = [item["row"]["power"] for item in items if item["row"]["power"] is not None]
        totals["power"] = sum(powers) / len(powers) if powers else None
        return render_template(
            "projects.html", projects=items, totals=totals,
            low_balance=balance is not None and balance <= 0,
        )

    @app.route("/projects/new", methods=["GET", "POST"])
    @app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
    def edit_project(project_id=None):
        with connect(path) as db:
            project, keywords = {"interval_seconds": 10, "region": "1"}, []
            settings = dict(DEFAULT_SETTINGS)
            if project_id is not None:
                project = get_project(db, project_id)
                keywords = project_keywords(db, project_id)
                settings = project_settings(db, project_id)
            error = None
            if request.method == "POST":
                try:
                    project, keywords = read_project(request.form)
                    settings = read_settings(request.form) or settings
                    brands = brand_list(settings)
                    values, words, attributes = validate_project(
                        project, keywords, default_brand=brands[0] if brands else "",
                    )
                except ValueError as exc:
                    error = str(exc)
                else:
                    project_id = save_project(db, project_id, values, words, attributes=attributes)
                    save_project_settings(db, project_id, settings)
                    return redirect(url_for("project", project_id=project_id), code=303)

        return render_template(
            "edit.html", project=project, keywords=keywords, project_id=project_id, error=error,
            settings=settings, regions=REGIONS, query_regions=QUERY_REGIONS, frequency_types=FREQUENCY_TYPES,
            themes=THEMES, schedule_modes=SCHEDULE_MODES, not_found_modes=NOT_FOUND_MODES, weekdays=WEEKDAYS,
            view="settings", status_project=get_project_safe(project_id),
        ), 422 if error else 200

    @app.get("/projects/<int:project_id>")
    @app.get("/projects/<int:project_id>/<any(keywords,clusters,positions,visits,schedule,expenses):view>")
    @app.get("/projects/<int:project_id>/runs/<int:run_id>")
    def project(project_id, run_id=None, view="positions"):
        today = today_msk()
        context = {}
        with connect(path) as db:
            db.execute("BEGIN")
            item = get_project(db, project_id)
            words = project_keywords(db, project_id)
            settings = project_settings(db, project_id)
            balance = read_balance(db)
            runs = db.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY id DESC", (project_id,)
            ).fetchall()
            active = db.execute("SELECT project_id FROM runs WHERE state = 'running'").fetchone()
            run = next((r for r in runs if r["id"] == run_id), None) if run_id is not None else (
                runs[0] if runs else None
            )
            if run_id is not None and run is None:
                abort(404)
            if view == "keywords":
                _, _, latest = read_history(db, project_id, limit=1)
                flt = request.args.get("filter", "")
                flt = flt if flt in views.KEYWORD_FILTERS else ""
                sort = request.args.get("sort", "demand")
                sort = sort if sort in views.KEYWORD_SORTS else "demand"
                descending = request.args.get("order", "desc") != "asc"
                rows = views.keyword_rows(
                    words, latest, views.today_queue(db, project_id, today), flt, sort, descending,
                )
                page, pagination = views.page_slice(rows, request.args)
                context.update(
                    rows=page, pagination=pagination, page_totals=views.keyword_totals(page),
                    all_totals=views.keyword_totals(rows), filter=flt, filters=views.KEYWORD_FILTERS,
                    sort=sort, descending=descending,
                )
            elif view == "clusters":
                details = {
                    row["name"]: row for row in db.execute(
                        "SELECT * FROM cluster_attributes WHERE project_id = ?", (project_id,),
                    )
                }
                clusters, unclustered = views.cluster_tree(words, details)
                context.update(clusters=clusters, unclustered=unclustered)
            elif view == "positions":
                since, until, limit, period = views.period_days(request.args, today)
                days, history, latest = read_history(db, project_id, since=since, until=until, limit=limit)
                days = views.trim_days(days, period)
                history = [line for line in history if any(day in line["days"] for day in days)]
                hinted_chart = request.args.get("query") == "hint"
                lines, pagination = views.page_slice(history, request.args)
                measurements = db.execute(
                    "SELECT * FROM measurements WHERE run_id = ? ORDER BY ordinal", (run["id"],)
                ).fetchall() if run else []
                has_report = run and db.execute(
                    "SELECT 1 FROM run_reports WHERE run_id = ?", (run["id"],),
                ).fetchone() is not None
                context.update(
                    days=days, history=lines, pagination=pagination, latest=latest, tiers=TIER_LABELS,
                    chart=visibility(days, history, hinted_chart), hinted_chart=hinted_chart,
                    measurements=measurements, has_report=has_report, period=period, since=since, until=until,
                )
            elif view == "visits":
                _, _, latest = read_history(db, project_id, limit=1)
                context.update(visits=views.visit_matrix(db, project_id, words, settings, latest, request.args, today))
            elif view == "expenses":
                expense_rows, expense_totals = views.expenses(db, project_id, settings)
                context.update(expense_rows=expense_rows, expense_totals=expense_totals)
            elif view == "schedule":
                context.update(schedule_modes=SCHEDULE_MODES, weekdays=WEEKDAYS)
        return render_template(
            "project.html", project=item, keywords=words, domain=domain_hint(item["url"]), settings=settings,
            runs=runs, run=run, active=active, view=view, notices=views.warnings(balance, words),
            low_balance=balance is not None and balance <= 0, region=region_label(item["region"], item["region_name"]),
            position_city=position_region(item["region"]), **context,
        )

    @app.post("/projects/<int:project_id>/pause")
    def toggle_pause(project_id):
        with connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            get_project(db, project_id)
            paused = project_settings(db, project_id)["paused"]
            save_project_settings(db, project_id, {"paused": int(not paused)})
        return back_to(project_id, "positions")

    @app.post("/projects/<int:project_id>/schedule")
    def edit_schedule(project_id):
        with connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            get_project(db, project_id)
            try:
                settings = read_schedule_settings(request.form)
            except ValueError as exc:
                abort(422, str(exc))
            save_project_settings(db, project_id, settings)
        return redirect(url_for("project", project_id=project_id, view="schedule"), code=303)

    @app.post("/projects/<int:project_id>/keywords/<int:ordinal>")
    def edit_keyword(project_id, ordinal):
        with connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            word = next((word for word in project_keywords(db, project_id) if word["ordinal"] == ordinal), None)
            if word is None:
                abort(404)
            try:
                if request.form.get("auto") == "1":
                    power = views.AUTO_POWER.get(word["competition"] or "", 10)
                else:
                    power = optional_number(request.form.get("power", "").strip(), "Мощность", 0, 100, decimal=True)
                visits = optional_number(request.form.get("visits", "").strip(), "Переходы в день", 0, 999999999)
            except ValueError as exc:
                abort(422, str(exc))
            fields = {"power": power}
            if "visits" in request.form:
                fields["visits"] = visits
            db.execute(
                "INSERT OR IGNORE INTO keyword_attributes(project_id, ordinal, cluster, competition) VALUES (?, ?, '', '')",
                (project_id, ordinal),
            )
            db.execute(
                f"UPDATE keyword_attributes SET {', '.join(f'{key} = ?' for key in fields)} "
                "WHERE project_id = ? AND ordinal = ?",
                (*fields.values(), project_id, ordinal),
            )
        return back_to(project_id, "keywords")

    @app.post("/projects/<int:project_id>/keywords/auto")
    def auto_strategy(project_id):
        with connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            get_project(db, project_id)
            for word in project_keywords(db, project_id):
                db.execute(
                    "INSERT OR IGNORE INTO keyword_attributes(project_id, ordinal, cluster, competition) VALUES (?, ?, '', '')",
                    (project_id, word["ordinal"]),
                )
                db.execute(
                    "UPDATE keyword_attributes SET power = ? WHERE project_id = ? AND ordinal = ?",
                    (views.AUTO_POWER.get(word["competition"] or "", 10), project_id, word["ordinal"]),
                )
        return back_to(project_id, "keywords")

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

    @app.post("/projects/<int:project_id>/clusters/recluster")
    def recluster(project_id):
        with connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            item = get_project(db, project_id)
            words = project_keywords(db, project_id)
            _, lines, _ = read_history(db, project_id, limit=None)
            groups = views.recluster(words, lines)
            site = site_host(item["url"])
            for word in words:
                name, url = groups.get(word["ordinal"], ("", ""))
                db.execute(
                    "INSERT OR IGNORE INTO keyword_attributes(project_id, ordinal, cluster, competition) VALUES (?, ?, '', '')",
                    (project_id, word["ordinal"]),
                )
                db.execute(
                    "UPDATE keyword_attributes SET cluster = ? WHERE project_id = ? AND ordinal = ?",
                    (name, project_id, word["ordinal"]),
                )
                host = site_host(url) if url else ""
                if name and (host == site or host.endswith("." + site)):
                    db.execute(
                        "INSERT OR IGNORE INTO cluster_attributes(project_id, name, url) VALUES (?, ?, ?)",
                        (project_id, name, url),
                    )
            db.execute(
                """DELETE FROM cluster_attributes WHERE project_id = ? AND NOT EXISTS (
                       SELECT 1 FROM keyword_attributes a WHERE a.project_id = ? AND a.cluster = cluster_attributes.name
                   )""",
                (project_id, project_id),
            )
        return redirect(url_for("project", project_id=project_id, view="clusters"), code=303)

    @app.post("/projects/<int:project_id>/visits")
    def import_visits(project_id):
        upload = request.files.get("visits")
        try:
            if upload is None or not upload.filename:
                raise ValueError("Выберите CSV-файл с переходами.")
            rows = read_visits(upload.read().decode("utf-8-sig"))
        except UnicodeDecodeError:
            abort(422, "Сохраните файл в кодировке UTF-8.")
        except ValueError as exc:
            abort(422, str(exc))
        with connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            get_project(db, project_id)
            db.executemany(
                """INSERT INTO visit_stats VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, phrase, day) DO UPDATE SET done=excluded.done,
                       planned=excluded.planned, cost=excluded.cost, depth=excluded.depth""",
                [(project_id, *row) for row in rows],
            )
        return redirect(url_for("project", project_id=project_id, view="visits"), code=303)

    @app.get("/projects/<int:project_id>/export/<any(keywords,clusters,positions,visits):kind>.csv")
    def export(project_id, kind):
        today = today_msk()
        with connect(path) as db:
            db.execute("BEGIN")
            item = get_project(db, project_id)
            words = project_keywords(db, project_id)
            name = f"{site_host(item['url']) or 'project'}-{kind}"
            if kind == "keywords":
                _, _, latest = read_history(db, project_id, limit=1)
                rows = views.keyword_rows(words, latest, views.today_queue(db, project_id, today), "", "demand", True)
                return csv_response(name, (
                    "Ключевик", "Позиция", "Шанс топа, %", "!W", "Спрос", "Мощность, %", "Переходы в день",
                    "В очереди", "Конкуренция", "Бюджет в день, ₽",
                ), [(
                    row["word"]["phrase"], row["position"] if row["position"] is not None else "",
                    *(row["word"][key] if row["word"][key] is not None else "" for key in ("chance", "iw", "demand", "power", "visits")),
                    row["queue"], COMPETITION.get(row["word"]["competition"] or "", ""),
                    row["word"]["budget"] if row["word"]["budget"] is not None else "",
                ) for row in rows])
            if kind == "clusters":
                details = {row["name"]: row for row in db.execute(
                    "SELECT * FROM cluster_attributes WHERE project_id = ?", (project_id,),
                )}
                clusters, unclustered = views.cluster_tree(words, details)
                return csv_response(name, ("Кластер", "Ключевик", "!W", "URL", "Коммерция"), [
                    (cluster["name"] or "(не кластеризовано)", word["phrase"], word["iw"] if word["iw"] is not None else "",
                     cluster["details"]["url"] if cluster["details"] else "",
                     cluster["details"]["commercial"] if cluster["details"] and cluster["details"]["commercial"] is not None else "")
                    for cluster in clusters + ([unclustered] if unclustered else []) for word in cluster["words"]
                ])
            if kind == "positions":
                days, lines, _ = read_history(db, project_id, limit=92)
                return csv_response(name, ("Запрос", "!W", "Найденный URL", *(day.isoformat() for day in days)), [
                    (line["query"], line["iw"] if line["iw"] is not None else "", line["url"] or "", *(
                        "" if (sample := line["days"].get(day)) is None else
                        "ошибка" if sample["error"] else (sample["rank"] or "51+") for day in days
                    )) for line in lines
                ])
            _, _, latest = read_history(db, project_id, limit=1)
            data = views.visit_matrix(
                db, project_id, words, project_settings(db, project_id), latest,
                request.args, today, paginate=False,
            )
            return csv_response(name, ("Запрос", "Найденный URL", "Расход, ₽", *(day.isoformat() for day in data["days"])), [
                (line["phrase"], line["url"] or "", plain(line["cost"]), *(
                    "" if (cell := line["days"].get(day.isoformat())) is None else
                    f"{cell['done']}/{cell['planned']} · {plain(cell['cost'])} ₽" for day in data["days"]
                )) for line in data["lines"]
            ])

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        error = None
        if request.method == "POST":
            try:
                balance = optional_number(
                    request.form.get("balance", "").strip(), "Баланс", -999999999, 999999999, decimal=True,
                )
                user_name = request.form.get("user_name", "").strip()
                telegram = request.form.get("telegram_url", "").strip()
                if len(user_name) > 50:
                    raise ValueError("Имя пользователя — до 50 символов.")
                if telegram and (not telegram.startswith("https://") or not validate_url(telegram)):
                    raise ValueError("Ссылка на Телеграм-группу должна начинаться с https://.")
                connection = read_connection(request.form)
            except ValueError as exc:
                error = str(exc)
            else:
                with connect(path) as db:
                    save_panel_setting(db, "balance", "" if balance is None else str(balance))
                    save_panel_setting(db, "user_name", user_name)
                    save_panel_setting(db, "telegram_url", telegram)
                    # An empty key field keeps the saved key: the key is never sent back to the browser.
                    if request.form.get("clear_api_key"):
                        save_panel_setting(db, "yandex_api_key", "")
                    save_connection(db, connection)
                return redirect(url_for("settings"), code=303)
        with connect(path) as db:
            values = {
                key: panel_setting(db, key, "")
                for key in ("balance", "user_name", "telegram_url", "yandex_folder_id", "novnc_url")
            }
            values["position_source"] = panel_setting(db, "position_source", "api")
            saved_key = panel_setting(db, "yandex_api_key", "")
            env_key, env_folder = os.getenv("YANDEX_API_KEY", ""), os.getenv("YANDEX_FOLDER_ID", "")
        if values["balance"]:
            values["balance"] = metric(float(values["balance"])).replace(" ", "")
        key_state = (
            f"Ключ сохранён в панели: {mask_secret(saved_key)}" if saved_key
            else f"Используется YANDEX_API_KEY из .env: {mask_secret(env_key)}" if env_key
            else "Ключ не задан."
        )
        return render_template(
            "settings.html", values=values, error=error, key_state=key_state, has_saved_key=bool(saved_key),
            env_folder=env_folder, position_sources=POSITION_SOURCES, day_price=DAY_PRICE, night_price=NIGHT_PRICE,
            browser_configured=browser_configured,
        ), 422 if error else 200

    @app.get("/help")
    def help_page():
        with connect(path) as db:
            telegram = panel_setting(db, "telegram_url", "")
        return render_template("help.html", telegram=telegram)

    @app.post("/projects/<int:project_id>/runs")
    def new_run(project_id):
        try:
            launch_run(project_id)
        except Conflict as exc:
            abort(409, str(exc))
        return redirect(url_for("project", project_id=project_id), code=303)

    def agent_session_view(row):
        report = json.loads(row["report_json"]) if row["report_json"] else {}
        events = report.get("events", []) if isinstance(report, dict) else []
        return {
            "id": row["id"], "project_id": row["project_id"], "project": row["name"], "state": row["state"],
            "status": row["status"], "label": AGENT_STATUSES.get(row["status"], row["status"]),
            "message": row["message"] or "", "started_at": row["started_at"], "finished_at": row["finished_at"],
            "keywords": report.get("keywords", []) if isinstance(report, dict) else [],
            "events": events[-AGENT_EVENTS_SHOWN:][::-1],
        }

    def read_agent_session(db, session_id=None):
        row = db.execute(
            """SELECT s.*, p.name FROM agent_sessions s JOIN projects p ON p.id = s.project_id
               WHERE ? IS NULL OR s.id = ? ORDER BY s.id DESC LIMIT 1""",
            (session_id, session_id),
        ).fetchone()
        if row is None and session_id is not None:
            abort(404)
        return row and agent_session_view(row)

    @app.get("/agent")
    def agent_page():
        session_id = request.args.get("session", type=int)
        with connect(path) as db:
            db.execute("BEGIN")
            current = read_agent_session(db, session_id)
            projects = [
                {"row": item, "keywords": project_keywords(db, item["id"])}
                for item in db.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
            ]
            history = db.execute(
                """SELECT s.id, s.state, s.status, s.message, s.started_at, p.name FROM agent_sessions s
                   JOIN projects p ON p.id = s.project_id ORDER BY s.id DESC LIMIT 10""",
            ).fetchall()
            running = db.execute("SELECT 1 FROM agent_sessions WHERE state = 'running'").fetchone() is not None
        return render_template(
            "agent.html", current=current, projects=projects, history=history, running=running,
            agent_statuses=AGENT_STATUSES, selected=request.args.get("target", ""),
        )

    @app.get("/agent/<int:session_id>/status.json")
    def agent_status(session_id):
        with connect(path) as db:
            return jsonify(read_agent_session(db, session_id))

    @app.post("/agent/start")
    def start_agent():
        if agent_session_runner is None:
            abort(409, "Агент не подключён: запустите панель командой py -m dashboard и контейнер dp-agent.")
        try:
            project_id, _, ordinal = request.form.get("target", "").partition(":")
            project_id, ordinal = int(project_id), int(ordinal) if ordinal else None
        except ValueError:
            abort(400, "Выберите проект и ключевую фразу.")
        try:
            with connect(path) as db:
                db.execute("BEGIN IMMEDIATE")
                get_project(db, project_id)
                exists = db.execute(
                    "SELECT 1 FROM keywords WHERE project_id = ? AND (? IS NULL OR ordinal = ?)",
                    (project_id, ordinal, ordinal),
                ).fetchone()
                if exists is None:
                    abort(409, "Добавьте поисковые фразы в проект.")
                session_id = start_agent_session(db, project_id, ordinal)
        except sqlite3.IntegrityError as exc:
            if "active_agent" not in str(exc):
                raise
            abort(409, "Агент уже работает. Дождитесь окончания текущего запуска.")
        try:
            Thread(target=agent_session_runner, args=(path, session_id), daemon=True).start()
        except RuntimeError:
            logger.exception("Не удалось запустить агента %s", session_id)
            with connect(path) as db:
                finish_agent_session(db, session_id, "failed", "ERROR", "Не удалось запустить агента.")
        return redirect(url_for("agent_page", session=session_id), code=303)

    @app.post("/agent/<int:session_id>/resume")
    def resume_agent(session_id):
        with connect(path) as db:
            updated = db.execute(
                "UPDATE agent_sessions SET resume = 1 WHERE id = ? AND state = 'running' AND status = 'CAPTCHA'",
                (session_id,),
            ).rowcount
        if not updated:
            abort(409, "Агент не ожидает оператора.")
        return redirect(url_for("agent_page", session=session_id), code=303)

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
