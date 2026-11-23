from datetime import datetime, timedelta, timezone
import logging

MSK = timezone(timedelta(hours=3), "MSK")


def timestamp():
    return datetime.now(MSK).isoformat(timespec="seconds")


class MoscowFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, MSK).strftime("%Y-%m-%d %H:%M:%S %Z")
