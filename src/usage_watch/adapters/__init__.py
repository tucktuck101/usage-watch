"""Harness adapters. To support a new harness, add a module and list it here."""

from .base import Adapter, Reading
from .claude import Claude
from .codex import Codex
from .omp import Omp

ADAPTERS: list[Adapter] = [Omp(), Claude(), Codex()]


def for_process(argv0: str, args: str) -> Adapter | None:
    return next((a for a in ADAPTERS if a.matches(argv0, args)), None)


def by_name(name: str) -> Adapter | None:
    return next((a for a in ADAPTERS if a.name == name), None)


__all__ = ["ADAPTERS", "Adapter", "Reading", "by_name", "for_process"]
