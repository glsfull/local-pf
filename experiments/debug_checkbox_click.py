"""Open the checkbox fixture and trace where CaptchaHandler clicks."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
from modules.browser import CaptchaHandler, close_browser, element_box, open_browser
from modules.journal import Journal
from tests.test_agent import CHECKBOX_PAGE

page = CHECKBOX_PAGE.replace("NEXT", "about:blank#done") + """
<script>addEventListener('mousedown', e => document.title = 'down ' + e.clientX + ',' + e.clientY + ' ' + e.target.id);</script>"""
with tempfile.TemporaryDirectory() as profile:
    path = Path(profile) / "page.html"
    path.write_text(page, encoding="utf-8")
    browser = open_browser(5, headless=True, profile=profile + "/p")
    try:
        tab = browser.latest_tab
        tab.get(path.as_uri())
        report = {"events": []}
        handler = CaptchaHandler(Journal(report["events"], 0), report)
        context, task = handler.classify(tab)
        print("classified", task, type(context).__name__)
        box = element_box(context, context.ele("#js-button"))
        print("box", box)
        print("click at", handler.human_click(tab, context, context.ele("#js-button")))
        print("actions curr", tab.actions.curr_x, tab.actions.curr_y)
        import time; time.sleep(1)
        print("title", tab.title, "url", tab.url)
    finally:
        close_browser(browser)
