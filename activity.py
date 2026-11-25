from collections import deque
from datetime import datetime
import logging
import secrets
from threading import Lock


LOG = logging.getLogger("frame.activity")


class ActivityLog:
    def __init__(self, capacity=200):
        self.entries = deque(maxlen=capacity)
        self.lock = Lock()
        self.sequence = 0
        self.instance = secrets.token_hex(8)

    def record(self, level, message):
        with self.lock:
            self.sequence += 1
            self.entries.append({
                "id": self.sequence,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "level": logging.getLevelName(level),
                "message": message,
            })
            LOG.log(level, message)

    def info(self, message):
        self.record(logging.INFO, message)

    def warning(self, message):
        self.record(logging.WARNING, message)

    def error(self, message):
        self.record(logging.ERROR, message)

    def snapshot(self, after=0, instance=None):
        with self.lock:
            reset = instance != self.instance or after > self.sequence
            return {
                "instance": self.instance,
                "reset": reset,
                "cursor": self.sequence,
                "entries": [
                    dict(entry) for entry in self.entries
                    if reset or entry["id"] > after
                ],
            }
