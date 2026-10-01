"""Collectors (F3, C1, C2): runtime sources (D6)."""

from __future__ import annotations

import importlib

# (module, class): the nine sources `run` and `dashboard --watch` host.
DEFAULT_SOURCES: tuple[tuple[str, str], ...] = (
    ("screen", "ScreenSource"),
    ("topology", "TopologySource"),
    ("panes", "PanesSource"),
    ("claude", "ClaudeTranscriptSource"),
    ("claude", "ClaudeCachedUtilizationSource"),
    ("statusline", "ClaudeStatuslineSource"),
    ("codex", "CodexRolloutSource"),
    ("omp", "OmpSessionSource"),
    ("omp", "OmpUsageCacheSource"),
)


def default_sources() -> list:
    """One instance of every source, with its defaults. A source whose module
    fails to import, or that fails to construct, is skipped, so one broken
    collector never stops the others."""
    out = []
    for module, cls in DEFAULT_SOURCES:
        try:
            out.append(getattr(importlib.import_module(f"{__name__}.{module}"), cls)())
        except Exception:
            continue
    return out
