"""Stub optional third-party modules only when they are genuinely missing.

Several test files import agent code whose heavy dependencies (anthropic,
playwright, the Google clients, pandas, matplotlib, ...) may not be
installed. They used to drop an empty ``types.ModuleType`` into
``sys.modules`` for each name unconditionally, at import time, for the whole
session. That broke every later test that needed the real library: 40 Wally
tests failed with ``module 'pandas' has no attribute 'DataFrame'`` and
``cannot import name 'Workbook' from 'openpyxl'``, but only when the whole
suite ran, depending on collection order.

``stub_missing`` installs a stub only when the module can't be found, so
with the real dependencies installed the suite runs against the real
libraries, and without them it degrades exactly as before.
"""

from __future__ import annotations

import importlib.util
import sys
import types


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # parent missing, or parent is itself a stub
        return False


def stub_missing(*names: str) -> set[str]:
    """Stub each of *names* that is neither loaded nor installed. Returns the
    names actually stubbed, so a caller can patch attributes onto its own
    stubs without touching a real module."""
    stubbed: set[str] = set()
    for name in names:
        if name in sys.modules or _importable(name):
            continue
        sys.modules[name] = types.ModuleType(name)
        stubbed.add(name)
    return stubbed
