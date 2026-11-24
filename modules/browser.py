import logging
import os
import shutil
import time
from importlib import import_module
from pathlib import Path
from random import randint, uniform
from urllib.parse import urlsplit
from uuid import uuid4

from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.errors import ContextLostError, PageDisconnectedError
from psutil import NoSuchProcess, Process, TimeoutExpired

from .clock import timestamp

log = logging.getLogger(__name__)


class RunError(Exception):
    pass


class PageFailure(RunError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class AccessDenied(RunError):
    status = "ACCESS_DENIED"


def open_browser(timeout, *, headless=False, profile=None):
    chrome = os.getenv("CHROME_PATH") or shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        raise RunError("Chromium не найден. Укажите CHROME_PATH.")
    options = ChromiumOptions(read_file=False).set_browser_path(chrome).auto_port()
    if profile is not None:
        options.set_tmp_path(str(profile))
    options.set_argument("--disable-dev-shm-usage")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        options.set_argument("--no-sandbox")
    options.set_retry(0).set_timeouts(base=timeout, page_load=timeout, script=timeout)
    options.headless(headless)
    return ChromiumPage(options)


def close_browser(browser):
    try:
        process = Process(browser.process_id) if browser.process_id else None
    except NoSuchProcess:
        process = None
    try:
        browser.quit(force=True)
    finally:
        # Chrome can still be flushing its profile after the CDP connection closes.
        if process is not None:
            try:
                process.wait(timeout=5)
            except TimeoutExpired:
                try:
                    process.kill()
                except NoSuchProcess:
                    pass
                else:
                    process.wait(timeout=5)


def wait_for(probe, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = probe()
        except ContextLostError:
            result = None
        except RuntimeError as error:
            # Chrome temporarily detaches the page during a history navigation.
            if "Not attached to an active page" not in str(error):
                raise
            result = None
        if result:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.1, remaining))


def document(tab):
    response = tab.run_cdp(
        "Runtime.evaluate",
        expression="""({
            url: location.href,
            error: document.documentURI.startsWith('chrome-error:'),
            complete: document.readyState === 'complete',
            started: performance.timeOrigin,
            status: performance.getEntriesByType('navigation')[0]?.responseStatus || 0
        })""",
        returnByValue=True,
        _timeout=2,
    )
    return response["result"].get("value")


def check_document(state):
    if state["error"]:
        raise PageFailure("NETWORK_ERROR", "Chromium показал страницу ошибки сети")
    code = state["status"]
    if code in (401, 403, 429):
        raise AccessDenied(f"Сервис ограничил доступ (HTTP {code})")
    if code in (404, 410):
        raise PageFailure("NOT_FOUND", f"Страница отсутствует (HTTP {code})")
    if code >= 400:
        raise PageFailure("HTTP_ERROR", f"Ошибка HTTP {code}")


def loaded_document(tab, timeout, *, validate=True, expected_url=None):
    def probe():
        state = document(tab)
        if state and state["complete"] and (expected_url is None or state["url"] == expected_url):
            return state
        return None

    state = wait_for(probe, timeout)
    if state is None:
        raise PageFailure("TIMEOUT", "Документ не загрузился за отведённое время")
    if validate:
        check_document(state)
    return state


def click_element(tab, element, timeout):
    tab.scroll.to_see(element, center=True)
    # Multiline links can have a bounding-box center outside every rendered line.
    point = wait_for(
        lambda: element.run_js("""
            for (const rect of this.getClientRects()) {
                const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
                const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
                if (left >= right || top >= bottom) continue;
                const x = (left + right) / 2, y = (top + bottom) / 2;
                if (this.contains(document.elementFromPoint(x, y))) {
                    return [x + scrollX, y + scrollY];
                }
            }
            return null;
        """, timeout=min(timeout, 2)),
        timeout,
    )
    if point is None:
        raise PageFailure("CLICK_UNAVAILABLE", "Элемент недоступен для клика")
    return tab.actions.move_to(point, duration=0.2).click()


