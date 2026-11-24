"""Run one captcha browser test and print the report events (debug helper)."""
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")
import tests.test_agent as t

name = sys.argv[1] if len(sys.argv) > 1 else "test_checkbox_captcha_is_clicked_with_smooth_mouse_movement"
original = t.BrowserTests.check_run


def check_run(self, *args, **kwargs):
    code, report = original(self, *args, **kwargs)
    print("CODE", code)
    for event in report["events"]:
        print(json.dumps(event, ensure_ascii=False))
    print("CAPTCHA_LOG", self.server.captcha_log)
    return code, report


with patch.object(t.BrowserTests, "check_run", check_run):
    suite = unittest.TestSuite([t.BrowserTests(name)])
    unittest.TextTestRunner(verbosity=2).run(suite)
