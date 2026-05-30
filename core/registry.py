"""Strategy auto-discovery.

Scans the `strategies/` package for any class that subclasses Strategy
and registers it by its `name` attribute. Drop a file in strategies/
and it's instantly available.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import strategies as strategies_pkg

from core.strategy_base import Strategy


def discover_strategies() -> dict[str, type[Strategy]]:
    """Return a mapping of strategy name -> Strategy class."""
    found: dict[str, type[Strategy]] = {}
    for _, mod_name, _ in pkgutil.iter_modules(strategies_pkg.__path__):
        module = importlib.import_module(f"strategies.{mod_name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, Strategy)
                and obj is not Strategy
                and obj.__module__ == module.__name__
            ):
                if obj.name in found and found[obj.name] is not obj:
                    raise RuntimeError(
                        f"Duplicate strategy name {obj.name!r}: "
                        f"{found[obj.name]} vs {obj}"
                    )
                found[obj.name] = obj
    return found
