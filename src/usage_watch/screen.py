"""Read a pane's screen, and type into it."""

import time

from . import sh
from .errors import Problem

HISTORY = 60  # lines of scrollback to read; the adapters only need the tail


def capture(pane_id: str) -> tuple[str, str]:
    """(plain text, text with styles) of a pane's recent screen, wrapped lines joined."""
    plain = sh.run(["tmux", "capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{HISTORY}"])
    if plain.code != 0:
        raise Problem(
            f"cannot read pane {pane_id}: {plain.err.strip() or 'tmux capture-pane failed'}",
            fix=f"check the pane still exists: tmux list-panes -a | grep '{pane_id}'",
        )
    styled = sh.run(["tmux", "capture-pane", "-p", "-J", "-e", "-t", pane_id, "-S", "-12"])
    return plain.out, styled.out if styled.code == 0 else ""


def type_into(pane_id: str, text: str) -> None:
    """Type text literally, then press Enter as a separate key."""
    r = sh.run(["tmux", "send-keys", "-t", pane_id, "-l", text])
    if r.code != 0:
        raise Problem(
            f"could not type into pane {pane_id}: {r.err.strip()}",
            fix=f"check the pane exists and accepts input: tmux send-keys -t '{pane_id}' ''",
        )
    time.sleep(0.5)
    sh.run(["tmux", "send-keys", "-t", pane_id, "Enter"])
