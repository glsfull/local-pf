"""Agent session started by «🚀 Запустить агента»: search → click on the target site for project phrases.

For every selected phrase the agent opens ya.ru, types the phrase, presses «Найти», looks through
up to five results pages and clicks the result of the project site. When the site is not found,
the refined query of the phrase (brand or domain without the zone) is searched the same way;
it is not used when the main phrase finds the site.

On a SmartCaptcha the agent does not try to pass it: the session switches to the CAPTCHA status
and waits until the operator solves it in noVNC or presses «Продолжить» in the panel.
The status and the latest actions are published to SQLite so that the panel can show them live.
"""
import json
import logging
import os
import time

from dashboard.search import domain_hint, site_host
from dashboard.storage import AGENT_STATUSES, connect, finish_agent_session, update_agent_session

from .browser import RunError
from .clock import timestamp
from .config import CAPTCHA_SELECTOR, Config
from .journal import Journal
from .search import SEARCH_HOME, SearchChallenge, TargetNotFound, find_target

CAPTCHA_WAIT_SECONDS = 15 * 60
CAPTCHA_ROUNDS = 3
log = logging.getLogger(__name__)


class SessionJournal(Journal):
    """Journal that publishes every action to the panel."""

    def __init__(self, events, delay, publish):
        super().__init__(events, delay)
        self.publish = publish

    def record(self, operation, url, **details):
        super().record(operation, url, **details)
        self.publish()


def session_config(query, domain, *, delay=0.7):
    return Config(
        keyword=query,
        domain=domain,
        pages=(),
        search_input="#text",
        search_button="button[type='submit']",
        result_link=os.getenv("LOCAL_PF_RESULT_LINK_SELECTOR", "li.serp-item a.OrganicTitle-Link, li.serp-item h2 > a"),
        captcha=CAPTCHA_SELECTOR,
        timeout=15,
        observation_delay=delay,
    )


def captcha_visible(browser, config):
    """True while any tab still shows SmartCaptcha; unknown states count as visible."""
    try:
        for tab_id in browser.get_tabs(as_id=True):
            tab = browser.get_tab(tab_id)
            if "showcaptcha" in (tab.url or "").lower() or tab.ele(f"css:{config.captcha}", timeout=0):
                return True
        return False
    except Exception:
        log.debug("Не удалось проверить капчу", exc_info=True)
        return True


