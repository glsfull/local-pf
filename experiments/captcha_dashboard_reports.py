"""Produce real report.json files of the SmartCaptcha flows for dashboard screenshots.

Runs the agent against the local SmartCaptcha fixture from tests/test_agent.py:
* ``waiting``: an image captcha the local model answers wrongly three times, snapshot of
  report.json while the agent waits for the operator (status CAPTCHA_MANUAL_REQUIRED);
* ``operator``: a complex text captcha the fixture "operator" solves after 3 seconds.

Usage: python experiments/captcha_dashboard_reports.py OUTPUT_DIR
then:  python -m dashboard --agent-log-dir OUTPUT_DIR/waiting  (or OUTPUT_DIR/operator)
"""
from dataclasses import replace
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
from modules.config import TargetPage, read_config  # noqa: E402
from tests.test_agent import Fixture  # noqa: E402


def main(output):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    server.requests, server.captcha_log, server.solved, server.operator_delay = [], [], False, None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = replace(
        read_config(ROOT / "config.yaml"), keyword="проверочный запрос", domain="127.0.0.1",
        result_link="a.result", timeout=2, captcha_poll=0.5, pages=(TargetPage("/news", url="/news"),),
    )
    home = f"http://localhost:{server.server_port}"
    real_write = agent.write_report

    # 1. Three wrong answers of the local model, then the operator is requested.
    waiting = output / "waiting"
    waiting.mkdir(parents=True, exist_ok=True)
    (output / "waiting-run").mkdir(parents=True, exist_ok=True)

    def snapshot(path, report):
        real_write(path, report)
        if report.get("status") == "CAPTCHA_MANUAL_REQUIRED" and not (waiting / "report.json").exists():
            shutil.copy(path, waiting / "report.json")

    server.mode = "captcha_image"
    os.environ.update(LOCAL_PF_CAPTCHA_SOLVER="examples.captcha_solver_fixed:solve", LOCAL_PF_CAPTCHA_POINTS="10,10;20,20")
    with patch("agent.write_report", side_effect=snapshot):
        code = agent.verify(replace(config, captcha_timeout=3), output / "waiting-run", headless=True, search_home=home)
    print("waiting run exit", code, json.loads((waiting / "report.json").read_text())["status"])

    # 2. A complex captcha the operator solves in noVNC.
    server.mode, server.solved, server.operator_delay = "captcha_text", False, 3
    (output / "operator").mkdir(parents=True, exist_ok=True)
    code = agent.verify(replace(config, captcha_timeout=30), output / "operator", headless=True, search_home=home)
    print("operator run exit", code, json.loads((output / "operator" / "report.json").read_text())["status"])
    server.shutdown()


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
