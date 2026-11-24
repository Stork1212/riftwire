"""Test-only crash points used by the crash-safety test-suite.

``crashpoint("name")`` hard-kills the current process (``os._exit``, no cleanup, exactly
like a power loss or SIGKILL) when the settings are in ``test`` mode and
``RIFTWIRE_TEST_CRASH_AT`` equals ``name``. In any other environment it does nothing.
"""

from __future__ import annotations

import os

from app.core.config import get_settings

CRASH_EXIT_CODE = 137


def crashpoint(name: str) -> None:
    s = get_settings()
    if s.env == "test" and s.test_crash_at == name:
        os._exit(CRASH_EXIT_CODE)
