"""Offline self-checks under pytest (mirror of selfcheck.py).

These run everywhere — no cluster needed — so a stg ``pytest tests/contract``
run also proves the suite's own invariants, and the terminal report shows them
alongside the C/F/X ids. CI runs ``selfcheck.py`` directly with plain python3.
"""
from __future__ import annotations

import pytest

from . import selfcheck


@pytest.mark.offline
@pytest.mark.parametrize("check", selfcheck.CHECKS, ids=[c.__name__ for c in selfcheck.CHECKS])
def test_selfcheck(check):
    check()
