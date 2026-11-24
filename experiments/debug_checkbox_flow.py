"""Run the checkbox captcha test and trace each human click (debug helper)."""
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")
import tests.test_agent as t
from modules.browser import CaptchaHandler, element_box

original = CaptchaHandler.human_click


def human_click(self, tab, context, element):
    print("tab", type(tab).__name__, tab.url, "context", type(context).__name__)
    print("box", element_box(context, element), "curr", tab.actions.curr_x, tab.actions.curr_y)
    x, y = original(self, tab, context, element)
    print("clicked", x, y, "hit", tab.run_js(f"const e = document.elementFromPoint({x}, {y}); return e && e.outerHTML.slice(0, 80);"))
    print("viewport", tab.run_js("return [innerWidth, innerHeight, scrollX, scrollY]"))
    return x, y


with patch.object(CaptchaHandler, "human_click", human_click):
    suite = unittest.TestSuite([t.BrowserTests("test_checkbox_captcha_is_clicked_with_smooth_mouse_movement")])
    unittest.TextTestRunner(verbosity=1).run(suite)