class AgentSession:
    def __init__(
        self, path, session_id, *, search=find_target, wait=time.sleep, clock=time.monotonic,
        captcha_wait=CAPTCHA_WAIT_SECONDS, poll=2.0, search_home=SEARCH_HOME, delay=None,
    ):
        self.path = path
        self.session_id = session_id
        self.search = search
        self.wait = wait
        self.clock = clock
        self.captcha_wait = captcha_wait
        self.poll = poll
        self.search_home = search_home
        if delay is None:
            delay = float(os.getenv("LOCAL_PF_OBSERVATION_DELAY", "0.7"))
        if not 0 <= delay <= 5:
            raise ValueError("LOCAL_PF_OBSERVATION_DELAY: укажите паузу от 0 до 5 секунд.")
        self.delay = delay
        self.report = {
            "session_id": session_id, "timestamp": timestamp(), "captcha_encountered": False,
            "keywords": [], "pages_checked": [], "events": [],
        }
        self.journal = SessionJournal(self.report["events"], delay, self.publish)
        self.status, self.message = "STARTING", None

    def load(self):
        with connect(self.path) as db:
            session = db.execute("SELECT * FROM agent_sessions WHERE id = ?", (self.session_id,)).fetchone()
            if session is None or session["state"] != "running":
                raise ValueError("Активный запуск агента не найден.")
            project = db.execute("SELECT * FROM projects WHERE id = ?", (session["project_id"],)).fetchone()
            words = db.execute(
                "SELECT * FROM keywords WHERE project_id = ? AND (? IS NULL OR ordinal = ?) ORDER BY ordinal",
                (session["project_id"], session["ordinal"], session["ordinal"]),
            ).fetchall()
        if project is None or not words:
            raise ValueError("В проекте нет выбранных поисковых фраз.")
        domain = site_host(project["url"])
        self.report.update(project=project["name"], target_domain=domain)
        return project, words, domain

    def publish(self):
        with connect(self.path) as db:
            update_agent_session(
                db, self.session_id, self.status, self.message,
                report_json=json.dumps(self.report, ensure_ascii=False),
            )

    def set_status(self, status, message):
        self.status, self.message = status, message
        log.info("%s: %s", AGENT_STATUSES[status], message)
        self.publish()

    def resume_requested(self):
        with connect(self.path) as db:
            row = db.execute("SELECT resume FROM agent_sessions WHERE id = ?", (self.session_id,)).fetchone()
            if row is not None and row["resume"]:
                db.execute("UPDATE agent_sessions SET resume = 0 WHERE id = ?", (self.session_id,))
                return True
        return False

    def wait_for_operator(self, browser, config, url):
        self.report["captcha_encountered"] = True
        self.journal.record("Капча", url, status="CAPTCHA", error="Требуется решение оператора в noVNC")
        self.set_status("CAPTCHA", "Решите капчу в окне noVNC или нажмите «Продолжить».")
        self.resume_requested()  # A click made before the captcha does not count.
        deadline = self.clock() + self.captcha_wait
        while self.clock() < deadline:
            self.wait(self.poll)
            if self.resume_requested():
                self.journal.record("Оператор нажал «Продолжить»", url, status="OK")
                return
            if not captcha_visible(browser, config):
                self.journal.record("Капча решена оператором", url, status="OK")
                return
        raise RunError(f"Капча не решена за {self.captcha_wait // 60:g} мин.")

    def find(self, browser, config, result):
        for _ in range(CAPTCHA_ROUNDS):
            try:
                return self.search(browser, config, result, self.journal, self.search_home)
            except SearchChallenge as challenge:
                self.wait_for_operator(browser, config, challenge.url)
                self.set_status("SEARCH", f"Повтор поиска «{config.keyword}» после капчи.")
        raise RunError("Капча появляется повторно. Запуск остановлен.")

    def close_extra_tabs(self, browser):
        try:
            for tab_id in browser.get_tabs(as_id=True):
                if tab_id != browser.tab_id:
                    browser.get_tab(tab_id).close()
        except Exception:
            log.debug("Не удалось закрыть вкладки", exc_info=True)

    def run_keyword(self, browser, project, word, domain):
        hint = word["brand"] if word["hint_type"] == "brand" else domain_hint(project["url"])
        queries = [word["phrase"]] + ([f"{word['phrase']} {hint}"] if hint else [])
        outcome = {"phrase": word["phrase"], "query": None, "found": False, "position": None, "page": None,
                   "landing_url": None, "refined": False}
        self.report["keywords"].append(outcome)
        for index, query in enumerate(queries):
            if index:
                self.journal.record("Уточнённый запрос", None, query=query, hint_type=word["hint_type"])
            self.close_extra_tabs(browser)
            self.set_status("SEARCH", f"Поиск «{query}» в Яндексе.")
            result = {"search_result_found": False}
            outcome.update(query=query, refined=bool(index))
            try:
                landing = self.find(browser, session_config(query, domain, delay=self.delay), result)
            except TargetNotFound as missing:
                self.journal.record("Сайт не найден", None, query=query, status="NOT_FOUND", error=str(missing))
                self.set_status("NOT_FOUND", f"«{query}»: {missing}")
                continue
            outcome.update(
                found=True, position=result.get("search_position"), page=result.get("search_page"),
                landing_url=landing.url,
            )
            self.report["pages_checked"].append({
                "url": landing.url, "exists": True, "status": "OK", "scroll_position": None,
                "error": None, "timestamp_msk": timestamp(),
            })
            self.set_status(
                "FOUND", f"«{query}»: сайт найден на позиции {outcome['position']} "
                f"(страница {outcome['page']}) и открыт.",
            )
            return outcome
        return outcome

    def run(self, browser):
        project, words, domain = self.load()
        for index, word in enumerate(words):
            if index and project["interval_seconds"]:
                self.wait(project["interval_seconds"])
            self.run_keyword(browser, project, word, domain)
        found = sum(1 for item in self.report["keywords"] if item["found"])
        return f"Найдено {found} из {len(words)} фраз."

    def execute(self, open_browser, close_browser):
        browser = None
        try:
            browser = open_browser()
            message = self.run(browser)
        except Exception as error:
            if not isinstance(error, (RunError, ValueError)):
                log.exception("Запуск агента остановлен")
            message = str(error) or "Внутренняя ошибка агента."
            self.journal.record("Ошибка", self.journal.url, status=getattr(error, "status", "ERROR"), error=message)
            self.finish("failed", "ERROR", message)
            return 1
        finally:
            if browser is not None:
                try:
                    close_browser(browser)
                except Exception:
                    log.exception("Не удалось закрыть Chromium")
        self.finish("complete", "DONE", message)
        return 0

    def finish(self, state, status, message):
        self.status, self.message = status, message
        self.report.update(state=state, error=message if state == "failed" else None)
        with connect(self.path) as db:
            finish_agent_session(
                db, self.session_id, state, status, message,
                report_json=json.dumps(self.report, ensure_ascii=False),
            )
