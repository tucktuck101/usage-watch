"""The one place that runs external commands, so tests can replace it."""

import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class Result:
    code: int
    out: str
    err: str


def run(cmd: list[str], cwd: str | None = None, timeout: float = 15) -> Result:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return Result(127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{cmd[0]}: timed out after {timeout}s")
    except NotADirectoryError:
        return Result(2, "", f"{cwd}: not a directory")
    return Result(p.returncode, p.stdout, p.stderr)


def which(name: str) -> str | None:
    return shutil.which(name)
