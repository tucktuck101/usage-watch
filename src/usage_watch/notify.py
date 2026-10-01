"""Tell the person: a desktop notification, and a timestamped log line."""

import datetime as dt
import json
import sys

from . import sh
from .config import state_dir


class Log:
    def __init__(self, to_file: bool = True):
        self.path = state_dir() / "usage-watch.log" if to_file else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = f"{dt.datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {msg}"
        print(line, flush=True)
        if self.path:
            with open(self.path, "a") as f:
                f.write(line + "\n")


def notify(title: str, msg: str) -> None:
    if sys.platform == "darwin":
        sh.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title {json.dumps(title)}"])
    elif sh.which("notify-send"):
        sh.run(["notify-send", title, msg])
