import logging
from time import sleep

from .clock import timestamp

log = logging.getLogger(__name__)


class Journal:
    def __init__(self, events, delay):
        self.events = events
        self.delay = delay

    @property
    def url(self):
        return self.events[-1]["url"] if self.events else None

    def record(self, operation, url, **details):
        self.events.append({
            "timestamp_msk": timestamp(),
            "operation": operation,
            "url": url,
            **details,
        })
        level = logging.WARNING if details.get("error") else logging.INFO
        log.log(level, "%s | %s | %s", operation, url or "—", details)

    def step(self, operation, url, **details):
        self.record(operation, url, delay_seconds=self.delay, **details)
        if self.delay:
            sleep(self.delay)