def click_and_follow(browser, source, link, timeout):
    before = document(source)
    known_tabs = set(browser.get_tabs(as_id=True))
    click_element(source, link, timeout)

    def probe():
        tabs = browser.get_tabs(as_id=True)
        new_tab = next((tab_id for tab_id in tabs if tab_id not in known_tabs), None)
        tab = browser.get_tab(new_tab) if new_tab else source
        state = document(tab)
        if not state or not state["complete"]:
            return None
        if tab is source and all(state[key] == before[key] for key in ("url", "started", "error")):
            return None
        if state["url"] == "about:blank":
            return None
        return tab, state

    result = wait_for(probe, timeout)
    if not result:
        raise PageFailure("TIMEOUT", "После клика документ не загрузился")
    return result


def hostname(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            return parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        pass
    return ""


def matches_domain(url, domain):
    host = hostname(url)
    return host == domain or host.endswith("." + domain)


CAPTCHA_SELECTOR = "[data-sitekey], input[name='smart-token'], #checkbox-captcha-form"
CAPTCHA_FRAMES = "css:iframe[src*='smartcaptcha'], iframe[title*='SmartCaptcha']"
CAPTCHA_MANUAL_REQUIRED = "CAPTCHA_MANUAL_REQUIRED"
MANUAL_MESSAGE = "CAPTCHA_MANUAL_REQUIRED: Требуется участие оператора."
OPERATOR_URL = "http://localhost:56565"
# The classifier runs in the main document and in each SmartCaptcha frame.
CAPTCHA_PROBE = """
const visible = element => {
    if (!element || !element.getClientRects().length) return false;
    const style = getComputedStyle(element);
    return style.visibility !== 'hidden' && style.display !== 'none';
};
const find = selector => [...document.querySelectorAll(selector)].find(visible) || null;
const label = find('.AdvancedCaptcha-FormLabel, .AdvancedCaptcha-Instruction, '
    + '.AdvancedCaptcha-TaskText, [data-testid=instruction]');
const instruction = label ? label.innerText.trim() : '';
if (find('.AdvancedCaptcha input[type=text], .AdvancedCaptcha .Textinput-Control, '
        + 'input[name=rep]')) {
    return {type: 'complex', instruction};
}
const view = find('.AdvancedCaptcha-View, .AdvancedCaptcha-ImageWrapper, .AdvancedCaptcha-SilhouetteTask');
if (view) {
    const slider = find('.CaptchaSlider [data-testid=thumb], .CaptchaSlider .Thumb');
    return {type: 'image', kind: slider ? 'slider' : 'clicks', instruction};
}
if (find('#js-button, .CheckboxCaptcha-Button, .CheckboxCaptcha-Anchor')) {
    return {type: 'checkbox', instruction};
}
return null;
"""
# Selectors are tried in order: a comma-joined CSS list would return the first element in
# document order, i.e. the wide .CheckboxCaptcha-Anchor wrapper instead of the checkbox itself.
CHECKBOX = ("#js-button", ".CheckboxCaptcha-Button", ".CheckboxCaptcha-Anchor")
IMAGE_VIEW = (".AdvancedCaptcha-View", ".AdvancedCaptcha-ImageWrapper", ".AdvancedCaptcha-SilhouetteTask")
SLIDER_THUMB = (".CaptchaSlider [data-testid=thumb]", ".CaptchaSlider .Thumb")
# Pauses from the issue: 3-5 s after the checkbox, 0.5-1 s between clicks, 5 s after "Submit".
CHECKBOX_WAIT = (3, 5)
CLICK_DELAY = (0.5, 1)
SUBMIT_WAIT = 5
SUBMIT = (
    "[data-testid=submit]", ".AdvancedCaptcha_submit button", ".AdvancedCaptcha_submit",
    "button.CaptchaButton_view_action", "button[type=submit]",
)


class SearchChallenge(RunError):
    status = "CAPTCHA"

    def __init__(self, url, message="На странице обнаружена SmartCaptcha"):
        super().__init__(message)
        self.url = url


class CaptchaTimeout(SearchChallenge):
    status = "TIMEOUT"


def captcha_present(tab, url, selector=CAPTCHA_SELECTOR):
    if "showcaptcha" in urlsplit(url or "").path.lower():
        return True
    if not tab.ele(f"css:{selector}", timeout=0):
        return False
    # An embedded widget stays in the DOM after Yandex issues the token.
    token = tab.ele("css:input[name='smart-token']", timeout=0)
    return not (token and token.value)


def load_solver(spec):
    """Import an optional local solver from ``package.module:function``."""
    if not spec:
        return None
    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError("LOCAL_PF_CAPTCHA_SOLVER: укажите модуль и функцию в формате module:function.")
    return getattr(import_module(module), name)


def first_element(context, selectors, timeout):
    """Return the first visible-in-DOM element for the highest-priority matching selector."""
    deadline = time.monotonic() + timeout
    while True:
        for selector in selectors:
            element = context.ele(f"css:{selector}", timeout=0)
            if element:
                return element
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def element_box(context, element):
    """Return the viewport box of an element, including the offset of its frame."""
    left, top, width, height = element.run_js(
        "const r = this.getBoundingClientRect(); return [r.left, r.top, r.width, r.height];"
    )
    frame = getattr(context, "frame_ele", None)
    if frame is not None:
        frame_left, frame_top = frame.run_js(
            "const r = this.getBoundingClientRect(); "
            "return [r.left + this.clientLeft, r.top + this.clientTop];"
        )
        left, top = left + frame_left, top + frame_top
    return left, top, width, height


class CaptchaHandler:
    """Detect, classify and solve Yandex SmartCaptcha, then wait for an operator."""

    def __init__(
        self, journal, report, *, selector=CAPTCHA_SELECTOR, timeout=15, attempts=3,
        manual_timeout=300, poll_interval=2, solver=None, screenshot_dir=None,
        save_progress=None, operator_url=OPERATOR_URL,
    ):
        self.journal = journal
        self.report = report
        self.selector = selector
        self.timeout = timeout
        self.attempts = attempts
        self.manual_timeout = manual_timeout
        self.poll_interval = poll_interval
        self.solver = solver
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.save_progress = save_progress
        self.operator_url = operator_url

    def present(self, tab):
        try:
            state = loaded_document(tab, min(self.timeout, 5), validate=False)
            return captcha_present(tab, state["url"], self.selector)
        except (ContextLostError, PageDisconnectedError, PageFailure):
            return True
        except RuntimeError as error:
            if "Not attached to an active page" not in str(error):
                raise
            return True

    def classify(self, tab):
        contexts = [tab]
        try:
            contexts.extend(tab.get_frames(CAPTCHA_FRAMES, timeout=0) or [])
        except Exception:
            log.debug("Не удалось получить фреймы SmartCaptcha", exc_info=True)
        for context in contexts:
            try:
                found = context.run_js(CAPTCHA_PROBE, timeout=2)
            except Exception:
                log.debug("Не удалось классифицировать капчу во фрейме", exc_info=True)
                continue
            if found:
                return context, found
        return tab, {"type": "unknown", "instruction": ""}

    def remember(self, event, **details):
        entry = self.report.setdefault("captcha", {"attempts": 0, "history": []})
        entry["history"].append({"event": event, "timestamp_msk": timestamp(), **details})

    def move_mouse(self, tab, x, y):
        """Move the pointer along a jittered Bézier curve with intermediate points."""
        actions = tab.actions
        start_x, start_y = actions.curr_x, actions.curr_y
        control_x = (start_x + x) / 2 + uniform(-80, 80)
        control_y = (start_y + y) / 2 + uniform(-60, 60)
        steps = randint(12, 20)
        for step in range(1, steps + 1):
            t = step / steps
            ease = t * t * (3 - 2 * t)
            next_x = (1 - ease) ** 2 * start_x + 2 * (1 - ease) * ease * control_x + ease ** 2 * x
            next_y = (1 - ease) ** 2 * start_y + 2 * (1 - ease) * ease * control_y + ease ** 2 * y
            actions.move(next_x - actions.curr_x, next_y - actions.curr_y, duration=0.02)
            time.sleep(uniform(0.005, 0.03))
        return actions

    def human_click(self, tab, context, element):
        element.scroll.to_see(center=True)
        left, top, width, height = element_box(context, element)
        x = left + width * uniform(0.3, 0.7)
        y = top + height * uniform(0.3, 0.7)
        self.move_mouse(tab, x, y)
        time.sleep(uniform(0.1, 0.3))
        tab.actions.click()
        return x, y

    def click_checkbox(self, tab, context, url, attempt):
        self.journal.record(
            "Попытка нажать чекбокс", url, captcha_type="checkbox", attempt=attempt, status="CAPTCHA",
        )
        checkbox = first_element(context, CHECKBOX, 2)
        if not checkbox:
            return False
        self.human_click(tab, context, checkbox)
        time.sleep(uniform(*CHECKBOX_WAIT))
        return True

    def solve_image(self, tab, context, url, attempt, task):
        self.journal.record(
            "Попытка решить задание с картинками", url, captcha_type="image",
            captcha_kind=task.get("kind"), attempt=attempt, message=task.get("instruction") or None,
            status="CAPTCHA",
        )
        view = first_element(context, IMAGE_VIEW, 2)
        if not view:
            return False
        view.scroll.to_see(center=True)
        image = view.get_screenshot(as_bytes="png")
        screenshot = None
        if self.screenshot_dir is not None:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"captcha-{uuid4().hex[:8]}.png"
            path.write_bytes(image)
            screenshot = str(path)
        if self.solver is None:
            self.journal.record(
                "Локальная модель не подключена", url, captcha_type="image", attempt=attempt,
                screenshot=screenshot, status=CAPTCHA_MANUAL_REQUIRED,
            )
            return None
        try:
            points = list(self.solver({
                "type": "image", "kind": task.get("kind"), "instruction": task.get("instruction") or "",
                "image": image, "screenshot": screenshot,
            }) or [])
        except Exception as error:
            log.exception("Локальная модель не смогла решить капчу")
            self.journal.record(
                "Ошибка локальной модели", url, captcha_type="image", attempt=attempt,
                status="CAPTCHA", error=str(error),
            )
            return False
        self.journal.record(
            "Ответ локальной модели", url, captcha_type="image", attempt=attempt,
            screenshot=screenshot, points=len(points), status="CAPTCHA",
        )
        if not points:
            return None
        left, top, _, _ = element_box(context, view)
        if task.get("kind") == "slider":
            thumb = first_element(context, SLIDER_THUMB, 2)
            if not thumb:
                return False
            start_x, start_y = self.human_click_point(tab, context, thumb)
            tab.actions.hold()
            self.move_mouse(tab, left + float(points[0][0]), start_y)
            time.sleep(uniform(*CLICK_DELAY))
            tab.actions.release()
        else:
            for x, y in points:
                self.move_mouse(tab, left + float(x), top + float(y))
                tab.actions.click()
                time.sleep(uniform(*CLICK_DELAY))
            submit = first_element(context, SUBMIT, 1)
            if submit:
                self.journal.record("Нажатие кнопки «Отправить»", url, captcha_type="image", attempt=attempt)
                self.human_click(tab, context, submit)
        time.sleep(SUBMIT_WAIT)
        return True

    def human_click_point(self, tab, context, element):
        element.scroll.to_see(center=True)
        left, top, width, height = element_box(context, element)
        x, y = left + width / 2, top + height / 2
        self.move_mouse(tab, x, y)
        return x, y

    def solved(self, tab, url, attempt, kind):
        self.journal.record(
            "Капча решена автоматически", tab.url, captcha_type=kind, attempt=attempt, status="OK",
        )
        self.remember("solved", attempt=attempt, captcha_type=kind, url=tab.url)
        self.report["captcha"]["resolution"] = "automatic"
        self.report["status"] = "RUNNING"
        self.persist()
        return loaded_document(tab, self.timeout, validate=False)

    def persist(self):
        if self.save_progress is None:
            return
        try:
            self.save_progress()
        except Exception:
            log.exception("Не удалось сохранить промежуточный отчёт")

    def handle_captcha(self, tab, url):
        """Try to pass SmartCaptcha; return the document state after the captcha disappears.

        Raises ``CaptchaTimeout`` when neither the solver nor the operator resolved it in time.
        """
        self.report["captcha_encountered"] = True
        context, task = self.classify(tab)
        kind = task["type"]
        log.warning("Обнаружена капча: %s (%s)", url, kind)
        self.journal.record(
            "Обнаружена капча", url, captcha_type=kind, status="CAPTCHA",
            message=task.get("instruction") or None,
        )
        self.remember("detected", captcha_type=kind, url=url)
        for attempt in range(1, self.attempts + 1):
            if kind in ("complex", "unknown"):
                break
            self.report["captcha"]["attempts"] += 1
            if kind == "checkbox":
                done = self.click_checkbox(tab, context, url, attempt)
            else:
                done = self.solve_image(tab, context, url, attempt, task)
            self.remember("attempt", attempt=attempt, captcha_type=kind)
            if done and not self.present(tab):
                return self.solved(tab, url, attempt, kind)
            if done is None:
                break
            self.journal.record(
                "Капча не решена", tab.url, captcha_type=kind, attempt=attempt, status="CAPTCHA",
            )
            context, task = self.classify(tab)
            if kind != task["type"]:
                self.journal.record(
                    "Тип капчи изменился", tab.url, captcha_type=task["type"],
                    message=task.get("instruction") or None, status="CAPTCHA",
                )
            kind = task["type"]
            if not self.present(tab):
                return self.solved(tab, url, attempt, kind)
        return self.wait_for_captcha_resolution(tab, captcha_type=kind)

    def wait_for_captcha_resolution(self, tab, timeout=None, *, captcha_type=None):
        """Keep the browser open and poll until an operator solves the captcha in noVNC."""
        timeout = self.manual_timeout if timeout is None else timeout
        url = tab.url
        self.report["captcha_encountered"] = True
        log.warning("%s Откройте %s и решите капчу.", MANUAL_MESSAGE, self.operator_url)
        self.journal.record(
            "Капча не решена, ожидание оператора", url, captcha_type=captcha_type,
            status=CAPTCHA_MANUAL_REQUIRED, message=MANUAL_MESSAGE, operator_url=self.operator_url,
            timeout_seconds=timeout,
        )
        self.remember("manual_required", captcha_type=captcha_type, url=url, timeout_seconds=timeout)
        self.report["status"] = CAPTCHA_MANUAL_REQUIRED
        self.persist()
        started = time.monotonic()
        deadline = started + timeout
        reminder = started + 30
        while time.monotonic() < deadline:
            time.sleep(max(0, min(self.poll_interval, deadline - time.monotonic())))
            if not self.present(tab):
                waited = round(time.monotonic() - started, 1)
                log.warning("Оператор решил капчу, продолжаем")
                self.journal.record(
                    "Оператор решил капчу, продолжаем", tab.url, status="OK", waited_seconds=waited,
                )
                self.remember("operator_solved", url=tab.url, waited_seconds=waited)
                self.report["captcha"]["resolution"] = "operator"
                self.report["status"] = "RUNNING"
                self.persist()
                return loaded_document(tab, self.timeout, validate=False)
            if time.monotonic() >= reminder:
                reminder += 30
                log.warning(
                    "%s Осталось %d с.", MANUAL_MESSAGE, max(0, round(deadline - time.monotonic())),
                )
        message = f"Капча не решена за {timeout:g} с ожидания оператора"
        log.error("%s", message)
        self.journal.record("Время ожидания оператора истекло", tab.url, status="TIMEOUT", error=message)
        self.remember("timeout", url=tab.url, timeout_seconds=timeout)
        self.report["captcha"]["resolution"] = "timeout"
        self.report["status"] = "TIMEOUT"
        self.persist()
        raise CaptchaTimeout(tab.url, message)
